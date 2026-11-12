"""Build LLaMA-Factory ShareGPT datasets from per-backend API input/output pairs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SYSTEM_PROMPT = (
    "You are a senior backend engineer specialized in secure FastAPI, Pydantic v2, "
    "and SQLAlchemy APIs. Given endpoint-level requirements and pseudocode for route, "
    "schemas, controllers, and ORM models, generate the requested Python code only. "
    "Preserve authorization, validation, controlled error handling, transactions, and "
    "HTTP status contracts."
)

VALIDATION_KEYS = {
    "clinic-appointments-rooms-conflicts",
    "clinic-owners-animals-relations",
    "clinic-records-vaccinations-temporal",
    "recipes-ingredients-servings",
    "recipes-macronutrients-tags-filters",
    "recipes-weekly-plan-references",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path("datasets/v1"))
    parser.add_argument("--output-dir", type=Path, default=Path("generated/llamafactory-v1"))
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def find_backend_dirs(source_dir: Path) -> list[Path]:
    dirs = []
    for inputs in source_dir.glob("**/inputs.json"):
        backend_dir = inputs.parent
        if (backend_dir / "outputs.json").exists():
            dirs.append(backend_dir)
    return sorted(dirs, key=lambda path: path.name)


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def build_user_prompt(input_record: dict[str, Any]) -> str:
    return "\n\n".join(
        (
            "Generate the OUTPUT object for this API. Return exactly four fenced Python sections named "
            "schemas_code, orm_model_code, controller_code, and route_code.",
            f"dataset_key:\n{input_record['dataset_key']}",
            f"endpoint:\n{input_record['endpoint']}",
            f"context_requirements:\n{input_record['context_requirements']}",
            f"formal_requirement:\n{input_record['formal_requirement']}",
            f"schemas_pseudocode:\n{pretty_json(input_record['schemas_pseudocode'])}",
            f"orm_model_pseudocode:\n{pretty_json(input_record['orm_model_pseudocode'])}",
            f"controller_pseudocode:\n{pretty_json(input_record['controller_pseudocode'])}",
            f"route_pseudocode:\n{pretty_json(input_record['pseudocode'])}",
        )
    )


def code_list_section(name: str, blocks: list[str]) -> str:
    if not blocks:
        return f"<{name}>\n```python\n# None required for this endpoint.\n```\n</{name}>"
    joined = "\n\n".join(block.strip() for block in blocks)
    return f"<{name}>\n```python\n{joined}\n```\n</{name}>"


def build_assistant_answer(output_record: dict[str, Any]) -> str:
    return "\n\n".join(
        (
            code_list_section("schemas_code", output_record["schemas_code"]),
            code_list_section("orm_model_code", output_record["orm_model_code"]),
            code_list_section("controller_code", output_record["controller_code"]),
            f"<route_code>\n```python\n{output_record['route_code'].strip()}\n```\n</route_code>",
        )
    )


def build_example(input_record: dict[str, Any], output_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversations": [
            {"from": "system", "value": SYSTEM_PROMPT},
            {"from": "human", "value": build_user_prompt(input_record)},
            {"from": "gpt", "value": build_assistant_answer(output_record)},
        ],
        "dataset_key": input_record["dataset_key"],
        "endpoint": input_record["endpoint"],
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def main() -> None:
    args = parse_args()
    train_rows: list[dict[str, Any]] = []
    val_rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    backend_dirs = find_backend_dirs(args.source_dir)
    if not backend_dirs:
        raise SystemExit(f"No backend datasets found under {args.source_dir}")

    for backend_dir in backend_dirs:
        inputs = load_json(backend_dir / "inputs.json")
        outputs = load_json(backend_dir / "outputs.json")
        if len(inputs) != len(outputs):
            raise SystemExit(f"Mismatched inputs/outputs in {backend_dir}")
        if not inputs:
            continue
        dataset_key = inputs[0]["dataset_key"]
        if any(row["dataset_key"] != dataset_key for row in inputs):
            raise SystemExit(f"Mixed dataset_key values in {backend_dir}")
        rows = [build_example(input_record, output_record) for input_record, output_record in zip(inputs, outputs)]
        counts[dataset_key] = len(rows)
        if dataset_key in VALIDATION_KEYS:
            val_rows.extend(rows)
        else:
            train_rows.extend(rows)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.output_dir / "qwen_fastapi_codegen_train.jsonl", train_rows)
    write_jsonl(args.output_dir / "qwen_fastapi_codegen_validation.jsonl", val_rows)

    dataset_info = {
        "qwen_fastapi_codegen_train": {
            "file_name": "qwen_fastapi_codegen_train.jsonl",
            "formatting": "sharegpt",
            "columns": {"messages": "conversations"},
            "tags": {
                "role_tag": "from",
                "content_tag": "value",
                "user_tag": "human",
                "assistant_tag": "gpt",
                "system_tag": "system",
            },
        },
        "qwen_fastapi_codegen_validation": {
            "file_name": "qwen_fastapi_codegen_validation.jsonl",
            "formatting": "sharegpt",
            "columns": {"messages": "conversations"},
            "tags": {
                "role_tag": "from",
                "content_tag": "value",
                "user_tag": "human",
                "assistant_tag": "gpt",
                "system_tag": "system",
            },
        },
    }
    (args.output_dir / "dataset_info.json").write_text(
        json.dumps(dataset_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest = {
        "source_dir": str(args.source_dir).replace("\\", "/"),
        "train_records": len(train_rows),
        "validation_records": len(val_rows),
        "validation_dataset_keys": sorted(VALIDATION_KEYS),
        "backend_record_counts": dict(sorted(counts.items())),
    }
    (args.output_dir / "build_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Training records: {len(train_rows)}")
    print(f"Validation records: {len(val_rows)}")
    print(f"Dataset dir: {args.output_dir}")


if __name__ == "__main__":
    main()
