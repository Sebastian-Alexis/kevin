"""Computer-use loop: KVM screenshot -> local VLM -> USB HID actions, until done.

    python3 -m agent "open a new browser tab and search for raspberry pi"

One model call per turn. The model names each click target and gives its point in Qwen's native
point_2d format (0-1000 over the screen picture), which grounds as precisely as a separate
"Locate X" query at half the latency. Replies are constrained to TURN_SCHEMA, so they always parse.
open_app brings an app to the front through the OS launcher, checked with two small yes/no crops.
"""
import argparse
import json
import os
import re
import sys
import time

from PIL import ImageChops, ImageStat

from .kvm import KVM, PLACEHOLDER_SIZE, content_rect, mean_diff, settled, to_hid
from .vlm import VLM, load_key

SYSTEM = "You are the vision model of an agent that controls a computer through its USB keyboard and mouse."

# The rules sit after the screenshot in the user turn: moved into the (prefix-cached) system prompt they bought no
# measurable latency, and that layout is the one validated on live tasks.
PLAN = """Task: %(task)s
Target OS: %(os)s
Previous turns:
%(past)s
Reply with ONLY one JSON object, no prose:
{"plan": "<=80 words: possible groupings of the items, the surest one and why, and the move this turn makes", "observe": "<=8 words: what changed / what you see", "actions": ["...", "..."], "done": false}
Each action is one short string:
 "click <target> @x,y"   (also "double_click", "right_click"; x,y = center of the element, %(coords)s; target = short unique description)
 "type <text>"           types into whatever has keyboard focus
 "key cmd+l"             key combo of cmd ctrl alt shift + a-z 0-9 enter esc tab backspace delete space up down left right home end pageup pagedown f1-f12
 "scroll <target> @x,y -5"   negative = down
 "wait <ms>"             only for things that take longer than 2 s (the next screenshot is taken once the screen has settled anyway)
 "open_app <name>"       brings that app to the front, starting it if needed; ends the turn
Rules:
- Keystrokes go to the focused window. Right after open_app the app's shortcuts work directly (e.g. cmd+t, cmd+l), but text needs an input with focus first (that shortcut or a click on it). Otherwise, before any "type" or "key", click the exact field or window you mean in the same turn.
- Windows overlap and look alike: whenever you are not sure the task's app is the frontmost window, open_app it.
- Do every step the task names, in order; never skip one (e.g. open the new tab it asks for before searching).
- Never type into terminals, chats or apps the task does not name.
- Only target things visible in this screenshot; for anything not visible yet, end the turn and look again.
- If the last turn did not work, do something different instead of repeating it.
- Batch only what you can see (e.g. click a visible field, type, enter), and finish the move in the same batch: when a confirm/submit/OK button is visible, click it too.
Task complete: {"plan": "...", "observe": "...", "actions": [], "done": true}. Impossible: {"plan": "...", "observe": "...", "actions": [], "fail": "reason"}."""

CLICKS = ("click", "double_click", "right_click")
HISTORY_TURNS = 8   # turns of history shown to the model


# Replies are constrained to this (vLLM structured outputs): free-form replies dropped a brace in ~1 of 8 turns.
# Actions are one-line strings (see PLAN): ~3x fewer output tokens than one object per action, and output tokens
# are what a step's model time is made of.
TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {"type": "string"},
        "observe": {"type": "string"},
        "actions": {"type": "array", "items": {"type": "string"}},
        "done": {"type": "boolean"},
        "fail": {"type": "string"}},
    "required": ["plan", "observe", "actions", "done"],
    "additionalProperties": False}


def parse(text):
    """The first JSON object in a model reply: schema-constrained replies are exactly one, free-form ones may wrap
    it in ```json fences. raw_decode stops at the object's end and treats braces inside strings as text."""
    start = text.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in reply: {text[:200]!r}")
    return json.JSONDecoder().raw_decode(text, start)[0]


