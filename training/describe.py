"""Teacher writes planner-voice targets for ground-truth boxes (icons by look/function, not just their text).

  python training/describe.py --data pool --out data/grounding/described.jsonl
  python training/eval_point.py --data described --model teacher -c 64 --out data/grounding/teacher_described.jsonl

The teacher sees the screenshot with the element outlined in red and describes it; the description is then
re-grounded on the clean screenshot (eval_point.py) and kept only if that lands inside the true box, so every
kept label is still the dataset's box, and every kept description provably picks that element out.
"""
import argparse
import base64
import io
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "datagen"))
from common import DATA  # noqa: E402

PROMPT = """The red box marks one element of this screenshot. Write the short, unique description you would give someone so they click exactly this element, the way a computer-use agent names a click target. Examples: "the Wi-Fi icon in the menu bar", "the Save button in the dialog", "the search field at the top of the Finder window", "the gear icon that opens settings", "the close button of the left window".
If it has visible text, use the text. If it is an icon, say what it shows or does. Add its location only when needed to make it unique. Never mention the red box. At most 12 words."""
SCHEMA = {"type": "object", "properties": {"target": {"type": "string", "maxLength": 100}},
          "required": ["target"], "additionalProperties": False}


def boxed(path, bbox):
    img = Image.open(DATA / path).convert("RGB")
    w, h = img.size
    l, t, r, b = (bbox[0] * w / 1000, bbox[1] * h / 1000, bbox[2] * w / 1000, bbox[3] * h / 1000)
    pad = 3
    ImageDraw.Draw(img).rectangle([l - pad, t - pad, r + pad, b + pad], outline=(255, 0, 0), width=3)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


def describe(a, r):
    body = {"model": a.model, "max_tokens": 48, "temperature": 0,
            "response_format": {"type": "json_schema", "json_schema": {"name": "target", "schema": SCHEMA}},
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + boxed(r["image"], r["bbox"])}},
                {"type": "text", "text": PROMPT}]}]}
    req = urllib.request.Request(f"{a.url}/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            text = json.loads(resp.read())["choices"][0]["message"]["content"]
        t = json.loads(text)["target"].strip().rstrip(".")
    except Exception as e:
        print(f"  ! {r['id']}: {type(e).__name__}", flush=True)
        return None
    if not t or re.search(r"red (box|rectangle|outline)|highlighted|<[^>]*>", t, re.I):
        return None
    return {**r, "id": r["id"] + "#d", "target": t, "style": "described", "orig_target": r["target"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="pool")
    ap.add_argument("--url", default="http://127.0.0.1:18000/v1")
    ap.add_argument("--model", default="teacher")
    ap.add_argument("-c", "--concurrency", type=int, default=64)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = [json.loads(line) for line in open(DATA / "grounding" / f"{a.data}.jsonl")]
    rows = rows[:a.limit] if a.limit else rows
    with ThreadPoolExecutor(a.concurrency) as ex, open(a.out, "w") as f:
        n = 0
        for i, d in enumerate(ex.map(lambda r: describe(a, r), rows)):
            if d:
                f.write(json.dumps(d) + "\n")
                n += 1
            if (i + 1) % 1000 == 0:
                print(f"  {i + 1}/{len(rows)} described, {n} kept", flush=True)
    print(f"{n}/{len(rows)} descriptions -> {a.out}")


if __name__ == "__main__":
    main()
