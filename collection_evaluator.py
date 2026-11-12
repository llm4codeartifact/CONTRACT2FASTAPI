"""Fuzz a FastAPI app, scan it with ZAP, run pytest, and update the dataset."""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, get_args

import requests
from fastapi.routing import APIRoute
from pydantic import BaseModel


HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Schemathesis, ZAP, pytest, and export API data.")
    parser.add_argument("--target", required=True, help="FastAPI target in module:app form.")
    parser.add_argument("--tests", required=True, type=Path)
    parser.add_argument("--specification", required=True, type=Path)
    parser.add_argument("--test-metadata", required=True, type=Path)
    parser.add_argument("--dataset", type=Path, default=Path("secure_code_dataset.json"))
    parser.add_argument("--dataset-key", required=True, help="Unique identifier saved with each exported endpoint.")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--zap-path", help="Path to zap.bat or zap.sh; auto-detected when omitted.")
    parser.add_argument("--zap-port", type=int, default=8090)
    parser.add_argument("--exclude-positive-data-acceptance", action="store_true")
    return parser.parse_args()


def require_path(path: Path, description: str, directory: bool = False) -> Path:
    resolved = path.resolve()
    valid = resolved.is_dir() if directory else resolved.is_file()
    if not valid:
        raise ValueError(f"{description} non trovato: {resolved}")
    return resolved


def start_uvicorn(target: str, port: int, timeout: int) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", target, "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/openapi.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Uvicorn si e' arrestato durante l'avvio di {target}.")
        try:
            if requests.get(url, timeout=1).status_code == 200:
                return process
        except requests.RequestException:
            pass
        time.sleep(0.5)
    stop_process(process)
    raise TimeoutError(f"Uvicorn non e' pronto entro {timeout} secondi.")


