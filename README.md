# CONTRACT2FASTAPI replication package

Anonymous supplementary artifact for double-anonymous review at LLM4Code 2027.

## Contents

- `backends/` contains 30 runnable synthetic FastAPI backend variants across ten domains, their specifications, OpenAPI snapshots, endpoint-test metadata, and black-box tests.
- `datasets/` contains the endpoint-level releases v1, v2, v3, v3b, and v3c. Each release contains the canonical paired `inputs.json` / `outputs.json` corpus and immediately usable `llamafactory/train.jsonl` and `llamafactory/validation.jsonl` splits.
- `scripts/collection/collection_evaluator.py` runs the collection quality gate: OpenAPI extraction, Schemathesis, optional OWASP ZAP, pytest, and dataset export.
- `scripts/` contains collection validation, dataset derivation, fine-tuning setup, structural evaluation, and host-aware runtime evaluation.
- `results/` is partitioned by dataset version. Each version contains structural candidates; v3c additionally contains the 30 candidates evaluated by the host-aware runtime evaluator. Model checkpoints are deliberately not distributed.

## Quick reproduction

Python 3.11 or newer is required. Create an isolated environment, install `requirements/collection.txt`, and run the host-aware evaluation over the supplied v3c candidates:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements\collection.txt
python scripts\finetuning\evaluate_integrated_samples.py `
  --results results\v3c\runtime\validation_generation_start0_n10.jsonl `
  --results results\v3c\runtime\validation_generation_start10_n10.jsonl `
  --results results\v3c\runtime\validation_generation_start20_n10.jsonl `
  --manifest datasets\v3c\manifest.json `
  --report results\v3c\runtime\reproduced_v3c_runtime_report.json `
  --repeats 2 --timeout 30 --candidate-timeout 90 --skip-mutations
```

The expected summary is 30 candidates, 28 patch-and-compile, 26 startup/OpenAPI-compatible, 10 endpoint-test and full-regression passes, and zero worker timeouts.

See `REPRODUCE.md` for dataset derivation, collection-gate invocation, and optional fine-tuning instructions.
