"""Drive the KVM with H Company's Holo4 (local vLLM, :8010) in its native format.

    python3 -m agent.holo [--app Helium] [--think] "task"

Holo4 is a computer-use VLM (OSWorld-Verified 80%): a multi-turn tool-calling chat where every turn is a
screenshot wrapped in <observation> and the reply is one tool call (click/double_click/right_click/type/key/
scroll/answer) with coordinates on a 0-1000 grid. The last 3 screenshots stay in context; older ones are evicted.
Rails are shared with agent.run: the app the task names is raised and the others hidden, and shown again at the end.
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.request

from PIL import ImageChops, ImageStat

from .kvm import KVM, content_rect, frame, mean_diff, settled, to_hid
from .run import MENU_BAR_LEFT, open_app, show_all, task_app
from .vlm import VLM, load_key

TOOLS = [
    {"type": "function", "function": {"name": "click", "description": "Left-click at (x, y) coordinates", "parameters": {
        "type": "object", "properties": {"element": {"type": "string", "description": "Detailed description of the target UI element"},
                                         "x": {"type": "integer", "description": "X coordinate as integer in [0, 1000]"},
                                         "y": {"type": "integer", "description": "Y coordinate as integer in [0, 1000]"}},
        "required": ["element", "x", "y"]}}},
    {"type": "function", "function": {"name": "double_click", "description": "Double-click at (x, y) coordinates", "parameters": {
        "type": "object", "properties": {"element": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"}},
        "required": ["element", "x", "y"]}}},
    {"type": "function", "function": {"name": "right_click", "description": "Right-click at (x, y) coordinates", "parameters": {
        "type": "object", "properties": {"element": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"}},
        "required": ["element", "x", "y"]}}},
    {"type": "function", "function": {"name": "type", "description": "Type text with the keyboard into the focused element",
                                      "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "key", "description": "Press a key or key combination, e.g. 'Return', 'cmd+t'",
                                      "parameters": {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}}},
    {"type": "function", "function": {"name": "scroll", "description": "Scroll the mouse wheel at (x, y)", "parameters": {
        "type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                                         "direction": {"type": "string", "enum": ["up", "down"]}, "amount": {"type": "integer"}},
        "required": ["x", "y", "direction"]}}},
    {"type": "function", "function": {"name": "answer", "description": "Provide a final answer when the task is complete",
                                      "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}}},
]
KEYS = {"return": "enter", "escape": "esc", "command": "cmd", "super": "cmd", "option": "alt", "page_down": "pagedown",
        "page_up": "pageup", "backspace": "backspace"}


def changed_px(a, b, threshold=40):
    """Number of pixels that visibly differ between two frames (a toggled tile is ~5000; JPEG noise ~0)."""
    d = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > threshold else 0)
    return int(ImageStat.Stat(d).mean[0] / 255 * a.width * a.height)


def chat(url, key, model, messages, think, max_tokens):
    body = {"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "required", "max_tokens": max_tokens,
            "temperature": 0, "chat_template_kwargs": {"enable_thinking": think}}
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + (key or "none")})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=180) as r:
        out = json.load(r)
    msg = out["choices"][0]["message"]
    return msg, out.get("usage", {}), time.monotonic() - t0


def to_actions(name, args, os_name):
    """One Holo tool call -> Pi actions (clicks hover first, like agent.run)."""
    if name in ("click", "double_click", "right_click"):
        fx, fy = to_hid(float(args["x"]), float(args["y"]))
        return [{"type": "move", "x": max(0.0, fx - 0.004), "y": fy}, {"type": "wait", "ms": 20},
                {"type": "move", "x": fx, "y": fy}, {"type": "wait", "ms": 40},
                {"type": "click", "button": "right" if name == "right_click" else "left", "count": 2 if name == "double_click" else 1}]
    if name == "type":
        return [{"type": "type", "text": str(args.get("text", ""))}]
    if name == "key":
        combo = "+".join(KEYS.get(k.strip().lower(), k.strip().lower()) for k in str(args.get("keys", "")).split("+"))
        return [{"type": "key", "combo": combo}]
    if name == "scroll":
        fx, fy = to_hid(float(args["x"]), float(args["y"]))
        ticks = int(args.get("amount") or 3) * (-1 if args.get("direction") == "down" else 1)
        if os_name.lower().startswith("mac"):
            ticks = -ticks   # natural scrolling
        return [{"type": "scroll", "x": fx, "y": fy, "ticks": ticks}]
    raise ValueError(f"unknown tool {name}")


def run(task, kvm, url, key, model, rails_vlm, os_name="macOS", max_steps=30, think=False, dry_run=False, save_dir=None,
        app=None, log=print):
    msgs = [{"role": "system", "content": "You are a computer-use agent operating a computer through the available "
                                          "tools. Complete the user's task step by step, then call answer."},
            {"role": "user", "content": task}]
    hidden = False
    noops, recent = [], []
    try:
        app = None if dry_run else (app or task_app(rails_vlm, task))
        if app:
            focused, note, _ = open_app(kvm, rails_vlm, app, os_name)
            hidden = focused and os_name.lower().startswith("mac")
            log(note)
        for step in range(1, max_steps + 1):
            t0 = time.monotonic()
            img = frame(kvm)
            if img is None:
                return {"done": False, "fail": "no video (NO SIGNAL)", "steps": step}
            screen = img.crop(content_rect(img))
            if save_dir:
                screen.save(os.path.join(save_dir, f"step{step:02d}.jpg"), quality=85)
            buf = io.BytesIO()
            screen.save(buf, format="JPEG", quality=80)
            import base64
            part = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}
            msgs.append({"role": "user", "content": [{"type": "text", "text": "<observation>\n"}, part, {"type": "text", "text": "\n</observation>"}]})
            seen = 0   # keep the last 3 screenshots only
            for m in reversed(msgs):
                if m["role"] == "user" and isinstance(m["content"], list):
                    for c in m["content"]:
                        if c.get("type") == "image_url":
                            seen += 1
                            if seen > 3:
                                c.clear(); c.update({"type": "text", "text": "[screenshot evicted]"})
            t_cap = time.monotonic() - t0
            msg, usage, t_model = chat(url, key, model, msgs, think, 1500 if think else 400)
            calls = msg.get("tool_calls") or []
            if not calls:
                log(f"step {step}: model {t_model:.2f}s no tool call: {str(msg.get('content'))[:120]!r}")
                msgs.append({"role": "assistant", "content": msg.get("content") or "(no action)"})
                continue
            call = calls[0]
            name, args = call["function"]["name"], json.loads(call["function"].get("arguments") or "{}")
            cid = call.get("id") or f"call_{step}"
            msgs.append({"role": "assistant", "content": msg.get("content") or None,
                         "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
            summary = f"{name} {json.dumps(args)}"
            recent.append(summary)
            if recent[-3:] == [summary] * 3:
                return {"done": False, "fail": "stuck repeating the same action", "steps": step}
            if name == "answer":
                log(f"step {step}: capture {t_cap:.2f}s model {t_model:.2f}s ({usage.get('prompt_tokens')}+{usage.get('completion_tokens')} tok) | {summary}")
                return {"done": True, "fail": None, "steps": step, "answer": args.get("content")}
            t1 = time.monotonic()
            try:
                actions = to_actions(name, args, os_name)
            except (ValueError, KeyError) as e:
                log(f"step {step}: bad tool call {summary}: {e}")
                msgs.append({"role": "tool", "tool_call_id": cid, "content": f"error: {e}"})
                continue
            result = "ok"
            if not dry_run:
                kvm.actions(actions)
                after = settled(kvm, timeout=2.0, interval=0.0, tolerance=2.0, first_delay=0.1)
                # the tool result is the model's only feedback: say so when the action visibly did nothing
                if after is not None and after.size == img.size and changed_px(after, img) < 400:
                    result = "The screen did not change after this action; it had no effect. Do something different."
                    noops.append(summary)
                    if noops[-3:] == [summary] * 3:
                        return {"done": False, "fail": "stuck repeating an action that changes nothing", "steps": step}
                else:
                    noops.clear()
            msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
            log(f"step {step}: capture {t_cap:.2f}s model {t_model:.2f}s ({usage.get('prompt_tokens')}+{usage.get('completion_tokens')} tok) "
                f"act+settle {time.monotonic() - t1:.2f}s | {summary}{' | NO CHANGE' if result != 'ok' else ''}")
        return {"done": False, "fail": "max steps reached", "steps": max_steps}
    finally:
        if hidden:
            try:
                log(f"show all: {show_all(kvm, rails_vlm) or show_all(kvm, rails_vlm)}")
            except Exception as e:
                log(f"show all: failed ({e})")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m agent.holo", description=__doc__.splitlines()[0])
    p.add_argument("task")
    p.add_argument("--kvm", default=os.environ.get("AIKVM_URL", "http://10.42.0.101:8080"))
    p.add_argument("--llm", default="http://127.0.0.1:8010/v1")
    p.add_argument("--model", default="holo4")
    p.add_argument("--rails-llm", default=os.environ.get("AIKVM_LLM_BASE", "http://127.0.0.1:8000/v1"),
                   help="endpoint for the yes/no rails (open_app, show_all)")
    p.add_argument("--rails-model", default=os.environ.get("AIKVM_LLM_MODEL", "qwen27"))
    p.add_argument("--key-file", default=os.environ.get("AIKVM_LLM_KEY_FILE", "/etc/gemma4-vllm-api-key"))
    p.add_argument("--os", default="macOS", dest="os_name")
    p.add_argument("--max-steps", type=int, default=30)
    p.add_argument("--think", action="store_true")
    p.add_argument("--app")
    p.add_argument("--save-dir")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    if a.save_dir:
        os.makedirs(a.save_dir, exist_ok=True)
    key = load_key(a.key_file)
    rails = VLM(a.rails_llm, a.rails_model, key=key)
    result = run(a.task, KVM(a.kvm), a.llm.rstrip("/") + "/chat/completions", key, a.model, rails, a.os_name, a.max_steps,
                 a.think, a.dry_run, a.save_dir, a.app)
    print(json.dumps(result))
    return 0 if result["done"] else 1


if __name__ == "__main__":
    sys.exit(main())
