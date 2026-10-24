"""Computer-use loop: KVM screenshot -> local VLM -> USB HID actions, until done.

    python3 -m agent "open a new browser tab and search for raspberry pi"

One model call per turn. The model names each click target and gives its point in Qwen's native
point_2d format (0-1000 over the screen picture), which grounds as precisely as a separate
"Locate X" query at half the latency.
"""
import argparse
import json
import os
import re
import sys
import time

from .kvm import KVM, content_rect, to_hid
from .vlm import VLM, load_key

SYSTEM = "You are the vision model of an agent that controls a computer through its USB keyboard and mouse."

PLAN = """Task: %(task)s
Target OS: %(os)s
Previous turns:
%(past)s
Reply with ONLY one JSON object, no prose:
{"observe": "<=8 words: what changed / what you see", "actions": [{...}, {...}], "done": false}
Each action is its own object, e.g. [{"click": {...}}, {"type": "hello"}, {"key": "enter"}].
Actions (point_2d = [x, y] center of the element, normalized 0-1000; target = short unique description):
 {"click": {"target": "address bar", "point_2d": [x, y]}} (same shape for "double_click" and "right_click")
 {"type": "text"} types into whatever has keyboard focus
 {"key": "cmd+l"} key combo of cmd ctrl alt shift + a-z 0-9 enter esc tab backspace delete space up down left right home end pageup pagedown f1-f12
 {"scroll": {"target": "page body", "point_2d": [x, y], "ticks": -5}} negative ticks scroll down
 {"wait": ms}
Rules:
- Keystrokes go to the focused window. Before any "type" or "key", click the exact field or window you mean in the same turn.
- Never type into terminals, chats or apps the task does not name.
- Only target things visible in this screenshot; for anything not visible yet, end the turn and look again.
- If the last turn did not work, do something different instead of repeating it.
- Batch only what you can see (e.g. click a visible field, type, enter).
Task complete: {"observe": "...", "actions": [], "done": true}. Impossible: {"observe": "...", "actions": [], "fail": "reason"}."""

CLICKS = ("click", "double_click", "right_click")


def parse(text):
    """First balanced JSON object in the model reply (tolerates ```json fences)."""
    start = text.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in reply: {text[:200]!r}")
    depth = 0
    for i in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return json.loads(text[start:i + 1])
    raise ValueError(f"unterminated JSON in reply: {text[:200]!r}")


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


def split_actions(actions):
    """Split objects holding several actions ({"click": ..., "type": ..., "key": ...}) in written order."""
    out = []
    for a in actions:
        out += [{k: v} for k, v in a.items()] if isinstance(a, dict) else [a]
    return out


def unfocused_keys(actions):
    """True if a keyboard action comes before any click in the batch (it would hit whatever window has focus)."""
    for a in actions:
        if any(k in a for k in CLICKS):
            return False
        if "type" in a or "key" in a:
            return True
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
            out += [{"type": "move", "x": max(0.0, fx - 0.004), "y": fy}, {"type": "wait", "ms": 40},
                    {"type": "move", "x": fx, "y": fy}, {"type": "wait", "ms": 80},
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


def run(task, kvm, vlm, os_name="macOS", max_steps=20, settle=0.3, dry_run=False, save_dir=None, log=print):
    history, batches = [], []
    for step in range(1, max_steps + 1):
        t0 = time.monotonic()
        frame = kvm.screenshot()
        rect = content_rect(frame)
        screen = frame.crop(rect)
        if save_dir:
            screen.save(os.path.join(save_dir, f"step{step:02d}.jpg"), quality=85)
        image = vlm.image_part(screen)
        t_cap = time.monotonic() - t0
        past = "\n".join(f"{i}. {h}" for i, h in enumerate(history, 1)) or "(none)"
        reply, usage, t_plan = vlm.chat([{"role": "system", "content": SYSTEM},
                                         {"role": "user", "content": [image, {"type": "text", "text": PLAN % {
                                             "task": task, "os": os_name, "past": past}}]}], 200)
        try:
            decision = parse(reply)
            actions = split_actions(decision.get("actions", []))
            observe = str(decision.get("observe", ""))[:120]
            summary = json.dumps(actions, separators=(",", ":"))
            if unfocused_keys(actions):
                history.append(f"saw: {observe} | REJECTED, keyboard action without first clicking its target: {summary}")
                log(f"step {step}: plan {t_plan:.2f}s rejected unfocused keys -> {summary}")
                continue
            if actions and batches[-2:] == [summary, summary]:
                log(f"step {step}: plan {t_plan:.2f}s stopping, same batch three times -> {summary}")
                return {"done": False, "fail": "stuck repeating the same actions", "steps": step}
            pi_actions = hid_actions(actions)
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as e:
            history.append(f"invalid reply ignored ({e})")
            log(f"step {step}: unusable reply ({e}) {reply[:120]!r}")
            continue
        t1 = time.monotonic()
        if pi_actions and not dry_run:
            kvm.actions(pi_actions)
        t_exec = time.monotonic() - t1
        log(f"step {step}: capture {t_cap:.2f}s model {t_plan:.2f}s ({usage.get('prompt_tokens')}+"
            f"{usage.get('completion_tokens')} tok) exec {t_exec:.2f}s | {observe} -> {summary}")
        history.append(f"saw: {observe} | did: {summary}")
        batches.append(summary)
        if decision.get("done") or decision.get("fail"):
            return {"done": bool(decision.get("done")), "fail": decision.get("fail"), "steps": step}
        if pi_actions:
            time.sleep(settle)
    return {"done": False, "fail": "max steps reached", "steps": max_steps}


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
    p.add_argument("--settle", type=float, default=0.3, help="seconds to wait after actions before the next screenshot")
    p.add_argument("--save-dir")
    p.add_argument("--dry-run", action="store_true", help="ask the model but do not send HID actions")
    a = p.parse_args(argv)
    if a.save_dir:
        os.makedirs(a.save_dir, exist_ok=True)
    vlm = VLM(a.llm, a.model, key=load_key(a.key_file), max_side=a.max_side)
    result = run(a.task, KVM(a.kvm), vlm, a.os_name, a.max_steps, a.settle, a.dry_run, a.save_dir)
    print(json.dumps(result))
    return 0 if result["done"] else 1


if __name__ == "__main__":
    sys.exit(main())
