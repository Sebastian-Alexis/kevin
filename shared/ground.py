"""Grounder task shared by data building, teacher labelling, eval and the agent (agent.run --grounder-format point): screenshot + target -> point_2d.

The planner names each click target ("click <target>", a short unique description) and the grounder answers one
point on the 0-1000 grid. Same prompt and reply shape for teacher, student and baselines, so scores compare.
"""
import json
import re

PROMPT = ('Locate this element in the screenshot: {target}\n'
          'Reply with its center as {{"point_2d": [x, y]}}, integers 0-1000 relative to the image width and height.')

SCHEMA = {"type": "object", "properties": {"point_2d": {"type": "array", "items": {"type": "integer"},
                                                        "minItems": 2, "maxItems": 2}},
          "required": ["point_2d"], "additionalProperties": False}

VERB = re.compile(r"^(click|double[- ]click|right[- ]click|open|select|choose|press|tap|go to|check|uncheck)\s+(on\s+)?",
                  re.I)
QUOTED = re.compile(r"'([^']*)'")


def target(intent):
    """Instruction in the actor's voice -> target in the planner's voice: "Click the 'Save' button" -> "the 'Save' button"."""
    return VERB.sub("", intent.strip()).strip()


def junk(intent):
    """Labels no planner would name: bare numbers/values, tooltips and sentences, empty quotes."""
    t = target(intent)
    q = QUOTED.search(t)
    text = q.group(1) if q else t
    if not text.strip() or len(text) > 60 or len(text.split()) > 8:
        return True
    return bool(re.fullmatch(r"[\d\s.,:%/+\-–—()$€£]*", text))


def reply(x, y):
    return json.dumps({"point_2d": [int(x), int(y)]})


def parse(text):
    """(x, y) from a reply; tolerates prose around the JSON. None when there is no point."""
    m = re.search(r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", text or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


def inside(pt, bbox, margin=0):
    l, t, r, b = bbox
    return pt is not None and l - margin <= pt[0] <= r + margin and t - margin <= pt[1] <= b + margin
