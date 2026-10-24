"""Actor action schema: the single definition used by datagen, training export, eval and inference.

Every training label and every actor output is ONE JSON object, coordinates 0..1000 relative to the screenshot:
  {"action": "click",  "x": 512, "y": 300}                      # optional "button": left|right|middle, "count": 1|2
  {"action": "type",   "text": "quarterly report"}
  {"action": "key",    "combo": "cmd+c"}                        # names match pi/api/hid.py (MOD / KEY)
  {"action": "scroll", "x": 500, "y": 500, "ticks": -3}         # ticks > 0 scrolls up, < 0 down (HID wheel)
  {"action": "drag",   "x": 100, "y": 200, "to_x": 400, "to_y": 200}

to_pi() converts to the Pi control API (pi/api/server.py), which takes 0..1 floats when width/height are omitted.
"""
import json
import re

COORD_MAX = 1000
ACTIONS = ("click", "type", "key", "scroll", "drag")
BUTTONS = ("left", "right", "middle")

# Modifier / key names accepted by pi/api/hid.py, plus friendly aliases we normalise away.
MODIFIERS = {"ctrl", "shift", "alt", "cmd", "meta", "super", "win"}
KEY_ALIASES = {"option": "alt", "opt": "alt", "command": "cmd", "control": "ctrl", "return": "enter",
               "escape": "esc", "del": "delete", "pgup": "pageup", "pgdn": "pagedown", "arrowup": "up",
               "arrowdown": "down", "arrowleft": "left", "arrowright": "right", ".": "dot", "-": "minus",
               "=": "equal", "[": "leftbrace", "]": "rightbrace", "/": "slash", ",": "comma", ";": "semicolon",
               "'": "apostrophe", "`": "grave", "\\": "backslash"}
NAMED_KEYS = {"enter", "esc", "backspace", "tab", "space", "minus", "equal", "leftbrace", "rightbrace",
              "backslash", "semicolon", "apostrophe", "grave", "comma", "dot", "slash", "capslock",
              "printscreen", "scrolllock", "pause", "insert", "home", "pageup", "delete", "end", "pagedown",
              "right", "left", "down", "up", "numlock", "menu", *(f"f{i}" for i in range(1, 13))}


class ActionError(ValueError):
    pass


def _coord(a, k):
    v = a.get(k)
    if not isinstance(v, (int, float)) or not 0 <= v <= COORD_MAX:
        raise ActionError(f"{k} must be a number in 0..{COORD_MAX}, got {v!r}")
    return int(round(v))


def normalize_combo(combo):
    parts = [KEY_ALIASES.get(p.strip().lower(), p.strip().lower()) for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise ActionError("empty key combo")
    for p in parts:
        if p not in MODIFIERS and p not in NAMED_KEYS and not (len(p) == 1 and p.isascii() and p.isalnum()):
            raise ActionError(f"unknown key {p!r} in combo {combo!r}")
    mods = sorted((p for p in parts if p in MODIFIERS), key=["cmd", "ctrl", "alt", "shift", "meta", "super", "win"].index)
    keys = [p for p in parts if p not in MODIFIERS]
    return "+".join(mods + keys)


def validate(a):
    """Return a canonical copy of action `a`, or raise ActionError."""
    if not isinstance(a, dict):
        raise ActionError("action must be an object")
    kind = a.get("action")
    if kind == "click":
        out = {"action": "click", "x": _coord(a, "x"), "y": _coord(a, "y")}
        if a.get("button", "left") != "left":
            if a["button"] not in BUTTONS:
                raise ActionError(f"bad button {a['button']!r}")
            out["button"] = a["button"]
        if int(a.get("count", 1)) != 1:
            out["count"] = int(a["count"])
        return out
    if kind == "type":
        text = a.get("text")
        if not isinstance(text, str) or not text:
            raise ActionError("type needs non-empty text")
        if any(ord(c) > 126 or (ord(c) < 32 and c not in "\n\t") for c in text):
            raise ActionError("type text must be printable ASCII (US layout HID)")
        return {"action": "type", "text": text}
    if kind == "key":
        return {"action": "key", "combo": normalize_combo(a.get("combo", ""))}
    if kind == "scroll":
        ticks = int(a.get("ticks", 0))
        if ticks == 0:
            raise ActionError("scroll needs non-zero ticks")
        return {"action": "scroll", "x": _coord(a, "x"), "y": _coord(a, "y"), "ticks": ticks}
    if kind == "drag":
        return {"action": "drag", "x": _coord(a, "x"), "y": _coord(a, "y"),
                "to_x": _coord(a, "to_x"), "to_y": _coord(a, "to_y")}
    raise ActionError(f"unknown action {kind!r}; expected one of {ACTIONS}")


def dumps(a):
    """Canonical compact serialisation used as the training target string."""
    return json.dumps(validate(a), separators=(",", ":"))


_JSON_OBJ = re.compile(r"\{.*\}", re.S)


def parse_model_output(text):
    """Pull the first JSON action out of raw model text. Raises ActionError if none is valid."""
    m = _JSON_OBJ.search(text or "")
    if not m:
        raise ActionError(f"no JSON object in model output: {text!r}")
    try:
        return validate(json.loads(m.group(0)))
    except json.JSONDecodeError as e:
        raise ActionError(f"bad JSON in model output: {e}") from e


def to_pi(a):
    """Actor action -> pi/api/server.py action object (normalised 0..1 floats)."""
    a = validate(a)
    n = lambda v: round(v / COORD_MAX, 5)  # noqa: E731
    kind = a["action"]
    if kind == "click":
        return {"type": "click", "x": n(a["x"]), "y": n(a["y"]),
                "button": a.get("button", "left"), "count": a.get("count", 1)}
    if kind == "type":
        return {"type": "type", "text": a["text"]}
    if kind == "key":
        return {"type": "key", "combo": a["combo"]}
    if kind == "scroll":
        return {"type": "scroll", "x": n(a["x"]), "y": n(a["y"]), "ticks": a["ticks"]}
    return {"type": "drag", "x": n(a["x"]), "y": n(a["y"]), "to_x": n(a["to_x"]), "to_y": n(a["to_y"])}


def bbox_to_click(bbox):
    """[l, t, r, b] in 0..1000 -> click at its centre."""
    l, t, r, b = bbox
    return {"action": "click", "x": int(round((l + r) / 2)), "y": int(round((t + b) / 2))}


def point_in_bbox(x, y, bbox, pad=0):
    l, t, r, b = bbox
    return l - pad <= x <= r + pad and t - pad <= y <= b + pad
