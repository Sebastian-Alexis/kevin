"""Mix every data source into LLaMA-Factory sharegpt files: train / val / holdout (+ gold eval).

  python training/build_dataset.py --mix ax=0.3,web=0.2,osatlas=0.25,templates=0.15,teacher=0.1
  python training/build_dataset.py --holdout-apps Calendar,Mail --total 12000

Writes data/export/{hid_actor_train,hid_actor_val,hid_actor_holdout,hid_actor_gold}.json + dataset_info.json.
Image paths are relative to data/, so set media_dir: data in the LLaMA-Factory yaml (lora_sft.yaml does).

Split rules: teacher episodes split by episode; generated sources split by frame (never the same frame in two
splits); the "gold" source (hand-labelled from the real capture path) is eval-only and never trained on.
"""
import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "datagen"))
from common import DATA, dhash, iter_shards  # noqa: E402
from shared import actions  # noqa: E402

PROMPT = (ROOT / "shared" / "actor_prompt.txt").read_text().strip()
LAZY = re.compile(r"^(continue|next|do it|click it|proceed|ok|done)\b", re.I)
EVAL_ONLY = {"gold"}


def bucket(key, val_frac):
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < val_frac


def to_record(shard_dir, step):
    img = (shard_dir / step["frame"]).relative_to(DATA).as_posix()
    return {"messages": [{"role": "user", "content": "<image>" + PROMPT.replace("{instruction}", step["intent"])},
                         {"role": "assistant", "content": actions.dumps(step["action"])}],
            "images": [img],
            # extra keys are ignored by LLaMA-Factory; eval_grounding.py uses them
            "meta": {"id": step["id"], "source": step["source"], "bbox": step.get("bbox"), "app": step.get("app")}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mix", default="", help="source=weight,... ; omit to keep everything")
    ap.add_argument("--total", type=int, default=0, help="cap on train+val samples after mixing")
    ap.add_argument("--val-frac", type=float, default=0.05)
    ap.add_argument("--holdout-apps", default="", help="comma list; their steps go to the holdout split only")
    ap.add_argument("--max-click-frac", type=float, default=0.7, help="downsample clicks above this share")
    ap.add_argument("--no-dedupe", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=str(DATA / "export"))
    args = ap.parse_args()

    rng = random.Random(args.seed)
    holdout_apps = {a.strip().lower() for a in args.holdout_apps.split(",") if a.strip()}
    drops = Counter()
    pools = defaultdict(list)  # source -> [(split, record)]
    gold, holdout = [], []
    seen = set()
    hash_cache = {}

    for shard_dir, meta, steps in iter_shards():
        src = meta.get("source", shard_dir.parent.name)
        if src.startswith("_"):
            continue
        if src == "teacher" and not meta.get("success"):
            drops["teacher_unsuccessful_episode"] += len(steps)
            continue
        for s in steps:
            if s.get("drop"):
                drops["flagged_by_rephrase"] += 1
                continue
            try:
                s["action"] = actions.validate(s["action"])
            except actions.ActionError:
                drops["invalid_action"] += 1
                continue
            intent = s.get("intent", "").strip()
            if not intent or LAZY.match(intent) or (src == "teacher" and len(intent.split()) < 4):
                drops["weak_intent"] += 1
                continue
            if not args.no_dedupe:
                fp = shard_dir / s["frame"]
                h = hash_cache.get(fp)
                if h is None:
                    h = hash_cache[fp] = dhash(Image.open(fp))
                key = (h, actions.dumps(s["action"]), intent.lower())
                if key in seen:
                    drops["near_duplicate"] += 1
                    continue
                seen.add(key)
            rec = to_record(shard_dir, s)
            app = (s.get("app") or meta.get("app") or "").lower()
            if src in EVAL_ONLY:
                gold.append(rec)
            elif app and app in holdout_apps:
                holdout.append(rec)
            else:
                split_key = shard_dir.name if src == "teacher" else f"{shard_dir.name}/{s['frame']}"
                pools[src].append(("val" if bucket(split_key, args.val_frac) else "train", rec))

    # --- mixing -------------------------------------------------------------------------------------------
    counts = {k: len(v) for k, v in pools.items()}
    if args.mix:
        want = {k: float(v) for k, v in (p.split("=") for p in args.mix.split(","))}
        want = {k: v for k, v in want.items() if counts.get(k)}
        missing = [k for k in args.mix.split(",") if k.split("=")[0] not in want]
        if missing:
            print(f"note: no data yet for {missing}; renormalising the rest")
        tot_w = sum(want.values())
        total = min(counts[k] / (w / tot_w) for k, w in want.items())
        if args.total:
            total = min(total, args.total)
        for k in list(pools):
            if k not in want:
                drops[f"mix_excluded_{k}"] += len(pools.pop(k))
                continue
            rng.shuffle(pools[k])
            n = int(total * want[k] / tot_w)
            drops[f"mix_downsampled_{k}"] += len(pools[k]) - n
            pools[k] = pools[k][:n]
    elif args.total:
        allrecs = [(k, r) for k, v in pools.items() for r in v]
        rng.shuffle(allrecs)
        pools = defaultdict(list)
        for k, r in allrecs[: args.total]:
            pools[k].append(r)

    split = {"train": [], "val": []}
    for k, v in pools.items():
        for sp, rec in v:
            split[sp].append(rec)

    # --- action balance -------------------------------------------------------------------------------------
    def kind(rec):
        return json.loads(rec["messages"][1]["content"])["action"]

    tr = split["train"]
    n_click = sum(kind(r) == "click" for r in tr)
    if tr and n_click / len(tr) > args.max_click_frac:
        others = len(tr) - n_click
        keep = int(args.max_click_frac / (1 - args.max_click_frac) * others)
        clicks = [r for r in tr if kind(r) == "click"]
        rng.shuffle(clicks)
        drops["click_cap"] += len(clicks) - keep
        tr = [r for r in tr if kind(r) != "click"] + clicks[:keep]
        split["train"] = tr

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files = {"hid_actor_train": split["train"], "hid_actor_val": split["val"], "hid_actor_holdout": holdout,
             "hid_actor_gold": gold}
    info = {}
    for name, recs in files.items():
        rng.shuffle(recs)
        (out / f"{name}.json").write_text(json.dumps(recs, ensure_ascii=False))
        info[name] = {"file_name": f"{name}.json", "formatting": "sharegpt",
                      "columns": {"messages": "messages", "images": "images"},
                      "tags": {"role_tag": "role", "content_tag": "content", "user_tag": "user",
                               "assistant_tag": "assistant"}}
    (out / "dataset_info.json").write_text(json.dumps(info, indent=2))

    print("\nsamples per source (after filters, before mix):", dict(counts))
    for name, recs in files.items():
        by_src = Counter(r["meta"]["source"] for r in recs)
        by_act = Counter(kind(r) for r in recs)
        tot = max(1, len(recs))
        acts = ", ".join(f"{a} {c} ({c / tot:.0%})" for a, c in by_act.most_common())
        print(f"{name:18s} {len(recs):6d} | {dict(by_src)} | {acts}")
    print("dropped:", dict(drops))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
