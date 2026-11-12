# Script inventory

## `collection/`

- `collection_evaluator.py`: quality gate for corpus construction. It starts one backend, captures the runtime OpenAPI document, runs Schemathesis fuzzing, optionally executes OWASP ZAP, runs pytest on a fresh server, and exports only the accepted endpoint records.

## `dataset/`

- `prepare_per_backend_datasets.py`: builds the initial paired endpoint corpus from validated backend material.
- `build_per_backend_dataset_v2.py`, `build_per_backend_dataset_v3.py`, `build_per_backend_dataset_v3b.py`, and `build_per_backend_dataset_v3c.py`: derive the successive contract formats from the preceding release.
- `build_llamafactory_dataset*.py`: convert each paired corpus into the checked-in ShareGPT `train.jsonl` and `validation.jsonl` splits.

## `finetuning/`

- `bootstrap_env.sh`: installs the optional CUDA-capable fine-tuning environment.
- `evaluate_validation_samples.py`: generates candidates from a base model plus adapter and records structural checks (section presence, syntax, route match, and section similarity).
- `evaluate_integrated_samples.py`: evaluates already-generated candidates by replacing route/controller fragments in fresh temporary backend copies, then checks compilation, startup, OpenAPI, endpoint tests, whole-backend regression, repeats, and timeouts.
