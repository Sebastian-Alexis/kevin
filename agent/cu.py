"""Native computer use on the KVM: Claude drives the computer through the standard `computer` tool (the schema
Anthropic trained it on: left_click, type, key, scroll, zoom, ...), the way its own computer-use agent works.
No planner prompt, no action grammar, no grounder: the model sees a screenshot, calls the tool as often as it
likes (several calls per turn when it is confident), gets a screenshot back after each batch, and stops when
the task is done. The KVM is the executor.

    python3 -m agent.cu "In the Helium web browser, go to connectionsunlimited.org, click New Game and solve it"

Kept from the KVM side: off-screen clicks are refused with an error result; an Enter right after typed text is
gated (a local model checks the text landed in an input that fits the task, so a chat box or terminal that
happened to have focus never gets it sent); the last few screenshots only stay in context; frames and a step
log are saved.
"""
import argparse
import base64
import io
import json
import os
import sys
import time
import urllib.request

from .kvm import KVM, PLACEHOLDER_SIZE, content_rect
from .memory import learn, notes_text, recall
from .run import latest, typed_where_wanted
from .vlm import VLM, load_key

SYSTEM = """You are operating a {os} computer through its keyboard and mouse. You see the screen as screenshots;
coordinates are pixels of the screenshot you were given. After each batch of actions you get a fresh screenshot.
What is already open is the user's workflow: when the task names a kind of app (a browser, a terminal, mail), use the
one already running (front window, windows behind it, Dock dots); never launch or sign in to a different one if an
open one serves. When you are confident about several actions (e.g. several items to select then a Submit, or a
field to click then text to type then Enter), do them in one batch of tool calls. You get a screenshot back after
every batch once the screen has settled, so do not spend a turn on a lone screenshot: if the screen may still be
loading, batch a short wait with the next action instead. Prefer keyboard shortcuts for
navigation (cmd+t, cmd+l, cmd+f to jump to text on a page). On this Mac the launcher is cmd+space (Raycast).
When the task is complete, or impossible, stop calling tools and say so in one short paragraph."""

TOOL = {"name": "computer", "input_schema": {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["key", "type", "mouse_move", "left_click", "left_click_drag", "right_click",
                                               "middle_click", "double_click", "triple_click", "screenshot",
                                               "cursor_position", "scroll", "wait", "zoom"]},
        "coordinate": {"type": "array", "items": {"type": "integer"}, "description": "[x, y] pixels of the screenshot"},
        "start_coordinate": {"type": "array", "items": {"type": "integer"}, "description": "left_click_drag: where to press"},
        "text": {"type": "string", "description": "type: the text; key: the key combo, e.g. cmd+l, Return, ctrl+shift+t"},
        "scroll_direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
        "scroll_amount": {"type": "integer", "description": "scroll: number of wheel ticks (1 tick ~ 55 px; a screenful ~13)"},
        "duration": {"type": "number", "description": "wait: seconds"},
        "region": {"type": "array", "items": {"type": "integer"}, "description": "zoom: [x0, y0, x1, y1] of the screenshot to see enlarged"},
        "scale": {"type": "number", "description": "screenshot: return it downscaled by this factor (0.25-1) to save tokens; coordinates stay in full-size pixels"}},
    "required": ["action"]}}

KEYMAP = {"return": "enter", "escape": "esc", "page_down": "pagedown", "page_up": "pageup", "super": "cmd",
          "command": "cmd", "control": "ctrl", "option": "alt", "backspace": "backspace", "delete": "delete",
          "tab": "tab", "space": "space", "up": "up", "down": "down", "left": "left", "right": "right", "home": "home", "end": "end"}


def combo(text):
    return "+".join(KEYMAP.get(k.strip().lower(), k.strip().lower()) for k in text.replace(" ", "+").split("+") if k.strip())


