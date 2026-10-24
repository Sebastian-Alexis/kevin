"""One step of computer use at a time, with the rails of agent.run, for planners that call it as a tool (Droid).

    ex = Executor(kvm, vlm, grounder=..., verifier=...)
    screen = ex.screen()                       # the current frame, cropped to the target's picture
    report = ex.step(["click Submit", "key enter"], expect="a green 'Saved' banner")

`step` grounds targets named without "@x,y" (Holo4), rejects off-screen points and keystrokes that have nowhere
to go, gates Enter after typed text on where the text landed, settles on the screen after acting, and checks the
expectation with the cheap verifier. Everything it did or refused goes into the returned report (and the event
log), so the caller can show its work.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from .kvm import PLACEHOLDER_SIZE, content_rect
from .run import (CLICKS, FOCUS_KEYS, ground, latest, leave_overview, open_app, segments, split_actions, survey,
                  typed_where_wanted, unfocused_keys, yes)


class Executor:
    def __init__(self, kvm, vlm, grounder=None, verifier=None, os_name="macOS", settle_max=2.0, save_dir=None,
                 isolate=False, task="", log=None):
        self.kvm, self.vlm, self.grounder, self.verifier = kvm, vlm, grounder, verifier or vlm
        self.os_name, self.settle_max, self.save_dir, self.isolate, self.task = os_name, settle_max, save_dir, isolate, task
        self.log = log or (lambda event: None)
        self.frame = None          # the frame the last step settled on (None: capture a fresh one)
        self.focused = False       # the app open_app raised still has keyboard focus (no click since)
        self.primed = False        # the last batch ended with a focus key: its input has focus
        self.overview = False      # the OS window overview (survey) is open: the next batch picks a window or closes it
        self.n = 0
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

    def screen(self, fresh=False):
        """Current picture of the target (cropped to its content), or raises if the KVM has no video."""
        frame = None if fresh else self.frame
        frame = frame or self.kvm.screenshot()
        deadline = time.monotonic() + 6
        while frame.size == PLACEHOLDER_SIZE:   # NO SIGNAL placeholder
            if time.monotonic() > deadline:
                raise RuntimeError("no video from the computer (NO SIGNAL)")
            time.sleep(0.5)
            frame = self.kvm.screenshot()
        self.frame = frame
        return frame.crop(content_rect(frame))

    def save(self, img, name):
        if self.save_dir:
            path = os.path.join(self.save_dir, name)
            img.save(path, quality=85)
            return path
        return None

    def step(self, actions, expect=""):
        """Execute one batch of actions (the string grammar of agent.run). Returns a report dict."""
        self.n += 1
        n, t0 = self.n, time.monotonic()
        screen = self.screen()
        sw, sh = screen.size
        mac = self.os_name.lower().startswith("mac")
        report = {"step": n, "actions": list(actions), "expect": expect, "executed": False, "notes": []}
        try:
            parsed = split_actions(actions)
            for a in parsed:
                for kind, spec in a.items():
                    if isinstance(spec, dict) and spec.get("point_2d") is not None:
                        spec["pixels"] = True   # points the planner wrote itself are pixels of the screenshot it saw
                if "scroll" in a and a["scroll"].get("point_2d") is None:
                    a["scroll"]["point_2d"] = [500, 500]   # a scroll just needs the pointer over the page
            specs = [spec for a in parsed for kind, spec in a.items() if kind in CLICKS and spec.get("point_2d") is None]
            if specs and not self.grounder:
                raise ValueError(f"no coordinates for {[s['target'] for s in specs]} and no grounder configured")
            if specs:   # all of a batch's targets at once: vLLM batches the requests
                t_g = time.monotonic()
                with ThreadPoolExecutor(len(specs)) as ex:
                    for spec, pt in zip(specs, ex.map(lambda s: ground(self.grounder, screen, s["target"]), specs)):
                        spec["point_2d"] = pt
                report["grounded"] = {s["target"]: s["point_2d"] for s in specs}
                report["ground_s"] = round(time.monotonic() - t_g, 2)
            off = []
            for a in parsed:
                for spec in a.values():
                    if isinstance(spec, dict) and "point_2d" in spec:
                        if spec["point_2d"] is None:
                            off.append(f"{spec.get('target', '?')} (not found on screen)")
                        elif isinstance(spec["point_2d"], list) and len(spec["point_2d"]) == 2:
                            x, y = spec["point_2d"]
                            lim = (sw, sh) if spec.get("pixels") else (1000, 1000)
                            if not (0 <= x <= lim[0] and 0 <= y <= lim[1]):
                                off.append(f"{spec.get('target', '?')} @{x:.0f},{y:.0f}")
                            elif spec.get("pixels"):
                                spec["point_2d"] = [round(x * 1000 / sw), round(y * 1000 / sh)]
                if "scroll" in a and mac:   # natural scrolling: +ticks moves the page down
                    a["scroll"]["ticks"] = -int(a["scroll"].get("ticks", -3))
            if off:
                report["refused"] = f"off-screen or unfound point(s) {off}; the picture is {sw}x{sh}"
                return self._finish(report, t0)
            if not self.isolate and unfocused_keys(parsed, self.focused, self.primed):
                report["refused"] = ("nothing was typed: no text field has focus. Begin with the shortcut that focuses "
                                     "the input (e.g. cmd+t, cmd+l) or a click on it, then type")
                return self._finish(report, t0)
            plan, dropped = segments(parsed)
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as e:
            report["refused"] = f"unusable action(s): {e}"
            return self._finish(report, t0)
        if dropped:
            report["notes"].append(f"{dropped} later action(s) skipped: open_app/survey ends the batch")
        blocked, settle_cap, self.frame = False, self.settle_max, None
        if self.overview:
            leave_overview(self.kvm, parsed)
            self.overview = False
        for kind, arg in plan:
            if kind == "hid":
                if arg and arg[-1]["type"] == "wait" and arg[-1] is plan[-1][1][-1]:
                    settle_cap, arg = max(settle_cap, arg[-1]["ms"] / 1000), arg[:-1]
                self.kvm.actions(arg)
                self.focused = self.focused and not any(a["type"] == "click" for a in arg)
            elif kind == "gate":
                fits, holder = typed_where_wanted(self.kvm, self.vlm, self.task, arg)
                if not fits:
                    report["notes"].insert(0, f'Enter NOT pressed: "{arg}" landed in: {holder}, not an input that fits '
                                              'the task. Move the keyboard focus into the right input first')
                    blocked = True
                    break
            elif kind == "survey":
                self.frame = survey(self.kvm, self.os_name)
                self.overview = True
                report["notes"].insert(0, "survey: the screenshot is the overview of every open window; click the "
                                          "window of the app already in use to bring it to the front")
            else:
                self.focused, note, self.frame = open_app(self.kvm, self.vlm, arg, self.os_name, hide=self.isolate)
                report["notes"].insert(0, note)
        report["executed"] = True
        tail = plan[-1][1][-1] if plan and plan[-1][0] == "hid" and not blocked else {}
        self.primed = tail.get("type") == "key" and tail["combo"].lower().replace(" ", "") in FOCUS_KEYS
        if plan and self.frame is None:
            self.frame = latest(self.kvm, timeout=settle_cap, interval=0.0, tolerance=2.0, first_delay=0.1)
        after = self.screen()
        report["after"] = self.save(after, f"step{n:02d}.jpg")
        if expect and not blocked:
            t_v = time.monotonic()
            report["verified"] = yes(self.verifier, after, f"Does this screenshot show the following? {expect}")
            report["verify_s"] = round(time.monotonic() - t_v, 2)
        return self._finish(report, t0)

    def _finish(self, report, t0):
        report["seconds"] = round(time.monotonic() - t0, 2)
        self.log(report)
        return report
