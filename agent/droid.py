"""Droid (Factory's agent harness, via the Droid SDK) as the planner of the KVM: it drives the computer under control
only through the ai-kvm MCP server running on the KVM, and every step is written out so the run can be reviewed.

    .venv-droid/bin/python -m agent.droid "In the Helium browser, go to example.com and report the heading"
    .venv-droid/bin/python -m agent.droid --inbox /var/lib/ai-kvm/inbox      # trigger: a task file dropped there

Inbox trigger: any `*.task` file (its text is the task) starts a run as soon as the KVM daemon's state file says
the computer is ready; the report lands next to it (`<name>.report.md`, `<name>.events.jsonl`, frames) and the task
file is renamed `<name>.done`. Anything else (an HTTP hook, a cron, another agent) only has to drop a file.

What Droid can touch: the four MCP tools of the KVM (begin, look, act, open_app). Droid's own tools that reach this
machine (shell, file edits, web) are disabled, and the permission handler cancels anything that is not a KVM tool.
The rails (grounding, focus and off-screen checks, the Enter gate, local verification of each step's `expect`) run
inside the KVM, so they apply to any model.
"""
import argparse
import asyncio
import base64
import json
import os
import sys
import time

from droid_sdk import (AssistantMessage, HttpMcpServerConfig, InteractionHandlers, Session, SessionConfig,
                       ToolCall, ToolConfirmationOutcome, ToolResult)
from droid_sdk.permissions import ApplyPatchAction, CreateFile, EditAction, ExecuteAction, McpToolAction

SYSTEM = ("You operate a computer through the ai-kvm tools only. Call begin(task) first; then look at the screenshot "
          "and act in batches, each with a precise `expect` so the KVM can verify it locally; use look whenever you "
          "need to see the screen again. Never use shell, file or web tools for the task. When the task is complete, "
          "or impossible, say so in one short paragraph.")
# Droid's tools that reach this machine. ToolSearch and Script stay: that is how Droid loads and calls MCP tools.
LOCAL_TOOLS = {"Execute", "Edit", "Create", "Read", "Grep", "Glob", "LS", "FetchUrl", "WebSearch", "ApplyPatch",
               "MultiEdit", "Task", "TodoWrite"}


def plain(o):
    """JSON for the SDK's frozen values (mappingproxy, tuples, dataclasses)."""
    if hasattr(o, "keys"):
        return dict(o)
    if isinstance(o, (list, tuple, set)):
        return list(o)
    return str(o)


WRAPPERS = {"Script", "ToolSearch"}


class Report:
    """JSON-lines event log plus a markdown report with the frames Droid saw."""

    def __init__(self, base):
        self.base, self.images = base, 0
        os.makedirs(os.path.dirname(base) or ".", exist_ok=True)
        self.events = open(base + ".events.jsonl", "a")
        self.md = open(base + ".report.md", "a")

    def event(self, kind, **data):
        self.events.write(json.dumps({"t": round(time.time(), 3), "kind": kind, **data}, default=plain) + "\n")
        self.events.flush()

    def write(self, text):
        self.md.write(text + "\n\n")
        self.md.flush()

    def image(self, data_b64, mime):
        self.images += 1
        path = f"{self.base}.frame{self.images:02d}.{'jpg' if 'jpeg' in mime else 'png'}"
        with open(path, "wb") as f:
            f.write(base64.b64decode(data_b64))
        return os.path.basename(path)

    def close(self):
        self.events.close()
        self.md.close()


def content_blocks(content):
    if isinstance(content, (list, tuple)):
        return [dict(b) if hasattr(b, "keys") else b for b in content]
    return [content]


