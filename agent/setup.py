"""First contact: make a freshly plugged-in computer drivable, with no clicks from a person.

    python3 -m agent.setup

macOS 26 flow (the KVM presents a TV-class DVI EDID and an Apple keyboard identity, see pi/):
1. A new TV makes macOS ask "What do you want to show on ai-kvm?" with Entire Screen / Start Mirroring as the
   default. The display stays black until then, so a uniform-black capture means the dialog is up: press Return.
2. macOS then mirrors with the KVM as master at 720p, dropping the Mac's own panel to 720p. Set Displays >
   "Optimize for" to the Mac itself (via the Screen Mirroring menu) so the panel returns to its native
   resolution and the KVM mirrors it. macOS stores this per display, so later plug-ins skip steps 1-2.
3. Check that the USB pointer lands where the agent aims.
Prints a JSON report with timings; exit status 0 when the agent can drive the computer.
"""
import argparse
import json
import os
import sys
import time

from PIL import ImageChops, ImageStat

from .kvm import KVM, content_rect, to_hid
from .run import SYSTEM, point
from .vlm import VLM, load_key

PLACEHOLDER_SIZE = (640, 480)   # ustreamer's "NO SIGNAL" frame
GROUND = 'Locate %s. Reply only JSON {"point_2d": [x, y]} with coordinates normalized to 0-1000.'


def awaiting_choice(img):
    """macOS hasn't configured the new display yet: it drives it with uniform black, or with a small centered
    "Choose to Mirror or Extend Display" card, while the "What do you want to show" dialog waits on the Mac."""
    gray = img.convert("L")
    if ImageStat.Stat(gray).mean[0] > 30:
        return False
    bright = gray.point(lambda v: 255 if v > 100 else 0).getbbox()
    if bright is None:
        return True
    w, h = gray.size
    x0, y0, x1, y1 = bright
    return x0 > 0.25 * w and x1 < 0.75 * w and y0 > 0.3 * h and y1 < 0.7 * h


def frame(kvm):
    img = kvm.screenshot()
    return None if img.size == PLACEHOLDER_SIZE else img


def wait_for(cond, timeout, interval=0.25):
    """Poll cond() until it returns a truthy value; connection errors (Pi still booting) count as not yet."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = cond()
        except OSError:
            value = None
        if value:
            return value
        time.sleep(interval)
    return None


def click(kvm, nx, ny, count=1):
    fx, fy = to_hid(nx, ny)
    kvm.actions([{"type": "move", "x": max(0.0, fx - 0.004), "y": fy}, {"type": "wait", "ms": 40},
                 {"type": "move", "x": fx, "y": fy}, {"type": "wait", "ms": 80},
                 {"type": "click", "button": "left", "count": count}])


def locate(kvm, vlm, target, log, timeout=6.0):
    """0-1000 point of a described element; retries while it isn't on screen yet (menus/windows still opening)."""
    deadline = time.monotonic() + timeout
    while True:
        img = frame(kvm)
        if img is None:
            raise RuntimeError("video lost")
        screen = img.crop(content_rect(img))
        text, _, dt = vlm.chat([{"role": "system", "content": SYSTEM},
                                {"role": "user", "content": [vlm.image_part(screen),
                                                             {"type": "text", "text": GROUND % target}]}], 24)
        try:
            x, y = point(text)
            log(f"  located {target!r} at ({x:.0f},{y:.0f}) in {dt:.2f}s")
            return x, y
        except ValueError:
            if time.monotonic() > deadline:
                raise RuntimeError(f"{target} not found: {text[:80]!r}")
            log(f"  {target!r} not on screen yet ({text[:40]!r}), retrying")
            time.sleep(0.3)


def optimize_for_mac(kvm, vlm, log):
    """Displays > Optimize for: this Mac (not ai-kvm). Returns True when the capture shows the Mac's native aspect."""
    kvm.actions([{"type": "move", "x": 0.5, "y": 0.0}, {"type": "wait", "ms": 500}])   # reveal an auto-hidden menu bar
    click(kvm, *locate(kvm, vlm, "the Screen Mirroring icon in the menu bar (two overlapping rectangles, highlighted)", log))
    time.sleep(0.4)
    click(kvm, *locate(kvm, vlm, 'the "Display Settings..." item at the bottom of the Screen Mirroring menu', log))
    time.sleep(1.0)
    click(kvm, *locate(kvm, vlm, 'the pop-up button to the right of "Optimize for" in Displays settings', log))
    time.sleep(0.4)
    click(kvm, *locate(kvm, vlm, "the menu item naming this Mac (the option that is not ai-kvm)", log))
    time.sleep(1.0)
    kvm.actions([{"type": "key", "combo": "cmd+q"}])   # quit System Settings
    img = wait_for(lambda: (lambda f: f if f is not None and content_rect(f)[2] - content_rect(f)[0] < f.width * 0.98
                            else None)(frame(kvm)), 6)
    return img is not None


