#!/usr/bin/env bash
# Grounder servers for distillation runs on one big GPU (H200): teacher on :18000, students on :18001 (RunPod nginx owns 8000-8001).
#   training/serve_ground.sh teacher      Qwen3.8-27B-FP8 (labels + accuracy bar)
#   training/serve_ground.sh base         GUI-Owl-1.5-4B zero-shot
#   training/serve_ground.sh base8        GUI-Owl-1.5-8B zero-shot
#   training/serve_ground.sh student      merged fine-tune (training/saves/owl4b-ground-merged)
#   training/serve_ground.sh student8     merged 8B fine-tune (training/saves/owl8b-ground-merged)
set -euo pipefail
cd "$(dirname "$0")/.."
export HF_HOME=${HF_HOME:-/workspace/hf} PATH="$PWD/.venv-serve/bin:$PATH"
MAX_PIXELS=1003520   # = lora_ground.yaml image_max_pixels
mm=(--limit-mm-per-prompt "{\"image\":1,\"video\":0}" --mm-processor-kwargs "{\"size\":{\"longest_edge\":$MAX_PIXELS,\"shortest_edge\":65536}}")
case "${1:?teacher|base|student}" in
  teacher) VLLM_USE_DEEP_GEMM=0 exec vllm serve Qwen/Qwen3.8-27B-FP8 --served-model-name teacher --port 18000 --max-model-len 16384 \
             --max-num-seqs 64 --gpu-memory-utilization 0.55 --reasoning-parser qwen3 "${mm[@]}" ;;
  base)    exec vllm serve mPLUG/GUI-Owl-1.5-4B-Instruct --served-model-name base --port 18001 --max-model-len 8192 \
             --gpu-memory-utilization 0.25 --trust-remote-code "${mm[@]}" ;;
  base8)   exec vllm serve mPLUG/GUI-Owl-1.5-8B-Instruct --served-model-name base8 --port 18001 --max-model-len 8192 \
             --gpu-memory-utilization 0.3 --trust-remote-code "${mm[@]}" ;;
  student) exec vllm serve training/saves/owl4b-ground-merged --served-model-name student --port 18001 --max-model-len 8192 \
             --gpu-memory-utilization 0.25 --trust-remote-code "${mm[@]}" ;;
  student8) exec vllm serve training/saves/owl8b-ground-merged --served-model-name student8 --port 18001 --max-model-len 8192 \
             --gpu-memory-utilization 0.3 --trust-remote-code "${mm[@]}" ;;
esac
