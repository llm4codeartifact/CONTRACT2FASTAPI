"""Create paired input/output datasets for each verified FastAPI backend variant."""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import Any


INPUT_FIELDS = (
    "dataset_key",
    "endpoint",
    "schemas_pseudocode",
    "controller_pseudocode",
    "orm_model_pseudocode",
    "formal_requirement",
    "context_requirements",
    "pseudocode",
)
OUTPUT_FIELDS = ("route_code", "schemas_code", "controller_code", "orm_model_code")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("secure_code_dataset.json"))
    parser.add_argument("--backends-dir", type=Path, default=Path("backends"))
    parser.add_argument("--output-dir", type=Path, default=Path("datasets/v1"))
    parser.add_argument("--overwrite", action="store_true", help="Replace generated files in the output directory.")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def endpoint_set(specification: dict[str, Any]) -> frozenset[str]:
    apis = specification.get("apis")
    if not isinstance(apis, list):
        raise ValueError("Specification has no APIs list")
    endpoints = {api.get("endpoint") for api in apis if isinstance(api, dict)}
    if not endpoints or not all(isinstance(endpoint, str) and endpoint for endpoint in endpoints):
        raise ValueError("Specification contains an invalid endpoint")
    return frozenset(endpoints)


def discover_specifications(backends_dir: Path) -> list[tuple[Path, dict[str, Any], frozenset[str]]]:
    specifications = []
    for path in sorted(backends_dir.glob("**/*.specification.json")):
        specification = load_json(path)
        if not isinstance(specification, dict):
            raise ValueError(f"Specification {path} is not an object")
        specifications.append((path, specification, endpoint_set(specification)))
    if not specifications:
        raise ValueError(f"No specifications found under {backends_dir}")
    return specifications


def map_keys_to_specifications(
    grouped_records: dict[str, list[dict[str, Any]]], specifications: list[tuple[Path, dict[str, Any], frozenset[str]]]
) -> dict[str, tuple[Path, dict[str, Any]]]:
    mapped: dict[str, tuple[Path, dict[str, Any]]] = {}
    used_paths: set[Path] = set()
    for dataset_key, records in sorted(grouped_records.items()):
        endpoints = frozenset(record["endpoint"] for record in records)
        matches = [(path, specification) for path, specification, spec_endpoints in specifications if spec_endpoints == endpoints]
        if len(matches) != 1:
            candidates = ", ".join(str(path) for path, _, _ in specifications if endpoints <= _)
            raise ValueError(
                f"Could not uniquely map {dataset_key} to a specification; exact matches: {len(matches)}; candidates: {candidates}"
            )
        path, specification = matches[0]
        if path in used_paths:
            raise ValueError(f"Specification {path} maps to more than one dataset key")
        mapped[dataset_key] = (path, specification)
        used_paths.add(path)
    return mapped


def target_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def function_summary(function: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    parameters = [argument.arg for argument in function.args.posonlyargs + function.args.args + function.args.kwonlyargs]
    parameters = [parameter for parameter in parameters if parameter not in {"self", "cls"}]
    parameter_text = ", ".join(parameters) if parameters else "no explicit parameters"
    operation = f"DEFINE {'async ' if isinstance(function, ast.AsyncFunctionDef) else ''}routine {function.name} accepting {parameter_text}."
    source = ast.unparse(function)
    behaviors = []
    if "Depends(" in source:
        behaviors.append("Resolve declared dependencies before executing the routine.")
    if "raise " in source:
        behaviors.append("Raise the declared controlled errors when a guard fails.")
    if any(token in source for token in ("commit(", "rollback(", "flush(", "refresh(")):
        behaviors.append("Coordinate persistence changes through the surrounding transaction/session.")
    if "return " in source:
        behaviors.append("Return the value produced by the implemented branch.")
    return " ".join([operation, *behaviors])


def class_summary(class_node: ast.ClassDef) -> str:
    fields = []
    routines = []
    for node in class_node.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            fields.append(node.target.id)
        elif isinstance(node, ast.Assign):
            fields.extend(name for target in node.targets if (name := target_name(target)))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            routines.append(node.name)
    clauses = [f"DEFINE class {class_node.name}."]
    if fields:
        clauses.append(f"Declare fields or class attributes: {', '.join(sorted(set(fields)))}.")
    if routines:
        clauses.append(f"Provide routines: {', '.join(sorted(set(routines)))}.")
    source = ast.unparse(class_node)
    if "relationship(" in source:
        clauses.append("Maintain the declared ORM relationships.")
    if "UniqueConstraint(" in source or "unique=True" in source:
        clauses.append("Enforce the declared uniqueness constraints.")
    return " ".join(clauses)


def normalise_fragment(block: str) -> str:
    """Restore class-level indentation on decorators extracted with a method body."""
    lines = block.splitlines()
    function_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.lstrip().startswith(("def ", "async def "))
        ),
        None,
    )
    if function_index is not None and function_index > 0:
        function_indent = len(lines[function_index]) - len(lines[function_index].lstrip())
        if function_indent:
            for index in range(function_index):
                if lines[index].strip():
                    lines[index] = " " * function_indent + lines[index]
    return textwrap.dedent("\n".join(lines))


