#!/usr/bin/env bash
# One-time (idempotent) setup on a Vultr Cloud GPU box (Ubuntu + NVIDIA image). Run from the synced repo root:
#   bash training/vultr_setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

nvidia-smi || { echo "no GPU visible: wrong image/plan?"; exit 1; }

if ! command -v uv >/dev/null; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi
export PATH="$HOME/.local/bin:$PATH"

[ -d .venv-train ] || uv venv --python 3.11 .venv-train
source .venv-train/bin/activate

if [ ! -d LLaMA-Factory ]; then git clone --depth 1 https://github.com/hiyouga/LLaMA-Factory.git; fi
uv pip install -e "LLaMA-Factory[torch,metrics]"
uv pip install vllm "huggingface_hub[cli]" pillow remotezip playwright
# Separate vLLM venv if the two fight over torch versions: uv venv .venv-serve && uv pip install vllm

huggingface-cli download mPLUG/GUI-Owl-1.5-4B-Instruct
huggingface-cli download Qwen/Qwen3-VL-4B-Instruct   # zero-shot baseline

cat <<'EOF'

ready. next (see training/RUNBOOK.md):
  source .venv-train/bin/activate
  training/serve.sh base &                                   # zero-shot eval first
  python training/eval_grounding.py --split val --model base
  llamafactory-cli train training/lora_sft.yaml              # train (use tmux!)
EOF
