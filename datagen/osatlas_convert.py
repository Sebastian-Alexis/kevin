"""Source C: OS-Atlas desktop grounding data (Apache-2.0) -> our shard layout. Replay against forgetting.

The macOS image zip is 5.5 GB. By default we DON'T download it: --remote pulls only the sampled images
out of the zip on Hugging Face with HTTP range requests (remotezip). Pass --zip to use a local copy.

  python datagen/osatlas_convert.py --os macos -n 4000
  python datagen/osatlas_convert.py --os linux -n 1000 --zip ~/Downloads/linux_images.zip
"""
import argparse
import io
import json
import random
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DATA, Shard, bbox_to1000, verbify  # noqa: E402

HF = "https://huggingface.co/datasets/OS-Copilot/OS-Atlas-data/resolve/main/desktop_domain"
ZIPS = {"macos": "macos_images.zip", "linux": "linux_images.zip"}  # windows is split into 70 GB of parts: skip


def load_annotations(os_name, cache):
    path = cache / f"{os_name}_splited.json"
    if not path.exists():
        print(f"downloading {path.name} ...", flush=True)
        cache.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(f"{HF}/{path.name}", path)
    return json.loads(path.read_text())


def keep(el, min_area, max_area, max_chars):
    l, t, r, b = el["bbox"]
    if not (0 <= l < r <= 1 and 0 <= t < b <= 1):  # some boxes run off-image
        return False
    area = (r - l) * (b - t)
    text = " ".join(str(el.get("instruction", "")).split())
    return min_area <= area <= max_area and 2 <= len(text) <= max_chars


def open_zip(args):
    if args.zip:
        return zipfile.ZipFile(args.zip)
    try:
        from remotezip import RemoteZip
    except ImportError:
        sys.exit("pip install remotezip   (or pass --zip with a downloaded archive)")
    return RemoteZip(f"{HF}/{ZIPS[args.os]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--os", choices=sorted(ZIPS), default="macos")
    ap.add_argument("-n", type=int, default=4000, help="samples (elements) to keep")
    ap.add_argument("--per-image", type=int, default=4, help="max elements per screenshot")
    ap.add_argument("--min-area", type=float, default=0.0002, help="fraction of screen (plan's 0.003 drops most buttons)")
    ap.add_argument("--max-area", type=float, default=0.25)
    ap.add_argument("--max-chars", type=int, default=80)
    ap.add_argument("--zip", help="local images zip instead of remote range reads")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cache", default=str(DATA / "_cache" / "osatlas"))
    args = ap.parse_args()

    rng = random.Random(args.seed)
    ann = load_annotations(args.os, Path(args.cache))
    rng.shuffle(ann)

    picked, total = [], 0
    for item in ann:
        els = [e for e in item["elements"] if keep(e, args.min_area, args.max_area, args.max_chars)]
        if not els:
            continue
        els = rng.sample(els, min(len(els), args.per_image))
        picked.append((item["img_filename"], els))
        total += len(els)
        if total >= args.n:
            break
    print(f"{len(ann)} screenshots in annotations; using {len(picked)} screenshots / {total} elements")

    zf = open_zip(args)
    by_base = defaultdict(list)
    for name in zf.namelist():
        by_base[Path(name).name].append(name)

    with Shard("osatlas", shard_id=f"{args.os}-seed{args.seed}", os=args.os, dataset="OS-Copilot/OS-Atlas-data",
               license="Apache-2.0") as shard:
        missing = 0
        for i, (fname, els) in enumerate(picked):
            members = by_base.get(Path(fname).name)
            if not members:
                missing += 1
                continue
            img = Image.open(io.BytesIO(zf.read(members[0])))
            w, h = img.size
            frame = shard.add_frame(img)
            for e in els:
                l, t, r, b = e["bbox"]
                box = bbox_to1000(l * w, t * h, r * w, b * h, w, h)
                shard.add_step(frame, verbify(e["instruction"], e.get("data_type")),
                               {"action": "click", "x": (box[0] + box[2]) // 2, "y": (box[1] + box[3]) // 2},
                               bbox=box, os=args.os, raw_instruction=e["instruction"], role=e.get("data_type"))
            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(picked)} screenshots, {shard.n_steps} samples", flush=True)
        print(f"wrote {shard.n_steps} samples to {shard.dir} ({missing} images not found in zip)")


if __name__ == "__main__":
    main()
