"""Drive the KVM with Agent S3 (simular-ai/Agent-S) instead of the in-house loop.

    .venv-s3/bin/python -m agent.s3 "In Safari, search for raspberry pi kvm"

Agent S3 plans with one model and grounds clicks with a second ("Query: <element>, output one point"); both
run on the local vLLM here. It emits pyautogui code in the pixel space of the screenshot it was given, so the
bridge is a pyautogui look-alike whose calls go to the Pi as HID actions, and a pyperclip look-alike that types
(the target has no clipboard we can reach). Clicks hover first, like agent.run.

Needs gui-agents (Python <=3.12) and tesseract: see README.
"""
import argparse
import ast
import re
import io
import json
import os
import sys
import time
import types

from .kvm import KVM, content_rect, frame
from .run import open_app, show_all, task_app
from .vlm import VLM, load_key

KEYS = {"command": "cmd", "win": "cmd", "option": "alt", "return": "enter", "escape": "esc", "pgup": "pageup",
        "pgdn": "pagedown", " ": "space"}


def _key(k):
    k = str(k).lower()
    return KEYS.get(k, k)


class Screen:
    """pyautogui stand-in: coordinates are pixels of the screenshot last handed to the agent."""

    def __init__(self, kvm, dry_run=False):
        self.kvm = kvm
        self.dry_run = dry_run
        self.size = (1, 1)
        self.held = []
        self.pos = (0.5, 0.5)

    def _frac(self, x, y):
        w, h = self.size
        return min(1.0, max(0.0, x / w)), min(1.0, max(0.0, y / h))

    def _send(self, actions):
        if self.dry_run:
            print("  would send:", json.dumps(actions))
        else:
            self.kvm.actions(actions)

    def moveTo(self, x, y, *_, **__):
        self.pos = self._frac(x, y)
        self._send([{"type": "move", "x": self.pos[0], "y": self.pos[1]}])

    def click(self, x=None, y=None, clicks=1, button="left", **_):
        fx, fy = self._frac(x, y)
        self.pos = (fx, fy)
        self._send([{"type": "move", "x": max(0.0, fx - 0.004), "y": fy}, {"type": "wait", "ms": 40},
                    {"type": "move", "x": fx, "y": fy}, {"type": "wait", "ms": 80},
                    {"type": "click", "button": button, "count": int(clicks)}])

    def mouseDown(self, *_, **__):
        self.drag_from = self.pos   # the Pi drags as one action: remember the start, drag on mouseUp/dragTo

    def mouseUp(self, *_, **__):
        start = getattr(self, "drag_from", None)
        if start and start != self.pos:
            self._send([{"type": "drag", "x": start[0], "y": start[1], "to_x": self.pos[0], "to_y": self.pos[1]}])
        self.drag_from = None

    def dragTo(self, x, y, *_, **__):
        start = getattr(self, "drag_from", None) or self.pos
        self.pos = self._frac(x, y)
        self._send([{"type": "drag", "x": start[0], "y": start[1], "to_x": self.pos[0], "to_y": self.pos[1]}])
        self.drag_from = None

    def vscroll(self, clicks, *_, **__):
        self._send([{"type": "scroll", "ticks": int(clicks)}])

    scroll = vscroll

    def hscroll(self, clicks, *_, **__):
        self._send([{"type": "key", "combo": "shift"}, {"type": "scroll", "ticks": int(clicks)}])   # best effort

    def write(self, text, *_, **__):
        self._send([{"type": "type", "text": str(text)}])

    typewrite = write

    def press(self, keys, *_, **__):
        for k in ([keys] if isinstance(keys, str) else keys):
            self._send([{"type": "key", "combo": "+".join(self.held + [_key(k)])}])

    def hotkey(self, *keys, **_):
        self._send([{"type": "key", "combo": "+".join(_key(k) for k in keys)}])

    def keyDown(self, k, *_, **__):
        self.held.append(_key(k))

    def keyUp(self, k, *_, **__):
        if _key(k) in self.held:
            self.held.remove(_key(k))


