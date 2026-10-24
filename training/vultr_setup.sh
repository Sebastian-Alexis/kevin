#!/usr/bin/env bash
# One-time (idempotent) setup on a Vultr Cloud GPU box (Ubuntu + NVIDIA image). Run from the synced repo root:
#   bash training/vultr_setup.sh          # serving (vLLM) + training (LLaMA-Factory)
#   bash training/vultr_setup.sh serve    # serving only, fastest path to a zero-shot eval
# vLLM and LLaMA-Factory pin different torch/transformers versions, so they get separate venvs.
set -euo pipefail
cd "$(dirname "$0")/.."
WHAT=${1:-all}

nvidia-smi || { echo "no GPU visible: wrong image/plan?"; exit 1; }
command -v tmux >/dev/null || { apt-get update -qq && apt-get install -y -qq tmux rsync; }
if ! command -v uv >/dev/null; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi
export PATH="$HOME/.local/bin:$PATH"

# --- serving ---
[ -d .venv-serve ] || uv venv --python 3.12 .venv-serve
uv pip install -p .venv-serve -q vllm "huggingface_hub[cli]" pillow
.venv-serve/bin/hf download mPLUG/GUI-Owl-1.5-4B-Instruct
.venv-serve/bin/hf download Qwen/Qwen3-VL-4B-Instruct   # zero-shot baseline

# --- training ---
if [ "$WHAT" != serve ]; then
  [ -d LLaMA-Factory ] || git clone --depth 1 https://github.com/hiyouga/LLaMA-Factory.git
  [ -d .venv-train ] || uv venv --python 3.11 .venv-train
  uv pip install -p .venv-train -q -e "LLaMA-Factory[torch,metrics]" pillow
fi

cat <<'EOF'

ready. next (training/RUNBOOK.md):
  tmux new -s serve 'training/serve.sh base'                       # zero-shot GUI-Owl on 127.0.0.1:8000
  .venv-serve/bin/python training/eval_grounding.py --split val --model base --show 5
  tmux new -s train '.venv-train/bin/llamafactory-cli train training/lora_sft.yaml'
EOF
