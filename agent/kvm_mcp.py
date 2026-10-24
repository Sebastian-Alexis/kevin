"""MCP server that is the whole of what a planner (Droid) may touch: the target computer's screen, mouse and
keyboard through the KVM, with the rails of agent.executor around every action. It runs ON the KVM, so the
planner's machine needs nothing but an MCP client; the grounder and verifier are local models reached over HTTP.

    python3 -m agent.kvm_mcp --http 0.0.0.0:8090     # on the Pi (systemd: ai-kvm-mcp.service); MCP at /mcp
    python3 -m agent.kvm_mcp                         # stdio, for a planner on this machine

Tools: `look` (screenshot), `act` (one batch of actions + what to expect afterwards; verified locally), `open_app`.
Every call is appended to $AIKVM_EVENTS (JSON lines), the planner's run report reads it to show what was done.
"""
import base64
import io
import json
import os
import time

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent, TextContent

from .executor import Executor
from .kvm import KVM
from .vlm import VLM, load_key

GRAMMAR = ('Actions, one string each, in order: "click <target>", "double_click <target>", "right_click <target>", '
           '"key <combo>" (cmd+l, enter, esc, cmd+shift+t ...), "type <text>", "scroll <target> -N" (negative N scrolls '
           'the page down; 1 tick is ~55 px, a screenful ~13), "wait <ms>", "open_app <name>", "survey" (a screenshot of every open window at once, to see which apps are in use). A <target> is a specific '
           'description of what to hit (its text and where it is: "Submit button below the grid", and the dialog or '
           'window it is in when another window shows behind: "URL field in the Add MCP Server dialog", never just '
           '"URL input"); a separate vision model locates it on the current screen. Append "@x,y" (pixels of the last screenshot) only if you are sure. '
           "What is already open is the user's workflow: when a task names a kind of app (browser, terminal, mail...) use "
           'the one already running (front window, windows behind it, Dock icons with a dot) instead of launching another; '
           'open_app something else only when nothing suitable is open.')