def install_stubs(screen):
    """Make `import pyautogui` / `import pyperclip` inside the agent's code resolve to the KVM."""
    pag = types.ModuleType("pyautogui")
    for name in ("moveTo", "click", "mouseDown", "mouseUp", "dragTo", "vscroll", "scroll", "hscroll", "write",
                 "typewrite", "press", "hotkey", "keyDown", "keyUp"):
        setattr(pag, name, getattr(screen, name))
    pag.FAILSAFE = False
    sys.modules["pyautogui"] = pag
    clip = types.ModuleType("pyperclip")
    clip.copy = lambda text: setattr(clip, "_text", text)
    clip.paste = lambda: getattr(clip, "_text", "")
    sys.modules["pyperclip"] = clip
    # the agent pastes unicode with cmd+v: no shared clipboard with the target, so type it instead
    real_hotkey = screen.hotkey

    def hotkey(*keys, **kw):
        if [_key(k) for k in keys] in (["cmd", "v"], ["ctrl", "v"]) and getattr(clip, "_text", None) is not None:
            screen.write(clip._text)
            clip._text = None
        else:
            real_hotkey(*keys, **kw)
    pag.hotkey = hotkey


def build_agent(a, platform):
    from gui_agents.s3.agents.agent_s import AgentS3
    from gui_agents.s3.agents.grounding import OSWorldACI
    # Planner: a cloud model through the omp bridge (agent.omp_gateway). Grounding: the local vLLM (Qwen answers
    # "Query: <element>" with a point on a 0-1000 grid, which is what Agent S3's grounding prompt asks for).
    engine = {"engine_type": "vllm", "model": a.model, "base_url": a.planner_url, "api_key": "omp"}
    key = load_key(a.key_file) or "none"
    ground = {"engine_type": "vllm", "model": a.ground_model, "base_url": a.llm, "api_key": key,
              "grounding_width": 1000, "grounding_height": 1000}
    aci = OSWorldACI(env=None, platform=platform, engine_params_for_generation=engine, engine_params_for_grounding=ground,
                     width=a.width, height=a.height)
    return AgentS3(engine, aci, platform=platform, max_trajectory_length=a.history, enable_reflection=a.reflection)


def _ok_stmt(node):
    """One top-level statement: `import pyautogui|time|pyperclip`, or a call `pyautogui.<f>(...)`, `pyperclip.copy(...)`,
    `time.sleep(<number>)` whose arguments are plain literals."""
    if isinstance(node, ast.Import):
        return all(a.name in ("pyautogui", "time", "pyperclip") for a in node.names)
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    f = node.value.func
    if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)):
        return False
    target = (f.value.id, f.attr)
    if not (target[0] == "pyautogui" or target in (("pyperclip", "copy"), ("time", "sleep"))):
        return False
    args = list(node.value.args) + [kw.value for kw in node.value.keywords]
    def lit(a):
        if isinstance(a, ast.UnaryOp) and isinstance(a.op, ast.USub):   # -5
            a = a.operand
        return isinstance(a, ast.Constant) or (isinstance(a, ast.List) and all(isinstance(e, ast.Constant) for e in a.elts))
    return all(lit(a) for a in args)


def safe_code(code):
    """Agent S3's action code runs in THIS process. Only pyautogui/pyperclip/time.sleep statements may run: anything
    else it can emit (wmctrl, sudo via subprocess, LibreOffice UNO for set_cell_values, the pyperclip pip install
    fallback) targets the machine the agent believes it is on, which here is the controller, not the target.
    Returns (code, None) or (None, offending statements)."""
    code = re.sub(r"\ntry:\n    import pyperclip\nexcept ImportError:.*?    import pyperclip\n", "\nimport pyperclip\n",
                  code, flags=re.S)
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return None, [f"syntax error: {e}"]
    bad = [ast.unparse(n) for n in tree.body if not _ok_stmt(n)]
    return (code, None) if not bad else (None, bad)


