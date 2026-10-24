"""Source B: macOS accessibility-tree grounding. Exact labels for real native UI, read-only (never clicks).

Needs Screen Recording + Accessibility permission for the terminal running it
(System Settings > Privacy & Security). Two modes:

  # you drive: open menus, dialogs, settings panes, documents; it snapshots every 2s and skips near-duplicates
  python datagen/ax_grounding.py watch --minutes 20

  # unattended: activates each app in turn and snapshots whatever window it has open
  python datagen/ax_grounding.py apps --apps Finder,Safari,Notes,Calendar,Mail,"System Settings" --rounds 3

Output: data/ax/<shard>/ with source "ax". Spot-check with: python datagen/preview.py --source ax
"""
import argparse
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Shard, bbox_to1000, dhash, hamming  # noqa: E402

try:
    import ApplicationServices as AX
    import Quartz
    from AppKit import NSRunningApplication, NSWorkspace
except ImportError:
    sys.exit("macOS only: pip install pyobjc-framework-ApplicationServices pyobjc-framework-Quartz pyobjc-framework-Cocoa")

ROLES = {"AXButton": "button", "AXCheckBox": "checkbox", "AXRadioButton": "option", "AXPopUpButton": "pop-up menu",
         "AXMenuButton": "menu button", "AXMenuItem": "menu item", "AXMenuBarItem": "menu", "AXTextField": "field",
         "AXTextArea": "text area", "AXComboBox": "combo box", "AXLink": "link", "AXTab": "tab", "AXRow": "row",
         "AXCell": "item", "AXImage": "icon", "AXDockItem": "icon", "AXSlider": "slider", "AXDisclosureTriangle": "arrow",
         "AXIncrementor": "stepper", "AXSegmentedControl": "control", "AXStaticText": ""}
CLIP_ROLES = {"AXWindow", "AXScrollArea", "AXSheet", "AXDrawer", "AXMenu", "AXList", "AXGroup"}
MIN_SIZE = 6
MAX_NODES = 5000
TYPE_TEXT = ["meeting notes", "budget 2026", "hello world", "Cal Hacks", "groceries", "project plan",
             "screenshot", "invoice", "README", "weekly sync", "Berkeley", "flight confirmation"]


def attr(el, name):
    err, v = AX.AXUIElementCopyAttributeValue(el, name, None)
    return v if err == 0 else None


def frame(el):
    pos, size = attr(el, "AXPosition"), attr(el, "AXSize")
    if pos is None or size is None:
        return None
    _, p = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)
    _, s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
    return (p.x, p.y, p.x + s.width, p.y + s.height)


def intersect(a, b):
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return r if r[2] > r[0] and r[3] > r[1] else None


def text_of(v):
    if v is None:
        return ""
    s = " ".join(str(v).split())
    return s if 1 < len(s) <= 60 and any(c.isalnum() for c in s) else ""


def label_of(el, role):
    if role in ("AXTextField", "AXTextArea", "AXComboBox"):
        return text_of(attr(el, "AXPlaceholderValue")) or text_of(attr(el, "AXDescription")) or text_of(attr(el, "AXTitle"))
    lab = text_of(attr(el, "AXTitle")) or text_of(attr(el, "AXDescription"))
    if not lab and role in ("AXRow", "AXCell", "AXStaticText", "AXLink", "AXRadioButton", "AXCheckBox"):
        lab = text_of(attr(el, "AXValue")) if role == "AXStaticText" else ""
        for c in (attr(el, "AXChildren") or [])[:6]:
            if lab:
                break
            cr = attr(c, "AXRole")
            if cr in ("AXStaticText", "AXTextField"):
                lab = text_of(attr(c, "AXValue"))
            elif cr in ("AXCell", "AXGroup"):
                for cc in (attr(c, "AXChildren") or [])[:4]:
                    if attr(cc, "AXRole") == "AXStaticText":
                        lab = text_of(attr(cc, "AXValue"))
                        break
    return lab