def code_to_pseudocode(blocks: Any, label: str) -> dict[str, str]:
    if not isinstance(blocks, list):
        raise ValueError(f"{label} code must be a list")
    if not blocks:
        return {"language": "structured-pseudocode", "body": f"No {label} code is required by this endpoint."}

    summaries = []
    for block in blocks:
        if not isinstance(block, str) or not block.strip():
            raise ValueError(f"{label} contains an invalid code block")
        wrapped_fragment = False
        try:
            tree = ast.parse(normalise_fragment(block))
        except SyntaxError:
            # Controller methods can be extracted from a class with their decorators
            # left at module indentation. Parse them inside a synthetic class instead.
            try:
                tree = ast.parse("class _ExtractedSnippet:\n" + textwrap.indent(block, "    "))
                wrapped_fragment = True
            except SyntaxError as error:
                raise ValueError(f"Could not parse {label} block: {error.msg}") from error
        described = False
        for node in tree.body:
            if wrapped_fragment and isinstance(node, ast.ClassDef) and node.name == "_ExtractedSnippet":
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        summaries.append(function_summary(member))
                        described = True
                continue
            if isinstance(node, ast.ClassDef):
                summaries.append(class_summary(node))
                described = True
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                summaries.append(function_summary(node))
                described = True
        if not described:
            summaries.append("Preserve the supplied implementation block and its declared behavior.")

    body = "\n".join(f"{index}. {summary}" for index, summary in enumerate(summaries, start=1))
    return {"language": "structured-pseudocode", "body": body}


def context_requirements(specification: dict[str, Any]) -> str:
    domain = specification.get("domain", "the declared application domain")
    variant = specification.get("variant", "the declared backend variant")
    return "\n".join(
        (
            f"Application domain: {domain}.",
            f"Backend variant: {variant}.",
            "This is a local FastAPI application that manages persistent domain resources through REST endpoints.",
            "Apply the shared authorization, validation, controlled-error, and transaction conventions specified for the endpoint.",
        )
    )


def validate_source_record(record: Any, index: int) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError(f"Source record {index} is not an object")
    for field in ("dataset_key", "endpoint", "route_code", "formal_requirement", "pseudocode"):
        if not isinstance(record.get(field), str if field != "pseudocode" else dict):
            raise ValueError(f"Source record {index} has invalid {field}")
    for field in ("schemas_code", "controller_code", "orm_model_code"):
        if not isinstance(record.get(field), list):
            raise ValueError(f"Source record {index} has invalid {field}")
    return record


def build_records(record: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    input_record = {
        "dataset_key": record["dataset_key"],
        "endpoint": record["endpoint"],
        "schemas_pseudocode": code_to_pseudocode(record["schemas_code"], "schema"),
        "controller_pseudocode": code_to_pseudocode(record["controller_code"], "controller"),
        "orm_model_pseudocode": code_to_pseudocode(record["orm_model_code"], "ORM model"),
        "formal_requirement": record["formal_requirement"],
        "context_requirements": context,
        "pseudocode": record["pseudocode"],
    }
    output_record = {field: record[field] for field in OUTPUT_FIELDS}
    return input_record, output_record


def write_json(path: Path, content: Any, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite {path}; pass --overwrite to replace generated files")
    path.write_text(json.dumps(content, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    source = load_json(args.source)
    if not isinstance(source, list):
        raise ValueError("Source dataset must be a JSON array")

    grouped_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, raw_record in enumerate(source):
        record = validate_source_record(raw_record, index)
        grouped_records[record["dataset_key"]].append(record)

    specifications = discover_specifications(args.backends_dir)
    specification_map = map_keys_to_specifications(grouped_records, specifications)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_backends = []
    total_records = 0

    for dataset_key in sorted(grouped_records):
        records = sorted(grouped_records[dataset_key], key=lambda record: record["endpoint"])
        specification_path, specification = specification_map[dataset_key]
        context = context_requirements(specification)
        input_records = []
        output_records = []
        for record in records:
            input_record, output_record = build_records(record, context)
            if tuple(input_record) != INPUT_FIELDS or tuple(output_record) != OUTPUT_FIELDS:
                raise ValueError(f"Unexpected field contract for {dataset_key} {record['endpoint']}")
            input_records.append(input_record)
            output_records.append(output_record)

        backend_dir = args.output_dir / dataset_key
        backend_dir.mkdir(exist_ok=True)
        write_json(backend_dir / "inputs.json", input_records, args.overwrite)
        write_json(backend_dir / "outputs.json", output_records, args.overwrite)
        manifest_backends.append(
            {
                "dataset_key": dataset_key,
                "records": len(records),
                "specification": str(specification_path).replace("\\", "/"),
                "inputs": f"{dataset_key}/inputs.json",
                "outputs": f"{dataset_key}/outputs.json",
            }
        )
        total_records += len(records)

    manifest = {
        "source": str(args.source).replace("\\", "/"),
        "format": "paired JSON arrays; inputs[i] corresponds to outputs[i] in each backend directory",
        "input_fields": list(INPUT_FIELDS),
        "output_fields": list(OUTPUT_FIELDS),
        "backends": manifest_backends,
        "backend_count": len(manifest_backends),
        "record_count": total_records,
    }
    write_json(args.output_dir / "manifest.json", manifest, args.overwrite)
    print(f"Created {len(manifest_backends)} backend datasets containing {total_records} API records.")


if __name__ == "__main__":
    main()
