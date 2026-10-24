# Training runbook

Target: fresh Vultr GPU box to training-started in under 20 min. L40S 48GB is enough; A100 80GB is faster.

## 0. Before you start
- Vultr: GPU quota approved, MLH credits confirmed to apply to GPU plans.
- Locally: data generated and exported (`python training/build_dataset.py --mix ...`).

## 1. Box setup
```bash
# locally
GPU=root@<ip> training/sync.sh up
ssh root@<ip>
cd ai-kvm && bash training/vultr_setup.sh      # ~10 min, mostly downloads
```
Snapshot the instance once this finishes so the next box boots ready.

## 2. Zero-shot baselines (do this before training, it's the "before" number)
```bash
training/serve.sh base &          # GUI-Owl-1.5-4B
.venv-serve/bin/python training/eval_grounding.py --split val  --model base --show 5
.venv-serve/bin/python training/eval_grounding.py --split gold --model base           # if the gold set exists
kill %1; training/serve.sh alt &  # Qwen3-VL-4B, same evals, then kill
```
If eval prints `looks like PIXELS`, GUI-Owl isn't using 0..1000: try its native prompt with
`--native-prompt`, and tell the team before training (labels would need converting).

## 3. Train
```bash
tmux new -s train
.venv-train/bin/llamafactory-cli train training/lora_sft.yaml
```
~10k samples x 2 epochs is roughly 1-2 h on an L40S. Watch `training/saves/gui-owl-4b-lora/trainer_log.jsonl`.

## 4. Eval the fine-tune
```bash
training/serve.sh lora &          # serves "base" and "actor" side by side
for m in base actor; do for s in val holdout gold; do
  .venv-serve/bin/python training/eval_grounding.py --split $s --model $m; done; done
```

## 5. Serve for the agent
Keep vLLM on 127.0.0.1 and tunnel it; don't open the port publicly:
```bash
ssh -N -L 8000:localhost:8000 root@<ip>      # on the controller laptop
export ACTOR_URL=http://127.0.0.1:8000/v1 ACTOR_MODEL=actor
```
If you must expose it, set `VLLM_API_KEY` before `serve.sh` and firewall the port to your IP.

## 6. Shut down
`GPU=root@<ip> training/sync.sh down`, then snapshot or destroy the instance. Billing runs while it exists.
