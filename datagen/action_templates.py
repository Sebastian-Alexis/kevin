"""Source E: non-click actions (key combos, scroll, type) templated onto real frames from the other sources.

Without these the actor only ever sees clicks. Frames are hard-linked from existing shards, no copies.

  python datagen/action_templates.py --from osatlas,web,ax -n 2500 --keys macos
"""
import argparse
import os
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Shard, iter_shards  # noqa: E402

# (intent paraphrases, mac combo, windows/linux combo). None = not applicable on that OS.
KEYS = [
    (["Copy the selected text", "Copy the selection", "Copy that"], "cmd+c", "ctrl+c"),
    (["Paste", "Paste the clipboard", "Paste the copied text"], "cmd+v", "ctrl+v"),
    (["Cut the selected text", "Cut the selection"], "cmd+x", "ctrl+x"),
    (["Undo", "Undo the last change", "Undo that"], "cmd+z", "ctrl+z"),
    (["Redo", "Redo the last change"], "cmd+shift+z", "ctrl+y"),
    (["Select all", "Select all the text", "Select everything"], "cmd+a", "ctrl+a"),
    (["Save the file", "Save", "Save the document"], "cmd+s", "ctrl+s"),
    (["Open a new tab", "Open a new browser tab"], "cmd+t", "ctrl+t"),
    (["Close this tab", "Close the current tab", "Close the window"], "cmd+w", "ctrl+w"),
    (["Open a new window", "Create a new window"], "cmd+n", "ctrl+n"),
    (["Find on the page", "Open the find bar", "Search within this page"], "cmd+f", "ctrl+f"),
    (["Quit the app", "Quit this application"], "cmd+q", "alt+f4"),
    (["Open Spotlight search", "Open Spotlight"], "cmd+space", None),
    (["Open the Start menu"], None, "win"),
    (["Switch to the next app", "Switch apps"], "cmd+tab", "alt+tab"),
    (["Reload the page", "Refresh the page"], "cmd+r", "ctrl+r"),
    (["Focus the address bar", "Select the URL bar", "Go to the address bar"], "cmd+l", "ctrl+l"),
    (["Print this page", "Open the print dialog"], "cmd+p", "ctrl+p"),
    (["Open the app's settings", "Open preferences"], "cmd+comma", None),
    (["Minimize the window", "Minimize this window"], "cmd+m", "win+down"),
    (["Hide this app"], "cmd+h", None),
    (["Go back", "Go back to the previous page", "Navigate back"], "cmd+leftbrace", "alt+left"),
    (["Go forward", "Navigate forward"], "cmd+rightbrace", "alt+right"),
    (["Zoom in", "Make the page bigger"], "cmd+equal", "ctrl+equal"),
    (["Zoom out", "Make the page smaller"], "cmd+minus", "ctrl+minus"),
    (["Make the text bold", "Bold the selection"], "cmd+b", "ctrl+b"),
    (["Italicize the selection", "Make the text italic"], "cmd+i", "ctrl+i"),
    (["Open a file", "Show the open file dialog"], "cmd+o", "ctrl+o"),
    (["Create a new folder"], "cmd+shift+n", "ctrl+shift+n"),
    (["Reopen the last closed tab"], "cmd+shift+t", "ctrl+shift+t"),
    (["Press Enter", "Press Return", "Submit", "Confirm with Enter"], "enter", "enter"),
    (["Press Escape", "Dismiss the dialog with Escape", "Close the popup"], "esc", "esc"),
    (["Press Tab", "Move to the next field"], "tab", "tab"),
    (["Move to the previous field", "Press Shift+Tab"], "shift+tab", "shift+tab"),
    (["Delete the previous character", "Press Backspace"], "backspace", "backspace"),
    (["Delete the selected item", "Move the selected file to the Trash"], "cmd+backspace", "delete"),
    (["Press the down arrow", "Move down one item"], "down", "down"),
    (["Press the up arrow", "Move up one item"], "up", "up"),
    (["Page down", "Press Page Down"], "pagedown", "pagedown"),
    (["Go to the top of the page", "Jump to the top"], "cmd+up", "ctrl+home"),
    (["Go to the bottom of the page", "Jump to the end"], "cmd+down", "ctrl+end"),
    (["Take a screenshot of a region"], "cmd+shift+4", "win+shift+s"),
]

SCROLL = [
    (["Scroll down", "Scroll down a bit", "Scroll down the page"], -3),
    (["Scroll down a lot", "Scroll further down the page"], -8),
    (["Scroll up", "Scroll up a bit", "Scroll back up"], 3),
    (["Scroll up a lot", "Scroll back to near the top"], 8),
]

TYPE = ["hello", "Thanks, see you tomorrow!", "meeting at 3pm", "cal hacks demo", "2026 budget.xlsx",
        "https://github.com", "weather in San Francisco", "John Appleseed", "groceries: eggs, milk, bread",
        "ai-kvm", "Q3 planning", "npm run build", "ls -la", "sudo reboot", "flights SFO to JFK"]


def link(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="sources", default="osatlas,web,ax", help="shard sources to borrow frames from")
    ap.add_argument("-n", type=int, default=2500)
    ap.add_argument("--keys", choices=["macos", "windows", "auto"], default="auto",
                    help="combo family; auto = per frame os (web frames count as macos)")
    ap.add_argument("--mix", default="key=0.55,scroll=0.2,type=0.25")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    mix = {k: float(v) for k, v in (p.split("=") for p in args.mix.split(","))}
    frames = []
    for src in args.sources.split(","):
        for d, meta, steps in iter_shards(src.strip()):
            for f in sorted({s["frame"] for s in steps}):
                os_name = next((s.get("os") for s in steps if s["frame"] == f), meta.get("os", "macos"))
                frames.append((d / f, os_name))
    if not frames:
        sys.exit("no frames found: generate osatlas / web / ax data first")
    print(f"{len(frames)} source frames")

    with Shard("templates", seed=args.seed, keys=args.keys) as shard:
        counts = {"key": 0, "scroll": 0, "type": 0}
        while shard.n_steps < args.n:
            path, os_name = rng.choice(frames)
            kind = rng.choices(list(mix), weights=list(mix.values()))[0]
            if kind == "key":
                fam = args.keys if args.keys != "auto" else ("windows" if os_name in ("windows", "linux") else "macos")
                intents, mac, win = rng.choice(KEYS)
                combo = mac if fam == "macos" else win
                if combo is None:
                    continue
            rel = f"frames/{shard.n_frames:06d}.jpg"
            link(path, shard.dir / rel)
            shard.n_frames += 1
            if kind == "key":
                ok = shard.add_step(rel, rng.choice(intents), {"action": "key", "combo": combo}, os=os_name)
            elif kind == "scroll":
                intents, ticks = rng.choice(SCROLL)
                x, y = rng.randint(300, 700), rng.randint(350, 700)
                ok = shard.add_step(rel, rng.choice(intents), {"action": "scroll", "x": x, "y": y, "ticks": ticks}, os=os_name)
            else:
                text = rng.choice(TYPE)
                ok = shard.add_step(rel, rng.choice([f"Type '{text}'", f"Type \"{text}\"", f"Enter the text '{text}'"]),
                                    {"action": "type", "text": text}, os=os_name)
            counts[kind] += ok
        print(f"wrote {shard.n_steps} samples to {shard.dir}: {counts}")


if __name__ == "__main__":
    main()