def point(text):
    """(x, y) on the 0-1000 grid from a grounding reply: point_2d, or the center of a bbox_2d.
    Tolerates the model's occasional malformed brackets, e.g. {"point_2d": [899, 435}."""
    text = re.sub(r"\w*_2d", "", text)          # the "2" in point_2d / bbox_2d is not a coordinate
    start = text.find("[")
    if start < 0:
        raise ValueError(f"no point in grounding reply: {text[:120]!r}")
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text[start:])]
    if len(nums) >= 4:
        return (nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2
    if len(nums) >= 2:
        return nums[0], nums[1]
    raise ValueError(f"no point in grounding reply: {text[:120]!r}")


ACTION_RE = re.compile(r"^\s*(click|double_click|right_click|scroll)\s+(.*?)\s*@\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*(-?\d+)?\s*$", re.S)


def parse_action(s):
    """One action string -> the dict form the rails and executor work on. Dicts (older replies) pass through."""
    if isinstance(s, dict):
        return [{k: v} for k, v in s.items()]
    s = str(s).strip()
    m = ACTION_RE.match(s)
    if m:
        kind, target, x, y, ticks = m.groups()
        spec = {"target": target.strip(), "point_2d": [float(x), float(y)]}
        if kind == "scroll":
            spec["ticks"] = int(ticks) if ticks else -3
        return [{kind: spec}]
    verb, _, rest = s.partition(" ")
    verb = verb.lower()
    if verb == "type":
        return [{"type": rest}]
    if verb == "key":
        return [{"key": rest.strip()}]
    if verb == "wait":
        return [{"wait": int(float(rest.strip() or 0))}]
    if verb == "open_app":
        return [{"open_app": rest.strip()}]
    raise ValueError(f"unknown action {s!r}")


def split_actions(actions):
    """Reply actions (strings, or objects from older replies) -> one dict per action, in written order."""
    out = []
    for a in actions:
        out += parse_action(a)
    return out


# Keys that put keyboard focus into an input (new tab/window, location, find, quick switcher).
FOCUS_KEYS = {f"{m}+{k}" for m in ("cmd", "ctrl") for k in "tlnfk"}


def unfocused_keys(actions, focused=False, primed=False):
    """True if keystrokes would go to whatever happens to have focus. Keys need a click (or open_app) before them in
    the batch unless the app open_app raised still has focus. Typed text needs a click or a focus key (FOCUS_KEYS)
    before it, in this batch or ending the previous one (primed): letters typed into a page fire its one-key
    shortcuts."""
    for a in actions:
        if "open_app" in a or any(k in a for k in CLICKS):
            return False
        if "type" in a and not primed:
            return True
        if "key" in a:
            if not (focused or primed):
                return True
            primed = str(a["key"]).lower().replace(" ", "") in FOCUS_KEYS
    return False


def spec_point(spec):
    """(target, (x, y)) from {"target": ..., "point_2d": [x, y]} (a bbox counts by its center) or a bare [x, y]."""
    if isinstance(spec, dict):
        return str(spec.get("target", "")), point(json.dumps(spec.get("point_2d", spec.get("bbox_2d"))))
    if isinstance(spec, list):
        return "", point(json.dumps(spec))
    raise ValueError(f"expected a target with point_2d, got {spec!r}")


def hid_actions(actions):
    """Model actions -> Pi API actions. Clicks hover first: web apps ignore a click without a prior move."""
    out = []
    for a in actions:
        kind = next((k for k in CLICKS if k in a), None)
        if kind:
            _, (x, y) = spec_point(a[kind])
            fx, fy = to_hid(x, y)
            out += [{"type": "move", "x": max(0.0, fx - 0.004), "y": fy}, {"type": "wait", "ms": 20},
                    {"type": "move", "x": fx, "y": fy}, {"type": "wait", "ms": 40},
                    {"type": "click", "button": "right" if kind == "right_click" else "left",
                     "count": 2 if kind == "double_click" else 1}]
        elif "type" in a:
            out.append({"type": "type", "text": str(a["type"])})
        elif "key" in a:
            out.append({"type": "key", "combo": str(a["key"])})
        elif "scroll" in a:
            spec = a["scroll"]
            ticks = spec.get("ticks", -3) if isinstance(spec, dict) else spec[-1]
            _, (x, y) = spec_point(spec if isinstance(spec, dict) else spec[:2])
            fx, fy = to_hid(x, y)
            out.append({"type": "scroll", "x": fx, "y": fy, "ticks": int(ticks)})
        elif "wait" in a:
            out.append({"type": "wait", "ms": min(5000, int(a["wait"]))})
        else:
            raise ValueError(f"unknown action {a}")
    return out


def segments(actions):
    """Model actions -> ([("hid", pi_actions) | ("gate", typed text) | ("open_app", name), ...], number dropped).
    An Enter after typed text is gated: the text must be seen where the task wants it before Enter submits it (a
    chat box that happened to have focus would send it). open_app ends the batch: keys sent while an app is still
    launching would land in the previously active app."""
    out, pending, typed = [], [], None

    def flush():
        if pending:
            out.append(("hid", hid_actions(pending)))
            pending.clear()
    for i, a in enumerate(actions):
        if "open_app" in a:
            flush()
            return out + [("open_app", str(a["open_app"]))], len(actions) - i - 1
        if "key" in a and typed is not None and str(a["key"]).lower().split("+")[-1] in ("enter", "return"):
            flush()
            out.append(("gate", typed))
            typed = None
        elif "type" in a:
            typed = str(a["type"])
        pending.append(a)
    flush()
    return out, 0


GATE_SCHEMA = {"type": "object", "properties": {"input": {"type": "string"}, "fits": {"type": "boolean"}},
               "required": ["input", "fits"], "additionalProperties": False}


def typed_where_wanted(kvm, vlm, task, text):
    """Where did the text just typed land, and is that where the task wants it? Returns (fits, kind of input). The
    model names the input before judging it, on the whole screen at full size (at 960 px it took text that went
    nowhere for text in place). An unreadable answer counts as not fitting: Enter is not pressed."""
    frame = latest(kvm, timeout=1.0, interval=0.1, tolerance=1.5, first_delay=0.1)
    reply, _, _ = vlm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": [
        vlm.image_part(frame.crop(content_rect(frame))), {"type": "text", "text": (
            f'Task: "{task}". The agent just typed "{text}" and will press Enter next, which submits it wherever it '
            'is. Find that text on the screen. Reply JSON: {"input": "<the kind of input holding the text, e.g. '
            'browser address bar, web search box, chat message box, terminal, document>", "fits": <true only if the '
            'task asks for the text to go into exactly that kind of input>}')}]}], 80, GATE_SCHEMA)
    try:
        answer = parse(reply)
    except ValueError:
        return False, "unknown (unreadable check)"
    return answer.get("fits") is True, str(answer.get("input", ""))[:60]


