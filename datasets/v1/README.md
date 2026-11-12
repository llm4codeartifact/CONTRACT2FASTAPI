# Per-Backend Input and Output Datasets

Each directory represents one backend variant identified by `dataset_key`.

```text
per_backend_datasets/
  <dataset_key>/
    inputs.json
    outputs.json
  manifest.json
```

`inputs.json` and `outputs.json` are parallel JSON arrays. The input at index `i` belongs to the output at index `i`; records are sorted by endpoint inside each backend directory.

## Input Contract

- `dataset_key`
- `endpoint`
- `schemas_pseudocode`
- `controller_pseudocode`
- `orm_model_pseudocode`
- `formal_requirement`
- `context_requirements`
- `pseudocode`

The three `*_pseudocode` fields are deterministic structured summaries generated from the corresponding Python code blocks. `context_requirements` is a short backend overview: domain, variant, application purpose, and shared request-handling conventions. It intentionally does not repeat the full domain invariant catalogue for every API.

## Output Contract

- `route_code`
- `schemas_code`
- `controller_code`
- `orm_model_code`

## Generate

Run from the repository root:

```powershell
python finetuning/scripts/prepare_per_backend_datasets.py
```

The script reads `secure_code_dataset.json` and the backend specification files but does not modify them. It refuses to overwrite generated files unless `--overwrite` is passed.