# reached through forwards (the host's Tailscale address), so the Host header is not the bind address
# stateless: no per-client session ids, so a server restart (deploy) never strands a connected app with
# "Session not found"; the run state lives in the Executor, keyed by begin()
mcp = FastMCP("ai-kvm", stateless_http=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
_ex = None
_events = os.environ.get("AIKVM_EVENTS")


def executor():
    global _ex
    if _ex is None:
        key = load_key(os.environ.get("AIKVM_LLM_KEY_FILE", "/etc/ai-kvm/llm-key"))
        rails = VLM(os.environ.get("AIKVM_LLM_BASE", "http://10.42.0.1:8000/v1"),
                    os.environ.get("AIKVM_LLM_MODEL", "qwen27"), key=key)
        g = os.environ.get("AIKVM_GROUNDER", "http://10.42.0.1:8010/v1")
        grounder = {"url": g.rstrip("/") + "/chat/completions", "model": os.environ.get("AIKVM_GROUNDER_MODEL", "holo4"),
                    "key": key} if g else None
        _ex = Executor(KVM(os.environ.get("AIKVM_URL", "http://127.0.0.1:8080")), rails, grounder=grounder,
                       os_name=os.environ.get("AIKVM_OS", "macOS"), save_dir=os.environ.get("AIKVM_SAVE_DIR"),
                       isolate=False, task=os.environ.get("AIKVM_TASK", ""), log=record)
    return _ex


def record(event):
    path = os.path.join(_ex.save_dir, "events.jsonl") if _ex and _ex.save_dir else None
    if path:
        with open(path, "a") as f:
            f.write(json.dumps({"t": time.time(), **event}) + "\n")


@mcp.tool()
def begin(task: str) -> list:
    """Start a run for `task` (call once first): resets the step counter and keyboard-focus tracking, opens a run
    folder for the frames and the event log, and returns the run id with TWO screenshots: the current screen, then
    the overview of every open window (what the user already has running: use those apps for the task; open_app
    something else only when nothing suitable is open). The overview is closed again before this returns."""
    ex = executor()
    if ex.overview:   # a previous run ended in the window overview
        ex.kvm.actions([{"type": "key", "combo": "esc"}, {"type": "wait", "ms": 300}])
    run_id = time.strftime("%Y%m%d-%H%M%S")
    ex.task, ex.n, ex.focused, ex.primed, ex.frame, ex.overview = task, 0, False, False, None, False
    ex.save_dir = os.path.join(os.environ.get("AIKVM_RUNS", "/var/lib/ai-kvm/runs"), run_id)
    os.makedirs(ex.save_dir, exist_ok=True)
    with open(os.path.join(ex.save_dir, "task.txt"), "w") as f:
        f.write(task + "\n")
    img = ex.screen(fresh=True)
    ex.save(img, "step00.jpg")
    from .kvm import content_rect
    from .run import survey as overview_of
    frame = overview_of(ex.kvm, ex.os_name)
    ex.kvm.actions([{"type": "key", "combo": "esc"}, {"type": "wait", "ms": 400}])
    ex.frame = None   # the next look/act captures the normal screen, not the overview
    ov = frame.crop(content_rect(frame))
    ex.save(ov, "step00-overview.jpg")
    record({"begin": task, "size": list(img.size)})
    return [TextContent(type="text", text=f"run {run_id} started; screenshot {img.size[0]}x{img.size[1]}, then the "
                        "overview of every open window (which apps are already in use: prefer them)"),
            picture(img), picture(ov)]


def picture(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return ImageContent(type="image", data=base64.b64encode(buf.getvalue()).decode(), mimeType="image/jpeg")


@mcp.tool()
def look() -> list:
    """Current screenshot of the computer under control (fresh capture). Coordinates in it are pixels."""
    ex = executor()
    img = ex.screen(fresh=True)
    path = ex.save(img, f"look{int(time.time())}.jpg")
    record({"look": path, "size": list(img.size)})
    return [TextContent(type="text", text=f"screenshot {img.size[0]}x{img.size[1]}"), picture(img)]


@mcp.tool(description="Perform one batch of actions on the computer, then check the screen for `expect`. " + GRAMMAR
          + " `expect` is what the screen should show once the batch has taken effect (a cheap local model checks "
          "it: a failed check returns the screenshot so you can re-plan). Returns what was done, what was refused "
          "and why, the verdict, and the screenshot after the batch.")
def act(actions: list[str], expect: str = "") -> list:
    ex = executor()
    rep = ex.step(actions, expect)
    lines = []
    if rep.get("refused"):
        lines.append(f"REFUSED (nothing executed): {rep['refused']}")
    else:
        lines.append(f"executed {len(rep['actions'])} action(s) in {rep['seconds']}s")
    if rep.get("grounded"):
        lines.append("located: " + ", ".join(f"{t} -> {p}" for t, p in rep["grounded"].items()))
    lines += rep.get("notes", [])
    if "verified" in rep:
        lines.append(f'expectation "{expect}": {"confirmed" if rep["verified"] else "NOT seen on screen"}')
    out = [TextContent(type="text", text="\n".join(lines))]
    if rep.get("executed"):
        out.append(picture(ex.screen()))
    return out


@mcp.tool()
def survey() -> list:
    """Every open window at once (the OS overview): which apps and documents are already in use. Call it when the
    task names a kind of app and no such window is visible; then act(["click <that window>"]) brings it to the
    front (any other next action closes the overview first)."""
    return act(["survey"], "")


@mcp.tool()
def open_app(name: str) -> list:
    """Bring the named application to the front (launching it if needed). Returns the screenshot afterwards. Prefer an
    app the overview (begin / survey) showed already running; launch something else only when nothing open fits."""
    return act([f"open_app {name}"], "")


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