def yes(vlm, img, question):
    """Yes/no question about an image; cropping to the region that holds the answer keeps it at ~0.4 s."""
    text, _, _ = vlm.chat([{"role": "system", "content": SYSTEM},
                           {"role": "user", "content": [vlm.image_part(img),
                                                        {"type": "text", "text": question + " Answer only yes or no."}]}], 4)
    return text.strip().lower().startswith("yes")


MENU_BAR_LEFT = (0, 0, 0.32, 0.06)   # the Apple logo and the bold name of the active app


def reveal_menu_bar(kvm):
    """Touch the top edge (and wiggle) so an auto-hidden menu bar slides in."""
    kvm.actions([{"type": "move", "x": 0.5, "y": 0.02}, {"type": "wait", "ms": 40},
                 {"type": "move", "x": 0.5, "y": 0.0}, {"type": "wait", "ms": 40},
                 {"type": "move", "x": 0.52, "y": 0.0}, {"type": "wait", "ms": 450}])


def ask_yes(kvm, vlm, question, box=None, scale=2):
    """Yes/no question about the screen. box=(x0, y0, x1, y1) as fractions crops (and upscales) the region that
    holds the answer: a menu-bar strip is ~130 image tokens and answers in ~0.4 s instead of ~0.9 s."""
    img = kvm.screenshot()
    if img.size == PLACEHOLDER_SIZE:
        return False
    img = img.crop(content_rect(img))
    if box:
        w, h = img.size
        img = img.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))
        img = img.resize((img.width * scale, img.height * scale))
    return yes(vlm, img, question)