def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def executable(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    candidate = Path(sys.executable).parent / (f"{name}.exe" if os.name == "nt" else name)
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError(f"{name} non trovato. Installa le dipendenze con pip install -r requirements.txt.")


def save_openapi(port: int, specification_path: Path) -> None:
    suffix = ".specification.json"
    name = specification_path.name
    output = specification_path.with_name(name[: -len(suffix)] + ".openapi.json") if name.endswith(suffix) else specification_path.with_suffix(".openapi.json")
    response = requests.get(f"http://127.0.0.1:{port}/openapi.json", timeout=10)
    response.raise_for_status()
    output.write_text(json.dumps(response.json(), ensure_ascii=False, indent=2), encoding="utf-8")


def run_schemathesis(port: int, exclude_positive_data_acceptance: bool) -> bool:
    print("[*] Schemathesis...")
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    command = [
            executable("schemathesis"),
            "run",
            f"http://127.0.0.1:{port}/openapi.json",
            "--checks",
            "all",
            "--max-examples",
            "50",
            "--workers",
            "1",
        ]
    if exclude_positive_data_acceptance:
        command.extend(["--exclude-checks", "positive_data_acceptance"])
    return subprocess.run(
        command,
        env=environment,
        check=False,
    ).returncode == 0


def find_zap(path: str | None) -> str:
    if path:
        return path
    names = ["zap.bat", "zap.sh"] if os.name == "nt" else ["zap.sh"]
    for candidate in (
        r"C:\Program Files\ZAP\Zed Attack Proxy\zap.bat",
        r"C:\Program Files (x86)\ZAP\Zed Attack Proxy\zap.bat",
        *names,
    ):
        if Path(candidate).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError("OWASP ZAP non trovato. Installa ZAP o usa --zap-path.")


def run_zap(target_url: str, zap_path: str | None, zap_port: int, timeout: int) -> None:
    try:
        from zapv2 import ZAPv2
    except ImportError as error:
        raise RuntimeError("Client Python ZAP non trovato. Installa python-owasp-zap-v2.4.") from error

    command = find_zap(zap_path)
    print("[*] OWASP ZAP: spider e active scan...")
    process = subprocess.Popen(
        [command, "-daemon", "-host", "127.0.0.1", "-port", str(zap_port), "-config", "api.disablekey=true"],
        cwd=str(Path(command).parent) if Path(command).is_absolute() else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    zap = ZAPv2(proxies={"http": f"http://127.0.0.1:{zap_port}", "https": f"http://127.0.0.1:{zap_port}"})
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                zap.core.version
                break
            except Exception:
                time.sleep(1)
        else:
            raise TimeoutError("OWASP ZAP non e' pronto entro il timeout.")

        spider_id = zap.spider.scan(target_url)
        while int(zap.spider.status(spider_id)) < 100:
            time.sleep(1)
        scan_id = zap.ascan.scan(target_url)
        while int(zap.ascan.status(scan_id)) < 100:
            time.sleep(2)
        print(f"[+] OWASP ZAP completato: {len(zap.core.alerts())} alert rilevati.")
    finally:
        try:
            zap.core.shutdown()
        except Exception:
            pass
        stop_process(process)


def run_pytest(tests: Path, port: int) -> bool:
    print("[*] pytest...")
    environment = os.environ.copy()
    environment["API_BASE_URL"] = f"http://127.0.0.1:{port}"
    return subprocess.run([sys.executable, "-m", "pytest", str(tests), "-q"], env=environment, check=False).returncode == 0


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} non contiene JSON valido: {error}") from error
    if not isinstance(content, dict):
        raise ValueError(f"{label} deve essere un oggetto JSON.")
    return content


def load_inputs(specification_path: Path, metadata_path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    specification = load_json(specification_path, "Specification")
    case_id = specification.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("Specification priva di case_id.")
    apis = specification.get("apis")
    if not isinstance(apis, list):
        raise ValueError("Specification priva dell'array apis.")
    specification_apis = {api["endpoint"]: api for api in apis if isinstance(api, dict) and isinstance(api.get("endpoint"), str)}
    if len(specification_apis) != len(apis):
        raise ValueError("Ogni API deve contenere un endpoint univoco.")

    metadata = load_json(metadata_path, "Test metadata")
    endpoint_tests = metadata.get("endpoint_tests")
    if metadata.get("case_id") != case_id or not isinstance(endpoint_tests, dict):
        raise ValueError("Test metadata non coerente con la specification.")
    return specification, specification_apis, endpoint_tests


def import_application(target: str) -> tuple[Any, Any]:
    if ":" not in target:
        raise ValueError("Il target deve avere formato module:app.")
    module_name, attribute = target.rsplit(":", 1)
    module = importlib.import_module(module_name)
    return module, getattr(module, attribute)


def source_of(value: Any) -> str:
    try:
        return inspect.getsource(value).strip()
    except (OSError, TypeError):
        return ""


def pydantic_models(annotation: Any) -> list[type[BaseModel]]:
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for argument in get_args(annotation) for model in pydantic_models(argument)]


def endpoint_sources(module: Any, route: APIRoute) -> tuple[str, list[str], list[str], list[str]]:
    route_code = source_of(route.endpoint)
    models: list[type[BaseModel]] = []
    for parameter in inspect.signature(route.endpoint).parameters.values():
        models.extend(pydantic_models(parameter.annotation))
    models.extend(pydantic_models(route.response_model))
    schemas = list(dict.fromkeys(source_of(model) for model in models if source_of(model)))

    controllers: list[str] = []
    for class_name, method_name in re.findall(r"\b([A-Za-z_]\w*Controller)\.([A-Za-z_]\w*)", route_code):
        source = source_of(getattr(getattr(module, class_name, None), method_name, None))
        if source and source not in controllers:
            controllers.append(source)
    used_source = " ".join([route_code, *controllers])
    orm_models = [source_of(value) for name, value in vars(module).items() if inspect.isclass(value) and getattr(value, "__tablename__", None) and re.search(rf"\b{re.escape(name)}\b", used_source)]
    return route_code, schemas, controllers, [source for source in orm_models if source]


def export_dataset(target: str, dataset_key: str, specification: dict[str, Any], specification_apis: dict[str, dict[str, Any]], endpoint_tests: dict[str, dict[str, Any]], tests: Path, dataset: Path) -> int:
    module, app = import_application(target)
    records: list[dict[str, Any]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        route_code, schemas, controllers, orm_models = endpoint_sources(module, route)
        for method in route.methods & HTTP_METHODS:
            endpoint = f"{method} {route.path}"
            api = specification_apis.get(endpoint)
            test = endpoint_tests.get(endpoint)
            if api is None or test is None:
                raise ValueError(f"Specification o test metadata mancanti per {endpoint}.")
            test_sources = []
            for relative_path in test.get("test_files", []):
                path = (tests.parent / relative_path).resolve()
                if not path.is_file():
                    raise ValueError(f"File di test non trovato: {relative_path}")
                source = {"path": path.relative_to(tests.parent).as_posix(), "code": path.read_text(encoding="utf-8")}
                if source not in test_sources:
                    test_sources.append(source)
            records.append({"dataset_key": dataset_key, "endpoint": endpoint, "route_code": route_code, "schemas_code": schemas, "controller_code": controllers, "orm_model_code": orm_models, "formal_requirement": api.get("formal_requirement", ""), "pseudocode": api.get("pseudocode", {}), "tests_code": test_sources, "expected_output": test.get("expected_output", [])})

    if set(specification_apis) != {record["endpoint"] for record in records}:
        raise ValueError("Specification e endpoint runtime non coincidono.")
    existing = json.loads(dataset.read_text(encoding="utf-8")) if dataset.is_file() else []
    merged = {(str(item.get("dataset_key", "")), str(item.get("endpoint", ""))): item for item in existing if isinstance(item, dict)}
    merged.update({(record["dataset_key"], record["endpoint"]): record for record in records})
    dataset.parent.mkdir(parents=True, exist_ok=True)
    dataset.write_text(json.dumps(list(merged.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    return len(records)


def main() -> int:
    args = parse_arguments()
    try:
        tests = require_path(args.tests, "Directory test", directory=True)
        specification_path = require_path(args.specification, "Specification")
        metadata_path = require_path(args.test_metadata, "Test metadata")
        specification, specification_apis, endpoint_tests = load_inputs(specification_path, metadata_path)
        server = start_uvicorn(args.target, args.port, args.timeout)
        try:
            save_openapi(args.port, specification_path)
            if not run_schemathesis(args.port, args.exclude_positive_data_acceptance):
                return 1
            try:
                run_zap(f"http://127.0.0.1:{args.port}", args.zap_path, args.zap_port, args.timeout)
            except Exception as error:
                print(f"[-] OWASP ZAP non eseguito: {error}")
        finally:
            stop_process(server)

        server = start_uvicorn(args.target, args.port, args.timeout)
        try:
            if not run_pytest(tests, args.port):
                return 1
        finally:
            stop_process(server)
        print(f"[+] Dataset aggiornato: {export_dataset(args.target, args.dataset_key, specification, specification_apis, endpoint_tests, tests, args.dataset.resolve())} endpoint esportati.")
        return 0
    except (ImportError, OSError, RuntimeError, TimeoutError, ValueError, requests.RequestException, AttributeError) as error:
        print(f"[-] Pipeline interrotta: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
