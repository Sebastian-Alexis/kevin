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
import time

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent, TextContent

from .cu import SYSTEM, Session
from .kvm import KVM, content_rect
from .run import latest, survey
from .vlm import VLM, load_key

# reached through forwards (the host's Tailscale address), so the Host header is not the bind address
# stateless: no per-client session ids, so a server restart (deploy) never strands a connected app with
# "Session not found"; the run state lives in the Session, keyed by begin()
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


@mcp.tool(description="Start a run for `task` (call once first). Returns the run id, the rules of this computer, "
          "the current screenshot, then the overview of every open window (what the user already has running: use "
          "those apps for the task; launch something else only when nothing suitable is open). Coordinates for "
          "`computer` are pixels of the screenshots this returns.")
def begin(task: str) -> list:
    c = cu()
    run_id = time.strftime("%Y%m%d-%H%M%S")
    c.task, c.typed, c.frame = task, None, None
    c.save_dir = os.path.join(os.environ.get("AIKVM_RUNS", "/var/lib/ai-kvm/runs"), run_id)
    os.makedirs(c.save_dir, exist_ok=True)
    with open(os.path.join(c.save_dir, "task.txt"), "w") as f:
        f.write(task + "\n")
    img = c.screen(fresh=True)
    frame = survey(c.kvm, c.os_name)
    c.kvm.actions([{"type": "key", "combo": "esc"}, {"type": "wait", "ms": 400}])
    c.frame = None
    ov = frame.crop(content_rect(frame))
    record({"begin": task, "size": list(img.size)})
    text = (f"run {run_id} started. {SYSTEM.format(os=c.os_name)}\n\nFirst image: the screen "
            f"({img.size[0]}x{img.size[1]}); second image: the overview of every open window (now closed again).")
    c.shot_block(img)   # saves shot01.jpg in the run folder
    return [TextContent(type="text", text=text), picture(img), picture(ov)]


@mcp.tool(description="The standard computer tool: one action on the computer, then the screenshot after it once "
          "the screen has settled. action: left_click, double_click, triple_click, right_click, middle_click, "
          "mouse_move, left_click_drag (start_coordinate -> coordinate), type (text; a trailing newline presses "
          "Enter), key (text = combo: cmd+l, Return, ctrl+shift+t, Escape), scroll (coordinate + scroll_direction + "
          "scroll_amount in ticks, ~13 per screenful), wait (duration seconds), zoom (region [x0,y0,x1,y1] shown "
          "enlarged), screenshot. coordinate = [x, y] pixels of the last screenshot. Batch several calls when you "
          "are confident (e.g. four tiles then Submit; click a field, type, Enter).")
def computer(action: str, coordinate: list[int] | None = None, text: str | None = None, scroll_direction: str | None = None,
             scroll_amount: int | None = None, duration: float | None = None, region: list[int] | None = None,
             start_coordinate: list[int] | None = None) -> list:
    c = cu()
    if c.size is None:
        c.screen(fresh=True)
    inp = {k: v for k, v in {"action": action, "coordinate": coordinate, "text": text, "scroll_direction": scroll_direction,
                             "scroll_amount": scroll_amount, "duration": duration, "region": region,
                             "start_coordinate": start_coordinate}.items() if v is not None}
    try:
        note, image = c.do(inp)
    except Exception as e:
        record({"computer": inp, "error": str(e)})
        return [TextContent(type="text", text=f"error: {e}"), picture(c.screen(fresh=True))]
    if image is None:
        c.frame = latest(c.kvm, timeout=2.0, interval=0.0, tolerance=2.0, first_delay=0.25)
        image = c.shot_block(c.screen())
    record({"computer": inp, "note": note})
    return [TextContent(type="text", text=note or "ok"),
            ImageContent(type="image", data=image["source"]["data"], mimeType="image/jpeg")]


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
