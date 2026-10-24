"""Controller daemon: notice when the KVM is plugged into a computer and make it drivable automatically.

    python3 -m agent.daemon

Polls the Pi's /health. A new session starts when the Pi boots (new boot_id: it is powered by the target's
USB-C, so this is a plug-in), when a USB host attaches, or when HDMI signal appears. Each new session runs
agent.setup.first_contact(); the outcome is logged and written to the state file, which `python3 -m agent`
users (or anything else) can read to know the computer is ready.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

from .kvm import KVM
from .setup import first_contact
from .vlm import VLM, load_key

LEASES = "/var/lib/ai-kvm-ap/leases"
CLICK_FREE = ("no USB host", "no HDMI signal", "display still not configured", "video lost")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def discover(default, leases=LEASES, port=8080):
    """KVM API URL: the newest DHCP lease named ai-kvm* on the dev AP, else the default."""
    try:
        with open(leases) as f:
            rows = [line.split() for line in f if len(line.split()) >= 4]
    except OSError:
        return default
    rows = [r for r in rows if r[3].startswith("ai-kvm")]
    if not rows:
        return default
    newest = max(rows, key=lambda r: int(r[0]))
    return f"http://{newest[2]}:{port}"


def health(url):
    try:
        with urllib.request.urlopen(url + "/health", timeout=2) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def write_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m agent.daemon", description=__doc__.splitlines()[0])
    p.add_argument("--kvm", default=os.environ.get("AIKVM_URL", "http://10.42.0.101:8080"))
    p.add_argument("--llm", default=os.environ.get("AIKVM_LLM_BASE", "http://127.0.0.1:8000/v1"))
    p.add_argument("--model", default=os.environ.get("AIKVM_LLM_MODEL", "qwen27"))
    p.add_argument("--key-file", default=os.environ.get("AIKVM_LLM_KEY_FILE", "/etc/gemma4-vllm-api-key"))
    p.add_argument("--state", default=os.environ.get("AIKVM_STATE", "/run/ai-kvm/state.json"))
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--retry-after", type=float, default=20, help="seconds before retrying a failed setup")
    p.add_argument("--retries", type=int, default=5, help="setup attempts per session")
    a = p.parse_args(argv)
    os.makedirs(os.path.dirname(a.state), exist_ok=True)
    vlm = VLM(a.llm, a.model, key=load_key(a.key_file))
    url, last, state = a.kvm, None, {"status": "waiting for the KVM", "since": time.time()}
    attempts, heavy_failures, retry_at = 0, 0, None
    write_state(a.state, state)
    log(f"watching {url}")
    while True:
        h = health(url)
        if h is None:
            found = discover(a.kvm)
            if found != url:
                log(f"KVM moved: {url} -> {found}")
                url = found
            if last is not None:
                log("KVM offline")
                state = {"status": "KVM offline", "since": time.time()}
                write_state(a.state, state)
                last = None
            time.sleep(a.interval)
            continue
        key = (h.get("boot_id"), h.get("usb") == "configured", bool(h.get("signal")))
        new_session = last is None or key[0] != last[0] or (key[1] and not last[1]) or (key[2] and not last[2])
        last = key
        if new_session:
            attempts, heavy_failures, retry_at = 0, 0, None
        # a failed setup (e.g. the Mac was locked, or a transient glitch) is retried while the session lasts
        due = new_session or (retry_at is not None and time.monotonic() >= retry_at)
        if due and key[1] and attempts < a.retries:
            attempts += 1
            log(f"{'new session' if new_session else 'retry'} (boot {key[0][:8]}, usb {h.get('usb')}, "
                f"signal {h.get('signal')}, attempt {attempts}): first contact")
            write_state(a.state, {"status": "setting up", "since": time.time(), "kvm": url})
            t0 = time.monotonic()
            try:
                report = first_contact(KVM(url), vlm, log=log)
            except Exception as e:   # keep watching whatever happens
                report = {"ok": False, "problem": f"{type(e).__name__}: {e}"}
            report["setup_s"] = round(time.monotonic() - t0, 1)
            log(f"first contact: {json.dumps(report)}")
            state = {"status": "ready" if report.get("ok") else "needs attention", "since": time.time(),
                     "kvm": url, "report": report}
            write_state(a.state, state)
            # failures that clicked around the target's UI get one retry; click-free ones (waiting for USB,
            # video, or a locked Mac to show the dialog) keep retrying
            heavy = not report.get("ok") and not str(report.get("problem", "")).startswith(CLICK_FREE)
            heavy_failures += heavy
            retry_at = None if report.get("ok") or heavy_failures > 1 else time.monotonic() + a.retry_after
            # re-read the key after setup: setup itself must not count as a new session
            h2 = health(url)
            if h2:
                last = (h2.get("boot_id"), h2.get("usb") == "configured", bool(h2.get("signal")))
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