def latest(kvm, **kw):
    """settled() frame, or the current one (e.g. NO SIGNAL) when no picture settled."""
    return settled(kvm, **kw) or kvm.screenshot()


def changed_box(a, b, threshold=40, pad=16):
    """Padded bounding box of where frame b visibly differs from frame a, or None."""
    if a.size != b.size:
        return 0, 0, b.width, b.height
    box = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > threshold else 0).getbbox()
    if box is None:
        return None
    return max(0, box[0] - pad), max(0, box[1] - pad), min(b.width, box[2] + pad), min(b.height, box[3] + pad)


def await_change(kvm, ref, box, timeout, min_px=1500, threshold=40, interval=0.05):
    """First frame in which at least min_px pixels of the box region visibly differ from ref's, else None at
    timeout. Waiting for the expected change, not merely for two equal frames, keeps a UI that is slow to react
    from being judged before it shows up. An absolute count, because a dark menu opening on a dark page changes
    a small share of a large region."""
    deadline = time.monotonic() + timeout
    area = (box[2] - box[0]) * (box[3] - box[1])
    while True:
        cur = kvm.screenshot()
        if cur.size != ref.size:
            return cur
        diff = ImageChops.difference(ref.crop(box), cur.crop(box)).convert("L")
        if ImageStat.Stat(diff.point(lambda v: 255 if v > threshold else 0)).mean[0] / 255 * area >= min_px:
            return cur
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


LAUNCHER_OPEN = ("Is an app launcher search window (Spotlight, Raycast, Alfred, or a Start menu search) open in "
                 "this image, with an empty search field ready for typing?")


