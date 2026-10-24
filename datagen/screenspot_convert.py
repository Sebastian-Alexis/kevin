"""ScreenSpot-v2 (OS-Copilot, desktop + web) -> data/grounding/screenspot.jsonl. EVAL ONLY: never trained on.

  hf download OS-Copilot/ScreenSpot-v2 --repo-type dataset --local-dir /workspace/screenspot
  cd /workspace/screenspot && unzip -q screenspotv2_image.zip
  python datagen/screenspot_convert.py --src /workspace/screenspot

Images are fit to MAX_SIDE like every other source; bbox (x, y, w, h in pixels) becomes [l, t, r, b] on 0-1000.
"""
import argparse
import json
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DATA, JPEG_QUALITY, bbox_to1000, fit  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--splits", default="desktop,web")
    a = ap.parse_args()
    src = Path(a.src)
    out_dir = DATA / "grounding" / "screenspot"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for split in a.splits.split(","):
        for i, r in enumerate(json.loads((src / f"screenspot_{split}_v2.json").read_text())):
            img = Image.open(src / "screenspotv2_image" / r["img_filename"])
            w, h = img.size
            x, y, bw, bh = r["bbox"]
            frame = out_dir / (Path(r["img_filename"]).stem + ".jpg")
            if not frame.exists():
                fit(img).save(frame, quality=JPEG_QUALITY)
            rows.append({"id": f"screenspot-{split}/{i}", "image": frame.relative_to(DATA).as_posix(),
                         "target": r["instruction"], "bbox": bbox_to1000(x, y, x + bw, y + bh, w, h),
                         "source": "screenspot", "group": f"{split}/{r['data_source']}/{r['data_type']}"})
    with open(DATA / "grounding" / "screenspot.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    print(f"{len(rows)} samples -> data/grounding/screenspot.jsonl")


if __name__ == "__main__":
    main()
