# CONTRACT2FASTAPI replication package

Anonymous supplementary artifact for double-anonymous review at LLM4Code 2027.

## Contents

- `backends/` contains 30 runnable synthetic FastAPI backend variants across ten domains, their specifications, OpenAPI snapshots, endpoint-test metadata, and black-box tests.
- `datasets/` contains the endpoint-level releases v1, v2, v3, v3b, and v3c.
- `collection_evaluator.py` runs the collection quality gate: OpenAPI extraction, Schemathesis, optional OWASP ZAP, pytest, and dataset export.
- `scripts/dataset/` derives the successive dataset releases and produces LLaMA-Factory ShareGPT training data.
- `scripts/finetuning/` contains the structural and host-aware runtime evaluators.
- `results/` contains the generated candidates used for the supplied structural and runtime evaluation evidence. Model checkpoints are deliberately not distributed.

## Quick reproduction

Python 3.11 or newer is required. Create an isolated environment, install `requirements/collection.txt`, and run the host-aware evaluation over the supplied v3c candidates:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements\collection.txt
python scripts\finetuning\evaluate_integrated_samples.py `
  --results results\runtime\validation_generation_start0_n10.jsonl `
  --results results\runtime\validation_generation_start10_n10.jsonl `
  --results results\runtime\validation_generation_start20_n10.jsonl `
  --manifest datasets\v3c\manifest.json `
  --report results\runtime\reproduced_v3c_runtime_report.json `
  --repeats 2 --timeout 30 --candidate-timeout 90 --skip-mutations
```

The expected summary is 30 candidates, 28 patch-and-compile, 26 startup/OpenAPI-compatible, 10 endpoint-test and full-regression passes, and zero worker timeouts.

See `REPRODUCE.md` for dataset derivation, collection-gate invocation, and optional fine-tuning instructions.
