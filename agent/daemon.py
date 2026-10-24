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
    p.add_argument("--model", default=os.environ.get("AIKVM_LLM_MODEL", "qwen122"))
    p.add_argument("--key-file", default=os.environ.get("AIKVM_LLM_KEY_FILE", "/etc/gemma4-vllm-api-key"))
    p.add_argument("--state", default=os.environ.get("AIKVM_STATE", "/run/ai-kvm/state.json"))
    p.add_argument("--interval", type=float, default=0.5)
    a = p.parse_args(argv)
    os.makedirs(os.path.dirname(a.state), exist_ok=True)
    vlm = VLM(a.llm, a.model, key=load_key(a.key_file))
    url, last, state = a.kvm, None, {"status": "waiting for the KVM", "since": time.time()}
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
        if new_session and key[1]:
            log(f"new session (boot {key[0][:8]}, usb {h.get('usb')}, signal {h.get('signal')}): first contact")
            write_state(a.state, {"status": "setting up", "since": time.time(), "kvm": url})
            t0 = time.monotonic()
            try:
                report = first_contact(KVM(url), vlm, log=log)
            except Exception as e:   # keep watching whatever happens; the next plug-in retries
                report = {"ok": False, "problem": f"{type(e).__name__}: {e}"}
            report["setup_s"] = round(time.monotonic() - t0, 1)
            log(f"first contact: {json.dumps(report)}")
            state = {"status": "ready" if report.get("ok") else "needs attention", "since": time.time(),
                     "kvm": url, "report": report}
            write_state(a.state, state)
            # re-read the key after setup: setup itself must not count as a new session
            h2 = health(url)
            if h2:
                last = (h2.get("boot_id"), h2.get("usb") == "configured", bool(h2.get("signal")))
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
