"""Shared helpers for every data source: frame normalisation, shard layout, dedupe.

Layout (same for teacher episodes and generated data, so build_dataset.py treats them alike):
  data/<source>/<shard_id>/meta.json
  data/<source>/<shard_id>/steps.jsonl     one sample per line
  data/<source>/<shard_id>/frames/*.jpg

steps.jsonl line:
  {"id": "...", "frame": "frames/000012.jpg", "intent": "Click the 'Save' button",
   "action": {...shared/actions.py...}, "bbox": [l,t,r,b] (0..1000, optional), "source": "ax",
   "app": "Finder" (optional), "os": "macos", "extra": {...}}
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared import actions  # noqa: E402

DATA = Path(os.environ.get("AIKVM_DATA", ROOT / "data"))
MAX_SIDE = 1280  # must match capture, training image_max_pixels and vLLM max_pixels
JPEG_QUALITY = 88


def fit(img, max_side=MAX_SIDE):
    """Downscale so the longest side is <= max_side. Never upscales."""
    img = img.convert("RGB")
    w, h = img.size
    s = max_side / max(w, h)
    if s < 1:
        img = img.resize((round(w * s), round(h * s)), Image.LANCZOS)
    return img


def dhash(img, size=8):
    """64-bit difference hash for near-duplicate frame detection."""
    g = img.convert("L").resize((size + 1, size), Image.BILINEAR)
    px = list(g.getdata())
    bits = 0
    for r in range(size):
        for c in range(size):
            bits = (bits << 1) | (px[r * (size + 1) + c] > px[r * (size + 1) + c + 1])
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


def to1000(v, total):
    return max(0, min(actions.COORD_MAX, int(round(v / total * actions.COORD_MAX))))


def bbox_to1000(l, t, r, b, w, h):
    """Pixel/point box in a w x h space -> [l,t,r,b] in 0..1000."""
    return [to1000(l, w), to1000(t, h), to1000(r, w), to1000(b, h)]


class Shard:
    """Writes one shard. Use as a context manager so meta.json is always finalised."""

    def __init__(self, source, shard_id=None, **meta):
        self.source = source
        self.id = shard_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.dir = DATA / source / self.id
        (self.dir / "frames").mkdir(parents=True, exist_ok=True)
        self.meta = {"source": source, "created": time.time(), "success": True, **meta}
        self._steps = open(self.dir / "steps.jsonl", "a")
        self.n_frames = 0
        self.n_steps = 0
        self.rejected = 0

    def add_frame(self, img):
        """Save a (resized) frame and return its relative path."""
        img = fit(img)
        rel = f"frames/{self.n_frames:06d}.jpg"
        img.save(self.dir / rel, "JPEG", quality=JPEG_QUALITY)
        self.n_frames += 1
        return rel

    def add_step(self, frame, intent, action, bbox=None, **extra):
        try:
            action = actions.validate(action)
        except actions.ActionError:
            self.rejected += 1
            return False
        rec = {"id": f"{self.id}/{self.n_steps}", "frame": frame, "intent": intent.strip(),
               "action": action, "source": self.source}
        if bbox is not None:
            rec["bbox"] = [int(v) for v in bbox]
        for k in ("app", "os"):
            if k in extra:
                rec[k] = extra.pop(k)
        if extra:
            rec["extra"] = extra
        self._steps.write(json.dumps(rec) + "\n")
        self.n_steps += 1
        return True

    def close(self):
        self._steps.close()
        self.meta.update(n_frames=self.n_frames, n_steps=self.n_steps, rejected=self.rejected)
        (self.dir / "meta.json").write_text(json.dumps(self.meta, indent=2))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def iter_shards(source=None, root=DATA):
    """Yield (shard_dir, meta, steps) for every shard under data/ (optionally one source)."""
    sources = [source] if source else sorted(p.name for p in Path(root).iterdir() if p.is_dir())
    for src in sources:
        base = Path(root) / src
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            mf, sf = d / "meta.json", d / "steps.jsonl"
            if not (mf.exists() and sf.exists()):
                continue
            meta = json.loads(mf.read_text())
            steps = [json.loads(line) for line in sf.read_text().splitlines() if line.strip()]
            yield d, meta, steps


def verbify(text, role=None):
    """Turn a bare referring expression ('Downloads') into a click instruction."""
    t = " ".join(str(text).split()).strip().rstrip(".")
    first = t.split(" ", 1)[0].lower() if t else ""
    if first in {"click", "select", "open", "press", "choose", "tap", "toggle", "check", "uncheck",
                 "go", "switch", "expand", "collapse", "close", "show", "hide", "enable", "disable"}:
        return t[0].upper() + t[1:]
    noun = {"AXButton": "button", "AXCheckBox": "checkbox", "AXRadioButton": "option",
            "AXPopUpButton": "menu", "AXMenuItem": "menu item", "AXMenuBarItem": "menu",
            "AXTextField": "field", "AXLink": "link", "AXTab": "tab", "AXRadioGroup": "tab",
            "AXImage": "icon", "AXCell": "item", "AXRow": "row", "AXStaticText": ""}.get(role or "", "")
    return f"Click the '{t}' {noun}".rstrip() if noun else f"Click '{t}'"
