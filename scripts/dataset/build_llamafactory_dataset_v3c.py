"""Build LLaMA-Factory ShareGPT datasets from v3c contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SYSTEM_PROMPT = (
    "You are a senior backend engineer specialized in secure FastAPI, Pydantic v2, "
    "and SQLAlchemy APIs. Given a stable v3 endpoint contract plus targeted controller "
    "constraints, generate only the requested Python code. Preserve v3 structural "
    "stability while implementing controller_focus_constraints as actual behavior."
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
    parser.add_argument("--source-dir", type=Path, default=Path("datasets/v3c"))
    parser.add_argument("--output-dir", type=Path, default=Path("generated/llamafactory-v3c"))
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def compact_contract(input_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "codegen_spec_version": input_record["codegen_spec_version"],
        "dataset_key": input_record["dataset_key"],
        "endpoint": input_record["endpoint"],
        "target": input_record["target"],
        "application_context": input_record["application_context"],
        "domain_invariants": input_record["domain_invariants"],
        "seed_and_operational_context": input_record.get("seed_and_operational_context", []),
        "endpoint_contract": input_record["endpoint_contract"],
        "component_contract": input_record["component_contract"],
        "semantic_constraints": input_record["semantic_constraints"],
        "semantic_validation_rules": input_record["semantic_validation_rules"],
        "machine_readable_constraints": input_record["machine_readable_constraints"],
        "controller_focus_constraints": input_record["controller_focus_constraints"],
        "generation_constraints": input_record["generation_constraints"],
    }


def build_user_prompt(input_record: dict[str, Any]) -> str:
    return "\n\n".join(
        (
            "Generate the OUTPUT object for this API. Return exactly four fenced Python sections named "
            "schemas_code, orm_model_code, controller_code, and route_code.",
            "Use the v3 contract for structure and controller_focus_constraints for the known controller failure modes. "
            "Implement targeted controller constraints as behavior, not comments, without inventing code outside the declared contract.",
            f"codegen_contract:\n{pretty_json(compact_contract(input_record))}",
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
    manifest = load_json(args.source_dir / "manifest.json")
    train_rows: list[dict[str, Any]] = []
    val_rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for backend in manifest["backends"]:
        inputs = load_json(args.source_dir / backend["inputs"])
        outputs = load_json(args.source_dir / backend["outputs"])
        if len(inputs) != len(outputs) or len(inputs) != backend["records"]:
            raise SystemExit(f"Record mismatch for {backend['dataset_key']}")
        rows = [build_example(input_record, output_record) for input_record, output_record in zip(inputs, outputs)]
        counts[backend["dataset_key"]] = len(rows)
        if backend["dataset_key"] in VALIDATION_KEYS:
            val_rows.extend(rows)
        else:
            train_rows.extend(rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    train_name = "qwen_fastapi_codegen_v3c_train.jsonl"
    val_name = "qwen_fastapi_codegen_v3c_validation.jsonl"
    write_jsonl(args.output_dir / train_name, train_rows)
    write_jsonl(args.output_dir / val_name, val_rows)
    dataset_info = {
        "qwen_fastapi_codegen_v3c_train": {
            "file_name": train_name,
            "formatting": "sharegpt",
            "columns": {"messages": "conversations"},
            "tags": {"role_tag": "from", "content_tag": "value", "user_tag": "human", "assistant_tag": "gpt", "system_tag": "system"},
        },
        "qwen_fastapi_codegen_v3c_validation": {
            "file_name": val_name,
            "formatting": "sharegpt",
            "columns": {"messages": "conversations"},
            "tags": {"role_tag": "from", "content_tag": "value", "user_tag": "human", "assistant_tag": "gpt", "system_tag": "system"},
        },
    }
    (args.output_dir / "dataset_info.json").write_text(json.dumps(dataset_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    build_manifest = {
        "source_dir": str(args.source_dir).replace("\\", "/"),
        "input_version": manifest.get("input_version"),
        "train_records": len(train_rows),
        "validation_records": len(val_rows),
        "validation_dataset_keys": sorted(VALIDATION_KEYS),
        "backend_record_counts": dict(sorted(counts.items())),
        "controller_focus_constraint_summary": manifest.get("controller_focus_constraint_summary", {}),
    }
    (args.output_dir / "build_manifest.json").write_text(json.dumps(build_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Training records: {len(train_rows)}")
    print(f"Validation records: {len(val_rows)}")
    print(f"Dataset dir: {args.output_dir}")


if __name__ == "__main__":
    main()
