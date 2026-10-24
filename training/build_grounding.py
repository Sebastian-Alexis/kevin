"""Grounder data: shards -> candidate pool -> teacher-checked LLaMA-Factory files.

  python training/build_grounding.py pool                       # data/grounding/pool.jsonl (+ split per frame)
  python training/eval_point.py --data pool --model teacher --out data/grounding/teacher_pool.jsonl
  python training/build_grounding.py export --teacher data/grounding/teacher_pool.jsonl

Every kept sample has a ground-truth element box (OS-Atlas / DOM / AX). The teacher (Qwen3.8-27B) is the filter:
a sample stays only if the teacher's point lands inside that box, which drops ambiguous and mislabelled targets
(duplicate texts, slider values, tooltips) while the label itself stays the exact box center.
"""
import argparse
import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "datagen"))
sys.path.insert(0, str(ROOT))
from common import DATA, iter_shards  # noqa: E402
from shared import ground  # noqa: E402

OUT = DATA / "grounding"
SOURCES = ("osatlas", "web", "ax")


def val_frame(key, frac):
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < frac


def pool(a):
    rows, drop = [], Counter()
    for src in SOURCES:
        for shard, meta, steps in iter_shards(src):
            for s in steps:
                act = s.get("action") or {}
                if act.get("action") != "click" or not s.get("bbox"):
                    drop["not a boxed click"] += 1
                    continue
                if ground.junk(s["intent"]):
                    drop["junk target"] += 1
                    continue
                img = (shard / s["frame"]).relative_to(DATA).as_posix()
                rows.append({"id": s["id"], "image": img, "target": ground.target(s["intent"]), "bbox": s["bbox"],
                             "point": [act["x"], act["y"]], "source": src, "group": f"{src}/{s.get('os', meta.get('os', '?'))}",
                             "split": "val" if val_frame(img, a.val_frac) else "train"})
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "pool.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    print(f"{len(rows)} candidates -> {OUT / 'pool.jsonl'}", dict(Counter((r['group'], r['split']) for r in rows)))
    print("dropped:", dict(drop))


def sharegpt(r):
    cx, cy = (r["bbox"][0] + r["bbox"][2]) / 2, (r["bbox"][1] + r["bbox"][3]) / 2
    return {"messages": [{"role": "user", "content": "<image>" + ground.PROMPT.format(target=r["target"])},
                         {"role": "assistant", "content": ground.reply(round(cx), round(cy))}],
            "images": [r["image"]]}


def export(a):
    teacher = {r["id"]: r for r in map(json.loads, open(a.teacher))}
    rows = [json.loads(line) for line in open(OUT / "pool.jsonl")]
    keep, stats = {"train": [], "val": []}, Counter()
    for r in rows:
        t = teacher.get(r["id"])
        if ground.junk(r["target"]):   # filters added after the pool was built
            stats["junk target"] += 1
            continue
        if t is None:
            stats["no teacher answer"] += 1
            continue
        if not t["hit"]:
            stats[f"teacher missed ({r['source']})"] += 1
            continue
        keep[r["split"]].append(r)
        stats[f"kept ({r['source']})"] += 1
    random.Random(0).shuffle(keep["train"])
    files = {"ground_train": keep["train"], "ground_val": keep["val"]}
    exp = DATA / "export"
    for name, rs in files.items():
        (exp / f"{name}.json").write_text(json.dumps([sharegpt(r) for r in rs]))
        with open(OUT / f"{name}.jsonl", "w") as f:   # same rows with boxes, for eval_point.py --data
            f.writelines(json.dumps(r) + "\n" for r in rs)
    info_path = exp / "dataset_info.json"
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    for name in files:
        info[name] = {"file_name": f"{name}.json", "formatting": "sharegpt",
                      "columns": {"messages": "messages", "images": "images"},
                      "tags": {"role_tag": "role", "content_tag": "content", "user_tag": "user", "assistant_tag": "assistant"}}
    info_path.write_text(json.dumps(info, indent=2))
    print(dict(stats), {k: len(v) for k, v in files.items()})


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pool")
    p.add_argument("--val-frac", type=float, default=0.08)
    e = sub.add_parser("export")
    e.add_argument("--teacher", required=True)
    a = ap.parse_args()
    {"pool": pool, "export": export}[a.cmd](a)


if __name__ == "__main__":
    main()
