"""ai-kvm control API: HTTP/JSON for agents. Stdlib only (runs on ARMv6 Pi OS python3).

GET  /health                      -> {"ok": true, "hid": bool, "video": bool}
GET  /screenshot                  -> image/jpeg (latest frame from ustreamer)
GET  /stream                      -> MJPEG proxy redirect target (ustreamer /stream)
POST /actions  {"actions": [...]} -> run a batch sequentially, one HTTP round trip

Action objects (also accepted singly at POST /action):
  {"type": "move",   "x": 960, "y": 540, "width": 1920, "height": 1080}
  {"type": "click",  "x": .., "y": .., "width": .., "height": .., "button": "left", "count": 1}
  {"type": "drag",   "x": .., "y": .., "to_x": .., "to_y": .., "width": .., "height": .., "button": "left"}
  {"type": "scroll", "ticks": -3}
  {"type": "type",   "text": "hello\\n"}
  {"type": "key",    "combo": "ctrl+alt+delete"}
  {"type": "wait",   "ms": 200}
x/y are pixels in a width x height frame; omit width/height to pass normalised 0..1 floats.
"""
import json
import os
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hid import Keyboard, Mouse  # noqa: E402

PORT = int(os.environ.get("AIKVM_PORT", "8080"))
STREAM = os.environ.get("AIKVM_STREAM", "http://127.0.0.1:8081")

kbd = Keyboard()
mouse = Mouse()


def _abs(a, kx, ky):
    w, h = a.get("width"), a.get("height")
    x, y = a[kx], a[ky]
    if w and h:
        return Mouse.norm(x, w), Mouse.norm(y, h)
    return int(float(x) * Mouse.MAX), int(float(y) * Mouse.MAX)


def run_action(a):
    t = a.get("type")
    if t == "move":
        mouse.move(*_abs(a, "x", "y"))
    elif t == "click":
        if "x" in a:
            mouse.move(*_abs(a, "x", "y"))
            time.sleep(0.02)
        mouse.click(a.get("button", "left"), int(a.get("count", 1)))
    elif t == "drag":
        b = a.get("button", "left")
        mouse.move(*_abs(a, "x", "y"))
        mouse.press(b)
        x0, y0 = mouse.x, mouse.y
        x1, y1 = _abs(a, "to_x", "to_y")
        steps = int(a.get("steps", 12))
        for i in range(1, steps + 1):
            mouse.move(x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps)
            time.sleep(0.01)
        mouse.release(b)
    elif t == "scroll":
        if "x" in a:
            mouse.move(*_abs(a, "x", "y"))
        mouse.scroll(int(a["ticks"]))
    elif t == "type":
        kbd.type_text(a["text"])
    elif t == "key":
        kbd.combo(a["combo"])
    elif t == "wait":
        time.sleep(int(a["ms"]) / 1000)
    else:
        raise ValueError(f"unknown action type {t!r}")


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-kvm/0.1"

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        if self.path == "/health":
            video = True
            try:
                urllib.request.urlopen(STREAM + "/state", timeout=1).read()
            except Exception:
                video = False
            hid = os.path.exists(kbd.dev) and os.path.exists(mouse.dev)
            return self._json(200, {"ok": True, "hid": hid, "video": video})
        if self.path.startswith("/screenshot"):
            try:
                r = urllib.request.urlopen(STREAM + "/snapshot", timeout=3)
                data = r.read()
            except Exception as e:
                return self._json(503, {"error": f"video unavailable: {e}"})
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/stream":
            self.send_response(302)
            host = self.headers.get("Host", "").split(":")[0]
            self.send_header("Location", f"http://{host}:8081/stream")
            self.end_headers()
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/action":
                actions = [req]
            elif self.path == "/actions":
                actions = req["actions"]
            else:
                return self._json(404, {"error": "not found"})
            t0 = time.monotonic()
            for i, a in enumerate(actions):
                try:
                    run_action(a)
                except Exception as e:
                    kbd.release_all()
                    return self._json(400, {"error": str(e), "failed_index": i})
            self._json(200, {"ok": True, "count": len(actions),
                             "ms": round((time.monotonic() - t0) * 1000, 1)})
        except Exception as e:
            self._json(400, {"error": str(e)})


if __name__ == "__main__":
    print(f"ai-kvm api on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
