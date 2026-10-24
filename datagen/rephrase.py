"""Source F: rewrite templated click intents into the planner's voice, and drop bad labels, using Claude.

One request per frame: the frame is sent with the targets drawn as numbered boxes, plus the current intents.
Claude returns a natural instruction per target (the kind of thing the Droid planner says) and marks targets
that are ambiguous or whose label doesn't match what's in the box. Those get "drop": true and
build_dataset.py skips them, so this doubles as label QA for the generated sources.

Rewrites happen in place (steps.jsonl.bak is kept). The original text stays in "intent_orig".

  export ANTHROPIC_API_KEY=...
  python datagen/rephrase.py --source osatlas --limit-frames 50     # try a few, then preview
  python datagen/rephrase.py --source ax,web,osatlas --rate 0.6     # rewrite ~60%, keep some template text
"""
import argparse
import base64
import io
import json
import random
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import iter_shards  # noqa: E402

SYSTEM = """You write training instructions for a small GUI-grounding model.
A planner agent sends that model short imperative instructions like:
  "Click the Downloads folder in the Finder sidebar"
  "Click the blue Sign in button at the top right"
  "Open the File menu"
  "Click the search field in the toolbar"
For each numbered red box on the screenshot, write ONE instruction a planner would give to click exactly that element.
Rules:
- 4 to 16 words, imperative, no coordinates, no box numbers.
- Refer to the element by its visible text when it has some; otherwise describe it (icon shape, color, role) and where it is.
- Add location or context (sidebar, toolbar, dialog, which card or row) only when needed to make it unambiguous.
- Use the current instruction as a hint about what the element is, but trust the screenshot.
- Set drop=true if the box doesn't sit on a single clickable element, the hint clearly doesn't match the element,
  or there is no way to word the instruction so it identifies only this element."""

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "instruction": {"type": "string"}, "drop": {"type": "boolean"}},
        "required": ["n", "instruction", "drop"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


def annotate(frame_path, steps):
    img = Image.open(frame_path).convert("RGB")
    w, h = img.size
    d = ImageDraw.Draw(img)
    for n, s in enumerate(steps, 1):
        l, t, r, b = (v / 1000 for v in s["bbox"])
        box = [l * w - 2, t * h - 2, r * w + 2, b * h + 2]
        d.rectangle(box, outline=(255, 0, 0), width=2)
        d.rectangle([box[0], box[1] - 14, box[0] + 8 + 7 * len(str(n)), box[1]], fill=(255, 0, 0))
        d.text((box[0] + 3, box[1] - 13), str(n), fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode()


def rewrite_frame(client, model, frame_path, steps):
    listing = "\n".join(f"{n}. current instruction: {s['intent']}" for n, s in enumerate(steps, 1))
    resp = client.beta.messages.create(
        model=model,
        max_tokens=4000,
        system=SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": annotate(frame_path, steps)}},
            {"type": "text", "text": listing},
        ]}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("refused")
    text = next(b.text for b in resp.content if b.type == "text")
    return {it["n"]: it for it in json.loads(text)["items"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="ax,web,osatlas")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--rate", type=float, default=1.0, help="fraction of frames to rewrite")
    ap.add_argument("--limit-frames", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    client = anthropic.Anthropic()
    totals = {"frames": 0, "rewritten": 0, "dropped": 0, "errors": 0}

    for src in args.source.split(","):
        for shard_dir, _, steps in iter_shards(src.strip()):
            by_frame = {}
            for s in steps:
                if s["action"]["action"] == "click" and s.get("bbox") and "intent_orig" not in s:
                    by_frame.setdefault(s["frame"], []).append(s)
            frames = [f for f in by_frame if rng.random() < args.rate]
            if args.limit_frames:
                frames = frames[: max(0, args.limit_frames - totals["frames"])]
            if not frames:
                continue
            print(f"{shard_dir}: {len(frames)} frames", flush=True)
            with ThreadPoolExecutor(args.workers) as pool:
                futs = {pool.submit(rewrite_frame, client, args.model, shard_dir / f, by_frame[f]): f for f in frames}
                for fut in as_completed(futs):
                    f = futs[fut]
                    totals["frames"] += 1
                    try:
                        out = fut.result()
                    except Exception as e:
                        totals["errors"] += 1
                        print(f"  ! {f}: {type(e).__name__}: {str(e)[:120]}", flush=True)
                        continue
                    for n, s in enumerate(by_frame[f], 1):
                        it = out.get(n)
                        if not it:
                            continue
                        s["intent_orig"] = s["intent"]
                        if it["drop"]:
                            s["drop"] = True
                            totals["dropped"] += 1
                        elif it["instruction"].strip():
                            s["intent"] = it["instruction"].strip()
                            totals["rewritten"] += 1
            sf = shard_dir / "steps.jsonl"
            shutil.copy2(sf, sf.with_suffix(".jsonl.bak"))
            sf.write_text("".join(json.dumps(s) + "\n" for s in steps))
            if args.limit_frames and totals["frames"] >= args.limit_frames:
                break
    print(totals)


if __name__ == "__main__":
    main()
