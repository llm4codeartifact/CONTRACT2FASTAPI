#!/bin/bash
set -euo pipefail

python3 -m venv .venv-finetuning
source .venv-finetuning/bin/activate

python -m pip install --upgrade pip wheel setuptools

# CUDA-enabled PyTorch. Adjust cu121/cu124 if the cluster module requires a different CUDA stack.
python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

python -m pip install -r requirements/finetuning.txt

python - <<'PY'
import torch
print('torch:', torch.__version__)
print('cuda available:', torch.cuda.is_available())
if torch.cuda.is_available():
    print('gpu:', torch.cuda.get_device_name(0))
PY
