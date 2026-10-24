#!/usr/bin/env bash
# Serve the actor with vLLM (OpenAI-compatible) on 127.0.0.1:8000. Reach it via: ssh -L 8000:localhost:8000 <box>
#   training/serve.sh base       zero-shot GUI-Owl-1.5-4B
#   training/serve.sh alt        zero-shot Qwen3-VL-4B baseline
#   training/serve.sh lora       base + our LoRA as model "actor" (base also served, for side-by-side)
#   training/serve.sh merged     merged weights
set -euo pipefail
BASE=${BASE:-mPLUG/GUI-Owl-1.5-4B-Instruct}
BASE_ALT=${BASE_ALT:-Qwen/Qwen3-VL-4B-Instruct}
LORA=${LORA:-training/saves/gui-owl-4b-lora}
MERGED=${MERGED:-training/saves/gui-owl-4b-merged}
PORT=${PORT:-8000}
MAX_PIXELS=1003520   # must match lora_sft.yaml image_max_pixels

common=(--host 127.0.0.1 --port "$PORT" --max-model-len 8192 --trust-remote-code
        --limit-mm-per-prompt '{"image":1}' --mm-processor-kwargs "{\"max_pixels\":$MAX_PIXELS}"
        --gpu-memory-utilization 0.85)
[ -n "${VLLM_API_KEY:-}" ] && common+=(--api-key "$VLLM_API_KEY")

case "${1:-base}" in
  base)   exec vllm serve "$BASE" --served-model-name base "${common[@]}" ;;
  alt)    exec vllm serve "$BASE_ALT" --served-model-name base "${common[@]}" ;;
  lora)   exec vllm serve "$BASE" --served-model-name base --enable-lora --max-lora-rank 16 \
            --lora-modules "actor=$LORA" "${common[@]}" ;;
  merged) exec vllm serve "$MERGED" --served-model-name actor "${common[@]}" ;;
  *) echo "usage: $0 base|alt|lora|merged" >&2; exit 1 ;;
esac
