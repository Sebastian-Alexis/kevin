"""MCP server that is the whole of what a planner (Droid) may touch: the target computer through the KVM, as the
standard `computer` tool (the same loop as `python3 -m agent`, agent/cu.py). It runs ON the KVM, so the planner's
machine needs nothing but an MCP client; the Enter gate's judge is a local model reached over HTTP.

    python3 -m agent.kvm_mcp --http 0.0.0.0:8090     # on the Pi (systemd: ai-kvm-mcp.service); MCP at /mcp
    python3 -m agent.kvm_mcp                         # stdio, for a planner on this machine

Tools: `begin(task)` (run folder + first screenshot + overview of open windows), `computer(action, ...)`.
Every call is appended to the run's events.jsonl; frames are saved next to it.
"""
import base64
import io
import json
import os
import threading
import time

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent, TextContent

from .cu import SYSTEM, Session
from .kvm import KVM, content_rect
from .memory import learn, notes_text, recall
from .run import latest, survey as survey_frame
from .vlm import VLM, load_key

# reached through forwards (the host's Tailscale address), so the Host header is not the bind address
# stateless: no per-client session ids, so a server restart (deploy) never strands a connected app with
# "Session not found"; the run state lives in the Session, keyed by begin()
MEMORY = os.environ.get("AIKVM_MEMORY", "http://10.42.0.1:8020")   # agent.memory on the controller ('' disables)
COMPUTER = os.environ.get("AIKVM_COMPUTER", "mac")

