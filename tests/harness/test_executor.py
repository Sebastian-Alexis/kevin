"""Executor regression test against the instrumented page: no planner, the computer-tool actions are issued
directly and the page reports the DOM events it actually received.

    python3 tests/harness/server.py --bind 100.93.148.36:8098 &    # on the controller
    python3 tests/harness/test_executor.py                          # opens the page on the Mac, runs, prints a table

Calibration: two clicks at known screenshot pixels; the page reports where they landed in CSS px, which gives the
screenshot->page mapping (scale and offset) for everything else."""
import json, os, sys, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from agent.cu import Session
from agent.kvm import KVM
from agent.run import latest
BASE = os.environ.get("HARNESS", "http://100.93.148.36:8098")
def state():
    with urllib.request.urlopen(BASE + "/state", timeout=5) as r: return json.load(r)
def reset(s):
    urllib.request.urlopen(urllib.request.Request(BASE + "/reset", b"{}", {"content-type": "application/json"}), timeout=5)
    s.do({"action": "key", "text": "F9"}); time.sleep(0.4)
def wait_state(pred, timeout=3.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        st = state()
        if st and pred(st): return st
        time.sleep(0.2)
    return state()
s = Session(KVM(), "", "", None, "macOS", log=lambda *a: None)
s.kvm.actions([{"type": "release"}])
# open the page in the browser (front app must be the browser; cmd+l works in Helium/Chrome/Zen)
s.screen(fresh=True); s.do({"action": "key", "text": "cmd+l"}); time.sleep(0.3); s.do({"action": "type", "text": BASE + "/\n"}); time.sleep(2.5)
s.screen(fresh=True)
W, H = s.size
# calibration: click two points, read page coords
reset(s)
pts = [(W // 4, H // 2), (3 * W // 4, 3 * H // 4)]
got = []
for (x, y) in pts:
    s.do({"action": "left_click", "coordinate": [x, y]}); time.sleep(0.5)
    st = wait_state(lambda st: len(st.get("clicks", [])) > len(got))
    got.append((st["clicks"][-1]["x"], st["clicks"][-1]["y"]))
sx = (got[1][0] - got[0][0]) / (pts[1][0] - pts[0][0]); sy = (got[1][1] - got[0][1]) / (pts[1][1] - pts[0][1])
ox = got[0][0] - pts[0][0] * sx; oy = got[0][1] - pts[0][1] * sy
P = lambda cx, cy: [round((cx - ox) / sx), round((cy - oy) / sy)]   # page css px -> screenshot px
print(f"calibration: scale {sx:.3f},{sy:.3f} offset {ox:.0f},{oy:.0f}")
results = []
def case(name, ok, detail):
    results.append((name, ok, detail)); print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
# 1 resize handle drag: handle centre at (200+200+8-8, 200+120+8-8) = (400, 320) css; drag -80,-40 -> box 120x80
reset(s)
s.do({"action": "left_click_drag", "start_coordinate": P(400, 320), "coordinate": P(320, 280)}); time.sleep(0.6)
st = wait_state(lambda st: any(e.get("t") == "up" for e in st.get("events", [])))
ups = [e for e in st.get("events", []) if e.get("t") == "up"]
case("drag-resize", abs(st["box"]["w"] - 120) <= 6 and abs(st["box"]["h"] - 80) <= 6 and ups and ups[-1]["moves"] >= 10,
     f"box {st['box']} moves {ups[-1]['moves'] if ups else None}")
# 2 slider: track css x 200..600 at y ~412; drag knob from 50% (400) to 80% (520)
reset(s)
s.do({"action": "left_click_drag", "start_coordinate": P(400, 412), "coordinate": P(520, 412)}); time.sleep(0.6)
st = wait_state(lambda st: st.get("slider", 50) != 50)
case("drag-slider", 74 <= st.get("slider", 0) <= 86, f"slider {st.get('slider')}")
# 3 right click
reset(s)
s.do({"action": "right_click", "coordinate": P(800, 260)}); time.sleep(0.5)
st = wait_state(lambda st: st.get("ctxmenu", 0) > 0); s.do({"action": "key", "text": "Escape"})
case("right-click", st.get("ctxmenu", 0) == 1, f"contextmenu events {st.get('ctxmenu')}")
# 4 double click selects a word
reset(s)
s.do({"action": "double_click", "coordinate": P(800, 430)}); time.sleep(0.5)
st = wait_state(lambda st: st.get("dblclick"))
case("double-click", (st.get("dblclick") or "").strip() in ("alpha", "bravo", "charlie"), f"selection {st.get('dblclick')!r}")
# 5 hover tooltip
reset(s)
s.do({"action": "mouse_move", "coordinate": P(800, 550)}); time.sleep(0.5)
st = wait_state(lambda st: st.get("hover", 0) > 0)
case("hover", st.get("hover", 0) >= 1, f"hover events {st.get('hover')}")
# 6 type into a field (click, type)
reset(s)
s.do({"action": "left_click", "coordinate": P(350, 535)}); s.do({"action": "type", "text": "Hello, KVM 123!"}); time.sleep(0.6)
st = wait_state(lambda st: st.get("field"))
case("type", st.get("field") == "Hello, KVM 123!", f"field {st.get('field')!r}")
# 7 scroll inside a div
reset(s)
s.do({"action": "scroll", "coordinate": P(400, 640), "scroll_direction": "down", "scroll_amount": 3}); time.sleep(0.6)
st = wait_state(lambda st: st.get("scroll", 0) > 0)
case("scroll-div", st.get("scroll", 0) > 0, f"scrollTop {st.get('scroll')}")
# 8 modifier click (cmd held)
reset(s)
s.do({"action": "left_click", "coordinate": P(800, 260), "text": "cmd"}); time.sleep(0.5)
st = wait_state(lambda st: st.get("clicks"))
c = (st.get("clicks") or [{}])[-1]
case("cmd-click", c.get("meta") is True, f"last click {c}")
# 9 shift click
reset(s)
s.do({"action": "left_click", "coordinate": P(800, 260), "text": "shift"}); time.sleep(0.5)
st = wait_state(lambda st: st.get("clicks")); c = (st.get("clicks") or [{}])[-1]
case("shift-click", c.get("shift") is True, f"last click {c}")
# 10 key combo reaches the page
reset(s)
s.do({"action": "left_click", "coordinate": P(800, 260)}); s.do({"action": "key", "text": "ctrl+shift+a"}); time.sleep(0.5)
st = wait_state(lambda st: any(k.get("key", "").lower() == "a" for k in st.get("keys", [])))
k = [k for k in st.get("keys", []) if k.get("key", "").lower() == "a"]
case("key-combo", bool(k) and k[-1].get("ctrl") and k[-1].get("shift"), f"keys {k[-1:] }")
# (left_mouse_down/left_mouse_up are not in the tool schema until their split path passes here)
s.kvm.actions([{"type": "release"}])
n = sum(1 for _, ok, _ in results if ok)
print(f"\n{n}/{len(results)} passed")
sys.exit(0 if n == len(results) else 1)
