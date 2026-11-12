"""Build v3c datasets from v3 with targeted controller constraints.

v3c keeps v3's stable full contract and adds a small controller-focused block
for failure modes observed in v2/v3 evaluations. The added constraints remain
declarative: no required Python snippets or SQLAlchemy query text are embedded.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any


VERSION = "3.2.0-targeted-controller"
OUTPUT_FIELDS = ("route_code", "schemas_code", "controller_code", "orm_model_code")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path("datasets/v3"))
    parser.add_argument("--output-dir", type=Path, default=Path("datasets/v3c"))
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, content: Any, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite {path}; pass --overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def endpoint_text(record: dict[str, Any]) -> str:
    endpoint_contract = record.get("endpoint_contract", {})
    route_pseudocode = record.get("component_contract", {}).get("route", {}).get("pseudocode") or {}
    parts = [endpoint_contract.get("formal_requirement", "")]
    if isinstance(route_pseudocode, dict):
        parts.append(route_pseudocode.get("body", ""))
    return "\n".join(part for part in parts if part)


def query_params(record: dict[str, Any]) -> list[dict[str, Any]]:
    return record.get("endpoint_contract", {}).get("request", {}).get("query_parameters", [])


def path_params(record: dict[str, Any]) -> list[dict[str, Any]]:
    return record.get("endpoint_contract", {}).get("request", {}).get("path_parameters", [])


def admin_only_parameter_constraints(record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_text(record).lower()
    constraints = []
    for param in query_params(record):
        name = str(param.get("name"))
        alias = str(param.get("alias") or name)
        candidates = {name.lower(), alias.lower()}
        if any(
            f"{candidate} allowed only to admins" in text
            or f"{candidate} permitted only to admins" in text
            or f"admin for {candidate}" in text
            for candidate in candidates
        ):
            constraints.append(
                {
                    "type": "admin_only_parameter_guard",
                    "parameter": name,
                    "alias": alias,
                    "required_behavior": "if parameter is supplied by a non-admin caller, raise controlled 403 before applying the filter",
                    "failure_status": 403,
                    "derivation": "targeted_from_evaluation_failure_missing_admin_only_filter_guard",
                }
            )
    return constraints


def inclusive_temporal_filter_constraints(record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_text(record).lower()
    aliases = {(param.get("alias") or param.get("name")): param.get("name") for param in query_params(record)}
    if "from" not in aliases or "to" not in aliases:
        return []
    match = re.search(r"inclusive(?:\s+\w+){0,6}\s+for\s+(\w+)", text)
    if not match:
        match = re.search(r"inclusive\s+(\w+)\s+filter", text)
    if not match:
        return []
    field = match.group(1)
    forbidden = ["end_at"] if field == "start_at" else []
    return [
        {
            "type": "inclusive_bound_filter",
            "parameter": aliases["from"],
            "alias": "from",
            "required_model_field": field,
            "operator": ">=",
            "forbidden_model_fields_for_this_parameter": forbidden,
            "derivation": "targeted_from_evaluation_failure_wrong_temporal_bound_field",
        },
        {
            "type": "inclusive_bound_filter",
            "parameter": aliases["to"],
            "alias": "to",
            "required_model_field": field,
            "operator": "<=",
            "forbidden_model_fields_for_this_parameter": forbidden,
            "derivation": "targeted_from_evaluation_failure_wrong_temporal_bound_field",
        },
    ]


def reference_absence_controller_constraints(record: dict[str, Any]) -> list[dict[str, Any]]:
    constraints = []
    machine = record.get("machine_readable_constraints", {})
    for item in machine.get("references", []):
        if item.get("type") != "reference_absence_required":
            continue
        scope = item.get("scope")
        constraints.append(
            {
                "type": "reference_absence_guard",
                "source_model": item.get("source_model"),
                "source_field": item.get("source_field"),
                "target_parameter": item.get("target_parameter"),
                "scope": scope,
                "required_behavior": "check existence of referenced rows before mutation and raise the declared conflict status if any row matches",
                "must_not_filter_by_when_scope_is_any_status": item.get("forbidden_narrowing_fields_when_scope_is_any_status", []) if scope == "any_status" else [],
                "failure_status": item.get("failure_status", 409),
                "derivation": "targeted_from_evaluation_failure_narrowed_reference_absence_scope",
            }
        )
    return constraints


def owner_identity_constraints(record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_text(record).lower()
    if not any(marker in text for marker in ("owner", "client user", "caller account", "mapped to", "caller owns")):
        return []
    return [
        {
            "type": "owner_identity_guard",
            "caller_identity": "current_user.id",
            "forbidden_caller_identity_fields": ["current_user.username", "current_user.client_account_id"],
            "required_behavior": "for non-admin callers, compare persisted owner/account id fields to current_user.id",
            "failure_status": 403,
            "derivation": "targeted_from_evaluation_failure_username_or_wrong_identity_field",
        }
    ]


def route_controller_consistency_constraints(record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_text(record).lower()
    if not any(marker in text for marker in ("conflict", "owner", "client", "admin", "appointment", "ownership")):
        return []
    calls = record.get("machine_readable_constraints", {}).get("route", {}).get("route_controller_calls", [])
    if not calls:
        return []
    return [
        {
            "type": "route_controller_call_consistency",
            "declared_calls": calls,
            "required_behavior": "route_code must call a controller routine that is implemented in controller_code with the same routine name and compatible parameters",
            "derivation": "targeted_from_evaluation_failure_route_controller_mismatch",
        }
    ]


def syntax_stability_constraints() -> list[dict[str, Any]]:
    return [
        {
            "type": "section_syntax_stability",
            "required_behavior": "each generated Python section must parse as standalone Python after normal snippet normalization; do not emit malformed indentation",
            "derivation": "targeted_from_v3b_evaluation_failure_unexpected_indent",
        }
    ]


def controller_focus_constraints(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": VERSION,
        "design_policy": {
            "targeted_not_exhaustive": True,
            "constraints_are_declarative": True,
            "constraints_do_not_embed_required_python_or_sqlalchemy_snippets": True,
            "keep_full_v3_contract_as_primary_stability_context": True,
        },
        "guards": admin_only_parameter_constraints(record) + owner_identity_constraints(record),
        "filters": inclusive_temporal_filter_constraints(record),
        "preconditions": reference_absence_controller_constraints(record),
    }


def build_v3c_record(record: dict[str, Any]) -> dict[str, Any]:
    new_record = copy.deepcopy(record)
    new_record["codegen_spec_version"] = VERSION
    source = new_record.setdefault("source", {})
    source["v3c_source_input_format"] = "finetuning/per_backends_dataset_v3 paired inputs.json"
    source["v3c_derivation_policy"] = "Adds targeted declarative controller constraints for observed v2/v3 failure modes; target code is unchanged."
    new_record["controller_focus_constraints"] = controller_focus_constraints(new_record)
    constraints = list(new_record.get("generation_constraints", []))
    constraints.extend(
        [
            "Use controller_focus_constraints to resolve controller ambiguity, but keep the full v3 contract as the source of structural stability.",
            "Do not satisfy controller_focus_constraints with comments only; implement them in controller_code behavior.",
            "Do not add code snippets that are not implied by the declarative contract.",
        ]
    )
    new_record["generation_constraints"] = constraints
    return new_record


def main() -> None:
    args = parse_args()
    manifest = load_json(args.source_dir / "manifest.json")
    new_manifest = copy.deepcopy(manifest)
    new_manifest["source"] = str(args.source_dir)
    new_manifest["format"] = "v3c stable v3 contract plus targeted controller constraints; inputs[i] corresponds to outputs[i]"
    new_manifest["input_version"] = VERSION
    new_manifest["output_fields"] = list(OUTPUT_FIELDS)
    total = 0
    summary: dict[str, int] = {"guards": 0, "filters": 0, "preconditions": 0}
    for backend in new_manifest["backends"]:
        inputs = load_json(args.source_dir / backend["inputs"])
        outputs = load_json(args.source_dir / backend["outputs"])
        if len(inputs) != len(outputs):
            raise ValueError(f"Mismatched inputs/outputs for {backend['dataset_key']}")
        v3c_inputs = [build_v3c_record(record) for record in inputs]
        total += len(v3c_inputs)
        for row in v3c_inputs:
            focus = row["controller_focus_constraints"]
            for key in summary:
                summary[key] += len(focus.get(key, []))
        write_json(args.output_dir / backend["inputs"], v3c_inputs, args.overwrite)
        write_json(args.output_dir / backend["outputs"], outputs, args.overwrite)
    new_manifest["records"] = total
    new_manifest["controller_focus_constraint_summary"] = summary
    write_json(args.output_dir / "manifest.json", new_manifest, args.overwrite)
    print(f"Records: {total}")
    print(f"Dataset dir: {args.output_dir}")
    print("Controller focus constraints:")
    for key, value in summary.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