def run(agent, screen, kvm, vlm, task, max_steps, save_dir, platform="darwin", dry_run=False, log=print):
    """Agent S3's predict/exec loop over the KVM, inside the same rails as agent.run: the app the task names is
    raised first and the other apps hidden (so a stray click cannot reach them), and they are shown again at the
    end. dry_run logs the pyautogui calls instead of sending them."""
    os_name = "macOS" if platform == "darwin" else platform
    hidden = False
    try:
        app = None if dry_run else task_app(vlm, task)
        if app:
            focused, note, _ = open_app(kvm, vlm, app, os_name)
            hidden = focused and platform == "darwin"
            log(note)
        for step in range(1, max_steps + 1):
            img = frame(kvm)
            if img is None:
                return {"done": False, "fail": "no video from the computer (NO SIGNAL)", "steps": step}
            img = img.crop(content_rect(img))
            screen.size = img.size
            agent.grounding_agent.width, agent.grounding_agent.height = img.size
            if save_dir:
                img.save(os.path.join(save_dir, f"step{step:02d}.jpg"), quality=85)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            t0 = time.monotonic()
            info, code = agent.predict(instruction=task, observation={"screenshot": buf.getvalue()})
            code = code[0]
            log(f"step {step}: {time.monotonic() - t0:.1f}s | {info.get('executor_plan', '')[-200:].strip()!r} -> "
                f"{code[:160]!r}")
            code, bad = safe_code(code) if code.strip().upper() not in ("DONE", "FAIL", "WAIT", "NEXT") else (code, None)
            if bad:
                log(f"  refused, not a pyautogui action: {bad}")
                continue
            word = code.strip().upper()
            if word == "DONE":
                return {"done": True, "fail": None, "steps": step}
            if word == "FAIL":
                return {"done": False, "fail": "agent gave up", "steps": step}
            if word in ("WAIT", "NEXT"):
                time.sleep(1.0 if word == "NEXT" else 3.0)
                continue
            exec(code, {})   # the bridge's pyautogui/pyperclip receive it
            time.sleep(1.0)
        return {"done": False, "fail": "max steps reached", "steps": max_steps}
    finally:
        if hidden:
            try:
                log(f"show all: {show_all(kvm, vlm)}")
            except Exception as e:   # tidying up must not replace the task result
                log(f"show all: failed ({e})")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m agent.s3", description=__doc__.splitlines()[0])
    p.add_argument("task")
    p.add_argument("--kvm", default=os.environ.get("AIKVM_URL", "http://10.42.0.101:8080"))
    p.add_argument("--planner-url", default=os.environ.get("AIKVM_PLANNER_URL", "http://127.0.0.1:4001/v1"),
                   help="OpenAI-compatible endpoint for the planner (default: agent.omp_gateway)")
    p.add_argument("--model", default=os.environ.get("AIKVM_PLANNER_MODEL", "openai-codex/gpt-5.6-luna"),
                   help="planner model, any omp selector")
    p.add_argument("--llm", default=os.environ.get("AIKVM_LLM_BASE", "http://127.0.0.1:8000/v1"),
                   help="OpenAI-compatible endpoint for grounding (local vLLM)")
    p.add_argument("--ground-model", default=os.environ.get("AIKVM_LLM_MODEL", "qwen122"))
    p.add_argument("--key-file", default=os.environ.get("AIKVM_LLM_KEY_FILE", "/etc/gemma4-vllm-api-key"))
    p.add_argument("--os", default="darwin", dest="platform", choices=["darwin", "linux", "windows"])
    p.add_argument("--max-steps", type=int, default=15)
    p.add_argument("--history", type=int, default=8, help="image turns the agent keeps")
    p.add_argument("--no-reflection", dest="reflection", action="store_false")
    p.add_argument("--width", type=int, default=1110)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--save-dir")
    p.add_argument("--dry-run", action="store_true", help="ask the agent but log the input it would send instead")
    a = p.parse_args(argv)
    if a.save_dir:
        os.makedirs(a.save_dir, exist_ok=True)
    kvm = KVM(a.kvm)
    screen = Screen(kvm, dry_run=a.dry_run)
    install_stubs(screen)
    agent = build_agent(a, a.platform)
    vlm = VLM(a.llm, a.ground_model, key=load_key(a.key_file))   # the rails (open_app, show_all) use the local model
    result = run(agent, screen, kvm, vlm, a.task, a.max_steps, a.save_dir, a.platform, a.dry_run)
    print(json.dumps(result))
    return 0 if result["done"] else 1


if __name__ == "__main__":
    sys.exit(main())