def walk(root, screen, where):
    """Yield candidate elements under root, clipped to their scroll areas / windows."""
    out, stack, n = [], [(root, screen, (), 0)], 0
    while stack and n < MAX_NODES:
        el, clip, ancestors, depth = stack.pop()
        n += 1
        role = attr(el, "AXRole") or ""
        if attr(el, "AXHidden"):
            continue
        f = frame(el)
        vis = intersect(f, clip) if f else None
        if f and role in CLIP_ROLES:
            if vis is None:
                continue
            clip = vis
        if vis and role in ROLES and role != "AXStaticText":  # bare static text is rarely a click target
            w, h = vis[2] - vis[0], vis[3] - vis[1]
            if w >= MIN_SIZE and h >= MIN_SIZE and w * h < (screen[2] * screen[3]) * 0.25 and f == vis:
                lab = label_of(el, role)
                sub = attr(el, "AXSubrole") or ""
                if lab or sub == "AXSearchField":
                    out.append({"role": role, "subrole": sub, "label": lab, "box": vis, "ancestors": ancestors,
                                "where": where})
        if depth < 30:
            kids = attr(el, "AXChildren") or []
            anc = ancestors + (role,) if role in ("AXToolbar", "AXOutline", "AXTable", "AXTabGroup", "AXSheet") else ancestors
            for k in kids:
                stack.append((k, clip, anc, depth + 1))
    return out


def position_hint(box, screen):
    cx, cy = (box[0] + box[2]) / 2 / screen[2], (box[1] + box[3]) / 2 / screen[3]
    v = "top" if cy < 0.33 else "bottom" if cy > 0.67 else ""
    h = "left" if cx < 0.33 else "right" if cx > 0.67 else ""
    return f"{v} {h}".strip() or "middle"


def intent_for(el, app, screen, rng):
    role, lab, word = el["role"], el["label"], ROLES.get(el["role"], "")
    anc = el["ancestors"]
    if el["where"] == "dock":
        return rng.choice([f"Click the {lab} icon in the Dock", f"Open {lab} from the Dock"])
    if el["where"] == "menubar":
        return rng.choice([f"Click the '{lab}' menu in the menu bar", f"Open the {lab} menu"])
    if el["subrole"] == "AXSearchField" or (role == "AXTextField" and "search" in lab.lower()):
        return rng.choice(["Click the search field", f"Click the search field in {app}", f"Click the '{lab or 'Search'}' field"])
    if role in ("AXTextField", "AXTextArea", "AXComboBox"):
        return rng.choice([f"Click the '{lab}' field", f"Click into the {lab} {word}"])
    place = ""
    if "AXToolbar" in anc:
        place = " in the toolbar"
    elif "AXOutline" in anc and (el["box"][0] / screen[2]) < 0.35:
        place = f" in the {app} sidebar"
    elif "AXSheet" in anc:
        place = " in the dialog"
    opts = [f"Click the '{lab}' {word}{place}".replace("  ", " "), f"Click '{lab}'{place}"]
    if rng.random() < 0.3:
        opts.append(f"Click the '{lab}' {word} at the {position_hint(el['box'], screen)} of the screen")
    if rng.random() < 0.3:
        opts.append(f"In {app}, click the '{lab}' {word}")
    if role == "AXMenuItem":
        opts = [f"Click '{lab}' in the menu", f"Choose '{lab}' from the menu"]
    if role in ("AXRow", "AXCell"):
        opts = [f"Click '{lab}'{place}", f"Select '{lab}'{place}"]
    if role == "AXTab":
        opts = [f"Click the '{lab}' tab", f"Switch to the {lab} tab"]
    return rng.choice(opts).replace(" '' ", " ").strip()


def screen_bounds():
    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return (b.origin.x, b.origin.y, b.size.width, b.size.height)


def screenshot():
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        subprocess.run(["screencapture", "-x", "-m", "-t", "png", f.name], check=True)
        return Image.open(f.name).copy()


def app_elements(pid):
    return AX.AXUIElementCreateApplication(pid)


