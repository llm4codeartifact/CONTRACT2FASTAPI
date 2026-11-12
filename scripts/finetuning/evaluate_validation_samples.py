"""Generate and score validation examples for the FastAPI codegen LoRA adapter.

This script is intentionally lightweight: it does not try to run generated APIs.
It checks output structure, extracts the four expected sections, performs basic
syntax checks, and writes a Markdown report for manual review.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer


SECTION_NAMES = ("schemas_code", "orm_model_code", "controller_code", "route_code")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-model", default="Qwen/Qwen2.5-Coder-7B-Instruct")
    parser.add_argument(
        "--adapter",
        default="outputs/qwen25-coder-7b-fastapi-a40-3epoch",
    )
    parser.add_argument(
        "--dataset",
        default="generated/llamafactory-v1/qwen_fastapi_codegen_validation.jsonl",
    )
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--num-samples", type=int, default=10)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--max-new-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.0)
    return parser.parse_args()


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def section_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"<{re.escape(name)}>\s*```(?:python)?\s*(.*?)```\s*</{re.escape(name)}>",
        re.DOTALL | re.IGNORECASE,
    )


def extract_sections(text: str) -> Dict[str, Optional[str]]:
    sections: Dict[str, Optional[str]] = {}
    for name in SECTION_NAMES:
        match = section_pattern(name).search(text)
        sections[name] = match.group(1).strip() if match else None
    return sections


def normalize_python_fragment(code: str) -> str:
    """Normalize extracted class-method snippets enough for ast parsing."""
    if not code.strip() or code.strip().startswith("# None required"):
        return ""

    lines = code.splitlines()
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
                if lines[index].strip() and not lines[index].startswith(" " * function_indent):
                    lines[index] = " " * function_indent + lines[index]
    return textwrap.dedent("\n".join(lines))


def is_python_syntax_valid(code: Optional[str]) -> Tuple[bool, Optional[str]]:
    if code is None:
        return False, "section missing"
    normalized = normalize_python_fragment(code)
    if not normalized:
        return True, None
    try:
        ast.parse(normalized)
        return True, None
    except SyntaxError:
        try:
            ast.parse("class _Snippet:\n" + textwrap.indent(code, "    "))
            return True, None
        except SyntaxError as error:
            return False, error.msg


def endpoint_parts(endpoint: str) -> Tuple[str, str]:
    method, path = endpoint.split(" ", 1)
    return method.lower(), path.strip()


def route_matches_endpoint(route_code: Optional[str], endpoint: str) -> bool:
    if not route_code:
        return False
    method, path = endpoint_parts(endpoint)
    return f"@app.{method}" in route_code and path in route_code


def similarity(a: str, b: str) -> float:
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 4)


def build_prompt(row: Dict[str, Any], tokenizer: Any) -> str:
    conversations = row["conversations"]
    messages = []
    for message in conversations[:2]:
        role = "system" if message["from"] == "system" else "user"
        messages.append({"role": role, "content": message["value"]})
    return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)


def generate(model: Any, tokenizer: Any, prompt: str, max_new_tokens: int, temperature: float) -> str:
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    generation_kwargs: Dict[str, Any] = {
        "max_new_tokens": max_new_tokens,
        "eos_token_id": tokenizer.eos_token_id,
        "repetition_penalty": 1.05,
    }
    if temperature and temperature > 0:
        generation_kwargs.update({"do_sample": True, "temperature": temperature, "top_p": 0.9})
    else:
        generation_kwargs.update({"do_sample": False})

    with torch.no_grad():
        output_ids = model.generate(**inputs, **generation_kwargs)
    generated_ids = output_ids[0][inputs["input_ids"].shape[-1] :]
    return tokenizer.decode(generated_ids, skip_special_tokens=True)


def evaluate_row(index: int, row: Dict[str, Any], generated: str) -> Dict[str, Any]:
    endpoint = row["endpoint"]
    ground_truth = row["conversations"][2]["value"]
    generated_sections = extract_sections(generated)
    ground_truth_sections = extract_sections(ground_truth)

    section_checks = {}
    for name in SECTION_NAMES:
        valid, error = is_python_syntax_valid(generated_sections[name])
        section_checks[name] = {
            "present": generated_sections[name] is not None,
            "syntax_valid": valid,
            "syntax_error": error,
            "similarity_to_ground_truth": similarity(
                generated_sections[name] or "", ground_truth_sections[name] or ""
            ),
        }

    return {
        "index": index,
        "dataset_key": row.get("dataset_key"),
        "endpoint": endpoint,
        "all_sections_present": all(generated_sections[name] is not None for name in SECTION_NAMES),
        "all_present_sections_syntax_valid": all(section_checks[name]["syntax_valid"] for name in SECTION_NAMES),
        "route_matches_endpoint": route_matches_endpoint(generated_sections["route_code"], endpoint),
        "section_checks": section_checks,
        "input": row["conversations"][1]["value"],
        "ground_truth": ground_truth,
        "model_output": generated,
    }


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def markdown_report(rows: List[Dict[str, Any]]) -> str:
    total = len(rows)
    present = sum(1 for row in rows if row["all_sections_present"])
    syntax = sum(1 for row in rows if row["all_present_sections_syntax_valid"])
    route = sum(1 for row in rows if row["route_matches_endpoint"])
    lines = [
        "# Validation Generation Report",
        "",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Samples: {total}",
        f"All sections present: {present}/{total}",
        f"All present sections syntax-valid: {syntax}/{total}",
        f"Route matches endpoint: {route}/{total}",
        "",
        "## Per-Sample Summary",
        "",
        "| Index | Dataset | Endpoint | Sections | Syntax | Route | Route Similarity |",
        "|---:|---|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        route_similarity = row["section_checks"]["route_code"]["similarity_to_ground_truth"]
        lines.append(
            f"| {row['index']} | {row['dataset_key']} | `{row['endpoint']}` | "
            f"{row['all_sections_present']} | {row['all_present_sections_syntax_valid']} | "
            f"{row['route_matches_endpoint']} | {route_similarity} |"
        )

    for row in rows:
        lines.extend(
            [
                "",
                f"## Sample {row['index']} - {row['endpoint']}",
                "",
                "### Checks",
                "",
                "```json",
                json.dumps(
                    {
                        "all_sections_present": row["all_sections_present"],
                        "all_present_sections_syntax_valid": row["all_present_sections_syntax_valid"],
                        "route_matches_endpoint": row["route_matches_endpoint"],
                        "section_checks": row["section_checks"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
                "",
                "### Input",
                "",
                "```text",
                row["input"],
                "```",
                "",
                "### Ground Truth",
                "",
                row["ground_truth"],
                "",
                "### Model Output",
                "",
                row["model_output"],
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    adapter = Path(args.adapter)
    output_dir = Path(args.output_dir) if args.output_dir else adapter / "evaluation"
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = read_jsonl(Path(args.dataset))
    selected = rows[args.start_index : args.start_index + args.num_samples]
    if not selected:
        raise SystemExit("No validation rows selected")

    print("Loading tokenizer and model...")
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    base = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        torch_dtype=torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )
    model = PeftModel.from_pretrained(base, args.adapter)
    model.eval()

    results = []
    for offset, row in enumerate(selected):
        index = args.start_index + offset
        print(f"Generating validation sample {index}: {row['endpoint']}")
        prompt = build_prompt(row, tokenizer)
        generated = generate(model, tokenizer, prompt, args.max_new_tokens, args.temperature)
        results.append(evaluate_row(index, row, generated))

    jsonl_path = output_dir / f"validation_generation_start{args.start_index}_n{len(results)}.jsonl"
    md_path = output_dir / f"validation_generation_start{args.start_index}_n{len(results)}.md"
    write_jsonl(jsonl_path, results)
    md_path.write_text(markdown_report(results), encoding="utf-8")

    print(f"Wrote JSONL: {jsonl_path}")
    print(f"Wrote Markdown: {md_path}")


if __name__ == "__main__":
    main()
