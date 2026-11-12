"""Evaluate generated endpoint replacements inside temporary backend copies.

Each model candidate y1 replaces its matching controller and route x1 in the
reference backend X.  The source tree is never changed.  Results are saved as
JSON so every metric can be reproduced from the same candidate and test suite.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any


SECTIONS = ("schemas_code", "orm_model_code", "controller_code", "route_code")
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, action="append",
                        help="Validation JSONL; specify repeatedly to calculate pass@k.")
    parser.add_argument("--manifest", type=Path,
                        default=Path("datasets/v3c/manifest.json"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--index", action="append", type=int, help="Only this record index; repeatable.")
    parser.add_argument("--limit", type=int, help="Maximum records per results file.")
    parser.add_argument("--repeats", type=int, default=2, help="Fresh-server runs per candidate.")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--candidate-timeout", type=int,
                        help="Hard wall-clock limit for one candidate, including its child servers. "
                             "Defaults to a conservative value derived from --timeout and --repeats.")
    parser.add_argument("--replace-components", action="store_true",
                        help="Also replace ORM/schema fragments; unsafe for shared declarations.")
    parser.add_argument("--skip-mutations", action="store_true")
    parser.add_argument("--quiet", action="store_true",
                        help="Write only the final summary to stdout (full details remain in --report).")
    # Internal protocol used by the parent evaluator.  A candidate is evaluated
    # in a fresh Python process so a stuck Uvicorn/pytest child cannot stop the
    # whole batch.
    parser.add_argument("--worker-input", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rows(path: Path, indexes: set[int] | None, limit: int | None) -> list[dict[str, Any]]:
    values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if indexes is not None:
        values = [row for row in values if row["index"] in indexes]
    return values[:limit] if limit is not None else values


def sections(text: str) -> dict[str, str | None]:
    result = {}
    for name in SECTIONS:
        match = re.search(rf"<{name}>\s*```(?:python)?\s*(.*?)```\s*</{name}>", text, re.I | re.S)
        result[name] = match.group(1).strip() if match else None
    return result


def module_for(entry: dict[str, Any]) -> Path:
    specification = Path(entry["specification"])
    suffix = ".specification.json"
    if not specification.name.endswith(suffix):
        raise ValueError(f"unexpected specification path: {specification}")
    return specification.with_name(specification.name[:-len(suffix)] + ".py")


def patch(source: str, original: dict[str, str | None], generated: dict[str, str | None], replace_components: bool) -> tuple[str | None, dict[str, Any]]:
    selected = SECTIONS if replace_components else ("controller_code", "route_code")
    detail: dict[str, Any] = {}
    for name in selected:
        old, new = original[name], generated[name]
        if old is None or new is None:
            detail[name] = {"applicable": False, "reason": "missing section"}
            return None, detail
        if old.lstrip().startswith("# None required"):
            # Some endpoints (for example /health) intentionally have no
            # controller fragment.  There is nothing to replace in the host.
            detail[name] = {"applicable": new.lstrip().startswith("# None required"), "reason": "no code expected"}
            if not detail[name]["applicable"]:
                return None, detail
            continue
        count = source.count(old)
        detail[name] = {"applicable": count == 1, "reference_occurrences": count}
        if count != 1:
            return None, detail
        source = source.replace(old, new, 1)
    return source, detail


def run(command: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None,
        truncate_output: bool = True) -> tuple[bool, str]:
    try:
        process = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, "process timeout"
    output = process.stdout + process.stderr
    return process.returncode == 0, output[-12000:] if truncate_output else output


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get_json(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def terminate_tree(process: subprocess.Popen[str] | None) -> None:
    """Terminate a process and every descendant it created.

    Uvicorn can leave a Python child alive on Windows after its parent receives
    terminate().  ``taskkill /T`` is therefore essential here: killing just the
    parent is insufficient and can make a later candidate hang or keep its port.
    """
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        process.terminate()
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            pass


def stop(process: subprocess.Popen[str] | None) -> None:
    terminate_tree(process)


def reference_openapi(module: Path, timeout: int) -> tuple[dict[str, Any] | None, str | None]:
    snapshot = module.with_suffix(".openapi.json")
    if snapshot.is_file():
        try:
            return read_json(snapshot), None
        except json.JSONDecodeError as error:
            return None, f"invalid OpenAPI snapshot: {error}"
    code = (
        "import importlib.util,json,sys;"
        "s=importlib.util.spec_from_file_location('reference_api',sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m);"
        "print('__CODEX_OPENAPI__'+json.dumps(m.app.openapi()))"
    )
    ok, output = run([sys.executable, "-c", code, str(module.resolve())], Path.cwd(), timeout, truncate_output=False)
    if not ok:
        return None, output
    try:
        # Backends may initialise logging while importing; isolate our payload
        # with a sentinel rather than assuming stdout contains JSON only.
        _, payload = output.rsplit("__CODEX_OPENAPI__", 1)
        return json.loads(payload), None
    except (ValueError, json.JSONDecodeError):
        return None, output


def start(temp: Path, timeout: int) -> tuple[subprocess.Popen[str] | None, int | None, dict[str, Any] | None, str | None]:
    candidate_port = port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "candidate_api:app", "--host", "127.0.0.1", "--port", str(candidate_port)],
        # pytest can generate enough request logs to fill an unread PIPE and
        # deadlock Uvicorn.  The evaluator records pytest diagnostics instead.
        cwd=temp, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            return None, None, None, "uvicorn exited during startup"
        openapi = get_json(f"http://127.0.0.1:{candidate_port}/openapi.json")
        if openapi is not None:
            return server, candidate_port, openapi, None
        time.sleep(0.2)
    stop(server)
    return None, None, None, "server startup timeout"


def operations(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(method.upper(), path): value for path, item in document.get("paths", {}).items()
            for method, value in item.items() if method.lower() in METHODS and isinstance(value, dict)}


def operation_shape(operation: dict[str, Any]) -> dict[str, Any]:
    parameters = [{key: item.get(key) for key in ("name", "in", "required", "schema")}
                  for item in operation.get("parameters", [])]
    parameters.sort(key=lambda item: (item["in"], item["name"]))
    body = operation.get("requestBody") or {}
    return {
        "parameters": parameters,
        "request_body": {"required": body.get("required", False), "media_types": sorted(body.get("content", {}))} if body else None,
        "response_statuses": sorted(operation.get("responses", {})),
    }


def target_route_count(temp: Path, endpoint: str, timeout: int) -> int | None:
    method, path = endpoint.split(" ", 1)
    code = ("import candidate_api,sys;from fastapi.routing import APIRoute;"
            "print(sum(1 for r in candidate_api.app.routes if isinstance(r,APIRoute) and r.path==sys.argv[1] and sys.argv[2] in r.methods))")
    ok, output = run([sys.executable, "-c", code, path, method], temp, timeout)
    try:
        return int(output.strip()) if ok else None
    except ValueError:
        return None


def metadata_tests(module: Path, endpoint: str) -> tuple[list[Path], str | None]:
    metadata = module.with_name(module.stem + ".test-metadata.json")
    if not metadata.is_file():
        return [], "metadata file missing"
    item = read_json(metadata).get("endpoint_tests", {}).get(endpoint)
    if not isinstance(item, dict):
        return [], "endpoint has no declared tests"
    files = [module.parent / value for value in item.get("test_files", [])]
    if any(not value.is_file() for value in files):
        return files, "declared test file missing"
    return files, None


def pytest(tests: list[Path], candidate_port: int, timeout: int) -> tuple[bool | None, str | None]:
    if not tests:
        return None, "no endpoint-specific tests"
    environment = os.environ.copy()
    environment["API_BASE_URL"] = f"http://127.0.0.1:{candidate_port}"
    return run([sys.executable, "-m", "pytest", *map(str, tests), "-q"], Path.cwd(), timeout, environment)


def assess_once(source: str, ref_openapi: dict[str, Any], module: Path, endpoint: str,
                original: dict[str, str | None], generated: dict[str, str | None], args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {"patch_applicable": False, "compile": False, "import_startup": False,
                              "endpoint_contract": None, "backend_regression": None,
                              "openapi_compatible": False, "no_unexpected_routes": False, "error": None}
    candidate, result["patch_detail"] = patch(source, original, generated, args.replace_components)
    if candidate is None:
        result["error"] = "patch not applicable"
        return result
    result["patch_applicable"] = True
    try:
        compile(candidate, str(module), "exec")
    except SyntaxError as error:
        result["error"] = f"compile: {error.msg} at line {error.lineno}"
        return result
    result["compile"] = True
    with tempfile.TemporaryDirectory(prefix="endpoint-replacement-") as directory:
        temp = Path(directory)
        (temp / "candidate_api.py").write_text(candidate, encoding="utf-8")
        server, candidate_port, generated_openapi, error = start(temp, args.timeout)
        if server is None or candidate_port is None or generated_openapi is None:
            result["error"] = f"startup: {error}"
            return result
        try:
            result["import_startup"] = True
            method, path = endpoint.split(" ", 1)
            expected_ops, actual_ops = operations(ref_openapi), operations(generated_openapi)
            target = (method, path)
            result["openapi_compatible"] = target in actual_ops and target in expected_ops and operation_shape(actual_ops[target]) == operation_shape(expected_ops[target])
            count = target_route_count(temp, endpoint, args.timeout)
            result["target_runtime_route_count"] = count
            result["no_unexpected_routes"] = set(actual_ops) == set(expected_ops) and count == 1
            files, note = metadata_tests(module, endpoint)
            result["endpoint_test_files"] = [str(value) for value in files]
            result["endpoint_test_note"] = note
            result["endpoint_contract"], result["endpoint_test_output"] = pytest(files, candidate_port, args.timeout)
            # Endpoint and regression suites must not share an in-memory DB:
            # endpoint fixtures intentionally create data that regression tests
            # also create under deterministic names.
            stop(server)
            server = None
            regression_server, regression_port, _, regression_error = start(temp, args.timeout)
            if regression_server is None or regression_port is None:
                result["backend_regression"] = False
                result["backend_regression_output"] = f"startup: {regression_error}"
            else:
                server = regression_server
                result["backend_regression"], result["backend_regression_output"] = pytest(
                    [module.parent / "tests"], regression_port, args.timeout
                )
        finally:
            stop(server)
    return result


def mutants(controller: str) -> list[tuple[str, str]]:
    values = []
    for name, old, new in (("invert_inequality", " != ", " == "), ("relax_lower_bound", " <= ", " < "),
                           ("relax_upper_bound", " >= ", " > ")):
        if old in controller:
            values.append((name, controller.replace(old, new, 1)))
    return values


def evaluate(candidate_id: str, row: dict[str, Any], entries: dict[str, dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {"candidate_id": candidate_id, "index": row["index"], "dataset_key": row["dataset_key"],
                              "endpoint": row["endpoint"], "repetitions": [], "accepted": False, "mutation_score": None}
    entry = entries.get(row["dataset_key"])
    if entry is None:
        report["error"] = "unknown dataset key"
        return report
    module = module_for(entry)
    if not module.is_file():
        report["error"] = f"reference module missing: {module}"
        return report
    ref_openapi, error = reference_openapi(module, args.timeout)
    if ref_openapi is None:
        report["error"] = f"reference OpenAPI unavailable: {error}"
        return report
    source, original, generated = module.read_text(encoding="utf-8"), sections(row["ground_truth"]), sections(row["model_output"])
    for _ in range(args.repeats):
        report["repetitions"].append(assess_once(source, ref_openapi, module, row["endpoint"], original, generated, args))
    gates = ("patch_applicable", "compile", "import_startup", "endpoint_contract", "backend_regression", "openapi_compatible", "no_unexpected_routes")
    report["accepted"] = all(all(item.get(gate) is True for gate in gates) for item in report["repetitions"])
    report["deterministic_pass"] = report["accepted"] if args.repeats > 1 else None
    if report["accepted"] and not args.skip_mutations and generated["controller_code"]:
        killed = viable = 0
        report["mutations"] = []
        for name, controller in mutants(generated["controller_code"]):
            mutant = dict(generated)
            mutant["controller_code"] = controller
            value = assess_once(source, ref_openapi, module, row["endpoint"], original, mutant, args)
            is_viable = value["compile"] and value["import_startup"]
            is_killed = is_viable and value["endpoint_contract"] is False
            viable += int(is_viable)
            killed += int(is_killed)
            report["mutations"].append({"name": name, "viable": is_viable, "killed": is_killed})
        report["mutation_score"] = killed / viable if viable else None
    return report


def default_candidate_timeout(args: argparse.Namespace) -> int:
    """Allow every normal inner timeout, but eventually recover the batch.

    One assessment can start two servers and run two pytest invocations.  The
    multiplier also covers up to three small mutations when enabled.
    """
    assessments = args.repeats + (0 if args.skip_mutations else 3)
    return max(60, assessments * (5 * args.timeout + 20))


def timeout_report(candidate_id: str, row: dict[str, Any], seconds: int) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id, "index": row["index"], "dataset_key": row["dataset_key"],
        "endpoint": row["endpoint"], "repetitions": [], "accepted": False,
        "mutation_score": None, "timed_out": True,
        "error": f"candidate worker exceeded hard timeout ({seconds}s); process tree was terminated",
    }


def evaluate_isolated(candidate_id: str, row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Evaluate one candidate in a killable worker process.

    This boundary is deliberately outside ``assess_once``: it covers imports,
    Uvicorn, pytest and any grandchildren those programs spawn.
    """
    hard_timeout = args.candidate_timeout or default_candidate_timeout(args)
    config = {
        "manifest": str(args.manifest), "repeats": args.repeats, "timeout": args.timeout,
        "replace_components": args.replace_components, "skip_mutations": args.skip_mutations,
    }
    with tempfile.TemporaryDirectory(prefix="endpoint-worker-") as directory:
        temp = Path(directory)
        input_path, output_path = temp / "input.json", temp / "output.json"
        input_path.write_text(json.dumps({"candidate_id": candidate_id, "row": row, "config": config}), encoding="utf-8")
        worker = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker-input", str(input_path),
             "--worker-output", str(output_path)],
            cwd=Path.cwd(), text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            worker.wait(hard_timeout)
        except subprocess.TimeoutExpired:
            terminate_tree(worker)
            return timeout_report(candidate_id, row, hard_timeout)
        if not output_path.is_file():
            return {
                "candidate_id": candidate_id, "index": row["index"], "dataset_key": row["dataset_key"],
                "endpoint": row["endpoint"], "repetitions": [], "accepted": False, "mutation_score": None,
                "error": f"candidate worker exited {worker.returncode} without a result",
            }
        try:
            result = read_json(output_path)
        except json.JSONDecodeError as error:
            return {
                "candidate_id": candidate_id, "index": row["index"], "dataset_key": row["dataset_key"],
                "endpoint": row["endpoint"], "repetitions": [], "accepted": False, "mutation_score": None,
                "error": f"candidate worker wrote invalid JSON: {error}",
            }
    result["timed_out"] = False
    return result


