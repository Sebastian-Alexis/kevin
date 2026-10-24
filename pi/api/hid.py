"""Raw HID report writers for the configfs gadget (/dev/hidg0 keyboard, /dev/hidg1 absolute mouse)."""
import os
import select
import struct
import threading
import time

KBD_DEV = os.environ.get("AIKVM_KBD", "/dev/hidg0")
MOUSE_DEV = os.environ.get("AIKVM_MOUSE", "/dev/hidg1")

MOD = {
    "ctrl": 0x01, "lctrl": 0x01, "shift": 0x02, "lshift": 0x02, "alt": 0x04, "lalt": 0x04,
    "meta": 0x08, "super": 0x08, "win": 0x08, "cmd": 0x08, "lmeta": 0x08,
    "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "altgr": 0x40, "rmeta": 0x80,
}

# HID usage IDs (keyboard page 0x07)
KEY = {
    **{c: 0x04 + i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")},
    **{c: 0x1E + i for i, c in enumerate("1234567890")},
    "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29, "backspace": 0x2A, "tab": 0x2B,
    "space": 0x2C, "minus": 0x2D, "equal": 0x2E, "leftbrace": 0x2F, "rightbrace": 0x30,
    "backslash": 0x31, "semicolon": 0x33, "apostrophe": 0x34, "grave": 0x35, "comma": 0x36,
    "dot": 0x37, "period": 0x37, "slash": 0x38, "capslock": 0x39,
    **{f"f{i}": 0x3A + i - 1 for i in range(1, 13)},
    "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48, "insert": 0x49, "home": 0x4A,
    "pageup": 0x4B, "delete": 0x4C, "del": 0x4C, "end": 0x4D, "pagedown": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52, "numlock": 0x53, "menu": 0x65,
}

# ASCII -> (usage, needs_shift) for US layout
_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "minus", "+": "equal", "{": "leftbrace", "}": "rightbrace", "|": "backslash", ":": "semicolon",
    '"': "apostrophe", "~": "grave", "<": "comma", ">": "dot", "?": "slash",
}
_PLAIN = {
    "-": "minus", "=": "equal", "[": "leftbrace", "]": "rightbrace", "\\": "backslash", ";": "semicolon",
    "'": "apostrophe", "`": "grave", ",": "comma", ".": "dot", "/": "slash", " ": "space",
    "\n": "enter", "\t": "tab",
}


def ascii_to_key(ch):
    if ch.isalpha() and ch.isascii():
        return KEY[ch.lower()], ch.isupper()
    if ch in KEY:
        return KEY[ch], False
    if ch in _PLAIN:
        return KEY[_PLAIN[ch]], False
    if ch in _SHIFTED:
        return KEY[_SHIFTED[ch]], True
    raise ValueError(f"unmappable character {ch!r}")


WRITE_TIMEOUT = float(os.environ.get("AIKVM_HID_TIMEOUT", "0.5"))
# Writes block until the host polls the previous report, so typing self-paces at the endpoint rate.
# Measured on a Linux host: 0/1 ms is lossless (~4 ms/char floor); 4/2 leaves margin for slower targets.
KEY_HOLD = float(os.environ.get("AIKVM_KEY_HOLD_MS", "4")) / 1000
KEY_GAP = float(os.environ.get("AIKVM_KEY_GAP_MS", "2")) / 1000


class HostNotConnected(OSError):
    """No USB host is polling the gadget (cable unplugged or target asleep)."""


def _write(obj, buf):
    """Write one report on a persistent non-blocking fd.

    Blocks at most WRITE_TIMEOUT for the host to poll the endpoint; raises HostNotConnected otherwise.
    Reopens once if the host re-enumerated (ENODEV/ESHUTDOWN).
    """
    for attempt in (0, 1):
        if getattr(obj, "fd", None) is None:
            obj.fd = os.open(obj.dev, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK)
        try:
            os.write(obj.fd, buf)
            return
        except BlockingIOError:
            _, w, _ = select.select([], [obj.fd], [], WRITE_TIMEOUT)
            if not w:
                raise HostNotConnected("USB host not polling HID endpoint (target unplugged, asleep, or no input consumer open)")
            os.write(obj.fd, buf)
            return
        except OSError:
            os.close(obj.fd)
            obj.fd = None
            if attempt:
                raise


class Keyboard:
    def __init__(self, dev=KBD_DEV):
        self.dev = dev
        self.lock = threading.Lock()

    def _report(self, mods=0, keys=()):
        buf = bytes([mods, 0, *keys, *([0] * (6 - len(keys)))])
        _write(self, buf)

    def release_all(self):
        with self.lock:
            self._report()

    def tap(self, usage, mods=0, hold=None):
        hold = KEY_HOLD if hold is None else hold
        with self.lock:
            self._report(mods, [usage])
            time.sleep(hold)
            self._report()

    def combo(self, spec):
        """'ctrl+alt+delete', 'shift+tab', 'win+r', 'f5'"""
        mods, keys = 0, []
        for part in spec.lower().split("+"):
            part = part.strip()
            if part in MOD:
                mods |= MOD[part]
            elif part in KEY:
                keys.append(KEY[part])
            elif len(part) == 1:
                u, sh = ascii_to_key(part)
                keys.append(u)
                if sh:
                    mods |= MOD["shift"]
            else:
                raise ValueError(f"unknown key {part!r}")
        if len(keys) > 6:
            raise ValueError("max 6 simultaneous keys")
        with self.lock:
            self._report(mods, keys)
            time.sleep(0.03)
            self._report()

    def hold(self, spec):
        """Press and hold modifiers ('cmd', 'cmd+shift') until release_all(): for modifier-clicks."""
        mods = 0
        for part in spec.lower().split("+"):
            part = part.strip()
            if part not in MOD:
                raise ValueError(f"not a modifier: {part!r}")
            mods |= MOD[part]
        with self.lock:
            self._report(mods)

    def type_text(self, text, delay=None):
        delay = KEY_GAP if delay is None else delay
        for ch in text:
            usage, shift = ascii_to_key(ch)
            self.tap(usage, MOD["shift"] if shift else 0)
            time.sleep(delay)


class Mouse:
    """Absolute pointer, coordinates normalised to 0..32767 on both axes."""
    MAX = 32767
    BTN = {"left": 1, "right": 2, "middle": 4}

    def __init__(self, dev=MOUSE_DEV):
        self.dev = dev
        self.lock = threading.Lock()
        self.x = self.y = self.MAX // 2
        self.buttons = 0

    def _report(self, wheel=0):
        buf = struct.pack("<BHHb", self.buttons, self.x, self.y, wheel)
        _write(self, buf)

    @classmethod
    def norm(cls, v, size):
        return max(0, min(cls.MAX, int(round(v * cls.MAX / max(1, size - 1)))))

    def move(self, x, y):
        with self.lock:
            self.x, self.y = max(0, min(self.MAX, x)), max(0, min(self.MAX, y))
            self._report()

    def press(self, button="left"):
        with self.lock:
            self.buttons |= self.BTN[button]
            self._report()

    def release(self, button="left"):
        with self.lock:
            self.buttons &= ~self.BTN[button]
            self._report()

    def click(self, button="left", count=1, hold=0.03):
        for i in range(count):
            self.press(button)
            time.sleep(hold)
            self.release(button)
            if i + 1 < count:
                time.sleep(0.06)

    def scroll(self, ticks):
        step = 1 if ticks > 0 else -1
        with self.lock:
            for _ in range(abs(int(ticks))):
                self._report(wheel=step)
                time.sleep(0.01)
