# Reproduction guide

## Dataset releases

`datasets/v1` is the initial endpoint-level release. The later releases are derived in order:

```powershell
python scripts\dataset\build_per_backend_dataset_v2.py --source-dir datasets\v1 --output-dir generated\v2
python scripts\dataset\build_per_backend_dataset_v3.py --source-dir generated\v2 --output-dir generated\v3
python scripts\dataset\build_per_backend_dataset_v3b.py --source-dir generated\v2 --output-dir generated\v3b
python scripts\dataset\build_per_backend_dataset_v3c.py --source-dir generated\v3 --output-dir generated\v3c
```

The checked-in `datasets/v2`, `datasets/v3`, `datasets/v3b`, and `datasets/v3c` are the evaluated releases. To build the LLaMA-Factory files for v3c:

```powershell
python scripts\dataset\build_llamafactory_dataset_v3c.py --source-dir datasets\v3c --output-dir generated\llamafactory-v3c
```

This yields the backend-level split used in the experiment: 425 training and 113 held-out records.

## Collection quality gate

The collection evaluator starts a target FastAPI backend, records its OpenAPI document, runs Schemathesis, optionally invokes OWASP ZAP when `--zap-path` is supplied, and then runs the black-box pytest suite on a fresh server. For example:

```powershell
python collection_evaluator.py `
  --target backends.Fleet.temporal_conflict.temporal_conflicts_trip_driver_vehicle:app `
  --tests backends\Fleet\temporal_conflict\tests `
  --specification backends\Fleet\temporal_conflict\temporal_conflicts_trip_driver_vehicle.specification.json `
  --test-metadata backends\Fleet\temporal_conflict\temporal_conflicts_trip_driver_vehicle.test-metadata.json `
  --dataset-key fleet-temporal-conflict `
  --dataset generated\collection-check.json
```

Do not invoke all tests together with a plain `pytest backends`: some black-box suites require the evaluator-provided `API_BASE_URL` for the fresh server process.

## Fine-tuning and structural evaluation

The model is `Qwen/Qwen2.5-Coder-7B-Instruct`; the supplied YAML files record the QLoRA configuration. On a CUDA-capable Linux host, install the optional fine-tuning environment with `bash scripts/finetuning/bootstrap_env.sh`, build the required ShareGPT dataset, and run:

```bash
llamafactory-cli train configs/qwen25_coder_7b_a40_3epoch_qlora_v3c.yaml
```

Then call `scripts/finetuning/evaluate_validation_samples.py` with the adapter, input dataset, and output directory. The raw generated candidates needed for the supplied runtime result are distributed in `results/runtime/`; no model adapter or optimizer state is required to rerun the host-aware evaluation.

## Scope and external tools

All backends and records are synthetic. OWASP ZAP is optional evidence: alerts are recorded but do not determine dataset inclusion. Schemathesis, pytest, FastAPI, Pydantic, SQLAlchemy, and Uvicorn are installed through `requirements/collection.txt`.