APP_SCHEMA = {"type": "object", "properties": {"app": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
              "required": ["app"], "additionalProperties": False}


def task_app(vlm, task):
    """The application a task names ("In Safari, ..." -> "Safari"), else None. Text only, ~0.3 s."""
    text, _, _ = vlm.chat([{"role": "user", "content": (
        'Which application does this computer-use task name? Reply only JSON with the app name as the task gives it.\n'
        'Examples: "In the Helium web browser, open a new tab" -> {"app": "Helium"}; "In System Settings, turn on '
        'Dark Mode" -> {"app": "System Settings"}; "Reply to Bob in Slack" -> {"app": "Slack"}; "Search the web for '
        'the weather" -> {"app": null}; "Close the dialog on screen" -> {"app": null}.\n'
        f'Task: "{task}"')}], 30, APP_SCHEMA)
    app = parse(text).get("app")
    return app.strip() if isinstance(app, str) and app.strip() else None


def open_app(kvm, vlm, name, os_name="macOS"):
    """Bring an app to the front, starting it if needed, through the OS launcher: cmd+space on macOS (Spotlight, or
    Raycast/Alfred when they own it), the bare Windows/Super key elsewhere. Nothing is typed until a launcher is
    seen open (only what changed is asked about), and Enter is pressed only when its top result is that app.
    On macOS the menu bar must then name the app as the active one (a cold start leaves the previous app active for
    seconds) before every other app is hidden (Hide Others), so stray clicks and keys cannot reach them; show_all()
    brings them back. Returns (focused: the app is verified active, note for the model, latest frame)."""
    mac = os_name.lower().startswith("mac")
    fast = {"interval": 0.1, "tolerance": 1.5}
    active = f'Is "{name}" (or a short form of that name) the bold active-app name right of the Apple logo?'

    def hide_others():
        kvm.actions([{"type": "key", "combo": "alt+cmd+h"}])
        return True, (f"open_app {name}: done, the menu bar shows {name} as the active app; other apps are hidden. "
                      "No text field has focus yet: begin the batch with the app's shortcut for the input (e.g. "
                      "cmd+t, cmd+l) or a click on it, then type in the same batch"), \
            latest(kvm, timeout=1.5, first_delay=0.15, **fast)
    # Already the active app (e.g. the previous task used it): one menu-bar look instead of the launcher round trip
    if mac:
        reveal_menu_bar(kvm)
        if ask_yes(kvm, vlm, active, MENU_BAR_LEFT):
            return hide_others()
    before = kvm.screenshot()
    w, h = before.size
    kvm.actions([{"type": "key", "combo": "cmd+space" if mac else "win"}])
    # Launchers open mid-screen. One that is slow to appear must not be judged absent: the next attempt's key press
    # would toggle it shut and type into the app behind it.
    if await_change(kvm, before, (int(w * 0.35), 0, int(w * 0.65), h), timeout=1.5) is None:
        return False, f"open_app {name}: no app launcher opened, nothing typed", kvm.screenshot()
    opened = latest(kvm, timeout=1.5, **fast)
    box = changed_box(before, opened)
    if box is None or not yes(vlm, opened.crop(box), LAUNCHER_OPEN):
        kvm.actions([{"type": "key", "combo": "esc"}])   # close what did open, e.g. a launcher judged mid-animation
        return False, f"open_app {name}: no app launcher opened, nothing typed", latest(kvm, timeout=1.0, **fast)
    kvm.actions([{"type": "type", "text": name}])
    await_change(kvm, opened, box, timeout=1.0)
    listed = latest(kvm, timeout=1.5, **fast)
    box = changed_box(before, listed) or box
    if not yes(vlm, listed.crop(box), f'Is the first (selected) result in this launcher the application named "{name}"?'):
        cur = listed
        for _ in range(2):   # Raycast/Alfred close on the first Esc; Spotlight's first Esc only clears the field
            kvm.actions([{"type": "key", "combo": "esc"}])
            await_change(kvm, cur, box, timeout=0.6)
            cur = latest(kvm, timeout=1.0, **fast)
            if cur.size == before.size and mean_diff(cur.crop(box), before.crop(box)) < 4:
                break
        return False, f'open_app {name}: the launcher has no app named "{name}"', cur
    kvm.actions([{"type": "key", "combo": "enter"}])
    if not mac:   # no menu bar to confirm it with: keys keep needing a click on their target
        return False, f"open_app {name}: started it; click its window before typing", latest(kvm, timeout=3.0, **fast)
    reveal_menu_bar(kvm)
    deadline = time.monotonic() + 8
    while not ask_yes(kvm, vlm, active, MENU_BAR_LEFT):
        if time.monotonic() > deadline:
            return False, (f"open_app {name}: started it, but the menu bar never showed it as the active app; "
                           "other apps left visible"), kvm.screenshot()
        time.sleep(0.3)
    return hide_others()


def show_all(kvm, vlm):
    """Bring back the apps open_app hid: the active app's "Show All" menu item, run through Help > Search (cmd+?)
    without touching the mouse (in the app menu, "Show All" sits just above "Quit"). Return is only pressed once
    the "Show All" result row is seen selected, and the Help menu must close after it. True if it ran."""
    fast = {"interval": 0.1, "tolerance": 1.5}
    before = kvm.screenshot()
    w, h = before.size
    # The menu bar and the Help menu below it (dark on a dark page: few pixels change, see await_change)
    menus = (0, 0, w, h // 2)

    def menu_top(img):   # menu bar, Help search field and the first results: enough to judge, few image tokens
        x0, y0, x1, y1 = content_rect(img)
        return img.crop((x0, y0, x1, y0 + int((y1 - y0) * 0.2)))
    reveal_menu_bar(kvm)   # with the bar auto-hidden the Help shortcut is swallowed until the bar is on screen
    kvm.actions([{"type": "key", "combo": "cmd+shift+/"}])
    # Settles are capped where the menu renders at once: behind a page that keeps animating (streamed search
    # answers) a full-screen settle never converges and would cost its whole timeout each time.
    await_change(kvm, before, menus, timeout=1.5)
    opened = latest(kvm, timeout=0.6, **fast)
    if yes(vlm, menu_top(opened), "Is a Help menu with a search field open in this image?"):
        kvm.actions([{"type": "type", "text": "Show All"}])
        # The field may still hold the last search (results already listed: typing changes nothing visible) or the
        # index may be cold (results take ~1.5 s), so judge the outcome instead of waiting for a change: press
        # Down until "Show All" is the selected row. Three tries stay clear of "Show All Help Topics", the last row.
        for pause in (0.3, 0.7, 1.2):
            time.sleep(pause)
            kvm.actions([{"type": "key", "combo": "down"}])
            time.sleep(0.25)
            chosen = kvm.screenshot()
            if yes(vlm, menu_top(chosen), 'In the Help menu search results, is the "Show All" result row highlighted '
                                          '(selected)?'):
                kvm.actions([{"type": "key", "combo": "enter"}])
                # a greyed-out "Show All" (nothing hidden any more) ignores Return and leaves the menu open
                if await_change(kvm, chosen, menus, timeout=1.0) is not None:
                    return True
                break
    kvm.actions([{"type": "key", "combo": "esc"}, {"type": "wait", "ms": 150}, {"type": "key", "combo": "esc"}])
    return False


def run(task, kvm, vlm, os_name="macOS", max_steps=20, settle_max=2.0, dry_run=False, save_dir=None, log=print,
        coords="norm"):
    """Drive the computer until the model reports the task done or impossible. An app the task names is brought
    to the front before the first look (on macOS with the other apps hidden until the run ends)."""
    history, batches, frame = [], [], None
    mac = os_name.lower().startswith("mac")
    focused = hidden = False   # focused: the app open_app raised still has keyboard focus (no click since)
    primed = False             # the last executed batch ended with a focus key (cmd+t, cmd+l, ...): its input has focus
    try:
        app = None if dry_run else task_app(vlm, task)
        if app:
            t0 = time.monotonic()
            focused, note, frame = open_app(kvm, vlm, app, os_name)
            hidden = focused and mac
            history.append(note)
            log(f"{note} ({time.monotonic() - t0:.2f}s)")
        for step in range(1, max_steps + 1):
            t0 = time.monotonic()
            frame = frame or kvm.screenshot()   # the frame the last turn's actions settled on, else a fresh one
            deadline = time.monotonic() + 6
            while frame.size == PLACEHOLDER_SIZE:   # NO SIGNAL: points grounded on it would click anywhere
                if time.monotonic() > deadline:
                    return {"done": False, "fail": "no video from the computer (NO SIGNAL)", "steps": step}
                time.sleep(0.5)
                frame = kvm.screenshot()
            screen = frame.crop(content_rect(frame))
            frame = None
            if save_dir:
                screen.save(os.path.join(save_dir, f"step{step:02d}.jpg"), quality=85)
            image = vlm.image_part(screen)
            sw, sh = vlm.sent_size(screen)
            # Qwen grounds on a 0-1000 grid; Anthropic models answer in pixels of the picture they were given
            coord_text = ("normalized 0-1000" if coords == "norm"
                          else f"pixel coordinates in this {sw}x{sh} screenshot")
            t_cap = time.monotonic() - t0
            # The last turns only: on long tasks the prompt grew to ~7k tokens by turn 30, slowing every call
            recent = history[-HISTORY_TURNS:]
            omitted = len(history) - len(recent)
            past = ("\n".join(f"{i}. {h}" for i, h in enumerate(recent, len(history) - len(recent) + 1))
                    or "(none yet. If the task names an app, start with open_app for it.)")
            if omitted:
                past = f"({omitted} earlier turns omitted)\n" + past
            reply, usage, t_plan = vlm.chat([{"role": "system", "content": SYSTEM},
                                             {"role": "user", "content": [image, {"type": "text", "text": PLAN % {
                                                 "task": task, "os": os_name, "past": past, "coords": coord_text}}]}],
                                            1500, TURN_SCHEMA)
            try:
                decision = parse(reply)
                actions = split_actions(decision.get("actions", []))
                off = []
                for a in actions:
                    for spec in a.values():
                        if isinstance(spec, dict) and isinstance(spec.get("point_2d"), list) and len(spec["point_2d"]) == 2:
                            x, y = spec["point_2d"]
                            lim = (sw, sh) if coords != "norm" else (1000, 1000)
                            if not (0 <= x <= lim[0] and 0 <= y <= lim[1]):
                                off.append(f"{spec.get('target', '?')} @{x:.0f},{y:.0f}")
                            if coords != "norm":
                                spec["point_2d"] = [round(x * 1000 / sw), round(y * 1000 / sh)]   # ints: stable repeat check
                observe = str(decision.get("observe", ""))[:120]
                summary = json.dumps(actions, separators=(",", ":"))
                # repeat key: verbs + targets + coordinates to the nearest 20 units, so a re-aimed click on the same
                # thing (339,575 vs 339,576) still counts as the same batch; rejected batches count too
                key = json.dumps([{k: (v if not isinstance(v, dict) else {"t": v.get("target"), "p": [round(c / 20) for c in v.get("point_2d", [])]})
                                   for k, v in a.items()} for a in actions], separators=(",", ":"))
                repeated = actions and batches[-2:] == [key, key]
                batches.append(key)
                if repeated:
                    log(f"step {step}: model {t_plan:.2f}s stopping, same batch three times -> {summary}")
                    return {"done": False, "fail": "stuck repeating the same actions", "steps": step}
                if off:
                    # clamping would click the screen edge and waste the turn (and the model's guess count)
                    history.append(f"saw: {observe} | REJECTED, off-screen point(s) {off}: the picture is {sw}x{sh}, "
                                   "give coordinates inside it")
                    log(f"step {step}: model {t_plan:.2f}s rejected off-screen points {off}")
                    continue
                if unfocused_keys(actions, focused, primed):
                    history.append(f"saw: {observe} | REJECTED, nothing was typed: no text field has focus. Begin the "
                                   "batch with the shortcut that focuses the input (e.g. cmd+t, cmd+l) or a click on "
                                   f"it, then type: {summary}")
                    log(f"step {step}: model {t_plan:.2f}s rejected unfocused keys -> {summary}")
                    continue
                plan, dropped = segments(actions)
            except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as e:
                history.append(f"invalid reply ignored ({e})")
                log(f"step {step}: unusable reply ({e}) {reply[:120]!r}")
                continue
            done = decision.get("done") or decision.get("fail")
            notes = [f"{dropped} later action(s) skipped: open_app ends the turn"] if dropped else []
            t1 = time.monotonic()
            if not dry_run:
                blocked, settle_cap = False, settle_max
                for kind, arg in plan:
                    if kind == "hid":
                        # a trailing wait is the model's guess at how long the UI needs: the settle below watches
                        # the screen instead, so take it as the settle cap rather than sleeping it out blind
                        if arg and arg[-1]["type"] == "wait" and arg[-1] is plan[-1][1][-1]:
                            settle_cap, arg = max(settle_cap, arg[-1]["ms"] / 1000), arg[:-1]
                        kvm.actions(arg)
                        # a click can move keyboard focus anywhere: another window, the desktop
                        focused = focused and not any(a["type"] == "click" for a in arg)
                    elif kind == "gate":
                        fits, holder = typed_where_wanted(kvm, vlm, task, arg)
                        if not fits:
                            notes.insert(0, f'Enter NOT pressed: "{arg}" landed in: {holder}, not an input that fits '
                                            'the task, so the keyboard focus is somewhere else. Move it into the right '
                                            'input first (the app\'s shortcut for it, e.g. cmd+l or cmd+t in a '
                                            'browser, or click the input), then type again')
                            done, blocked = False, True
                            break
                    else:
                        focused, note, frame = open_app(kvm, vlm, arg, os_name)
                        hidden = hidden or (focused and mac)
                        notes.insert(0, note)
                tail = plan[-1][1][-1] if plan and plan[-1][0] == "hid" and not blocked else {}
                primed = tail.get("type") == "key" and tail["combo"].lower().replace(" ", "") in FOCUS_KEYS
                if plan and frame is None and not done:
                    # paced by the capture itself (~70 ms a frame at 14 fps): an instant UI change settles in ~0.25 s
                    frame = latest(kvm, timeout=settle_cap, interval=0.0, tolerance=2.0, first_delay=0.1)
            t_act = time.monotonic() - t1
            extra = "".join(f" | {n}" for n in notes)
            history.append(f"saw: {observe} | did: {summary}{extra}")
            log(f"step {step}: capture {t_cap:.2f}s model {t_plan:.2f}s ({usage.get('prompt_tokens')}+"
                f"{usage.get('completion_tokens')} tok) act+settle {t_act:.2f}s | {observe} -> {summary}{extra}")
            if done:
                return {"done": bool(decision.get("done")), "fail": decision.get("fail"), "steps": step}
        return {"done": False, "fail": "max steps reached", "steps": max_steps}
    finally:
        if hidden:
            t0 = time.monotonic()
            try:
                shown = show_all(kvm, vlm) or show_all(kvm, vlm)   # the Help shortcut is occasionally swallowed
            except Exception as e:   # tidying up must not replace the task result (or its exception)
                shown = f"failed ({e})"
            log(f"show all: {shown} ({time.monotonic() - t0:.2f}s)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m agent", description=__doc__.splitlines()[0])
    p.add_argument("task")
    p.add_argument("--kvm", default=os.environ.get("AIKVM_URL", "http://10.42.0.101:8080"))
    p.add_argument("--llm", default=os.environ.get("AIKVM_LLM_BASE", "http://127.0.0.1:8000/v1"))
    p.add_argument("--model", default=os.environ.get("AIKVM_LLM_MODEL", "qwen122"))
    p.add_argument("--key-file", default=os.environ.get("AIKVM_LLM_KEY_FILE", "/etc/gemma4-vllm-api-key"))
    p.add_argument("--os", default="macOS", dest="os_name")
    p.add_argument("--max-steps", type=int, default=20)
    p.add_argument("--max-side", type=int, default=1280, help="downscale screenshots so the longest side is at most this")
    p.add_argument("--coords", choices=["norm", "pixels"], default=None,
                   help="coordinate space the model answers in (default: pixels for claude/anthropic models, else norm)")
    p.add_argument("--settle-max", type=float, default=2.0,
                   help="after acting, wait for the screen to stop changing, at most this many seconds")
    p.add_argument("--save-dir")
    p.add_argument("--dry-run", action="store_true", help="ask the model but do not send HID actions")
    a = p.parse_args(argv)
    if a.save_dir:
        os.makedirs(a.save_dir, exist_ok=True)
    vlm = VLM(a.llm, a.model, key=load_key(a.key_file), max_side=a.max_side)
    coords = a.coords or ("pixels" if any(k in a.model.lower() for k in ("claude", "anthropic")) else "norm")
    result = run(a.task, KVM(a.kvm), vlm, a.os_name, a.max_steps, a.settle_max, a.dry_run, a.save_dir, coords=coords)
    print(json.dumps(result))
    return 0 if result["done"] else 1


if __name__ == "__main__":
    sys.exit(main())
