"""OpenAI-compatible HTTP front for `omp auth-gateway stdio`, so OpenAI-SDK clients (Agent S3) can use the cloud
models omp is logged into, with no API key on disk.

    python3 -m agent.omp_gateway            # serves http://127.0.0.1:4001/v1 until killed

Each HTTP request becomes one JSON line to the omp child process; the answer line becomes the HTTP response.
`model` takes any omp selector (provider/id, fuzzy name, @role). Only a trusted local client should reach it.
"""
import itertools
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Gateway:
    def __init__(self):
        self.proc = subprocess.Popen(["omp", "auth-gateway", "stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=sys.stderr, text=True, bufsize=1)
        ready = json.loads(self.proc.stdout.readline())
        if not ready.get("ready"):
            raise RuntimeError(f"omp auth-gateway did not come up: {ready}")
        self.ids = itertools.count(1)
        self.lock = threading.Lock()
        self.waiting = {}   # id -> (Event, [response])
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                slot = self.waiting.pop(msg.get("id"), None)
            if slot:
                slot[1].append(msg)
                slot[0].set()

    def request(self, path, body=None, method=None):
        rid = next(self.ids)
        req = {"id": rid, "path": path}
        if body is not None:
            req["body"] = body
        if method:
            req["method"] = method
        ev, out = threading.Event(), []
        with self.lock:
            self.waiting[rid] = (ev, out)
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        ev.wait()
        return out[0]


def fix_thinking(body):
    """Droid sends Anthropic's newer `thinking.type: between_tools` (and friends); omp's adapter only knows
    enabled/disabled/adaptive. Map anything else to adaptive so the request is not rejected."""
    th = body.get("thinking") if isinstance(body, dict) else None
    if isinstance(th, dict) and th.get("type") not in (None, "enabled", "disabled", "adaptive"):
        body["thinking"] = {"type": "adaptive"}


def fix_images(body):
    """Agent S3's vLLM path emits `data:image;base64,` (no MIME subtype); Codex rejects that. Our frames are PNG."""
    for m in body.get("messages", []) if isinstance(body, dict) else []:
        for part in m.get("content", []) if isinstance(m.get("content"), list) else []:
            url = part.get("image_url", {}).get("url", "") if isinstance(part, dict) else ""
            if url.startswith("data:image;base64,"):
                part["image_url"]["url"] = "data:image/png;base64," + url[len("data:image;base64,"):]


def make_handler(gw, key=None):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, msg):
            body = msg.get("body")
            data = (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(msg.get("status", 200))
            self.send_header("Content-Type", "text/event-stream" if isinstance(body, str) else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            """Loopback clients are trusted; anyone else must present the key (Anthropic x-api-key or OpenAI bearer)."""
            if key is None or self.client_address[0].startswith("127."):
                return True
            got = self.headers.get("x-api-key") or self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if got == key:
                return True
            self.send_error(401, "invalid API key")
            return False

        def do_GET(self):
            if self._authorized():
                self._reply(gw.request(self.path, method="GET"))

        def do_POST(self):
            if not self._authorized():
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                body = json.loads(raw)
            except ValueError:
                self.send_error(400, "body is not JSON")
                return
            fix_images(body)
            fix_thinking(body)
            t0 = time.monotonic()
            msg = gw.request(self.path, body)
            if os.environ.get("OMP_GATEWAY_TRACE"):
                out = msg.get("body") if isinstance(msg.get("body"), dict) else {}
                u = out.get("usage", {}) if isinstance(out, dict) else {}
                n_img = sum(1 for m in body.get("messages", []) for c in (m.get("content") if isinstance(m.get("content"), list) else [])
                            if isinstance(c, dict) and (c.get("type") == "image" or (c.get("type") == "tool_result" and any(isinstance(b, dict) and b.get("type") == "image" for b in (c.get("content") or [])))))
                sys_len = len(json.dumps(body.get("system", ""))); tool_len = len(json.dumps(body.get("tools") or []))
                if os.environ.get("OMP_GATEWAY_DUMP"):
                    with open(os.environ["OMP_GATEWAY_DUMP"], "a") as f:
                        f.write(json.dumps({"tools": [t.get("name") for t in body.get("tools") or []], "system": body.get("system"), "max_tokens": body.get("max_tokens"), "n_msgs": len(body.get("messages") or [])}) + "\n")
                sys.stderr.write(f"trace {self.path} model={body.get('model')} {time.monotonic() - t0:.2f}s in={u.get('input_tokens')} cached={u.get('cache_read_input_tokens')} out={u.get('output_tokens')} system_chars={sys_len} tools_chars={tool_len} "
                                 f"tools={len(body.get('tools') or [])} msgs={len(body.get('messages') or [])} imgs={n_img} max_tokens={body.get('max_tokens')} stop={out.get('stop_reason') if isinstance(out, dict) else ''}\n")
            self._reply(msg)

        def log_message(self, fmt, *args):
            sys.stderr.write("omp_gateway: " + fmt % args + "\n")
    return Handler


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bind", action="append", default=None, metavar="HOST:PORT",
                   help="listen here (repeatable; default 127.0.0.1:4001)")
    p.add_argument("--key-file", default=None, help="require this API key from clients (needed on any non-loopback bind)")
    a = p.parse_args(argv)
    key = open(a.key_file).read().strip() if a.key_file else None
    binds = a.bind or ["127.0.0.1:4001"]
    if key is None and any(not b.startswith("127.") for b in binds):
        p.error("a non-loopback bind needs --key-file")
    gw = Gateway()
    servers = [ThreadingHTTPServer((h, int(pt)), make_handler(gw, key)) for h, pt in (b.rsplit(":", 1) for b in binds)]
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"serving {' '.join(binds)} via omp auth-gateway stdio{' (key required)' if key else ''}", file=sys.stderr, flush=True)
    servers[0].serve_forever()

if __name__ == "__main__":
    main()