def worker_main(args: argparse.Namespace) -> None:
    if args.worker_input is None or args.worker_output is None:
        raise ValueError("both internal worker paths are required")
    payload = read_json(args.worker_input)
    config = payload["config"]
    worker_args = argparse.Namespace(**config)
    manifest = read_json(Path(config["manifest"]))
    entries = {item["dataset_key"]: item for item in manifest["backends"]}
    result = evaluate(payload["candidate_id"], payload["row"], entries, worker_args)
    args.worker_output.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")


def summarise(values: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(values)
    evaluated = [item for item in values if item.get("repetitions")]
    def rate(predicate: Any) -> float | None:
        return round(sum(bool(predicate(item)) for item in evaluated) / len(evaluated), 4) if evaluated else None
    first = lambda item: (item.get("repetitions") or [{}])[0]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for value in values:
        groups[(value["dataset_key"], value["endpoint"])].append(value)
    determinism_values = [item for item in evaluated if item.get("deterministic_pass") is not None]
    return {
        "candidates": count,
        "evaluated_candidates": len(evaluated),
        "timed_out_candidates": sum(item.get("timed_out") is True for item in values),
        "patch_applicability_rate": rate(lambda item: first(item).get("patch_applicable")),
        "compile_rate": rate(lambda item: first(item).get("compile")),
        "import_startup_rate": rate(lambda item: first(item).get("import_startup")),
        "endpoint_contract_pass_rate": rate(lambda item: first(item).get("endpoint_contract") is True),
        "backend_regression_pass_rate": rate(lambda item: first(item).get("backend_regression") is True),
        "openapi_compatibility_rate": rate(lambda item: first(item).get("openapi_compatible")),
        "no_unexpected_route_rate": rate(lambda item: first(item).get("no_unexpected_routes")),
        "deterministic_pass_rate": (
            round(sum(item["deterministic_pass"] is True for item in determinism_values) / len(determinism_values), 4)
            if determinism_values else None
        ),
        "accepted_rate": rate(lambda item: item.get("accepted") is True),
        "pass_at_k": {"unique_endpoints": len(groups), "k_max": max((len(group) for group in groups.values()), default=0),
                      "endpoints_with_at_least_one_accepted_candidate": sum(any(item["accepted"] for item in group) for group in groups.values())},
        "mutation_scores": [item["mutation_score"] for item in values if item.get("mutation_score") is not None],
    }


def main() -> None:
    args = arguments()
    if args.worker_input is not None or args.worker_output is not None:
        worker_main(args)
        return
    if not args.results or args.report is None:
        raise ValueError("--results and --report are required outside worker mode")
    if args.repeats < 1:
        raise ValueError("--repeats must be positive")
    manifest = read_json(args.manifest)
    entries = {item["dataset_key"]: item for item in manifest["backends"]}
    selected = set(args.index) if args.index else None
    values = []
    for results_path in args.results:
        for row in rows(results_path, selected, args.limit):
            value = evaluate_isolated(f"{results_path.as_posix()}#{row['index']}", row, args)
            values.append(value)
            if not args.quiet:
                print(json.dumps(value, ensure_ascii=False))
    report = {"configuration": vars(args), "summary": summarise(values), "candidates": values}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
