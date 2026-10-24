"""Score an actor served over an OpenAI-compatible API (vLLM) on an exported split.

  python training/eval_grounding.py --split val --model base           # zero-shot
  python training/eval_grounding.py --split gold --model actor         # fine-tuned, real capture path
  python training/eval_grounding.py --split gold --model base --native-prompt "Locate: {instruction}" --show 10

Metrics: click hit rate (point inside target bbox), action-type accuracy, exact match for type/key, latency.
Also reports the raw coordinate range: if x/y regularly exceed 1000 the model is emitting pixels, not 0..1000
(that's the Task 1 coordinate probe for GUI-Owl).
"""
import argparse
import base64
import json
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "datagen"))
from common import DATA  # noqa: E402
from shared import actions  # noqa: E402

PROMPT = (ROOT / "shared" / "actor_prompt.txt").read_text().strip()


def call(url, model, key, img_path, text, max_tokens=64):
    b64 = base64.b64encode(img_path.read_bytes()).decode()
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": text}]}]}
    req = urllib.request.Request(f"{url}/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())
    return out["choices"][0]["message"]["content"], (time.monotonic() - t0) * 1000


def score(rec, raw, args):
    gold = json.loads(rec["messages"][1]["content"])
    is_click = gold["action"] == "click"
    res = {"type_ok": False, "hit": False if is_click else None, "exact": None if is_click else False, "raw": raw}
    try:
        pred = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except ValueError:
        return res
    res["xy"] = (pred.get("x"), pred.get("y"))
    try:
        pred = actions.validate(pred)
    except actions.ActionError:
        return res
    res["type_ok"] = pred["action"] == gold["action"]
    if gold["action"] == "click":
        bbox = rec["meta"].get("bbox")
        if res["type_ok"]:
            res["hit"] = (actions.point_in_bbox(pred["x"], pred["y"], bbox, args.pad) if bbox else
                          abs(pred["x"] - gold["x"]) <= args.radius and abs(pred["y"] - gold["y"]) <= args.radius)
        else:
            res["hit"] = False
    else:
        res["exact"] = actions.dumps(pred) == actions.dumps(gold)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="val", help="val | holdout | gold | train")
    ap.add_argument("--model", default="base")
    ap.add_argument("--url", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--api-key", default="EMPTY")
    ap.add_argument("-n", type=int, default=300)
    ap.add_argument("--native-prompt", help="override prompt template; {instruction} is substituted")
    ap.add_argument("--pad", type=int, default=0, help="bbox padding in 0..1000 units")
    ap.add_argument("--radius", type=int, default=20, help="hit radius when no bbox")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--show", type=int, default=0, help="print N raw outputs")
    ap.add_argument("--export", default=str(DATA / "export"))
    args = ap.parse_args()

    recs = json.loads((Path(args.export) / f"hid_actor_{args.split}.json").read_text())[: args.n]
    if not recs:
        sys.exit(f"split {args.split} is empty")
    tmpl = args.native_prompt or PROMPT

    def run(rec):
        intent = rec["messages"][0]["content"].rsplit("Instruction: ", 1)[-1]
        raw, ms = call(args.url, args.model, args.api_key, DATA / rec["images"][0], tmpl.replace("{instruction}", intent))
        return rec, score(rec, raw, args), ms

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(run, recs))

    lat = [ms for _, _, ms in results]
    clicks = [r for _, r, _ in results if r["hit"] is not None]
    others = [r for _, r, _ in results if r["exact"] is not None]
    xs = [r["xy"][0] for _, r, _ in results if r.get("xy") and isinstance(r["xy"][0], (int, float))]
    ys = [r["xy"][1] for _, r, _ in results if r.get("xy") and isinstance(r["xy"][1], (int, float))]
    by_src = {}
    for rec, r, _ in results:
        if r["hit"] is not None:
            by_src.setdefault(rec["meta"]["source"], []).append(r["hit"])

    print(f"model={args.model} split={args.split} n={len(results)}")
    print(f"  click hit rate   {sum(r['hit'] for r in clicks) / max(1, len(clicks)):.1%}  (n={len(clicks)})")
    for s, hits in sorted(by_src.items()):
        print(f"    {s:12s} {sum(hits) / len(hits):.1%}  (n={len(hits)})")
    print(f"  action type acc  {sum(r['type_ok'] for _, r, _ in results) / len(results):.1%}")
    print(f"  type/key exact   {sum(r['exact'] for r in others) / max(1, len(others)):.1%}  (n={len(others)})")
    print(f"  latency ms       p50 {statistics.median(lat):.0f}  p90 {sorted(lat)[int(len(lat) * 0.9) - 1]:.0f}")
    if xs:
        print(f"  raw coord range  x {min(xs)}..{max(xs)}  y {min(ys)}..{max(ys)}"
              + ("   <-- looks like PIXELS, not 0..1000" if max(xs + ys) > 1000 else ""))
    for rec, r, _ in results[: args.show]:
        print(f"  - {rec['messages'][0]['content'].rsplit('Instruction: ', 1)[-1]!r}\n      gold {rec['messages'][1]['content']}"
              f"\n      pred {r['raw'].strip()[:160]}")


if __name__ == "__main__":
    main()
