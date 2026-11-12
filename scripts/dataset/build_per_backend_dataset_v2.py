"""Build contract-oriented v2 per-backend datasets from the v1 paired datasets.

The v2 input keeps the original target code unchanged but presents the prompt
side as a structured API contract with explicit, derivable constraints. It is
intentionally conservative: fields that come from natural language are copied or
marked as heuristic instead of being treated as manually verified facts.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


DATASET_GROUPS = {
    "clinic": {
        "folder": "Clinic",
        "keys": {
            "clinic-appointments-rooms-conflicts",
            "clinic-owners-animals-relations",
            "clinic-records-vaccinations-temporal",
        },
    },
    "fitness": {
        "folder": "fitness",
        "keys": {
            "fitness-goals-progress-loads",
            "fitness-plans-exercises-configurations",
            "fitness-sessions-sets-1rm",
        },
    },
    "fleet": {
        "folder": "fleet",
        "keys": {
            "fleet-maintenance-planning",
            "fleet-temporal-conflict",
            "fleet-vehicle-states",
        },
    },
    "hr": {
        "folder": "hr",
        "keys": {
            "hr-departments-roles-reports",
            "hr-leave-approval-overlaps",
            "hr-leave-balance-intervals",
        },
    },
    "inventory": {
        "folder": "inventory",
        "keys": {
            "inventory-multiwarehouse-transfers",
            "inventory-sku-categories-reorder",
            "inventory-stock-movements-nonnegative",
        },
    },
    "kanban": {
        "folder": "kanban",
        "keys": {
            "kanban-columns-bulk-updates",
            "kanban-filters-priorities-audit",
            "kanban-tickets-assignees-transitions",
        },
    },
    "real-estate": {
        "folder": "real-estate",
        "keys": {
            "real-estate-listings-publication",
            "real-estate-search-filters",
            "real-estate-visits-conflicts",
        },
    },
    "recipes": {
        "folder": "recipes",
        "keys": {
            "recipes-ingredients-servings",
            "recipes-macronutrients-tags-filters",
            "recipes-weekly-plan-references",
        },
    },
    "resturant": {
        "folder": "resturant",
        "keys": {
            "restaurant-cancellations-waitlist",
            "restaurant-capacity-tables-shifts",
            "restaurant-overbooking-reservations",
        },
    },
    "smart-home": {
        "folder": "smart-home",
        "keys": {
            "smart-home-hub-rooms-patch",
            "smart-home-rules-actuators",
            "smart-home-telemetry-range",
        },
    },
}

OUTPUT_FIELDS = ("route_code", "schemas_code", "controller_code", "orm_model_code")
PROMPT_OUTPUT_SECTIONS = ("schemas_code", "orm_model_code", "controller_code", "route_code")

HTTP_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("datasets/v1"),
        help="Existing v1 per-backend dataset directory.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("datasets/v2"),
        help="Destination for the v2 per-backend dataset directory.",
    )
    parser.add_argument(
        "--scope",
        action="append",
        choices=sorted(DATASET_GROUPS),
        default=None,
        help="Dataset group to generate. Can be passed multiple times. Defaults to clinic.",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.scope is None:
        args.scope = ["clinic"]
    return args


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, content: Any, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite {path}; pass --overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def parse_endpoint(endpoint: str) -> tuple[str, str]:
    method, path = endpoint.split(" ", 1)
    if method not in HTTP_METHODS:
        raise ValueError(f"Unsupported HTTP method in endpoint: {endpoint}")
    return method, path


def extract_status_codes(text: str) -> list[int]:
    status_clause = re.search(r"emit only\s+([^.]*)", text, flags=re.I)
    source = status_clause.group(1) if status_clause else text
    codes = sorted({int(match) for match in re.findall(r"\b[2-5][0-9]{2}\b", source)})
    return codes


def success_codes_for_method(method: str, allowed_codes: list[int]) -> list[int]:
    if method == "POST" and 201 in allowed_codes:
        return [201]
    if method == "DELETE" and 204 in allowed_codes:
        return [204]
    if 200 in allowed_codes:
        return [200]
    return [code for code in allowed_codes if 200 <= code < 300]


def error_codes(allowed_codes: list[int]) -> list[int]:
    return [code for code in allowed_codes if code >= 400]


def auth_policy(formal_requirement: str, endpoint: str) -> dict[str, Any]:
    lower = formal_requirement.lower()
    if "public" in lower or "no input or authorization requirement" in lower:
        return {
            "auth_required": False,
            "roles_or_ownership": ["public"],
            "derivation": "copied_from_formal_requirement",
        }
    roles: list[str] = []
    if "admin" in lower:
        roles.append("admin")
    if "user" in lower or "client" in lower or "owner" in lower or "mapped owner" in lower:
        roles.append("user_or_owner")
    if not roles:
        roles.append("authenticated_actor")
    return {
        "auth_required": endpoint != "GET /health",
        "roles_or_ownership": sorted(set(roles)),
        "derivation": "heuristic_from_formal_requirement",
    }


def idempotency_policy(formal_requirement: str) -> dict[str, str]:
    lower = formal_requirement.lower()
    if "non-idempotent" in lower:
        value = "non_idempotent"
    elif "idempotent for an identical valid payload" in lower:
        value = "idempotent_for_identical_valid_payload"
    elif "idempotent only insofar" in lower or "repeat is 404" in lower:
        value = "repeat_deleted_or_absent_id_is_404"
    else:
        value = "not_specified"
    return {"value": value, "derivation": "copied_or_heuristic_from_formal_requirement"}


def side_effect_policy(formal_requirement: str, pseudocode_body: str) -> dict[str, Any]:
    text = f"{formal_requirement}\n{pseudocode_body}".lower()
    constraints: list[str] = []
    if "make no persistent change" in text or "transaction: none" in text or "state unchanged" in text:
        constraints.append("must_not_modify_persistent_state")
    if "atomically" in text or "begin transaction" in text or "rollback" in text:
        constraints.append("must_coordinate_atomic_persistence_changes")
    if "never cascade" in text:
        constraints.append("must_not_cascade_related_records")
    if "preserve" in text or "retain" in text:
        constraints.append("must_preserve_declared_related_history")
    return {"constraints": sorted(set(constraints)), "derivation": "heuristic_from_requirement_and_pseudocode"}


def extract_query_parameters(route_code: str, path: str) -> list[dict[str, Any]]:
    parameters: list[dict[str, Any]] = []
    path_parameter_names = {parameter["name"] for parameter in extract_path_parameters(path)}
    signature_match = re.search(r"def\s+\w+\s*\((?P<body>.*?)\):", route_code, flags=re.S)
    if not signature_match:
        return parameters
    body = signature_match.group("body")
    for raw_line in body.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line or ":" not in line:
            continue
        name = line.split(":", 1)[0].strip()
        if name in path_parameter_names:
            continue
        if name in {"current_user", "db", "request"}:
            continue
        if "Depends(" in line:
            continue
        if "Query(" in line:
            alias_match = re.search(r"alias\s*=\s*[\"']([^\"']+)[\"']", line)
            parameters.append(
                {
                    "name": name,
                    "source": "query",
                    "required": "Query(..." in line,
                    "alias": alias_match.group(1) if alias_match else None,
                    "raw_declaration": line,
                    "derivation": "deterministic_from_route_code",
                }
            )
        elif "=" in line:
            parameters.append(
                {
                    "name": name,
                    "source": "query",
                    "required": False,
                    "alias": None,
                    "raw_declaration": line,
                    "derivation": "deterministic_from_route_code_implicit_fastapi_query_parameter",
                }
            )
    return parameters


def application_summary(specification: dict[str, Any]) -> str:
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


def extract_path_parameters(path: str) -> list[dict[str, Any]]:
    return [
        {"name": name, "source": "path", "required": True, "derivation": "deterministic_from_endpoint_path"}
        for name in re.findall(r"{([^}]+)}", path)
    ]


def schema_names(blocks: list[str]) -> list[str]:
    names: list[str] = []
    for block in blocks:
        names.extend(re.findall(r"^class\s+(\w+)\b", block, flags=re.M))
    return sorted(set(names))


def model_tables(blocks: list[str]) -> list[dict[str, str]]:
    models: list[dict[str, str]] = []
    for block in blocks:
        class_match = re.search(r"^class\s+(\w+)\b", block, flags=re.M)
        table_match = re.search(r"__tablename__\s*=\s*[\"']([^\"']+)[\"']", block)
        if class_match:
            models.append(
                {
                    "class_name": class_match.group(1),
                    "table_name": table_match.group(1) if table_match else "",
                }
            )
    return models


def foreign_keys(blocks: list[str]) -> list[str]:
    keys: set[str] = set()
    for block in blocks:
        keys.update(re.findall(r"ForeignKey\([\"']([^\"']+)[\"']\)", block))
    return sorted(keys)


def controller_routines(blocks: list[str]) -> list[str]:
    routines: set[str] = set()
    for block in blocks:
        routines.update(re.findall(r"def\s+(\w+)\s*\(", block))
    return sorted(routines)


def semantic_constraints(formal_requirement: str, pseudocode_body: str) -> list[dict[str, str]]:
    fragments: list[dict[str, str]] = [
        {
            "text": formal_requirement,
            "derivation": "copied_from_formal_requirement",
        }
    ]
    for sentence in re.split(r"(?<=[.;])\s+", formal_requirement):
        sentence = sentence.strip()
        if not sentence:
            continue
        lower = sentence.lower()
        if any(
            marker in lower
            for marker in (
                "only",
                "must",
                "require",
                "reject",
                "never",
                "without",
                "preserve",
                "retain",
                "restrict",
                "permit",
                "allowed",
                "idempotent",
                "non-idempotent",
            )
        ):
            fragments.append({"text": sentence, "derivation": "heuristic_sentence_from_formal_requirement"})
    if pseudocode_body:
        fragments.append({"text": pseudocode_body, "derivation": "copied_from_endpoint_pseudocode"})
    return fragments


def semantic_validation_rules(
    method: str,
    path: str,
    allowed_codes: list[int],
    route_code: str,
    output_record: dict[str, Any],
) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = [
        {
            "rule": "route_method_and_path_match",
            "must_contain": [f"@app.{method.lower()}", f'"{path}"'],
            "derivation": "deterministic_from_endpoint",
        },
        {
            "rule": "allowed_status_codes_declared_by_requirement",
            "allowed_status_codes": allowed_codes,
            "derivation": "deterministic_from_formal_requirement_status_numbers",
        },
    ]
    if "Depends(get_current_user)" in route_code:
        rules.append(
            {
                "rule": "authenticated_route_uses_current_user_dependency",
                "must_contain": ["Depends(get_current_user)"],
                "derivation": "deterministic_from_target_route_code",
            }
        )
    tables = [item["table_name"] for item in model_tables(output_record.get("orm_model_code", [])) if item["table_name"]]
    if tables:
        rules.append(
            {
                "rule": "orm_table_names_match_target_backend",
                "must_contain_table_names": sorted(set(tables)),
                "derivation": "deterministic_from_target_orm_code",
            }
        )
    fks = foreign_keys(output_record.get("orm_model_code", []))
    if fks:
        rules.append(
            {
                "rule": "foreign_key_targets_match_target_backend",
                "must_contain_foreign_keys": fks,
                "derivation": "deterministic_from_target_orm_code",
            }
        )
    return rules


def build_v2_record(
    input_record: dict[str, Any],
    output_record: dict[str, Any],
    specification: dict[str, Any],
    spec_path: str,
    scope: str,
) -> dict[str, Any]:
    endpoint = input_record["endpoint"]
    method, path = parse_endpoint(endpoint)
    formal_requirement = input_record["formal_requirement"]
    pseudocode = input_record.get("pseudocode", {})
    pseudocode_body = pseudocode.get("body", "") if isinstance(pseudocode, dict) else ""
    route_code = output_record.get("route_code", "")
    allowed_codes = extract_status_codes(formal_requirement)

    return {
        "codegen_spec_version": "2.0.0",
        "dataset_key": input_record["dataset_key"],
        "endpoint": endpoint,
        "source": {
            "v1_input_format": "finetuning/per_backend_datasets paired inputs.json",
            "specification_path": spec_path,
            "derivation_policy": f"Generated from existing v1 data and {scope} specification. No new domain requirements are invented.",
        },
        "target": {
            "language": "python",
            "framework": "fastapi",
            "backend_profile": "local_sqlalchemy_persistence",
            "architecture": "route_controller_orm_schema",
            "output_sections": list(PROMPT_OUTPUT_SECTIONS),
        },
        "application_context": {
            "domain": specification.get("domain"),
            "variant": specification.get("variant"),
            "summary": application_summary(specification),
            "persistent_database_required": bool(output_record.get("orm_model_code")),
            "orm_required": bool(output_record.get("orm_model_code")),
        },
        "domain_invariants": specification.get("domain_invariants", []),
        "seed_and_operational_context": specification.get("seed_data", []),
        "endpoint_contract": {
            "operation_id": endpoint.lower().replace(" ", "_").replace("/", "_").replace("{", "").replace("}", "").replace("-", "_"),
            "method": method,
            "path": path,
            "formal_requirement": formal_requirement,
            "auth_policy": auth_policy(formal_requirement, endpoint),
            "request": {
                "path_parameters": extract_path_parameters(path),
                "query_parameters": extract_query_parameters(route_code, path),
                "body_required": "application/json" in formal_requirement.lower() or "accepting json" in formal_requirement.lower(),
                "derivation": "deterministic_from_endpoint_and_route_code_with_body_flag_from_requirement",
            },
            "success_responses": {
                "status_codes": success_codes_for_method(method, allowed_codes),
                "derivation": "heuristic_from_method_and_allowed_status_codes",
            },
            "error_responses": {
                "status_codes": error_codes(allowed_codes),
                "error_model": "controlled AppException / existing backend error handling",
                "derivation": "deterministic_from_formal_requirement_status_numbers",
            },
            "allowed_status_codes": {
                "values": allowed_codes,
                "derivation": "deterministic_from_formal_requirement_status_numbers",
            },
            "idempotency": idempotency_policy(formal_requirement),
            "side_effects": side_effect_policy(formal_requirement, pseudocode_body),
        },
        "component_contract": {
            "schemas": {
                "pseudocode": input_record.get("schemas_pseudocode"),
                "target_class_names": schema_names(output_record.get("schemas_code", [])),
                "derivation": "pseudocode_from_v1_input_and_class_names_from_target_output",
            },
            "orm_models": {
                "pseudocode": input_record.get("orm_model_pseudocode"),
                "target_models": model_tables(output_record.get("orm_model_code", [])),
                "target_foreign_keys": foreign_keys(output_record.get("orm_model_code", [])),
                "derivation": "pseudocode_from_v1_input_and_tables_from_target_output",
            },
            "controller": {
                "pseudocode": input_record.get("controller_pseudocode"),
                "target_routines": controller_routines(output_record.get("controller_code", [])),
                "derivation": "pseudocode_from_v1_input_and_routines_from_target_output",
            },
            "route": {
                "pseudocode": input_record.get("pseudocode"),
                "derivation": "copied_from_v1_input",
            },
        },
        "semantic_constraints": semantic_constraints(formal_requirement, pseudocode_body),
        "semantic_validation_rules": semantic_validation_rules(method, path, allowed_codes, route_code, output_record),
        "generation_constraints": [
            "Return exactly four fenced Python sections named schemas_code, orm_model_code, controller_code, and route_code.",
            "Preserve the declared FastAPI method and path.",
            "Use only the ORM table names and foreign-key targets declared in the target backend contract.",
            "Respect every copied formal requirement and endpoint pseudocode constraint.",
            "Do not invent external services or database tables that are not present in the component contract.",
        ],
    }


def selected_keys(scopes: list[str]) -> set[str]:
    keys: set[str] = set()
    for scope in scopes:
        keys.update(DATASET_GROUPS[scope]["keys"])
    return keys


def scope_for_key(dataset_key: str) -> str:
    for scope, config in DATASET_GROUPS.items():
        if dataset_key in config["keys"]:
            return scope
    raise ValueError(f"No configured v2 scope for {dataset_key}")


def source_backend_dir(source_dir: Path, dataset_key: str, scope: str) -> Path:
    candidates = [
        source_dir / DATASET_GROUPS[scope]["folder"] / dataset_key,
        source_dir / dataset_key,
    ]
    candidates.extend(path.parent for path in source_dir.glob(f"**/{dataset_key}/inputs.json"))
    for candidate in candidates:
        if (candidate / "inputs.json").exists() and (candidate / "outputs.json").exists():
            return candidate
    raise FileNotFoundError(f"Could not find v1 inputs/outputs for {dataset_key} under {source_dir}")


def backend_entries(manifest: dict[str, Any], scopes: list[str]) -> list[dict[str, Any]]:
    entries = manifest.get("backends", [])
    keys = selected_keys(scopes)
    entries = [entry for entry in entries if entry.get("dataset_key") in keys]
    return sorted(entries, key=lambda entry: entry["dataset_key"])


def main() -> None:
    args = parse_args()
    manifest = load_json(args.source_dir / "manifest.json")
    entries = backend_entries(manifest, args.scope)
    if not entries:
        raise ValueError("No backend entries selected")

    generated_manifest: dict[str, Any] = {
        "source": str(args.source_dir),
        "format": "v2 contract-oriented paired JSON arrays; inputs[i] corresponds to outputs[i]",
        "scope": sorted(args.scope),
        "input_version": "2.0.0",
        "output_fields": list(OUTPUT_FIELDS),
        "backends": [],
    }

    for entry in entries:
        dataset_key = entry["dataset_key"]
        scope = scope_for_key(dataset_key)
        source_dir = source_backend_dir(args.source_dir, dataset_key, scope)
        inputs = load_json(source_dir / "inputs.json")
        outputs = load_json(source_dir / "outputs.json")
        if len(inputs) != len(outputs):
            raise ValueError(f"Mismatched inputs/outputs for {dataset_key}")

        specification = load_json(Path(entry["specification"]))
        v2_inputs = [
            build_v2_record(input_record, output_record, specification, entry["specification"], scope)
            for input_record, output_record in zip(inputs, outputs)
        ]
        v2_outputs = [{field: output_record[field] for field in OUTPUT_FIELDS} for output_record in outputs]

        target_folder = DATASET_GROUPS[scope]["folder"]
        target_dir = args.output_dir / target_folder / dataset_key
        write_json(target_dir / "inputs.json", v2_inputs, args.overwrite)
        write_json(target_dir / "outputs.json", v2_outputs, args.overwrite)
        generated_manifest["backends"].append(
            {
                "dataset_key": dataset_key,
                "scope": scope,
                "records": len(v2_inputs),
                "specification": entry["specification"],
                "inputs": f"{target_folder}/{dataset_key}/inputs.json",
                "outputs": f"{target_folder}/{dataset_key}/outputs.json",
            }
        )

    write_json(args.output_dir / "manifest.json", generated_manifest, args.overwrite)


if __name__ == "__main__":
    main()
