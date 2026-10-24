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
GROUND = ('Locate %s. If it is not visible in this screenshot, reply exactly {"point_2d": null}. '
          'Otherwise reply only JSON {"point_2d": [x, y]} with coordinates normalized to 0-1000.')
DEBUG_DIR = os.environ.get("AIKVM_SETUP_DEBUG", "/tmp/aikvm-setup")


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
    """0-1000 point of a described element; retries while it isn't on screen yet (menus/windows still opening).
    Each frame the model looked at is saved to DEBUG_DIR for post-mortems."""
    deadline = time.monotonic() + timeout
    os.makedirs(DEBUG_DIR, exist_ok=True)
    while True:
        img = frame(kvm)
        if img is None:
            raise RuntimeError("video lost")
        screen = img.crop(content_rect(img))
        tag = "".join(c if c.isalnum() else "_" for c in target[:40])
        screen.save(os.path.join(DEBUG_DIR, f"{time.strftime('%H%M%S')}-{tag}.jpg"))
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


def pillarboxed(img):
    """The capture shows the Mac's own panel (16:10-class, aspect ~1.55) inside the 16:9 frame: the built-in is
    the mirror master. With the KVM as master the picture fills the frame (aspect 1.78)."""
    x0, y0, x1, y1 = content_rect(img)
    return (x1 - x0) / (y1 - y0) < 1.70


def settled(kvm, timeout=6.0, interval=0.4, tolerance=2.0):
    """Wait until two consecutive frames are nearly identical (display transitions and animations done)."""
    prev = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        img = frame(kvm)
        if img is not None and prev is not None and img.size == prev.size:
            if ImageStat.Stat(ImageChops.difference(img, prev).convert("L")).mean[0] < tolerance:
                return img
        prev = img
        time.sleep(interval)
    return prev


def find(kvm, vlm, target, check, log, timeout=6.0, retry=None):
    """locate() whose answer must pass check(x, y); a failed check means the model guessed at something not on
    screen (e.g. a menu that didn't open), so take a fresh screenshot and ask again, optionally running retry()."""
    deadline = time.monotonic() + timeout
    while True:
        x, y = locate(kvm, vlm, target, log, timeout=max(0.5, deadline - time.monotonic()))
        if check(x, y):
            return x, y
        if time.monotonic() > deadline:
            raise RuntimeError(f"{target}: implausible position ({x:.0f},{y:.0f})")
        log(f"  implausible position for {target!r}, retrying")
        if retry:
            retry()
        time.sleep(0.4)


def reveal_menu_bar(kvm):
    """Touch the top edge (and wiggle) so an auto-hidden menu bar slides in."""
    kvm.actions([{"type": "move", "x": 0.5, "y": 0.02}, {"type": "wait", "ms": 40},
                 {"type": "move", "x": 0.5, "y": 0.0}, {"type": "wait", "ms": 40},
                 {"type": "move", "x": 0.52, "y": 0.0}, {"type": "wait", "ms": 450}])


def ask_yes(kvm, vlm, question):
    img = frame(kvm)
    if img is None:
        return False
    text, _, _ = vlm.chat([{"role": "system", "content": SYSTEM},
                           {"role": "user", "content": [vlm.image_part(img.crop(content_rect(img))),
                                                        {"type": "text", "text": question + " Answer only yes or no."}]}], 4)
    return text.strip().lower().startswith("yes")


SETTINGS_FRONT = 'Does the menu bar show "System Settings" as the active app name next to the Apple logo?'


# With the KVM as mirror master the Mac runs the KVM's only mode, 1280x720, so the Apple menu is laid out
# identically on every Mac: logo at the top-left corner, "System Settings..." as the second item.
APPLE_LOGO_720P = (16, 17)
SYSTEM_SETTINGS_ITEM_720P = (78, 115)


