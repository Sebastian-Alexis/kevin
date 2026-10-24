"""Score (or label with) a grounder served by vLLM: hit rate = point inside the true element box, plus latency.

  python training/eval_point.py --data screenspot --model teacher                  # accuracy
  python training/eval_point.py --data screenspot --model base -c 1 --limit 150     # single-stream latency
  python training/eval_point.py --data pool --model teacher -c 48 --out data/grounding/teacher_pool.jsonl

--data is a file under data/grounding/ (pool, ground_val, screenspot). Latency percentiles only mean anything at
-c 1 with nothing else on the GPU. Rows written with --out keep the model's point and hit flag.
"""
import argparse
import base64
import json
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "datagen"))
sys.path.insert(0, str(ROOT))
from common import DATA  # noqa: E402
from shared import ground  # noqa: E402


def ask(a, r):
    b64 = base64.b64encode((DATA / r["image"]).read_bytes()).decode()
    body = {"model": a.model, "max_tokens": 32, "temperature": 0,
            "response_format": {"type": "json_schema", "json_schema": {"name": "point", "schema": ground.SCHEMA}},
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": ground.PROMPT.format(target=r["target"])}]}]}
    req = urllib.request.Request(f"{a.url}/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            out = json.loads(resp.read())
        text, toks = out["choices"][0]["message"]["content"], out["usage"]
    except Exception as e:   # one failed request is a miss, not the end of the run
        text, toks = f"ERROR {e}", {}
    ms = (time.monotonic() - t0) * 1000
    pt = ground.parse(text)
    return {**r, "reply": text, "pred": pt, "hit": ground.inside(pt, r["bbox"]), "ms": ms,
            "prompt_tokens": toks.get("prompt_tokens"), "completion_tokens": toks.get("completion_tokens")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="screenspot")
    ap.add_argument("--split", help="only rows with this split (pool has train/val)")
    ap.add_argument("--url", default="http://127.0.0.1:18000/v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("-c", "--concurrency", type=int, default=16)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out")
    a = ap.parse_args()
    rows = [json.loads(line) for line in open(DATA / "grounding" / f"{a.data}.jsonl")]
    if a.split:
        rows = [r for r in rows if r.get("split") == a.split]
    rows = rows[:a.limit] if a.limit else rows
    if a.concurrency == 1 and rows:
        ask(a, rows[0])   # warm-up, not counted
    t0 = time.monotonic()
    with ThreadPoolExecutor(a.concurrency) as ex:
        res = list(ex.map(lambda r: ask(a, r), rows))
    wall = time.monotonic() - t0
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        with open(a.out, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in res)
    groups = defaultdict(list)
    for r in res:
        groups[r["group"]].append(r["hit"])
        groups["ALL"].append(r["hit"])
    print(f"model={a.model} data={a.data} n={len(res)} c={a.concurrency} wall={wall:.0f}s")
    print(f"  valid replies    {sum(r['pred'] is not None for r in res) / len(res):.1%}")
    for g in sorted(groups, key=lambda g: (g != "ALL", g)):
        print(f"  hit {g:28s} {sum(groups[g]) / len(groups[g]):6.1%}  (n={len(groups[g])})")
    lat = sorted(r["ms"] for r in res)
    print(f"  latency ms  p50 {statistics.median(lat):.0f}  p90 {lat[int(len(lat) * 0.9) - 1]:.0f}"
          f"  (prompt tok p50 {statistics.median([r['prompt_tokens'] or 0 for r in res]):.0f},"
          f" completion p50 {statistics.median([r['completion_tokens'] or 0 for r in res]):.0f})")


if __name__ == "__main__":
    main()
