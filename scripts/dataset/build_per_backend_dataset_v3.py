"""Build v3 machine-readable contract datasets from the v2 paired datasets.

The v3 input keeps the same target code as v2 but adds a separate
``machine_readable_constraints`` block. These constraints are intentionally
declarative: they describe what generated code must satisfy without embedding
required Python/SQLAlchemy snippets.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any


V3_VERSION = "3.0.0"
OUTPUT_FIELDS = ("route_code", "schemas_code", "controller_code", "orm_model_code")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("datasets/v2"),
        help="Existing v2 per-backend dataset directory.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("datasets/v3"),
        help="Destination for the v3 per-backend dataset directory.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, content: Any, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite {path}; pass --overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def combined_requirement_text(record: dict[str, Any]) -> str:
    endpoint_contract = record.get("endpoint_contract", {})
    component_contract = record.get("component_contract", {})
    route_contract = component_contract.get("route", {})
    route_pseudocode = route_contract.get("pseudocode") or {}
    domain_invariants = record.get("domain_invariants", [])
    seed_context = record.get("seed_and_operational_context", [])
    fragments: list[str] = [endpoint_contract.get("formal_requirement", "")]
    fragments.extend(item.get("requirement", "") for item in domain_invariants if isinstance(item, dict))
    fragments.extend(str(item) for item in seed_context)
    if isinstance(route_pseudocode, dict):
        fragments.append(route_pseudocode.get("body", ""))
    fragments.extend(item.get("text", "") for item in record.get("semantic_constraints", []) if isinstance(item, dict))
    return "\n".join(fragment for fragment in fragments if fragment)


def endpoint_requirement_text(record: dict[str, Any]) -> str:
    endpoint_contract = record.get("endpoint_contract", {})
    component_contract = record.get("component_contract", {})
    route_contract = component_contract.get("route", {})
    route_pseudocode = route_contract.get("pseudocode") or {}
    fragments = [endpoint_contract.get("formal_requirement", "")]
    if isinstance(route_pseudocode, dict):
        fragments.append(route_pseudocode.get("body", ""))
    return "\n".join(fragment for fragment in fragments if fragment)


def extract_route_function_name(route_code: str) -> str | None:
    match = re.search(r"def\s+(\w+)\s*\(", route_code)
    return match.group(1) if match else None


def extract_route_controller_calls(route_code: str) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []
    for class_name, routine_name in re.findall(r"\b(\w+Controller)\.(\w+)\s*\(", route_code):
        calls.append({"controller_class": class_name, "routine": routine_name})
    return dedupe_dicts(calls)


def parse_query_declaration(raw: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    alias_match = re.search(r"alias\s*=\s*[\"']([^\"']+)[\"']", raw)
    default_match = re.search(r"Query\(([^,)]*)", raw)
    ge_match = re.search(r"ge\s*=\s*(-?\d+)", raw)
    gt_match = re.search(r"gt\s*=\s*(-?\d+)", raw)
    le_match = re.search(r"le\s*=\s*(-?\d+)", raw)
    lt_match = re.search(r"lt\s*=\s*(-?\d+)", raw)
    if alias_match:
        metadata["alias"] = alias_match.group(1)
    if default_match:
        default = default_match.group(1).strip()
        if default == "...":
            metadata["required"] = True
        elif default == "None":
            metadata["default"] = None
            metadata["required"] = False
        elif default:
            try:
                metadata["default"] = int(default)
            except ValueError:
                metadata["default"] = default
            metadata["required"] = False
    for key, match in (("ge", ge_match), ("gt", gt_match), ("le", le_match), ("lt", lt_match)):
        if match:
            metadata[key] = int(match.group(1))
    return metadata


def extract_model_info(output_record: dict[str, Any]) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for block in output_record.get("orm_model_code", []):
        class_match = re.search(r"^class\s+(\w+)\b", block, flags=re.M)
        if not class_match:
            continue
        table_match = re.search(r"__tablename__\s*=\s*[\"']([^\"']+)[\"']", block)
        fields = sorted(set(re.findall(r"^\s*(\w+)\s*=\s*Column\(", block, flags=re.M)))
        foreign_keys = []
        for field_name in fields:
            field_pattern = rf"^\s*{re.escape(field_name)}\s*=\s*Column\((.*?)(?=^\s*\w+\s*=\s*Column\(|^\s*class\s+|\Z)"
            field_match = re.search(field_pattern, block, flags=re.M | re.S)
            if not field_match:
                continue
            fk_match = re.search(r"ForeignKey\([\"']([^\"']+)[\"']\)", field_match.group(1))
            if fk_match:
                foreign_keys.append({"field": field_name, "target": fk_match.group(1)})
        models.append(
            {
                "class_name": class_match.group(1),
                "table_name": table_match.group(1) if table_match else "",
                "fields": fields,
                "foreign_keys": sorted(foreign_keys, key=lambda item: (item["field"], item["target"])),
            }
        )
    return sorted(models, key=lambda item: item["class_name"])


def model_for_id_param(param_name: str, models: list[dict[str, Any]]) -> str | None:
    if not param_name.endswith("_id"):
        return None
    stem = param_name.removesuffix("_id").lower()
    best: tuple[int, str] | None = None
    for model in models:
        class_name = model["class_name"].lower().removesuffix("model")
        table_name = model["table_name"].lower()
        score = 0
        if class_name == stem or table_name.rstrip("s") == stem:
            score = 3
        elif stem in class_name or stem in table_name:
            score = 2
        if score and (best is None or score > best[0]):
            best = (score, model["class_name"])
    return best[1] if best else None


def endpoint_route_constraints(record: dict[str, Any], output_record: dict[str, Any]) -> dict[str, Any]:
    endpoint_contract = record.get("endpoint_contract", {})
    request_contract = endpoint_contract.get("request", {})
    query_params = []
    for param in request_contract.get("query_parameters", []):
        raw = param.get("raw_declaration", "")
        query_params.append(
            {
                "name": param.get("name"),
                "alias": param.get("alias"),
                "required": param.get("required", False),
                "constraints": parse_query_declaration(raw),
                "derivation": "deterministic_from_route_contract_query_declaration",
            }
        )
    return {
        "type": "route_contract",
        "method": endpoint_contract.get("method"),
        "path": endpoint_contract.get("path"),
        "operation_id": endpoint_contract.get("operation_id"),
        "route_function": extract_route_function_name(output_record.get("route_code", "")),
        "path_parameters": request_contract.get("path_parameters", []),
        "query_parameters": query_params,
        "body_required": request_contract.get("body_required", False),
        "route_controller_calls": extract_route_controller_calls(output_record.get("route_code", "")),
        "derivation": "deterministic_from_endpoint_contract_and_target_route_shape",
    }


def response_constraints(record: dict[str, Any]) -> dict[str, Any]:
    endpoint_contract = record.get("endpoint_contract", {})
    return {
        "type": "response_status_contract",
        "success_status_codes": endpoint_contract.get("success_responses", {}).get("status_codes", []),
        "error_status_codes": endpoint_contract.get("error_responses", {}).get("status_codes", []),
        "allowed_status_codes": endpoint_contract.get("allowed_status_codes", {}).get("values", []),
        "derivation": "deterministic_from_endpoint_contract_allowed_status_codes",
    }


def orm_identity_constraints(output_record: dict[str, Any]) -> dict[str, Any]:
    models = extract_model_info(output_record)
    table_names = sorted({model["table_name"] for model in models if model["table_name"]})
    foreign_key_targets = sorted({fk["target"] for model in models for fk in model["foreign_keys"]})
    return {
        "type": "orm_identity_contract",
        "allowed_models": models,
        "allowed_table_names": table_names,
        "allowed_foreign_key_targets": foreign_key_targets,
        "forbid_undeclared_tables": True,
        "forbid_undeclared_foreign_key_targets": True,
        "derivation": "deterministic_from_target_orm_identity_not_controller_logic",
    }


def auth_constraints(record: dict[str, Any], output_record: dict[str, Any]) -> dict[str, Any]:
    endpoint_contract = record.get("endpoint_contract", {})
    auth_policy = endpoint_contract.get("auth_policy", {})
    route_code = output_record.get("route_code", "")
    return {
        "type": "authorization_contract",
        "auth_required": auth_policy.get("auth_required", False),
        "allowed_actor_kinds": auth_policy.get("roles_or_ownership", []),
        "requires_current_user_dependency": "Depends(get_current_user)" in route_code,
        "public_route": not auth_policy.get("auth_required", False),
        "derivation": "heuristic_from_formal_requirement_and_target_route_dependency",
    }


def admin_only_parameter_constraints(record: dict[str, Any]) -> list[dict[str, Any]]:
    endpoint_contract = record.get("endpoint_contract", {})
    params = endpoint_contract.get("request", {}).get("query_parameters", [])
    text = endpoint_requirement_text(record).lower()
    constraints: list[dict[str, Any]] = []
    for param in params:
        name = param.get("name")
        alias = param.get("alias") or name
        names = {str(name).lower(), str(alias).lower()}
        if any(
            f"{candidate} allowed only to admins" in text
            or f"{candidate} permitted only to admins" in text
            or f"admin for {candidate}" in text
            or f"admins for {candidate}" in text
            for candidate in names
        ):
            constraints.append(
                {
                    "type": "admin_only_parameter",
                    "parameter": name,
                    "alias": alias,
                    "failure_status": 403,
                    "derivation": "heuristic_from_requirement_admin_only_parameter_phrase",
                }
            )
    return constraints


def side_effect_constraints(record: dict[str, Any]) -> dict[str, Any]:
    text = endpoint_requirement_text(record).lower()
    method = record.get("endpoint_contract", {}).get("method")
    read_only = any(phrase in text for phrase in ("make no persistent change", "without persistent effects", "transaction: none", "state unchanged"))
    transaction_required = any(phrase in text for phrase in ("atomically", "begin transaction", "rollback", "commit")) and not read_only
    constraints = {
        "type": "persistence_side_effect_contract",
        "read_only": read_only,
        "transaction_required": transaction_required,
        "must_not_cascade_related_records": "never cascade" in text,
        "must_preserve_related_history": any(phrase in text for phrase in ("preserve", "retain", "immutable history")),
        "forbidden_operations_when_read_only": ["commit", "delete", "insert", "update", "flush"] if read_only else [],
        "expected_write_method": method in {"POST", "PATCH", "PUT", "DELETE"},
        "derivation": "heuristic_from_requirement_and_endpoint_pseudocode_side_effect_phrases",
    }
    return constraints


def pagination_constraints(record: dict[str, Any]) -> list[dict[str, Any]]:
    params = record.get("endpoint_contract", {}).get("request", {}).get("query_parameters", [])
    param_by_name = {param.get("name"): param for param in params}
    constraints: list[dict[str, Any]] = []
    if "limit" in param_by_name or "after_id" in param_by_name:
        limit_meta = parse_query_declaration(param_by_name.get("limit", {}).get("raw_declaration", "")) if "limit" in param_by_name else {}
        after_meta = parse_query_declaration(param_by_name.get("after_id", {}).get("raw_declaration", "")) if "after_id" in param_by_name else {}
        constraints.append(
            {
                "type": "cursor_pagination",
                "order_by": {"field": "id", "direction": "ascending", "immutability_required": True},
                "limit_parameter": "limit" if "limit" in param_by_name else None,
                "limit_default": limit_meta.get("default"),
                "limit_min": limit_meta.get("ge") or limit_meta.get("gt"),
                "limit_max": limit_meta.get("le") or limit_meta.get("lt"),
                "after_id_parameter": "after_id" if "after_id" in param_by_name else None,
                "after_id_gt": after_meta.get("gt"),
                "next_cursor_field": "next_after_id",
                "derivation": "deterministic_from_query_parameters_and_shared_pagination_requirement",
            }
        )
    return constraints


def query_filter_constraints(record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_requirement_text(record).lower()
    params = record.get("endpoint_contract", {}).get("request", {}).get("query_parameters", [])
    aliases = {(param.get("alias") or param.get("name")): param.get("name") for param in params}
    constraints: list[dict[str, Any]] = []
    inclusive_match = re.search(r"inclusive(?:\s+\w+){0,6}\s+for\s+(\w+)", text)
    if not inclusive_match:
        inclusive_match = re.search(r"inclusive\s+(\w+)\s+filter", text)
    if inclusive_match and "from" in aliases and "to" in aliases:
        field = inclusive_match.group(1)
        constraints.extend(
            [
                {
                    "type": "query_field_filter",
                    "parameter": aliases["from"],
                    "alias": "from",
                    "model_field": field,
                    "operator": ">=",
                    "derivation": "heuristic_from_inclusive_field_filter_requirement",
                },
                {
                    "type": "query_field_filter",
                    "parameter": aliases["to"],
                    "alias": "to",
                    "model_field": field,
                    "operator": "<=",
                    "derivation": "heuristic_from_inclusive_field_filter_requirement",
                },
            ]
        )
    return constraints


def interval_overlap_constraints(record: dict[str, Any], output_record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_requirement_text(record).lower()
    endpoint_contract = record.get("endpoint_contract", {})
    params = endpoint_contract.get("request", {}).get("query_parameters", [])
    param_names = {param.get("name") for param in params}
    if "overlap" not in text and "conflict" not in text:
        return []
    if not {"start_at", "end_at"}.issubset(param_names):
        return []
    statuses = sorted(set(re.findall(r"\b[A-Z][A-Z_]{2,}\b", endpoint_requirement_text(record))))
    pseudo_keywords = {
        "AND",
        "AUTHORIZE",
        "BEGIN",
        "DELETE",
        "END",
        "FUNCTION",
        "GET",
        "IF",
        "ISO",
        "JSON",
        "OR",
        "PATCH",
        "POST",
        "PUT",
        "READ",
        "REQ",
        "REQUIRE",
        "RETURN",
        "THEN",
        "TRANSACTION",
        "UTC",
        "VALIDATE",
    }
    active_statuses = [status for status in statuses if status not in pseudo_keywords and not status.startswith("REQ_")]
    models = extract_model_info(output_record)
    source_model = next(
        (
            model["class_name"]
            for model in models
            if {"start_at", "end_at"}.issubset(set(model["fields"]))
        ),
        None,
    )
    endpoint_field = None
    for path_param in endpoint_contract.get("request", {}).get("path_parameters", []):
        name = path_param.get("name")
        if isinstance(name, str) and name.endswith("_id"):
            endpoint_field = name
            break
    return [
        {
            "type": "interval_overlap_filter",
            "source_model": source_model,
            "endpoint_scope_field": endpoint_field,
            "query_interval": {"start_parameter": "start_at", "end_parameter": "end_at"},
            "stored_interval": {"start_field": "start_at", "end_field": "end_at"},
            "semantics": "half_open",
            "required_predicates": [
                {"field": "start_at", "operator": "<", "parameter": "end_at"},
                {"field": "end_at", "operator": ">", "parameter": "start_at"},
            ],
            "status_scope": active_statuses,
            "exclude_parameter": "exclude_appointment_id" if "exclude_appointment_id" in param_names else None,
            "read_only": True,
            "derivation": "heuristic_from_overlap_conflict_requirement_and_query_parameters",
        }
    ]


def reference_absence_constraints(record: dict[str, Any], output_record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_requirement_text(record).lower()
    if "no " not in text or "references" not in text:
        return []
    scope = "any_status" if "any status" in text else "unspecified"
    endpoint_contract = record.get("endpoint_contract", {})
    models = extract_model_info(output_record)
    constraints: list[dict[str, Any]] = []
    for path_param in endpoint_contract.get("request", {}).get("path_parameters", []):
        param_name = path_param.get("name")
        if not isinstance(param_name, str) or not param_name.endswith("_id"):
            continue
        target_model = model_for_id_param(param_name, models)
        for model in models:
            if model["class_name"] == target_model:
                continue
            if param_name not in model["fields"]:
                continue
            constraints.append(
                {
                    "type": "reference_absence_required",
                    "source_model": model["class_name"],
                    "source_field": param_name,
                    "target_parameter": param_name,
                    "target_model": target_model,
                    "scope": scope,
                    "forbidden_narrowing_fields_when_scope_is_any_status": [
                        "status",
                        "cancelled_at",
                        "completed_at",
                        "deleted_at",
                        "active",
                    ]
                    if scope == "any_status"
                    else [],
                    "failure_status": 409,
                    "derivation": "heuristic_from_reference_absence_phrase_and_orm_fields",
                }
            )
    return dedupe_dicts(constraints)


def ownership_constraints(record: dict[str, Any], output_record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_requirement_text(record).lower()
    if not any(marker in text for marker in ("owner", "caller owns", "mapped to", "client account", "caller account")):
        return []
    models = extract_model_info(output_record)
    candidate_owner_fields = sorted(
        {
            field
            for model in models
            for field in model["fields"]
            if field.endswith("account_id") or field.endswith("owner_id") or field.endswith("user_id")
        }
    )
    return [
        {
            "type": "owner_or_admin_access_control",
            "admin_role_value": "admin",
            "caller_identity_field": "current_user.id",
            "candidate_owner_fields": candidate_owner_fields,
            "failure_status": 403,
            "applies_to_non_admin_users": True,
            "derivation": "heuristic_from_ownership_requirement_and_orm_identity_fields",
        }
    ]


def status_state_constraints(record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_requirement_text(record)
    constraints: list[dict[str, Any]] = []
    for match in re.finditer(r"(?:allow|permit|permitted|only while|only a|only an|permit only)\s+(?:only\s+)?(?:a\s+|an\s+)?([A-Z_]+)(?:\s+or\s+([A-Z_]+))?", text, flags=re.I):
        statuses = [status for status in match.groups() if status and status.isupper()]
        if statuses:
            constraints.append(
                {
                    "type": "status_state_guard",
                    "field": "status",
                    "allowed_current_statuses": sorted(set(statuses)),
                    "failure_status": 409,
                    "derivation": "heuristic_from_status_guard_requirement_phrase",
                }
            )
    return dedupe_dicts(constraints)


def body_constraints(record: dict[str, Any]) -> list[dict[str, Any]]:
    if not record.get("endpoint_contract", {}).get("request", {}).get("body_required"):
        return []
    return [
        {
            "type": "json_body_required",
            "content_type": "application/json",
            "unsupported_media_type_status": 415,
            "malformed_json_status": 400,
            "field_validation_status": 422,
            "derivation": "heuristic_from_body_required_and_seed_operational_context",
        }
    ]


def dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = json.dumps(item, sort_keys=True, ensure_ascii=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def machine_readable_constraints(record: dict[str, Any], output_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": V3_VERSION,
        "design_policy": {
            "constraints_are_declarative": True,
            "constraints_must_not_embed_required_python_code": True,
            "generator_must_implement_constraints_not_only_comment_them": True,
        },
        "route": endpoint_route_constraints(record, output_record),
        "responses": response_constraints(record),
        "orm_identity": orm_identity_constraints(output_record),
        "authorization": auth_constraints(record, output_record),
        "access_control": dedupe_dicts(admin_only_parameter_constraints(record) + ownership_constraints(record, output_record)),
        "persistence": side_effect_constraints(record),
        "request_body": body_constraints(record),
        "pagination": pagination_constraints(record),
        "query_filters": query_filter_constraints(record),
        "intervals": interval_overlap_constraints(record, output_record),
        "references": reference_absence_constraints(record, output_record),
        "state_transitions": status_state_constraints(record),
    }


def build_v3_record(v2_record: dict[str, Any], output_record: dict[str, Any]) -> dict[str, Any]:
    record = copy.deepcopy(v2_record)
    record["codegen_spec_version"] = V3_VERSION
    source = record.setdefault("source", {})
    source["v3_source_input_format"] = "finetuning/per_backends_dataset_v2 paired inputs.json"
    source["v3_derivation_policy"] = (
        "Generated from v2 contracts and target interface identity. Adds declarative machine-readable "
        "constraints; it does not add new domain requirements."
    )
    record["machine_readable_constraints"] = machine_readable_constraints(record, output_record)
    record["generation_constraints"] = list(record.get("generation_constraints", [])) + [
        "Treat machine_readable_constraints as executable contract requirements, not as optional comments.",
        "Do not implement a narrower predicate than a declared machine-readable scope.",
        "Do not invent ORM tables, foreign keys, owner identity fields, status values, or controller calls outside the declared constraints.",
    ]
    return record


def main() -> None:
    args = parse_args()
    manifest = load_json(args.source_dir / "manifest.json")
    generated_manifest = copy.deepcopy(manifest)
    generated_manifest["source"] = str(args.source_dir)
    generated_manifest["format"] = "v3 machine-readable contract paired JSON arrays; inputs[i] corresponds to outputs[i]"
    generated_manifest["input_version"] = V3_VERSION
    generated_manifest["output_fields"] = list(OUTPUT_FIELDS)

    total_records = 0
    constraint_counts: dict[str, int] = {}
    for backend in generated_manifest["backends"]:
        inputs = load_json(args.source_dir / backend["inputs"])
        outputs = load_json(args.source_dir / backend["outputs"])
        if len(inputs) != len(outputs):
            raise ValueError(f"Mismatched inputs/outputs for {backend['dataset_key']}")
        v3_inputs = [build_v3_record(input_record, output_record) for input_record, output_record in zip(inputs, outputs)]
        total_records += len(v3_inputs)
        for record in v3_inputs:
            constraints = record["machine_readable_constraints"]
            for key, value in constraints.items():
                if key in {"schema_version", "design_policy"}:
                    continue
                if isinstance(value, list):
                    constraint_counts[key] = constraint_counts.get(key, 0) + len(value)
                elif isinstance(value, dict):
                    constraint_counts[key] = constraint_counts.get(key, 0) + 1

        write_json(args.output_dir / backend["inputs"], v3_inputs, args.overwrite)
        write_json(args.output_dir / backend["outputs"], outputs, args.overwrite)

    generated_manifest["constraint_summary"] = dict(sorted(constraint_counts.items()))
    generated_manifest["records"] = total_records
    write_json(args.output_dir / "manifest.json", generated_manifest, args.overwrite)
    print(f"Records: {total_records}")
    print(f"Dataset dir: {args.output_dir}")
    print("Constraint summary:")
    for key, value in sorted(constraint_counts.items()):
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