async def run_task(task, mcp_url, model, report, log=print):
    def permission(req):
        # KVM tool calls and Droid's own wrappers that load and call them are approved; anything that would run a
        # command or touch files on this machine is cancelled. Note: a global PreToolUse auto-approve hook in
        # ~/.factory/settings.json pre-empts this handler, which is why `disabled_tools` is the hard gate.
        acts = list(req.actions or [])

        def allowed(a):
            if isinstance(a, McpToolAction):
                return a.server_name == "ai-kvm"
            # the SDK has no action class for Droid's wrappers (ToolSearch, Script): deny by what touches this machine
            return not isinstance(a, (ExecuteAction, EditAction, CreateFile, ApplyPatchAction))
        ok = bool(acts) and all(allowed(a) for a in acts)
        report.event("permission", actions=[f"{type(a).__name__}:{getattr(getattr(a, 'tool_use', None), 'name', '')}"
                                           for a in acts], allowed=ok)
        return req.respond(ToolConfirmationOutcome.PROCEED_ONCE if ok else ToolConfirmationOutcome.CANCEL)

    cfg = SessionConfig(system_prompt=SYSTEM, disabled_tools=LOCAL_TOOLS,
                        mcp_servers=[HttpMcpServerConfig(name="ai-kvm", url=mcp_url)])
    report.write(f"# {task}\n\nplanner `{model}` · KVM MCP `{mcp_url}` · {time.strftime('%Y-%m-%d %H:%M:%S')}")
    report.event("start", task=task, model=model, mcp=mcp_url)
    t0 = time.monotonic()
    async with Session(cwd=os.getcwd(), model=model, config=cfg,
                       interactions=InteractionHandlers(on_permission=permission)) as session:
        report.event("session", id=session.id)
        async with session.stream(task) as stream:
            async for m in stream:
                if isinstance(m, (ToolCall, ToolResult)) and (m.name if isinstance(m, ToolCall) else m.tool_name) in WRAPPERS:
                    continue   # Droid loads and calls MCP tools through these; the KVM tool call itself is logged
                if isinstance(m, ToolCall):
                    report.event("call", tool=m.name, input=m.input, id=m.tool_use_id)
                    report.write(f"**{m.name}** `{json.dumps(m.input, default=str)[:400]}`")
                    log(f"call {m.name} {json.dumps(m.input, default=str)[:160]}")
                elif isinstance(m, ToolResult):
                    texts, frames = [], []
                    for b in content_blocks(m.content):
                        if isinstance(b, dict) and b.get("type") == "image":
                            src = b.get("source") or b
                            frames.append(report.image(src.get("data", ""), src.get("media_type") or src.get("mimeType", "")))
                        elif isinstance(b, dict) and b.get("type") == "text":
                            texts.append(b["text"])
                        elif isinstance(b, str):
                            texts.append(b)
                    report.event("result", tool=m.tool_name, id=m.tool_use_id, error=m.is_error, text="\n".join(texts), frames=frames)
                    body = "\n".join(texts)[:800]
                    report.write(f"> {body.replace(chr(10), chr(10) + '> ')}" + "".join(f"\n\n![{f}]({f})" for f in frames))
                    log(f"  -> {body.splitlines()[0][:160] if body else ''}" + (f" [{len(frames)} frame]" if frames else ""))
                elif isinstance(m, AssistantMessage) and m.text.strip():
                    report.event("say", text=m.text)
                    report.write(m.text)
                    log(f"say {m.text[:200]}")
        r = stream.result
    usage = {k: getattr(r.usage, k) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "thinking_tokens",
                                              "factory_credits")} if r.usage else {}
    report.event("end", subtype=r.subtype, seconds=round(time.monotonic() - t0, 1), usage=usage)
    report.write(f"---\n\n**{r.subtype}** in {time.monotonic() - t0:.0f}s · tokens in {usage.get('input_tokens')} "
                 f"(+{usage.get('cache_read_tokens')} cached) out {usage.get('output_tokens')} · credits {usage.get('factory_credits')}")
    return {"done": r.success, "subtype": r.subtype, "text": r.text, "seconds": round(time.monotonic() - t0, 1)}


def ready(state_file):
    try:
        with open(state_file) as f:
            return json.load(f).get("status") == "ready"
    except (OSError, ValueError):
        return state_file == ""   # no daemon state configured: assume ready


async def watch_inbox(inbox, mcp_url, model, state_file, log=print):
    os.makedirs(inbox, exist_ok=True)
    log(f"watching {inbox}")
    while True:
        tasks = sorted(f for f in os.listdir(inbox) if f.endswith(".task"))
        if tasks and ready(state_file):
            name = tasks[0][:-5]
            path = os.path.join(inbox, tasks[0])
            with open(path) as f:
                task = f.read().strip()
            log(f"task {name}: {task[:120]}")
            report = Report(os.path.join(inbox, name))
            try:
                result = await run_task(task, mcp_url, model, report, log)
            except Exception as e:   # the next task must still run
                result = {"done": False, "subtype": f"{type(e).__name__}: {e}"}
                report.event("error", error=result["subtype"])
            finally:
                report.close()
            os.replace(path, os.path.join(inbox, name + ".done"))
            log(f"task {name}: {json.dumps(result)[:200]}")
        elif tasks:
            log("task waiting: the KVM reports the computer is not ready")
            await asyncio.sleep(5)
        await asyncio.sleep(1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m agent.droid", description=__doc__.splitlines()[0])
    p.add_argument("task", nargs="?")
    p.add_argument("--inbox", help="watch this directory for *.task files instead of running one task")
    p.add_argument("--mcp", default=os.environ.get("AIKVM_MCP", "http://100.93.148.36:8090/mcp"), help="the KVM's MCP endpoint")
    p.add_argument("--model", default=os.environ.get("AIKVM_DROID_MODEL", "custom:Claude-Opus-5.5-0"))
    p.add_argument("--state", default=os.environ.get("AIKVM_STATE", "/run/ai-kvm/state.json"), help="daemon state file; '' to skip the readiness check")
    p.add_argument("--report", default=None, help="report base path for a one-off task (default: runs/<timestamp>)")
    a = p.parse_args(argv)
    if a.inbox:
        asyncio.run(watch_inbox(a.inbox, a.mcp, a.model, a.state))
        return 0
    if not a.task:
        p.error("a task or --inbox is required")
    base = a.report or os.path.join("runs", time.strftime("%Y%m%d-%H%M%S"))
    report = Report(base)
    try:
        result = asyncio.run(run_task(a.task, a.mcp, a.model, report))
    finally:
        report.close()
    print(json.dumps(result)[:600])
    print(f"report: {base}.report.md")
    return 0 if result["done"] else 1


if __name__ == "__main__":
    sys.exit(main())
