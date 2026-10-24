"""Serves the input harness page and collects the state it reports: GET / -> page, POST /state <- page,
GET /state -> latest state (for the test), POST /reset -> forget it.
    python3 tests/harness/server.py --bind 100.93.148.36:8098
"""
import argparse, json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HERE = os.path.dirname(os.path.abspath(__file__))
state = {"latest": None}
lock = threading.Lock()
class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path.startswith("/state"):
            with lock: self._send(200, state["latest"] or {})
        else:
            self._send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.startswith("/state"):
            with lock: state["latest"] = json.loads(raw)
        elif self.path.startswith("/reset"):
            with lock: state["latest"] = None
        self._send(200, {"ok": True})
    def log_message(self, *a): pass
if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--bind", default="127.0.0.1:8098"); a = p.parse_args()
    h, pt = a.bind.rsplit(":", 1); ThreadingHTTPServer((h, int(pt)), H).serve_forever()
