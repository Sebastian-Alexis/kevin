"""GroundCUA (ServiceNow, MIT): human-verified boxes on desktop apps -> data/groundcua/<shard>/.

87 app folders, one PNG + one JSON (list of {bbox [x1,y1,x2,y2] px, text, category}) per screenshot. We sample a
few screenshots per app and only download those, instead of the whole 22.5 GB.

  python datagen/groundcua_convert.py --per-app 30 --per-image 4
"""
import argparse
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Shard, bbox_to1000  # noqa: E402

REPO = "ServiceNow/GroundCUA"
ROLE = {"Button": "button", "Menu": "menu", "Input Elements": "field", "Navigation": "tab", "Sidebar": "item",
        "Visual Elements": "icon"}


def fetch(path):
    return hf_hub_download(REPO, path, repo_type="dataset")


def target(e):
    """Planner-voice target from the human label: "the 'minimize' button"."""
    role = ROLE.get(e.get("category") or "")
    text = e["text"].strip()
    return f"the '{text}' {role}" if role else f"'{text}'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-app", type=int, default=30, help="screenshots per app")
    ap.add_argument("--per-image", type=int, default=4)
    ap.add_argument("--min-area", type=float, default=0.0001)
    ap.add_argument("--max-area", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    api = HfApi()
    apps = sorted(p.path.split("/", 1)[1] for p in api.list_repo_tree(REPO, "data", repo_type="dataset"))
    picks = []
    for app in apps:
        files = sorted(f.path for f in api.list_repo_tree(REPO, f"data/{app}", repo_type="dataset") if f.path.endswith(".json"))
        picks += [(app, f) for f in rng.sample(files, min(a.per_app, len(files)))]
    print(f"{len(apps)} apps, {len(picks)} screenshots picked", flush=True)

    def load(pick):
        app, jpath = pick
        try:
            ann = json.loads(Path(fetch(jpath)).read_text())
            img = Image.open(fetch(f"images/{app}/{Path(jpath).stem}.png"))
            img.load()
            return app, ann, img
        except Exception as e:   # a missing or unreadable pair is skipped, not fatal
            print(f"  ! {jpath}: {type(e).__name__}", flush=True)
            return None

    with Shard("groundcua", shard_id=f"seed{a.seed}", dataset=REPO, license="MIT") as shard, \
            ThreadPoolExecutor(16) as ex:
        for i, got in enumerate(ex.map(load, picks)):
            if got is None:
                continue
            app, ann, img = got
            w, h = img.size
            els = []
            for e in ann:
                if not (e.get("text") or "").strip() or not e.get("bbox"):
                    continue
                l, t, r, b = e["bbox"]
                area = (r - l) * (b - t) / (w * h)
                if a.min_area <= area <= a.max_area:
                    els.append(e)
            if not els:
                continue
            frame = shard.add_frame(img)
            for e in rng.sample(els, min(a.per_image, len(els))):
                box = bbox_to1000(*e["bbox"], w, h)
                shard.add_step(frame, "Click " + target(e),
                               {"action": "click", "x": (box[0] + box[2]) // 2, "y": (box[1] + box[3]) // 2},
                               bbox=box, os="desktop", app=app, category=e.get("category"), raw_text=e["text"])
            if (i + 1) % 200 == 0:
                print(f"  {i + 1}/{len(picks)} screenshots, {shard.n_steps} samples", flush=True)
        print(f"wrote {shard.n_steps} samples to {shard.dir}")


if __name__ == "__main__":
    main()
