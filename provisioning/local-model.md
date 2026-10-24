# Local VLM: Qwen3.8-27B on the GB10 controller

The agent's grounding/computer-use model is a local vLLM on the controller (NVIDIA DGX Spark, GB10 / sm121,
121 GB unified, ~273 GB/s), reachable at `http://127.0.0.1:8000/v1` as model **`qwen27`**, bearer key =
`/etc/gemma4-vllm-api-key` (root-only; `agent/vlm.py` reads it via `sudo -n cat`, the daemon via a systemd
credential). It replaced `qwen122` (Qwen3.5-122B-A10B INT4 + DFlash) on 2026-10-24.

## What

- Checkpoint: [`nvidia/Qwen3.8-27B-NVFP4`](https://huggingface.co/nvidia/Qwen3.8-27B-NVFP4) (NVIDIA ModelOpt
  build of [`Qwen/Qwen3.8-27B`](https://huggingface.co/Qwen/Qwen3.8-27B): NVFP4 MLPs + lm_head, FP8 attention
  projections, vision tower kept). Qwen3.8-27B is natively multimodal (`Qwen3_5ForConditionalGeneration`,
  image + video input; OSWorld-Verified 84.3), dense, 64 layers (48 Gated DeltaNet + 16 full attention), with
  an in-checkpoint MTP head that vLLM uses as a speculative drafter.
- The official [`Qwen/Qwen3.8-27B-FP8`](https://huggingface.co/Qwen/Qwen3.8-27B-FP8) is also in the cache and
  works with the same scripts (`MODEL=Qwen/Qwen3.8-27B-FP8`); it is ~1.7x slower here (see below), because the
  GB10 is memory-bandwidth bound and FP8 weights are 29 GB vs 15 GB.
- Engine: vLLM `0.23.0+aeon.sm121a` (`ghcr.io/aeon-7/aeon-vllm-ultimate@sha256:be9e05a1…`, the same sm121 image
  as the qwen122 recipe in `/opt/qwen122`). NVFP4 GEMMs run on `FlashInferCutlassNvFp4LinearKernel`.
- Weights live in the shared HF cache `/opt/qwen122/hf` (`HF_HOME=/hf` in the container, `HF_HUB_OFFLINE=1`).

## Why this model

Agent S4 advertises Qwen3.8-27B; it is the strongest ~30B-class open VLM for computer use, and it fits the
GB10 with room to spare. Requirements for the loop: image input, `response_format: json_schema` (vLLM
structured outputs), `chat_template_kwargs: {"enable_thinking": false}` (the model thinks by default), low
single-stream latency.

## Service

`qwen27-vllm.service` (systemd, `Restart=on-failure`, `Conflicts=qwen122-vllm.service`): runs
`/opt/qwen27/run-vllm.sh`, which starts the container `qwen27-vllm` (`--gpus all --network host --ipc host`,
API key mounted as `/run/secrets/vllm-api-key` and exported by `/opt/qwen27/container-entrypoint.sh`, never
on disk elsewhere) and `/opt/qwen27/serve.sh` inside it. Settings in `/etc/qwen27-vllm.env`:

    MODEL=nvidia/Qwen3.8-27B-NVFP4   SERVED_NAME=qwen27   PORT=8000
    MAX_MODEL_LEN=16384   MAX_NUM_SEQS=4   MAX_BATCHED_TOKENS=8192
    GPU_MEM=0.70          # ~84 GiB budget of 119; leaves >30 GiB for everything else
    KV_DTYPE=auto         # fp8 KV needs FLASHINFER; KV is a non-issue at this context
    BACKEND=flash_attn    SPEC_TOKENS=6   LOAD_FORMAT=fastsafetensors   EXTRA_ARGS=

`serve.sh` applies the sm121 FLA big-tile patch (GDN prefill) and runs:

    vllm serve $MODEL --served-model-name qwen27 --host 127.0.0.1 --port 8000 \
      --max-model-len 16384 --max-num-seqs 4 --max-num-batched-tokens 8192 \
      --gpu-memory-utilization 0.70 --kv-cache-dtype auto \
      --enable-prefix-caching --enable-chunked-prefill --trust-remote-code \
      --load-format fastsafetensors --attention-backend flash_attn \
      --limit-mm-per-prompt '{"image":4,"video":0}' \
      --enable-auto-tool-choice --tool-call-parser qwen3_xml --reasoning-parser qwen3 \
      --speculative-config '{"method":"qwen3_5_mtp","num_speculative_tokens":6,"model":"'$MODEL'"}'

Operate:

    sudo systemctl status qwen27-vllm            # cold start ≈ 4-5 min (compile cache in /opt/qwen122/hf/.vllm_cache_qwen27)
    sudo systemctl restart qwen27-vllm           # after editing /etc/qwen27-vllm.env
    docker logs -f qwen27-vllm
    curl -H "Authorization: Bearer $(sudo cat /etc/gemma4-vllm-api-key)" http://127.0.0.1:8000/v1/models
    sudo systemctl disable --now qwen27-vllm     # stop (also when bringing qwen122 back: the units conflict)
    sudo systemctl enable --now qwen122-vllm     # the old 122B stack, recipe untouched in /opt/qwen122

Then `sudo systemctl restart ai-kvm-agent@jpa` and check `journalctl -u ai-kvm-agent@jpa` for
`first contact: {"ok": true`. The agent code defaults to `qwen27` (`AIKVM_LLM_MODEL` overrides).

## Measured (2026-10-24)

Benchmark = the real request shape: one 1107x720 JPEG screenshot (NYT Connections, `/tmp/s3-conn/step05.jpg`)
+ ~440 tokens of text (1245 prompt tokens), temperature 0, thinking off, json_schema-constrained reply, one
request at a time, 5 timed calls after a warm-up. Ground truth for the COMIC tile is (333, 622) on the
0-1000 grid.

| config                                   | p50     | out tok | point_2d   |
| ---------------------------------------- | ------- | ------- | ---------- |
| qwen122 (previous, DFlash n=8)           | 2.42 s  | 72      | (327, 615) |
| Qwen3.8-27B-FP8, no spec                 | 8.55 s  | 67      | (331, 620) |
| Qwen3.8-27B-FP8, MTP n=3                 | 4.43 s  | 63      | (335, 622) |
| Qwen3.8-27B-FP8, MTP n=6                 | 4.30 s  | 63      | (331, 622) |
| **Qwen3.8-27B-NVFP4, MTP n=6** (shipped) | 2.54 s  | 71      | (333, 622) |
| Qwen3.8-27B-NVFP4, MTP n=6 + `--async-scheduling` | 2.68 s | 67 | (335, 622) |

Shipped config breakdown: TTFT ≈ 0.7 s (prefill ≈ 1000 tok/s + vision encoder), decode ≈ 39 tok/s (MTP
acceptance ≈ 72 %, ≈ 5.2 tokens per step at ≈ 130 ms/step); plain decode without MTP is ≈ 8 tok/s for FP8,
so the drafter is what makes this usable. The 1.5 s target is not reachable with a 27B dense model on this
memory bandwidth: a 70-token reply needs ≥ 13 verify steps, each a full weight read (15 GB / 273 GB/s ≈ 55 ms
floor before overheads), plus ~0.7 s prefill. Levers that would get closer: fewer output tokens (shorter
`observe`), smaller screenshots (fewer image tokens), or a smaller model.

Notes:
- Prefix caching is on but reports 0 hits: the hybrid GDN layout forces a 816-token attention block
  ("align" mamba cache mode), so the ~440-token system prefix never fills a block. Harmless.
- `--async-scheduling` (EXTRA_ARGS) was slower per token (40 vs 36 ms) with structured outputs + MTP; not kept.
- The KV pool is ~400k tokens at this setting; concurrency 4 is plenty for one KVM.
