# Dataset layout

Each version directory (`v1`, `v2`, `v3`, `v3b`, `v3c`) has two forms of the same corpus:

- Backend directories contain `inputs.json` and `outputs.json`. These are the canonical endpoint-level contract/code pairs; item `i` in one file matches item `i` in the other. `manifest.json` records all 30 backend variants and their record counts.
- `llamafactory/train.jsonl` and `llamafactory/validation.jsonl` are the direct ShareGPT training splits. The split is by backend, not by endpoint, preventing records from a held-out backend from entering training.

`llamafactory/dataset_info.json` maps the stable dataset identifiers used in the corresponding configuration file to `train.jsonl` and `validation.jsonl`.