class Session:
    def __init__(self, kvm, base, model, key, os_name="macOS", save_dir=None, log=print, keep_images=3, max_side=1280,
                 gate=None, task="", memory=None, computer="mac"):
        self.kvm, self.base, self.model, self.key, self.os_name = kvm, base.rstrip("/"), model, key, os_name
        self.memory, self.computer = memory, computer   # memory: base URL of agent.memory (Moss playbook), or None
        self.save_dir, self.log, self.keep_images, self.max_side = save_dir, log, keep_images, max_side
        self.gate, self.task, self.typed = gate, task, None   # gate: VLM judging where typed text landed before Enter
        self.frame = None        # the frame the last batch settled on
        self.size = None         # size of the screenshot the model sees (pixels its coordinates refer to)
        self.n_shots = 0
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

    # --- screen -----------------------------------------------------------------------------------------------
    def screen(self, fresh=False):
        frame = (None if fresh else self.frame) or self.kvm.screenshot()
        deadline = time.monotonic() + 6
        while frame.size == PLACEHOLDER_SIZE:
            if time.monotonic() > deadline:
                raise RuntimeError("no video from the computer (NO SIGNAL)")
            time.sleep(0.5)
            frame = self.kvm.screenshot()
        self.frame = frame
        img = frame.crop(content_rect(frame))
        if max(img.size) > self.max_side:
            s = self.max_side / max(img.size)
            img = img.resize((round(img.width * s), round(img.height * s)))
        self.size = img.size
        return img

    def shot_block(self, img, tag="shot"):
        self.n_shots += 1
        if self.save_dir:
            img.save(os.path.join(self.save_dir, f"{tag}{self.n_shots:02d}.jpg"), quality=85)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}}

    # --- actions ----------------------------------------------------------------------------------------------
    def frac(self, coordinate):
        if not (isinstance(coordinate, list) and len(coordinate) == 2):
            raise ValueError("coordinate must be [x, y]")
        x, y = coordinate
        w, h = self.size
        if not (0 <= x <= w and 0 <= y <= h):
            raise ValueError(f"[{x}, {y}] is outside the {w}x{h} screenshot")
        return x / w, y / h

    def do(self, inp):
        """One computer-tool call -> KVM actions. Returns (text, image_block or None)."""
        a = inp.get("action")
        if a in ("left_click", "right_click", "middle_click", "double_click", "triple_click", "mouse_move"):
            fx, fy = self.frac(inp.get("coordinate"))
            acts = [{"type": "move", "x": max(0.0, fx - 0.004), "y": fy}, {"type": "wait", "ms": 20},
                    {"type": "move", "x": fx, "y": fy}, {"type": "wait", "ms": 40}]
            if a != "mouse_move":
                btn = {"right_click": "right", "middle_click": "middle"}.get(a, "left")
                count = {"double_click": 2, "triple_click": 3}.get(a, 1)
                acts.append({"type": "click", "button": btn, "count": count})
            self.kvm.actions(acts)
            self.typed = None
        elif a == "left_click_drag":
            sx, sy = self.frac(inp.get("start_coordinate") or inp.get("coordinate"))
            ex, ey = self.frac(inp.get("coordinate"))
            self.kvm.actions([{"type": "drag", "x": sx, "y": sy, "to_x": ex, "to_y": ey, "button": "left"}])
        elif a == "type":
            text = str(inp.get("text", ""))
            enter = text.endswith("\n")
            text = text.rstrip("\n")
            if text:
                self.kvm.actions([{"type": "type", "text": text}])
                self.typed = text
            if enter:
                self.enter()
        elif a == "key":
            c = combo(str(inp.get("text", "")))
            if c.split("+")[-1] == "enter" and self.typed is not None:
                self.enter()
            else:
                self.kvm.actions([{"type": "key", "combo": c}])
                self.typed = None
        elif a == "scroll":
            fx, fy = self.frac(inp.get("coordinate") or [self.size[0] // 2, self.size[1] // 2])
            n = int(inp.get("scroll_amount") or 3)
            d = inp.get("scroll_direction", "down")
            ticks = n if d == "down" else -n   # natural scrolling on macOS: +ticks moves the page down
            if not self.os_name.lower().startswith("mac"):
                ticks = -ticks
            self.kvm.actions([{"type": "scroll", "x": fx, "y": fy, "ticks": ticks}])
        elif a == "wait":
            time.sleep(min(10.0, float(inp.get("duration") or 1)))
        elif a == "screenshot":
            # let the UI react to the actions before it: watch the capture until it stops changing
            self.frame = latest(self.kvm, timeout=2.0, interval=0.0, tolerance=2.0, first_delay=0.25)
            img = self.screen()
            try:
                scale = float(inp.get("scale") or 1)
            except (TypeError, ValueError):
                scale = 1.0
            if 0.2 <= scale < 1:
                img = img.resize((round(img.width * scale), round(img.height * scale)))
                return f"screenshot at {scale:.2f}x; coordinates for actions stay in full-size {self.size[0]}x{self.size[1]} pixels", self.shot_block(img)
            return "", self.shot_block(img)
        elif a == "cursor_position":
            return "unknown (the KVM does not read the pointer back); move it explicitly", None
        elif a == "zoom":
            r = inp.get("region") or []
            if len(r) != 4:
                raise ValueError("zoom needs region [x0, y0, x1, y1]")
            img = self.screen(fresh=True).crop((r[0], r[1], r[2], r[3]))
            s = min(2.0, self.max_side / max(1, max(img.size)))
            img = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))))
            return f"zoomed region {r} (shown at {s:.1f}x; coordinates for actions stay in full-screenshot pixels)", self.shot_block(img, "zoom")
        else:
            raise ValueError(f"unknown action {a!r}")
        self.frame = None
        return "ok", None

    def enter(self):
        """Enter after typed text: pressed only if the text is seen in an input the task wants it in."""
        typed, self.typed = self.typed, None
        if self.gate and typed:
            fits, holder = typed_where_wanted(self.kvm, self.gate, self.task, typed)
            if not fits:
                raise ValueError(f'Enter NOT pressed: "{typed}" landed in: {holder}, not an input that fits the task. '
                                 "Click the right input (or use its shortcut, e.g. cmd+l / cmd+t) and type again")
        self.kvm.actions([{"type": "key", "combo": "enter"}])

    # --- model ------------------------------------------------------------------------------------------------
    def chat(self, messages, max_tokens=1500):
        body = {"model": self.model, "max_tokens": max_tokens, "system": SYSTEM.format(os=self.os_name),
                "tools": [{**TOOL, "description": f"Use a mouse and keyboard to interact with the computer and take screenshots. "
                                                  f"The screenshot is {self.size[0]}x{self.size[1]} pixels; coordinates are [x, y] in it."}],
                "messages": messages}
        req = urllib.request.Request(self.base + "/messages", json.dumps(body).encode(),
                                     {"content-type": "application/json", "x-api-key": self.key or "omp"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))

    def trim_images(self, messages):
        """Only the newest screenshots stay as images; older ones become a note (the reference agent does the same)."""
        seen = 0
        for m in reversed(messages):
            if m["role"] != "user" or not isinstance(m["content"], list):
                continue
            for part in reversed(m["content"]):
                blocks = part.get("content") if part.get("type") == "tool_result" else [part]
                if not isinstance(blocks, list):
                    continue
                for i, b in enumerate(blocks):
                    if isinstance(b, dict) and b.get("type") == "image":
                        seen += 1
                        if seen > self.keep_images:
                            blocks[i] = {"type": "text", "text": "[earlier screenshot omitted]"}

    # --- loop -------------------------------------------------------------------------------------------------
    def run(self, task, max_turns=40, settle_max=2.0):
        self.task, self.typed = task, None
        notes = recall(self.memory, task, self.computer) if self.memory else []
        if notes:
            self.log(f"memory: {len(notes)} note(s) recalled: " + " | ".join(n["text"][:70] for n in notes))
        trajectory = []
        img = self.screen(fresh=True)
        prompt = task + ("\n\n" + notes_text(notes) if notes else "")
        messages = [{"role": "user", "content": [{"type": "text", "text": prompt}, self.shot_block(img)]}]
        result = {"done": False, "fail": "max turns reached", "turns": max_turns}
        for turn in range(1, max_turns + 1):
            t0 = time.monotonic()
            self.trim_images(messages)
            out = self.chat(messages)
            t_model = time.monotonic() - t0
            content = out.get("content", [])
            messages.append({"role": "assistant", "content": content})
            calls = [c for c in content if c.get("type") == "tool_use"]
            text = " ".join(c.get("text", "") for c in content if c.get("type") == "text").strip()
            if text:
                trajectory.append(f"turn {turn}: {text}")
            if calls:
                trajectory.append(f"turn {turn} actions: " + json.dumps([c["input"] for c in calls])[:400])
            if not calls:
                self.log(f"turn {turn}: model {t_model:.2f}s done -> {text[:300]}")
                result = {"done": True, "text": text, "turns": turn}
                break
            results, t1 = [], time.monotonic()
            for i, c in enumerate(calls):
                try:
                    note, image = self.do(c["input"])
                    if i == len(calls) - 1 and image is None:
                        # the batch settled: paced by the capture (~70 ms a frame), an instant UI change settles in ~0.25 s
                        self.frame = latest(self.kvm, timeout=settle_max, interval=0.0, tolerance=2.0, first_delay=0.25)
                        image = self.shot_block(self.screen())
                    blocks = ([{"type": "text", "text": note}] if note else []) + ([image] if image else [])
                    results.append({"type": "tool_result", "tool_use_id": c["id"], "content": blocks or "ok"})
                except Exception as e:   # the model gets the error and re-plans; the run goes on
                    results.append({"type": "tool_result", "tool_use_id": c["id"], "content": f"error: {e}", "is_error": True})
                    if i == len(calls) - 1:
                        results[-1]["content"] = [{"type": "text", "text": f"error: {e}"}, self.shot_block(self.screen(fresh=True))]
            messages.append({"role": "user", "content": results})
            u = out.get("usage", {})
            self.log(f"turn {turn}: model {t_model:.2f}s ({u.get('input_tokens')}+{u.get('cache_read_input_tokens')} cached "
                     f"+{u.get('output_tokens')} out) act+settle {time.monotonic() - t1:.2f}s | {text[:80]} -> "
                     + json.dumps([c["input"] for c in calls])[:300])
        if self.memory:
            done = result.get("done") and not any(w in result.get("text", "").lower()[:200] for w in ("couldn't", "could not", "unable", "cannot"))
            out = learn(self.memory, task, self.computer, "success" if done else "failure",
                        "\n".join(trajectory), [n["id"] for n in notes])
            if out.get("learned") or out.get("quarantined"):
                self.log("memory: learned " + " | ".join(l["text"][:80] for l in out.get("learned", []))
                         + (" ; quarantined " + ", ".join(q["id"] for q in out["quarantined"]) if out.get("quarantined") else ""))
        return result


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m agent.cu", description=__doc__.splitlines()[0])
    p.add_argument("task")
    p.add_argument("--kvm", default=os.environ.get("AIKVM_URL", "http://10.42.0.101:8080"))
    p.add_argument("--llm", default=os.environ.get("AIKVM_ANTHROPIC_BASE", "http://127.0.0.1:4001/v1"), help="Anthropic Messages-compatible base")
    p.add_argument("--model", default=os.environ.get("AIKVM_CU_MODEL", "anthropic/claude-opus-5-5"))
    p.add_argument("--key-file", default=None, help="key for --llm (the local gateway needs none)")
    p.add_argument("--gate", default=os.environ.get("AIKVM_LLM_BASE", "http://127.0.0.1:8000/v1"), help="local model judging Enter after typed text ('' disables)")
    p.add_argument("--gate-key-file", default="/etc/gemma4-vllm-api-key")
    p.add_argument("--os", dest="os_name", default="macOS")
    p.add_argument("--max-turns", type=int, default=40)
    p.add_argument("--save-dir", default=None)
    p.add_argument("--memory", default=os.environ.get("AIKVM_MEMORY", "http://127.0.0.1:8020"), help="agent.memory service ('' disables)")
    p.add_argument("--computer", default=os.environ.get("AIKVM_COMPUTER", "mac"), help="name of the computer under control, for memory")
    a = p.parse_args(argv)
    gate = VLM(a.gate, "qwen27", key=load_key(a.gate_key_file)) if a.gate else None
    s = Session(KVM(a.kvm), a.llm, a.model, load_key(a.key_file) if a.key_file else None, a.os_name, a.save_dir, gate=gate,
                memory=a.memory or None, computer=a.computer)
    t0 = time.monotonic()
    result = s.run(a.task, a.max_turns)
    result["seconds"] = round(time.monotonic() - t0, 1)
    print(json.dumps(result)[:800])
    return 0 if result.get("done") else 1


if __name__ == "__main__":
    sys.exit(main())
