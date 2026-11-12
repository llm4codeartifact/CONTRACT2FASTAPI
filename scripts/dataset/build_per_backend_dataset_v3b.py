"""Build compact controller-focused v3b per-backend datasets from v2 pairs.

v3b keeps the target code unchanged and replaces the large v3 contract with a
smaller declarative contract focused on controller behavior: auth, lookups,
filters, preconditions, side effects, pagination, and ORM identity.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any


VERSION = "3.1.0-controller-focused"
OUTPUT_FIELDS = ("route_code", "schemas_code", "controller_code", "orm_model_code")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path("datasets/v2"))
    parser.add_argument("--output-dir", type=Path, default=Path("datasets/v3b"))
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
    route = record.get("component_contract", {}).get("route", {}).get("pseudocode") or {}
    return "\n".join(
        part
        for part in [endpoint_contract.get("formal_requirement", ""), route.get("body", "") if isinstance(route, dict) else ""]
        if part
    )


def all_text(record: dict[str, Any]) -> str:
    parts = [endpoint_text(record)]
    parts.extend(item.get("requirement", "") for item in record.get("domain_invariants", []) if isinstance(item, dict))
    parts.extend(str(item) for item in record.get("seed_and_operational_context", []))
    return "\n".join(part for part in parts if part)


def route_calls(route_code: str) -> list[dict[str, str]]:
    return [
        {"controller_class": class_name, "routine": routine}
        for class_name, routine in sorted(set(re.findall(r"\b(\w+Controller)\.(\w+)\s*\(", route_code)))
    ]


def model_info(output_record: dict[str, Any]) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for block in output_record.get("orm_model_code", []):
        class_match = re.search(r"^class\s+(\w+)\b", block, flags=re.M)
        if not class_match:
            continue
        table_match = re.search(r"__tablename__\s*=\s*[\"']([^\"']+)[\"']", block)
        fields = sorted(set(re.findall(r"^\s*(\w+)\s*=\s*Column\(", block, flags=re.M)))
        fks = []
        for field in fields:
            field_re = rf"^\s*{re.escape(field)}\s*=\s*Column\((.*?)(?=^\s*\w+\s*=\s*Column\(|^\s*class\s+|\Z)"
            field_match = re.search(field_re, block, flags=re.M | re.S)
            if not field_match:
                continue
            fk_match = re.search(r"ForeignKey\([\"']([^\"']+)[\"']\)", field_match.group(1))
            if fk_match:
                fks.append({"field": field, "target": fk_match.group(1)})
        models.append(
            {
                "name": class_match.group(1),
                "table": table_match.group(1) if table_match else "",
                "fields": fields,
                "foreign_keys": sorted(fks, key=lambda item: (item["field"], item["target"])),
            }
        )
    return sorted(models, key=lambda item: item["name"])


def model_for_param(param: str, models: list[dict[str, Any]]) -> str | None:
    if not param.endswith("_id"):
        return None
    stem = param.removesuffix("_id").lower()
    best: tuple[int, str] | None = None
    for model in models:
        class_name = model["name"].lower().removesuffix("model")
        table = model["table"].lower().rstrip("s")
        score = 3 if stem in {class_name, table} else 2 if stem in class_name or stem in table else 0
        if score and (best is None or score > best[0]):
            best = (score, model["name"])
    return best[1] if best else None


def parse_query_meta(raw: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    alias = re.search(r"alias\s*=\s*[\"']([^\"']+)[\"']", raw)
    if alias:
        meta["alias"] = alias.group(1)
    default = re.search(r"Query\(([^,)]*)", raw)
    if default:
        value = default.group(1).strip()
        meta["required"] = value == "..."
        if value not in {"...", "None", ""}:
            try:
                meta["default"] = int(value)
            except ValueError:
                meta["default"] = value
        elif value == "None":
            meta["default"] = None
    for key in ("ge", "gt", "le", "lt"):
        match = re.search(rf"{key}\s*=\s*(-?\d+)", raw)
        if match:
            meta[key] = int(match.group(1))
    return meta


def route_contract(record: dict[str, Any], output_record: dict[str, Any]) -> dict[str, Any]:
    endpoint = record["endpoint_contract"]
    request = endpoint.get("request", {})
    query = []
    for param in request.get("query_parameters", []):
        meta = parse_query_meta(param.get("raw_declaration", ""))
        query.append(
            {
                "name": param.get("name"),
                "alias": param.get("alias"),
                "required": param.get("required", False),
                "constraints": meta,
            }
        )
    return {
        "method": endpoint.get("method"),
        "path": endpoint.get("path"),
        "path_params": request.get("path_parameters", []),
        "query_params": query,
        "body_required": request.get("body_required", False),
        "success_statuses": endpoint.get("success_responses", {}).get("status_codes", []),
        "error_statuses": endpoint.get("error_responses", {}).get("status_codes", []),
        "route_controller_calls": route_calls(output_record.get("route_code", "")),
    }


def auth_contract(record: dict[str, Any]) -> dict[str, Any]:
    auth = record.get("endpoint_contract", {}).get("auth_policy", {})
    roles = auth.get("roles_or_ownership", [])
    if not auth.get("auth_required", False):
        kind = "public"
    elif roles == ["admin"] or "admin" in roles and "user_or_owner" not in roles:
        kind = "admin_only"
    elif "user_or_owner" in roles:
        kind = "owner_or_admin"
    else:
        kind = "authenticated"
    return {"type": kind, "allowed_actor_kinds": roles, "failure_statuses": [401, 403]}


def admin_only_filters(record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_text(record).lower()
    filters = []
    for param in record.get("endpoint_contract", {}).get("request", {}).get("query_parameters", []):
        name = str(param.get("name"))
        alias = str(param.get("alias") or name)
        names = {name.lower(), alias.lower()}
        if any(
            f"{candidate} allowed only to admins" in text
            or f"{candidate} permitted only to admins" in text
            or f"admin for {candidate}" in text
            for candidate in names
        ):
            filters.append({"type": "admin_only_filter", "parameter": name, "alias": alias, "failure_status": 403})
    return filters


def owner_scope(record: dict[str, Any], output_record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_text(record).lower()
    if not any(marker in text for marker in ("owner", "caller owns", "mapped to", "caller account", "client user", "client owner")):
        return []
    owner_fields = sorted(
        {
            field
            for model in model_info(output_record)
            for field in model["fields"]
            if field.endswith("account_id") or field.endswith("owner_id") or field.endswith("user_id")
        }
    )
    return [
        {
            "type": "owner_scope_for_non_admin",
            "caller_field": "current_user.id",
            "candidate_owner_fields": owner_fields,
            "failure_status": 403,
        }
    ]


def filters_contract(record: dict[str, Any]) -> list[dict[str, Any]]:
    params = record.get("endpoint_contract", {}).get("request", {}).get("query_parameters", [])
    result = []
    inclusive = re.search(r"inclusive(?:\s+\w+){0,6}\s+for\s+(\w+)", endpoint_text(record).lower())
    if not inclusive:
        inclusive = re.search(r"inclusive\s+(\w+)\s+filter", endpoint_text(record).lower())
    for param in params:
        name = param.get("name")
        alias = param.get("alias") or name
        if alias == "from" and inclusive:
            result.append({"parameter": name, "alias": alias, "model_field": inclusive.group(1), "operator": ">="})
        elif alias == "to" and inclusive:
            result.append({"parameter": name, "alias": alias, "model_field": inclusive.group(1), "operator": "<="})
        elif name not in {"limit", "after_id"}:
            field = alias if alias else name
            result.append({"parameter": name, "alias": alias, "model_field": field, "operator": "=="})
    return result


def pagination_contract(record: dict[str, Any]) -> dict[str, Any] | None:
    params = {p.get("name"): p for p in record.get("endpoint_contract", {}).get("request", {}).get("query_parameters", [])}
    if "limit" not in params and "after_id" not in params:
        return None
    limit = parse_query_meta(params.get("limit", {}).get("raw_declaration", "")) if "limit" in params else {}
    after = parse_query_meta(params.get("after_id", {}).get("raw_declaration", "")) if "after_id" in params else {}
    return {
        "order_by": {"field": "id", "direction": "asc"},
        "limit": {"parameter": "limit", "default": limit.get("default"), "ge": limit.get("ge"), "le": limit.get("le")},
        "cursor": {"parameter": "after_id", "field": "id", "operator": ">", "gt": after.get("gt")},
        "next_cursor_field": "next_after_id",
    }


def interval_contract(record: dict[str, Any], output_record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_text(record).lower()
    params = {p.get("name") for p in record.get("endpoint_contract", {}).get("request", {}).get("query_parameters", [])}
    if not {"start_at", "end_at"}.issubset(params) or not any(word in text for word in ("overlap", "conflict")):
        return []
    statuses = sorted(set(re.findall(r"\b[A-Z][A-Z_]{2,}\b", endpoint_text(record))))
    blocked = {"FUNCTION", "REQUIRE", "RETURN", "VALIDATE", "READ", "REQ", "UTC", "JSON", "GET", "POST", "PATCH", "DELETE", "IF", "THEN", "END", "OR"}
    statuses = [s for s in statuses if s not in blocked]
    source = next((m["name"] for m in model_info(output_record) if {"start_at", "end_at"}.issubset(set(m["fields"]))), None)
    scope_field = next((p.get("name") for p in record.get("endpoint_contract", {}).get("request", {}).get("path_parameters", []) if str(p.get("name", "")).endswith("_id")), None)
    return [
        {
            "type": "half_open_interval_overlap",
            "source_model": source,
            "scope_field": scope_field,
            "required_predicates": [
                {"field": "start_at", "operator": "<", "parameter": "end_at"},
                {"field": "end_at", "operator": ">", "parameter": "start_at"},
            ],
            "status_scope": statuses,
            "exclude_parameter": "exclude_appointment_id" if "exclude_appointment_id" in params else None,
        }
    ]


def reference_preconditions(record: dict[str, Any], output_record: dict[str, Any]) -> list[dict[str, Any]]:
    text = endpoint_text(record).lower()
    if "no " not in text or "references" not in text:
        return []
    models = model_info(output_record)
    scope = "all_rows" if "any status" in text else "matching_rows"
    result = []
    for param in record.get("endpoint_contract", {}).get("request", {}).get("path_parameters", []):
        name = param.get("name")
        if not isinstance(name, str) or not name.endswith("_id"):
            continue
        target = model_for_param(name, models)
        for model in models:
            if model["name"] != target and name in model["fields"]:
                result.append(
                    {
                        "type": "reference_absence",
                        "source_model": model["name"],
                        "match": {name: name},
                        "target_model": target,
                        "scope": scope,
                        "must_not_filter_by": ["status", "cancelled_at", "completed_at", "active", "is_deleted"] if scope == "all_rows" else [],
                        "failure_status": 409,
                    }
                )
    return result


def mutation_contract(record: dict[str, Any]) -> dict[str, Any]:
    text = endpoint_text(record).lower()
    method = record.get("endpoint_contract", {}).get("method")
    return {
        "read_only": any(phrase in text for phrase in ("make no persistent change", "without persistent effects", "transaction: none", "state unchanged")),
        "write_method": method in {"POST", "PATCH", "PUT", "DELETE"},
        "transaction_required": any(phrase in text for phrase in ("atomically", "begin transaction", "rollback", "commit")),
        "preserve_history": any(phrase in text for phrase in ("preserve", "retain", "immutable history")),
        "no_cascade": "never cascade" in text,
    }


def controller_contract(record: dict[str, Any], output_record: dict[str, Any]) -> dict[str, Any]:
    controller = record.get("component_contract", {}).get("controller", {})
    routines = controller.get("target_routines", [])
    path_params = record.get("endpoint_contract", {}).get("request", {}).get("path_parameters", [])
    models = model_info(output_record)
    lookups = []
    for param in path_params:
        name = param.get("name")
        if isinstance(name, str) and name.endswith("_id"):
            lookups.append({"parameter": name, "model": model_for_param(name, models), "not_found_status": 404})
    return {
        "routine": routines[0] if routines else None,
        "auth": auth_contract(record),
        "resource_lookups": lookups,
        "access_control": admin_only_filters(record) + owner_scope(record, output_record),
        "filters": filters_contract(record),
        "intervals": interval_contract(record, output_record),
        "preconditions": reference_preconditions(record, output_record),
        "pagination": pagination_contract(record),
        "mutation": mutation_contract(record),
    }


def build_v3b_record(record: dict[str, Any], output_record: dict[str, Any]) -> dict[str, Any]:
    output_models = model_info(output_record)
    cited_req_ids = set(re.findall(r"REQ-\d+-\d+", endpoint_text(record)))
    invariants = [
        item
        for item in record.get("domain_invariants", [])
        if isinstance(item, dict) and item.get("id") in cited_req_ids
    ]
    if not invariants:
        invariants = record.get("domain_invariants", [])[:3]
    return {
        "codegen_spec_version": VERSION,
        "dataset_key": record["dataset_key"],
        "endpoint": record["endpoint"],
        "source": {
            "source_dataset": "finetuning/per_backends_dataset_v2",
            "specification_path": record.get("source", {}).get("specification_path"),
            "derivation_policy": "Compact controller-focused transformation of v2; target code is unchanged.",
        },
        "target": record["target"],
        "application_context": record["application_context"],
        "route_contract": route_contract(record, output_record),
        "data_model_contract": {
            "models": output_models,
            "allowed_tables": sorted({m["table"] for m in output_models if m["table"]}),
            "allowed_foreign_key_targets": sorted({fk["target"] for m in output_models for fk in m["foreign_keys"]}),
            "forbid_undeclared_tables": True,
            "forbid_undeclared_foreign_keys": True,
        },
        "controller_contract": controller_contract(record, output_record),
        "requirement_context": {
            "formal_requirement": record.get("endpoint_contract", {}).get("formal_requirement"),
            "endpoint_pseudocode": record.get("component_contract", {}).get("route", {}).get("pseudocode"),
            "relevant_domain_invariants": invariants,
        },
        "generation_constraints": [
            "Return exactly four fenced Python sections named schemas_code, orm_model_code, controller_code, and route_code.",
            "Implement controller_contract as behavior, not comments.",
            "Do not implement narrower filters than controller_contract scopes.",
            "Do not invent tables, foreign keys, status values, owner fields, or controller calls outside this contract.",
        ],
    }


def main() -> None:
    args = parse_args()
    manifest = load_json(args.source_dir / "manifest.json")
    new_manifest = copy.deepcopy(manifest)
    new_manifest["source"] = str(args.source_dir)
    new_manifest["format"] = "v3b compact controller-focused paired JSON arrays; inputs[i] corresponds to outputs[i]"
    new_manifest["input_version"] = VERSION
    new_manifest["output_fields"] = list(OUTPUT_FIELDS)

    total = 0
    for backend in new_manifest["backends"]:
        inputs = load_json(args.source_dir / backend["inputs"])
        outputs = load_json(args.source_dir / backend["outputs"])
        if len(inputs) != len(outputs):
            raise ValueError(f"Mismatched inputs/outputs for {backend['dataset_key']}")
        v3b_inputs = [build_v3b_record(input_record, output_record) for input_record, output_record in zip(inputs, outputs)]
        total += len(v3b_inputs)
        write_json(args.output_dir / backend["inputs"], v3b_inputs, args.overwrite)
        write_json(args.output_dir / backend["outputs"], outputs, args.overwrite)
    new_manifest["records"] = total
    write_json(args.output_dir / "manifest.json", new_manifest, args.overwrite)
    print(f"Records: {total}")
    print(f"Dataset dir: {args.output_dir}")


if __name__ == "__main__":
    main()