mcp = FastMCP("ai-kvm", stateless_http=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
_cu = None


def cu():
    global _cu
    if _cu is None:
        key = load_key(os.environ.get("AIKVM_LLM_KEY_FILE", "/etc/ai-kvm/llm-key"))
        gate = VLM(os.environ.get("AIKVM_LLM_BASE", "http://10.42.0.1:8000/v1"), os.environ.get("AIKVM_LLM_MODEL", "qwen27"), key=key)
        _cu = Session(KVM(os.environ.get("AIKVM_URL", "http://127.0.0.1:8080")), "", "", None,
                      os.environ.get("AIKVM_OS", "macOS"), save_dir=None, log=lambda *a: None, gate=gate)
    return _cu


def record(event):
    c = cu()
    if c.save_dir:
        with open(os.path.join(c.save_dir, "events.jsonl"), "a") as f:
            f.write(json.dumps({"t": time.time(), **event}) + "\n")


def picture(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return ImageContent(type="image", data=base64.b64encode(buf.getvalue()).decode(), mimeType="image/jpeg")


@mcp.tool(description="Start a run for `task` (call once first). Returns the run id, the rules of this computer, the "
          "current screenshot, then the overview of every open window (what the user already has running: use those "
          "apps for the task; launch something else only when nothing suitable is open). Coordinates for `computer` "
          "are pixels of the screenshots.")
def begin(task: str) -> list:
    c = cu()
    run_id = time.strftime("%Y%m%d-%H%M%S")
    c.task, c.typed, c.frame = task, None, None
    c.save_dir = os.path.join(os.environ.get("AIKVM_RUNS", "/var/lib/ai-kvm/runs"), run_id)
    os.makedirs(c.save_dir, exist_ok=True)
    img = c.screen(fresh=True)
    frame = survey_frame(c.kvm, c.os_name)
    c.kvm.actions([{"type": "key", "combo": "esc"}, {"type": "wait", "ms": 400}])
    c.frame = None
    ov = frame.crop(content_rect(frame))
    record({"begin": len(task), "size": list(img.size)})   # the task text may carry credentials: not persisted
    c.notes = recall(MEMORY, task, COMPUTER) if MEMORY else []
    c.trajectory = []
    text = (f"run {run_id} started. {SYSTEM.format(os=c.os_name)}\n\n" + (notes_text(c.notes) + "\n\n" if c.notes else "")
            + f"First image: the screen ({img.size[0]}x{img.size[1]}); second image: the overview of every open window "
            "(now closed again). Batch every action you are confident about in one turn; only screenshot/zoom return an "
            "image, so end a batch with a screenshot when you need to see the result. Call end(outcome, summary) when "
            "the task is complete or impossible.")
    c.shot_block(img)   # saves shot01.jpg in the run folder
    return [TextContent(type="text", text=text), picture(img), picture(ov)]


@mcp.tool(description="The overview of every open window on the computer (Mission Control), closed again before "
          "returning: which apps the user already has running. Use those for the task; launch something else only "
          "when nothing suitable is open.")
def survey() -> list:
    c = cu()
    frame = survey_frame(c.kvm, c.os_name)
    c.kvm.actions([{"type": "key", "combo": "esc"}, {"type": "wait", "ms": 400}])
    c.frame = None
    ov = frame.crop(content_rect(frame))
    record({"survey": True})
    return [TextContent(type="text", text="every open window, overview now closed"), picture(ov)]


@mcp.tool(description="The standard computer tool: one action on the computer. Only screenshot and zoom return an "
          "image: batch your actions and end the batch with a screenshot (the screen is allowed to settle first). action: left_click, double_click, triple_click, right_click, middle_click, "
          "mouse_move, left_click_drag (start_coordinate -> coordinate), type (text; a trailing newline presses "
          "Enter), key (text = combo: cmd+l, Return, ctrl+shift+t, Escape), scroll (coordinate + scroll_direction + "
          "scroll_amount in ticks, ~13 per screenful), wait (duration seconds), zoom (region [x0,y0,x1,y1] shown "
          "enlarged), screenshot (optional scale 0.25-1 for a smaller image; coordinates stay full-size). coordinate = [x, y] pixels of the last screenshot. Batch several calls when you "
          "are confident (e.g. four tiles then Submit; click a field, type, Enter).")
def computer(action: str, coordinate: list[int] | None = None, text: str | None = None, scroll_direction: str | None = None,
             scroll_amount: int | None = None, duration: float | None = None, region: list[int] | None = None,
             start_coordinate: list[int] | None = None, scale: float | None = None) -> list:
    c = cu()
    if c.size is None:
        c.screen(fresh=True)
    inp = {k: v for k, v in {"action": action, "coordinate": coordinate, "text": text, "scroll_direction": scroll_direction,
                             "scroll_amount": scroll_amount, "duration": duration, "region": region,
                             "start_coordinate": start_coordinate, "scale": scale}.items() if v is not None}
    try:
        note, image = c.do(inp)
    except Exception as e:
        record({"computer": {**inp, "text": "<redacted>"} if action == "type" else inp, "error": str(e)})
        return [TextContent(type="text", text=f"error: {e}"), picture(c.screen(fresh=True))]
    record({"computer": {**inp, "text": f"<redacted: {len(inp['text'])} chars>"} if action == "type" else inp, "note": note})
    if hasattr(c, "trajectory"):
        c.trajectory.append(json.dumps({**inp, "text": "<typed>"} if action == "type" else inp))
    out = [TextContent(type="text", text=note or "ok")]
    if image is not None:
        out.append(ImageContent(type="image", data=image["source"]["data"], mimeType="image/jpeg"))
    else:
        c.frame = None   # the next screenshot captures fresh (and settles) rather than reusing a pre-action frame
    return out


@mcp.tool(description="End the run: outcome is 'success' or 'failure', summary one short paragraph of what was done "
          "and found. The run's lessons go to memory so the next run on this computer starts smarter.")
def end(outcome: str, summary: str) -> str:
    c = cu()
    record({"end": outcome, "summary": summary})
    if MEMORY:   # lessons are distilled in the background: the planner's reply must not wait for them
        args = (MEMORY, c.task, COMPUTER, "success" if outcome == "success" else "failure",
                summary + "\nactions: " + " ".join(getattr(c, "trajectory", [])), [n["id"] for n in getattr(c, "notes", [])])
        threading.Thread(target=learn, args=args, daemon=True).start()
    c.notes, c.trajectory = [], []
    return f"run ended ({outcome})"


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--http", metavar="HOST:PORT", help="serve streamable HTTP MCP here instead of stdio")
    a = p.parse_args()
    if a.http:
        host, port = a.http.rsplit(":", 1)
        mcp.settings.host, mcp.settings.port = host, int(port)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
