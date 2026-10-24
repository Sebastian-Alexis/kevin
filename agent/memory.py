"""Memory of computers and workflows, on Moss: what earlier runs learned ("the Factory app adds MCP servers via
the /mcp composer command, not Settings", "this Mac's browser is Helium") is recalled in ~5 ms when a task starts
and fed to the planner as rules, so the agent gets faster on a machine the more it is used there.

    python -m agent.memory --serve 127.0.0.1:8020 --serve 10.42.0.1:8020     # systemd: ai-kvm-memory.service

HTTP (JSON): POST /recall {task, computer} -> {notes: [{id, text}]}; POST /learn {task, computer, outcome,
trajectory, recalled: [ids]} -> {learned: [...], quarantined: [...]}. Lessons are extracted by the planner model
from the trajectory, only from successful runs, and stored with outcome + timestamp + computer. A failed run that
contradicts a recalled note quarantines it (it is no longer recalled); a later success can re-assert it.
Credentials: MOSS_PROJECT_ID / MOSS_PROJECT_KEY (from /etc/ai-kvm/moss.env, root-only).
"""
import argparse
import asyncio
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INDEX = os.environ.get("AIKVM_MEMORY_INDEX", "ai-kvm-playbook")
LESSONS = """A computer-use agent just ran this task on the computer "{computer}" and {verdict}.
Task: {task}
What it did (its notes and actions, in order):
{trajectory}

List up to 3 short, specific, reusable facts this run established that would make the same kind of task faster
or safer next time on this computer: where things are in an app's UI, which app the user uses for what, a
shortcut or sequence that worked, a pitfall and its fix. Only facts actually observed in this run, each
self-contained (name the app or site), no task-specific data (no names, numbers, answers). Reply with JSON only:
{{"lessons": ["...", "..."]}}"""
CONTRADICT = """A computer-use agent ran this task on "{computer}" and failed.
Task: {task}
What it did:
{trajectory}

Before starting it was given these notes from earlier runs:
{notes}
Which of these notes were wrong or misleading in this run (the agent followed them and they did not hold)?
Reply with JSON only: {{"wrong": ["<id>", ...]}} (empty list if none)."""


class Memory:
    def __init__(self, project_id, key, llm_base, llm_model, llm_key=None):
        from moss import MossClient
        self.client = MossClient(project_id, key)
        self.llm_base, self.llm_model, self.llm_key = llm_base.rstrip("/"), llm_model, llm_key
        self.session = None
        self.lock = asyncio.Lock()

    async def open(self):
        self.session = await self.client.session(index_name=INDEX)
        return self

    async def recall(self, task, computer, k=5, min_score=0.25):
        from moss import QueryOptions
        hits = await self.session.query(task, QueryOptions(top_k=k, alpha=0.6, filter={"$and": [
            {"field": "outcome", "condition": {"$eq": "success"}},
            {"field": "suspect", "condition": {"$eq": "no"}},
            {"field": "computer", "condition": {"$in": [computer, "any"]}}]}))
        return [{"id": d.id, "text": d.text, "score": round(d.score, 2)} for d in hits.docs if d.score >= min_score]

    async def learn(self, task, computer, outcome, trajectory, recalled=()):
        from moss import DocumentInfo
        learned, quarantined = [], []
        async with self.lock:
            if outcome == "success":
                reply = self.ask(LESSONS.format(computer=computer, verdict="completed it", task=task, trajectory=trajectory[:6000]))
                for i, text in enumerate(parse(reply).get("lessons", [])[:3]):
                    text = str(text).strip()
                    if len(text) < 12:
                        continue
                    doc_id = f"{computer}-{int(time.time())}-{i}"
                    await self.session.add_docs([DocumentInfo(id=doc_id, text=text, metadata={
                        "computer": computer, "outcome": "success", "suspect": "no", "ts": str(int(time.time()))})])
                    learned.append({"id": doc_id, "text": text})
            elif recalled:
                from moss import GetDocumentsOptions
                docs = await self.session.get_docs(GetDocumentsOptions(doc_ids=list(recalled)))
                notes = "\n".join(f"- [{d.id}] {d.text}" for d in docs)
                reply = self.ask(CONTRADICT.format(computer=computer, task=task, trajectory=trajectory[:6000], notes=notes))
                wrong = {str(w) for w in parse(reply).get("wrong", [])}
                for d in docs:
                    if d.id in wrong:
                        await self.session.add_docs([DocumentInfo(id=d.id, text=d.text, metadata={**(d.metadata or {}), "suspect": "yes",
                                                                                                       "quarantined": str(int(time.time()))})])
                        quarantined.append({"id": d.id, "text": d.text})
            if learned or quarantined:
                await self.session.push_index()
        return {"learned": learned, "quarantined": quarantined}

    def ask(self, prompt):
        body = {"model": self.llm_model, "max_tokens": 400, "temperature": 0,
                "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request(self.llm_base + "/chat/completions", json.dumps(body).encode(),
                                     {"content-type": "application/json", "authorization": "Bearer " + (self.llm_key or "omp")})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)["choices"][0]["message"]["content"] or ""