def snapshot(shard, rng, recent, args, stats):
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return
    app_name = front.localizedName()
    sx, sy, sw, sh = screen_bounds()
    screen = (0, 0, sw, sh)
    app = app_elements(front.processIdentifier())
    AX.AXUIElementSetMessagingTimeout(app, 1.0)

    img = screenshot()
    hsh = dhash(img)
    if any(hamming(hsh, h) <= args.dedupe for h in recent):
        stats["dupes"] += 1
        return
    recent.append(hsh)
    del recent[:-80]

    els = []
    win = attr(app, "AXFocusedWindow") or attr(app, "AXMainWindow")
    if win is not None:
        els += walk(win, screen, "window")
    for w in (attr(app, "AXWindows") or [])[:4]:  # other windows of the same app that may be visible
        if w != win and attr(w, "AXRole") == "AXWindow" and rng.random() < 0.5:
            els += walk(w, screen, "window")
    mb = attr(app, "AXMenuBar")
    if mb is not None:
        els += [e for e in walk(mb, screen, "menubar") if e["role"] == "AXMenuBarItem"]
        # an open menu shows up as AXMenu children of a menu bar item; walk() already clipped them
        els += [e for e in walk(mb, screen, "menu") if e["role"] == "AXMenuItem"]
    for dock in NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.dock"):
        if rng.random() < args.dock_rate:
            els += [e for e in walk(app_elements(dock.processIdentifier()), screen, "dock") if e["role"] == "AXDockItem"]
    # de-duplicate identical label+box
    uniq = {}
    for e in els:
        uniq.setdefault((e["label"], tuple(round(v) for v in e["box"])), e)
    els = list(uniq.values())
    if len(els) < 3:
        stats["empty"] += 1
        return

    w_px, h_px = img.size
    frame_path = shard.add_frame(img)
    for e in rng.sample(els, min(len(els), args.per_frame)):
        l, t, r, b = e["box"]
        box = bbox_to1000(l - sx, t - sy, r - sx, b - sy, sw, sh)  # AX is in global points; image is the main display
        shard.add_step(frame_path, intent_for(e, app_name, screen, rng),
                       {"action": "click", "x": (box[0] + box[2]) // 2, "y": (box[1] + box[3]) // 2},
                       bbox=box, app=app_name, os="macos", role=e["role"], label=e["label"], where=e["where"])
        stats["click"] += 1

    focused = attr(app, "AXFocusedUIElement")
    if focused is not None and attr(focused, "AXRole") in ("AXTextField", "AXTextArea", "AXComboBox") and rng.random() < 0.6:
        text = rng.choice(TYPE_TEXT)
        lab = label_of(focused, attr(focused, "AXRole")) or "text"
        shard.add_step(frame_path, rng.choice([f"Type '{text}'", f"Type '{text}' into the {lab} field"]),
                       {"action": "type", "text": text}, app=app_name, os="macos")
        stats["type"] += 1
    stats["frames"] += 1
    print(f"  {app_name}: {len(els)} elements, total {shard.n_steps} samples", flush=True)


def check_permissions():
    if not AX.AXIsProcessTrusted():
        sys.exit("Accessibility permission missing: System Settings > Privacy & Security > Accessibility > add your terminal")
    img = screenshot()
    if img.convert("L").getextrema()[1] < 10:
        sys.exit("screenshot is blank: grant Screen Recording permission to your terminal")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["watch", "apps"])
    ap.add_argument("--minutes", type=float, default=15, help="watch: how long to run")
    ap.add_argument("--interval", type=float, default=2.0, help="watch: seconds between snapshots")
    ap.add_argument("--apps", default="Finder,Safari,Notes,Calendar,Mail,System Settings,Music,Photos,Preview,Reminders,"
                                      "TextEdit,Maps,Messages,Terminal,App Store")
    ap.add_argument("--rounds", type=int, default=2, help="apps: passes over the app list")
    ap.add_argument("--settle", type=float, default=2.5, help="apps: seconds to wait after activating")
    ap.add_argument("--per-frame", type=int, default=20)
    ap.add_argument("--dock-rate", type=float, default=0.15)
    ap.add_argument("--dedupe", type=int, default=4, help="max dhash distance counted as duplicate")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    check_permissions()
    rng = random.Random(args.seed)
    stats = {"frames": 0, "click": 0, "type": 0, "dupes": 0, "empty": 0}
    recent = []
    appearance = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True).stdout.strip()
    with Shard("ax", mode=args.mode, machine=subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True,
               text=True).stdout.strip(), appearance=appearance or "Light") as shard:
        try:
            if args.mode == "watch":
                end = time.time() + args.minutes * 60
                print(f"watching for {args.minutes} min: drive your apps (menus, dialogs, settings panes, documents)")
                while time.time() < end:
                    snapshot(shard, rng, recent, args, stats)
                    time.sleep(args.interval)
            else:
                apps = [a.strip() for a in args.apps.split(",") if a.strip()]
                for _ in range(args.rounds):
                    rng.shuffle(apps)
                    for a in apps:
                        if subprocess.run(["open", "-a", a], capture_output=True).returncode != 0:
                            print(f"  ! can't open {a}")
                            continue
                        time.sleep(args.settle)
                        snapshot(shard, rng, recent, args, stats)
        except KeyboardInterrupt:
            pass
        print(f"wrote {shard.n_steps} samples to {shard.dir}: {stats}")


if __name__ == "__main__":
    main()