def open_system_settings(kvm, vlm, log, attempts=3):
    """Apple menu > System Settings..., confirmed by the menu bar naming it the active app. The Apple logo is
    unambiguous, unlike picking the Screen Mirroring icon out of a dozen third-party status icons.
    First try clicks the known 720p positions (no model calls); later tries ground both clicks with the model."""
    for attempt in range(attempts):
        reveal_menu_bar(kvm)
        if attempt == 0:
            ax, ay = APPLE_LOGO_720P
            click(kvm, ax, ay)
            time.sleep(0.4)
            sx, sy = SYSTEM_SETTINGS_ITEM_720P
        else:
            ax, ay = find(kvm, vlm, "the Apple logo at the far left end of the menu bar",
                          lambda x, y: x < 80 and y < 45, log, retry=lambda: reveal_menu_bar(kvm))
            click(kvm, ax, ay)
            time.sleep(0.5)
            # the item sits in the dropped-down menu, clearly below the menu bar (~33 units tall at 720p)
            sx, sy = find(kvm, vlm, 'the "System Settings..." item in the open Apple menu',
                          lambda x, y: x < 400 and ay + 30 < y < 450, log,
                          retry=lambda: (reveal_menu_bar(kvm), click(kvm, ax, ay)))
        click(kvm, sx, sy)
        # a cold launch of System Settings can take several seconds: poll before deciding it failed
        if wait_for(lambda: (reveal_menu_bar(kvm), ask_yes(kvm, vlm, SETTINGS_FRONT))[1], 7, interval=0.2):
            return
        log("  System Settings did not come to the front, retrying")
        kvm.actions([{"type": "key", "combo": "esc"}])
    raise RuntimeError("could not open System Settings")


def show_all_and_close(kvm, vlm, log):
    """System Settings > Show All (undo Hide Others), then close the settings window if it is still in front."""
    kvm.actions([{"type": "key", "combo": "esc"}])   # close a pop-up menu a failed step may have left open
    reveal_menu_bar(kvm)
    ux, uy = find(kvm, vlm, 'the bold "System Settings" app menu title in the menu bar, right of the Apple logo',
                  lambda x, y: x < 250 and y < 45, log, retry=lambda: reveal_menu_bar(kvm))
    click(kvm, ux, uy)
    time.sleep(0.5)
    sx, sy = find(kvm, vlm, 'the "Show All" item in the open System Settings menu',
                  lambda x, y: abs(x - ux) < 300 and y > uy + 30, log)
    click(kvm, sx, sy)
    time.sleep(0.4)
    # a click in System Settings' own menu leaves it the active app, so cmd+w closes its window and nothing else
    kvm.actions([{"type": "key", "combo": "cmd+w"}])


def optimize_for_mac(kvm, vlm, log):
    """Displays > Optimize for: this Mac (not ai-kvm). Returns True once the capture shows the Mac's native panel.

    Every model-provided point is sanity-checked against where that element can be before it is clicked, and
    keyboard shortcuts are only sent once the menu bar confirms System Settings is the active app."""
    open_system_settings(kvm, vlm, log)
    # other apps' windows (e.g. a terminal set to float on top) can cover System Settings: hide them for now
    kvm.actions([{"type": "key", "combo": "alt+cmd+h"}, {"type": "wait", "ms": 400}])
    try:
        qx, qy = find(kvm, vlm, "the Search field at the top of the System Settings window's left sidebar",
                      lambda x, y: y < 450, log, timeout=6)
        click(kvm, qx, qy)
        kvm.actions([{"type": "key", "combo": "cmd+a"}, {"type": "type", "text": "Displays"},
                     {"type": "wait", "ms": 700}])
        dx, dy = find(kvm, vlm, '"Displays" in the System Settings sidebar search results',
                      lambda x, y: abs(x - qx) < 300 and y > qy, log)
        click(kvm, dx, dy)
        time.sleep(0.6)
        px, py = find(kvm, vlm, 'the pop-up button showing "ai-kvm" to the right of the "Optimize for" label in the '
                                'System Settings Displays pane', lambda x, y: x > 400 and 80 < y < 960, log, timeout=8)
        click(kvm, px, py)
        time.sleep(0.35)
        # macOS opens the list so the selected item sits on the button: other items appear above/below it
        mx, my = find(kvm, vlm, 'the item in the open pop-up menu that names this Mac (the option that is not '
                                '"ai-kvm")', lambda x, y: abs(y - py) < 250 and abs(x - px) < 300, log)
        click(kvm, mx, my)
        restored = wait_for(lambda: (lambda f: f if f is not None and pillarboxed(f) else None)(frame(kvm)), 6)
        settled(kvm, timeout=1.5)
    finally:
        try:
            show_all_and_close(kvm, vlm, log)
        except RuntimeError as e:
            # activating an app unhides it: cmd+tab at least brings back the most recently used one
            log(f"  Show All failed ({e}); cmd+tab to unhide the previous app")
            kvm.actions([{"type": "key", "combo": "esc"}, {"type": "key", "combo": "cmd+tab"}])
    return restored is not None


