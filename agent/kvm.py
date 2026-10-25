"""ai-kvm Pi API client: screenshots in, USB HID actions out.

Coordinates handed to this module are 0-1000 normalized over the *target screen picture*: the
capture frame cropped to its content rectangle (macOS pillarboxes a 16:10 panel into the 16:9 HDMI
frame). The absolute HID pointer spans exactly that picture, so 0-1000 maps straight to 0-1 HID.
"""
import io
import json
import time
import urllib.error
import urllib.request

from PIL import Image, ImageChops, ImageStat

BAR_LEVEL = 24       # max channel value of letterbox/pillarbox pixels (limited-range black is 16, plus JPEG noise)
MIN_CONTENT = 0.5     # never crop below half the frame (protects dark UIs from being "cropped")
PLACEHOLDER_SIZE = (640, 480)   # ustreamer's "NO SIGNAL" frame


class KVM:
    def __init__(self, base="http://100.98.36.92:8080", timeout=15):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def screenshot(self):
        with urllib.request.urlopen(self.base + "/screenshot", timeout=self.timeout) as r:
            return Image.open(io.BytesIO(r.read())).convert("RGB")

    def health(self):
        with urllib.request.urlopen(self.base + "/health", timeout=self.timeout) as r:
            return json.load(r)

    def actions(self, actions):
        req = urllib.request.Request(self.base + "/actions", json.dumps({"actions": actions}).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60)) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # the Pi answers 4xx with {"error": ..., "failed_index": i}; keep that instead of a bare "Bad Request"
            try:
                body = json.loads(e.read().decode())
            except Exception:
                raise
            i = body.get("failed_index")
            where = f" at action {i} ({actions[i].get('type')})" if isinstance(i, int) and i < len(actions) else ""
            raise RuntimeError(f"KVM refused the batch{where}: {body.get('error', e.reason)}") from None


def frame(kvm):
    """The current picture, or None while ustreamer shows its NO SIGNAL placeholder (nothing to see or ground)."""
    img = kvm.screenshot()
    return None if img.size == PLACEHOLDER_SIZE else img


def mean_diff(a, b):
    """Mean absolute difference of two same-size frames on a 0-255 scale (a static screen's JPEG noise is ~1)."""
    return ImageStat.Stat(ImageChops.difference(a, b).convert("L")).mean[0]


def settled(kvm, timeout=6.0, interval=0.4, tolerance=2.0, first_delay=0.0):
    """Latest frame once two consecutive frames are nearly identical (animations, page loads and display
    transitions done), else the last frame seen at timeout. ustreamer's NO SIGNAL placeholder counts as None."""
    time.sleep(first_delay)
    prev = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        img = frame(kvm)
        if img is not None and prev is not None and img.size == prev.size and mean_diff(img, prev) < tolerance:
            return img
        prev = img
        time.sleep(interval)
    return prev


def content_rect(img):
    """(x0, y0, x1, y1) of the target's screen inside the capture frame.

    Only symmetric left/right black bars are trimmed: that is how a 16:10 Mac panel sits in the 16:9 frame when
    the Mac mirrors to the KVM. Top/bottom rows are never trimmed (a dark menu bar or Dock is part of the screen)
    and lopsided dark columns are an app, not a pillarbox."""
    w, h = img.size
    px = img.load()

    def bar_col(x):
        return all(max(px[x, y]) <= BAR_LEVEL for y in range(0, h, max(1, h // 48)))

    x0, x1 = 0, w
    while x0 < w // 2 and bar_col(x0):
        x0 += 1
    while x1 > w // 2 and bar_col(x1 - 1):
        x1 -= 1
    if (x1 - x0) < w * MIN_CONTENT or abs(x0 - (w - x1)) > max(4, w // 100):
        return 0, 0, w, h
    return x0, 0, x1, h


def to_hid(nx, ny):
    """0-1000 normalized screen point -> 0-1 HID absolute fraction (clamped)."""
    return min(1.0, max(0.0, nx / 1000)), min(1.0, max(0.0, ny / 1000))
