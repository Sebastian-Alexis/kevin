"""Grounder data: shards -> candidate pool -> teacher-checked LLaMA-Factory files.

  python training/build_grounding.py pool                       # data/grounding/pool.jsonl (+ split per frame)
  python training/eval_point.py --data pool --model teacher --out data/grounding/teacher_pool.jsonl
  python training/build_grounding.py export --teacher data/grounding/teacher_pool.jsonl [teacher_described.jsonl ...]

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

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "datagen"))
sys.path.insert(0, str(ROOT))
from common import DATA, iter_shards  # noqa: E402
from shared import ground  # noqa: E402

OUT = DATA / "grounding"
SOURCES = ("osatlas", "web", "ax", "groundcua")


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


def sample(a):
    """Rows for the teacher to describe: n per source, preferring rows whose text target the teacher already hit."""
    hit = {r["id"] for path in a.prefer for r in map(json.loads, open(path)) if r["hit"]} if a.prefer else set()
    rows = [json.loads(line) for line in open(OUT / "pool.jsonl")]
    random.Random(1).shuffle(rows)
    out = []
    for spec in a.per_source.split(","):
        src, n = spec.split("=")
        cand = sorted((r for r in rows if r["source"] == src), key=lambda r: r["id"] not in hit)
        out += cand[:int(n)]
    random.Random(2).shuffle(out)
    with open(OUT / f"{a.name}.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in out)
    print(f"{len(out)} rows -> {OUT / a.name}.jsonl", dict(Counter(r["source"] for r in out)))


def sharegpt(r):
    cx, cy = (r["bbox"][0] + r["bbox"][2]) / 2, (r["bbox"][1] + r["bbox"][3]) / 2
    return {"messages": [{"role": "user", "content": "<image>" + ground.PROMPT.format(target=r["target"])},
                         {"role": "assistant", "content": ground.reply(round(cx), round(cy))}],
            "images": [r["image"]]}


def export(a):
    """Keep every teacher-checked row (eval_point.py --out files carry the full row) whose point hit the true box."""
    keep, stats, seen = {"train": [], "val": []}, Counter(), set()
    for path in a.teacher:
        for r in map(json.loads, open(path)):
            kind = r.get("style", "text")
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            if kind == "text" and ground.junk(r["target"]):   # filters added after the pool was built
                stats["junk target"] += 1
            elif not r["hit"]:
                stats[f"teacher missed ({r['source']}, {kind})"] += 1
            else:
                keep[r["split"]].append(r)
                stats[f"kept ({r['source']}, {kind})"] += 1
    easy = {r["id"] for path in a.student or [] for r in map(json.loads, open(path)) if r["hit"]}
    for r in map(json.loads, open(OUT / "pool.jsonl")):   # human-verified sources go in without a teacher check...
        if a.student and r["id"] not in easy:   # ...unless the student misses them: those need the teacher's check
            continue                            # (short labels like 'file' are often ambiguous on the screen)
        if r["source"] in a.trust and r["id"] not in seen and not ground.junk(r["target"]):
            seen.add(r["id"])
            keep[r["split"]].append(r)
            stats[f"kept ({r['source']}, trusted)"] += 1
    if a.student:   # difficulty: the student's misses all stay, its hits only as a replay share (GUI-G1, InfiGUI-G1)
        rng = random.Random(3)
        before = len(keep["train"])
        keep["train"] = [r for r in keep["train"] if r["id"] not in easy or rng.random() < a.replay]
        stats["student-easy dropped"] = before - len(keep["train"])
    if a.resize_frac:   # random downscale copies (Phi-Ground: random resize helps); boxes are relative, so unchanged
        rng, rdir = random.Random(4), OUT / "resized"
        rdir.mkdir(exist_ok=True)
        extra = []
        for r in rng.sample(keep["train"], int(len(keep["train"]) * a.resize_frac)):
            dst = rdir / (hashlib.md5((r["image"] + r["id"]).encode()).hexdigest()[:16] + ".jpg")
            if not dst.exists():
                img = Image.open(DATA / r["image"])
                k = rng.uniform(0.5, 0.9)
                img.resize((max(28, round(img.width * k)), max(28, round(img.height * k))), Image.LANCZOS).save(dst, quality=88)
            extra.append({**r, "id": r["id"] + "#r", "image": dst.relative_to(DATA).as_posix()})
        keep["train"] += extra
        stats["resized copies"] = len(extra)
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
    sp = sub.add_parser("sample")
    sp.add_argument("--per-source", required=True, help="e.g. groundcua=2500,osatlas=3000,web=1500")
    sp.add_argument("--prefer", nargs="*", help="teacher files: rows it already hit come first")
    sp.add_argument("--name", default="describe_in")
    e = sub.add_parser("export")
    e.add_argument("--teacher", nargs="+", required=True, help="eval_point.py --out files from the teacher")
    e.add_argument("--student", nargs="*", help="eval_point.py --out files from the base student (difficulty)")
    e.add_argument("--replay", type=float, default=0.35, help="share of student-easy rows kept")
    e.add_argument("--resize-frac", type=float, default=0.0, help="extra randomly downscaled copies, as a share of train")
    e.add_argument("--trust", nargs="*", default=[], help="sources taken from the pool without a teacher check")
    a = ap.parse_args()
    {"pool": pool, "sample": sample, "export": export}[a.cmd](a)


if __name__ == "__main__":
    main()
