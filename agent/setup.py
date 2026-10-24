"""First-contact check: run after plugging the KVM into a computer.

    python3 -m agent.setup

Verifies the HID and video links, recognizes macOS's "Choose to Mirror or Extend" placeholder,
quits macOS's Keyboard Setup Assistant (it pops up for any new keyboard and steals keystrokes), and
checks that the USB pointer lands where the agent aims (it does not if the display is extended).
Prints a JSON report; exit status 0 when the agent can drive the computer.
"""
import argparse
import json
import os
import sys
import time

from .kvm import KVM, content_rect
from .run import SYSTEM, parse, point, run
from .vlm import VLM, load_key

PLACEHOLDER_SIZE = (640, 480)   # ustreamer's "NO SIGNAL" frame
POINTER_TOLERANCE = 40          # max error, 0-1000 grid, for the pointer check

STATE = ('Answer with only JSON {"os": "macOS|Windows|Linux|unknown", "choose_mirror_or_extend": true|false, '
         '"keyboard_setup_assistant": true|false, "lock_screen": true|false}. choose_mirror_or_extend means the whole '
         'screen only says to choose Mirror or Extend Display; keyboard_setup_assistant means a "Keyboard Setup '
         'Assistant" window is visible.')
POINTER = 'Locate the mouse pointer (the tip of the arrow cursor). Reply only JSON {"point_2d": [x, y]} normalized to 0-1000.'


def look(kvm, vlm, prompt, max_tokens=60):
    frame = kvm.screenshot()
    if frame.size == PLACEHOLDER_SIZE:
        return None, None
    screen = frame.crop(content_rect(frame))
    text, _, _ = vlm.chat([{"role": "system", "content": SYSTEM},
                           {"role": "user", "content": [vlm.image_part(screen), {"type": "text", "text": prompt}]}],
                          max_tokens)
    return screen, text


def check(kvm, vlm, log=print):
    report = {"ok": False}
    health = kvm.health()
    report["hid"], report["video_service"] = health.get("hid"), health.get("video")
    if not health.get("hid"):
        report["problem"] = "USB HID gadget missing on the Pi"
        return report
    screen, text = look(kvm, vlm, STATE)
    if screen is None:
        report["problem"] = "no HDMI signal: check the cable and wake the computer"
        return report
    state = parse(text)
    report["os"] = state.get("os", "unknown")
    report["screen"] = list(screen.size)
    log(f"screen {screen.size[0]}x{screen.size[1]}, state {state}")
    if state.get("choose_mirror_or_extend"):
        report["problem"] = ("macOS is waiting for a display choice: pick Mirror in the Screen Mirroring menu "
                             "(then run host/macos/kvm-mirror.swift to keep the built-in resolution)")
        return report
    if state.get("lock_screen"):
        report["problem"] = "the computer is locked: unlock it first"
        return report
    if state.get("keyboard_setup_assistant"):
        result = run("Quit the Keyboard Setup Assistant window (click its Quit button, or click the window and "
                     "press cmd+q). Do nothing else.", kvm, vlm, report["os"], max_steps=4, log=log)
        report["keyboard_setup_assistant"] = "quit" if result["done"] else f"not quit: {result['fail']}"
    errors = []
    for fx, fy in ((0.3, 0.3), (0.7, 0.7)):
        kvm.actions([{"type": "move", "x": fx, "y": fy}])
        time.sleep(0.4)
        _, text = look(kvm, vlm, POINTER, 24)
        x, y = point(text)
        errors.append(max(abs(x - fx * 1000), abs(y - fy * 1000)))
    report["pointer_error"] = round(max(errors))
    if report["pointer_error"] > POINTER_TOLERANCE:
        report["problem"] = ("the USB pointer does not land where the capture shows it: the KVM display is probably "
                             "extended, not mirrored (macOS: Mirror in the Screen Mirroring menu; Windows: Win+P, Duplicate)")
        return report
    report["ok"] = True
    return report


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m agent.setup", description=__doc__.splitlines()[0])
    p.add_argument("--kvm", default=os.environ.get("AIKVM_URL", "http://10.42.0.101:8080"))
    p.add_argument("--llm", default=os.environ.get("AIKVM_LLM_BASE", "http://127.0.0.1:8000/v1"))
    p.add_argument("--model", default=os.environ.get("AIKVM_LLM_MODEL", "qwen122"))
    p.add_argument("--key-file", default=os.environ.get("AIKVM_LLM_KEY_FILE", "/etc/gemma4-vllm-api-key"))
    a = p.parse_args(argv)
    report = check(KVM(a.kvm), VLM(a.llm, a.model, key=load_key(a.key_file)))
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
