#!/usr/bin/env bash
# Local actor on Apple Silicon (MLX), OpenAI-compatible on 127.0.0.1:8000, for evals when there's no GPU box.
#   training/serve_mlx.sh                      zero-shot GUI-Owl-1.5-4B
#   MODEL=Qwen/Qwen3-VL-4B-Instruct training/serve_mlx.sh
#   ADAPTER=path/to/mlx-adapter training/serve_mlx.sh
# Then: .venv/bin/python training/eval_grounding.py --model "$MODEL" --workers 1
set -euo pipefail
cd "$(dirname "$0")/.."
MODEL=${MODEL:-mPLUG/GUI-Owl-1.5-4B-Instruct}
PORT=${PORT:-8000}
MAX_PIXELS=1003520   # same cap as training and vLLM
args=(--model "$MODEL" --host 127.0.0.1 --port "$PORT" --max-pixels "$MAX_PIXELS" --max-tokens 128)
[ -n "${ADAPTER:-}" ] && args+=(--adapter-path "$ADAPTER")
exec .venv/bin/python -m mlx_vlm.server "${args[@]}"
