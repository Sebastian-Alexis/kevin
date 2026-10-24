"""Visual spot-check: draw each sample's target (point + bbox) on its frame and write a contact sheet HTML.

  python datagen/preview.py --source osatlas -n 50
  open data/_preview/osatlas.html

Go through the page and count misses. Acceptance for a generator: >= 95% of points on the named element.
"""
import argparse
import html
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DATA, iter_shards  # noqa: E402


def render(shard_dir, step, out):
    img = Image.open(shard_dir / step["frame"]).convert("RGB")
    w, h = img.size
    d = ImageDraw.Draw(img)
    a = step["action"]
    sx, sy = w / 1000, h / 1000
    if step.get("bbox"):
        l, t, r, b = step["bbox"]
        d.rectangle([l * sx, t * sy, r * sx, b * sy], outline=(0, 200, 255), width=2)
    if "x" in a:
        x, y = a["x"] * sx, a["y"] * sy
        d.ellipse([x - 7, y - 7, x + 7, y + 7], outline=(255, 0, 60), width=3)
        d.line([x - 12, y, x + 12, y], fill=(255, 0, 60), width=1)
        d.line([x, y - 12, x, y + 12], fill=(255, 0, 60), width=1)
        if "to_x" in a:
            d.line([x, y, a["to_x"] * sx, a["to_y"] * sy], fill=(255, 0, 60), width=2)
    img.save(out, "JPEG", quality=80)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="osatlas | ax | web | templates | teacher ... (default: all)")
    ap.add_argument("-n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    pool = [(d, s) for d, _, steps in iter_shards(args.source) for s in steps]
    if not pool:
        sys.exit(f"no samples under {DATA}/{args.source or ''}")
    random.Random(args.seed).shuffle(pool)
    name = args.source or "all"
    out = DATA / "_preview" / name
    out.mkdir(parents=True, exist_ok=True)

    cards = []
    for i, (d, s) in enumerate(pool[: args.n]):
        fn = f"{i:03d}.jpg"
        render(d, s, out / fn)
        cards.append(f'<figure><img src="{name}/{fn}" loading="lazy"><figcaption><b>{i}</b> '
                     f'{html.escape(s["intent"])}<br><code>{html.escape(str(s["action"]))}</code>'
                     f'<br><small>{html.escape(s["id"])}</small></figcaption></figure>')
    page = DATA / "_preview" / f"{name}.html"
    page.write_text("<!doctype html><meta charset=utf-8><title>preview " + name + "</title><style>"
                    "body{font:13px system-ui;margin:16px;background:#111;color:#eee}"
                    "main{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));gap:14px}"
                    "img{width:100%;border-radius:4px}figure{margin:0}code{color:#9cf}</style>"
                    f"<h3>{len(cards)} samples from {name} ({len(pool)} total)</h3><main>{''.join(cards)}</main>")
    print(f"open {page}")


if __name__ == "__main__":
    main()