def parse(text):
    start = text.find("{") if isinstance(text, str) else -1
    if start < 0:
        return {}
    try:
        return json.JSONDecoder().raw_decode(text, start)[0]
    except ValueError:
        return {}


# ---- client side (cu.py, droid.py, the Pi's MCP server): plain HTTP, no Moss SDK needed -------------------------
def recall(base, task, computer, timeout=5):
    try:
        req = urllib.request.Request(base.rstrip("/") + "/recall", json.dumps({"task": task, "computer": computer}).encode(),
                                     {"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r).get("notes", [])
    except Exception:
        return []   # memory is a bonus: a run must never fail because the memory service is down


def learn(base, task, computer, outcome, trajectory, recalled=(), timeout=180):
    try:
        req = urllib.request.Request(base.rstrip("/") + "/learn", json.dumps({
            "task": task, "computer": computer, "outcome": outcome, "trajectory": trajectory, "recalled": list(recalled)}).encode(),
            {"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def notes_text(notes):
    if not notes:
        return ""
    return "What earlier runs learned on this computer (use it, but trust the screen over it):\n" + "\n".join(f"- {n['text']}" for n in notes)


# ---- server ---------------------------------------------------------------------------------------------------
def serve(binds, memory, loop):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            try:
                if self.path == "/recall":
                    out = {"notes": asyncio.run_coroutine_threadsafe(memory.recall(body["task"], body.get("computer", "any")), loop).result(10)}
                elif self.path == "/learn":
                    out = asyncio.run_coroutine_threadsafe(memory.learn(body["task"], body.get("computer", "any"), body.get("outcome", "failure"),
                                                                        body.get("trajectory", ""), body.get("recalled", ())), loop).result(240)
                else:
                    self.send_error(404)
                    return
                data = json.dumps(out).encode()
                self.send_response(200)
            except Exception as e:
                data = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            sys.stderr.write("memory: " + fmt % args + "\n")

    servers = [ThreadingHTTPServer((h, int(p)), H) for h, p in (b.rsplit(":", 1) for b in binds)]
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"memory: serving {' '.join(binds)} index {INDEX}", file=sys.stderr, flush=True)
    servers[0].serve_forever()


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m agent.memory", description=__doc__.splitlines()[0])
    p.add_argument("--serve", action="append", metavar="HOST:PORT", default=None)
    p.add_argument("--llm", default=os.environ.get("AIKVM_PLANNER_BASE", "http://127.0.0.1:4001/v1"))
    p.add_argument("--model", default=os.environ.get("AIKVM_PLANNER_MODEL", "anthropic/claude-sonnet-5-5"))
    p.add_argument("--recall", help="one-off: print what would be recalled for this task")
    p.add_argument("--computer", default="mac")
    a = p.parse_args(argv)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    mem = asyncio.run_coroutine_threadsafe(Memory(os.environ["MOSS_PROJECT_ID"], os.environ["MOSS_PROJECT_KEY"], a.llm, a.model).open(), loop).result(120)
    if a.recall:
        print(json.dumps(asyncio.run_coroutine_threadsafe(mem.recall(a.recall, a.computer), loop).result(30), indent=1))
        return 0
    serve(a.serve or ["127.0.0.1:8020"], mem, loop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