def pointer_error(kvm, points=((300, 300), (700, 650))):
    """Max distance (0-1000 grid) between where the pointer was sent and where it shows up in the capture.

    Finds the cursor by differencing frames with the pointer parked just below each target vs on it, so no
    model call is needed (parking stays off screen corners: macOS hot corners trigger there)."""
    errors = []
    for nx, ny in points:
        fx, fy = to_hid(nx, ny)
        kvm.actions([{"type": "move", "x": fx, "y": min(0.95, fy + 0.18)}])
        time.sleep(0.3)
        away = kvm.screenshot()
        kvm.actions([{"type": "move", "x": fx, "y": fy}])
        time.sleep(0.3)
        here = kvm.screenshot()
        rect = content_rect(here)
        diff = ImageChops.difference(here.crop(rect), away.crop(rect)).convert("L").point(lambda v: 255 if v > 40 else 0)
        w, h = diff.size
        # the cursor tip is the top-left of its blob; search a window around the target
        cx, cy = nx / 1000 * w, ny / 1000 * h
        box = (max(0, int(cx - 60)), max(0, int(cy - 60)), min(w, int(cx + 60)), min(h, int(cy + 60)))
        bbox = diff.crop(box).getbbox()
        if not bbox:
            return None
        tip_x, tip_y = box[0] + bbox[0], box[1] + bbox[1]
        errors.append(max(abs(tip_x / w * 1000 - nx), abs(tip_y / h * 1000 - ny)))
    return round(max(errors))


def first_contact(kvm, vlm, log=print, signal_timeout=45):
    t0 = time.monotonic()
    report = {"ok": False}

    def mark(key):
        report[key] = round(time.monotonic() - t0, 2)

    health = wait_for(lambda: (lambda h: h if h.get("usb") == "configured" else None)(kvm.health()), 30)
    if not health:
        report["problem"] = "no USB host: plug the KVM's USB-C into the target computer"
        return report
    mark("usb_s")
    img = wait_for(lambda: frame(kvm), signal_timeout)
    if img is None:
        report["problem"] = "no HDMI signal: connect the target's video output to the KVM and wake the computer"
        return report
    mark("video_s")
    if awaiting_choice(img):
        # macOS shows "What do you want to show on ai-kvm?"; give it a moment to settle, then accept the default
        time.sleep(1.0)
        if awaiting_choice(frame(kvm) or img):
            log("display waiting for a choice: pressing Return (Entire Screen / Start Mirroring)")
            kvm.actions([{"type": "key", "combo": "enter"}])
            report["accepted_mirror_dialog"] = True
            img = wait_for(lambda: (lambda f: f if f is not None and not awaiting_choice(f) else None)(frame(kvm)), 10)
            if img is None:
                report["problem"] = "display still not configured after accepting the mirroring dialog"
                return report
            mark("mirroring_s")
            if optimize_for_mac(kvm, vlm, log):
                report["native_resolution_restored"] = True
                mark("restored_s")
            else:
                report["problem"] = "could not set Displays > Optimize for to this Mac"
                return report
            img = frame(kvm)
    rect = content_rect(img)
    report["screen"] = [rect[2] - rect[0], rect[3] - rect[1]]
    report["pointer_error"] = pointer_error(kvm)
    mark("total_s")
    if report["pointer_error"] is None or report["pointer_error"] > 25:
        report["problem"] = ("pointer does not land where the capture shows it: the KVM display is probably extended, "
                             "not mirrored")
        return report
    report["ok"] = True
    return report


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m agent.setup", description=__doc__.splitlines()[0])
    p.add_argument("--kvm", default=os.environ.get("AIKVM_URL", "http://10.42.0.101:8080"))
    p.add_argument("--llm", default=os.environ.get("AIKVM_LLM_BASE", "http://127.0.0.1:8000/v1"))
    p.add_argument("--model", default=os.environ.get("AIKVM_LLM_MODEL", "qwen122"))
    p.add_argument("--key-file", default=os.environ.get("AIKVM_LLM_KEY_FILE", "/etc/gemma4-vllm-api-key"))
    a = p.parse_args(argv)
    report = first_contact(KVM(a.kvm), VLM(a.llm, a.model, key=load_key(a.key_file)))
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