def cursor_tip(kvm, nx, ny):
    """Where the cursor appears (0-1000) after moving it to (nx, ny), or None if the frame difference near the
    target isn't cursor-shaped (another part of the screen was changing)."""
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
    cx, cy = nx / 1000 * w, ny / 1000 * h
    box = (max(0, int(cx - 60)), max(0, int(cy - 60)), min(w, int(cx + 60)), min(h, int(cy + 60)))
    bbox = diff.crop(box).getbbox()
    if not bbox or bbox[2] - bbox[0] > 40 or bbox[3] - bbox[1] > 45:   # a macOS arrow is ~20x30 px at 720p
        return None
    # the cursor's hotspot is the top-left of the arrow
    return (box[0] + bbox[0]) / w * 1000, (box[1] + bbox[1]) / h * 1000


def pointer_error(kvm, points=((300, 300), (700, 650)), tries=3):
    """Max distance (0-1000 grid) between where the pointer was sent and where it shows up in the capture.

    Finds the cursor by differencing frames with the pointer parked just below each target vs on it, so no
    model call is needed (parking stays off screen corners: macOS hot corners trigger there). Each point takes
    the best of a few tries: a real mapping error is consistent, a window animating nearby is not."""
    settled(kvm, timeout=2)
    errors = []
    for nx, ny in points:
        best = None
        for _ in range(tries):
            tip = cursor_tip(kvm, nx, ny)
            if tip is not None:
                err = max(abs(tip[0] - nx), abs(tip[1] - ny))
                best = err if best is None else min(best, err)
                if best <= 10:
                    break
        if best is None:
            return None
        errors.append(best)
    return round(max(errors))


def first_contact(kvm, vlm, log=print, usb_timeout=120, signal_timeout=45):
    t0 = time.monotonic()
    report = {"ok": False}

    def mark(key):
        report[key] = round(time.monotonic() - t0, 2)

    health = wait_for(lambda: (lambda h: h if h.get("usb") == "configured" else None)(kvm.health()), usb_timeout)
    if not health:
        report["problem"] = "no USB host: plug the KVM's USB-C into the target computer"
        return report
    mark("usb_s")
    if wait_for(lambda: frame(kvm), signal_timeout) is None:
        report["problem"] = "no HDMI signal: connect the target's video output to the KVM and wake the computer"
        return report
    mark("video_s")
    img = settled(kvm, timeout=4)   # ignore a stale frame from before the target (re)configured the display
    if img is not None and awaiting_choice(img):
        # macOS shows "What do you want to show on ai-kvm?" with Entire Screen / Start Mirroring as the default
        log("display waiting for a choice: pressing Return (Entire Screen / Start Mirroring)")
        kvm.actions([{"type": "key", "combo": "enter"}])
        report["accepted_mirror_dialog"] = True
        img = wait_for(lambda: (lambda f: f if f is not None and not awaiting_choice(f) else None)(frame(kvm)), 10)
        if img is None:
            report["problem"] = "display still not configured after accepting the mirroring dialog"
            return report
        mark("mirroring_s")
        img = settled(kvm, timeout=4) or img   # judge the finished mirror setup, not the switch animation
    if img is not None and not pillarboxed(img):
        # mirroring with the KVM as master: the Mac's own panel is running at the KVM's 720p
        try:
            restored = optimize_for_mac(kvm, vlm, log)
        except RuntimeError as e:
            log(f"  {e}")
            kvm.actions([{"type": "key", "combo": "esc"}])   # close any menu left open by a failed step
            restored = False
        if not restored:
            report["problem"] = "could not set Displays > Optimize for to this Mac"
            return report
        report["native_resolution_restored"] = True
        mark("restored_s")
    img = frame(kvm)
    if img is None:
        report["problem"] = "video lost"
        return report
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
