"""Run our computer-use loop against OSWorld (1.0 task set in the OSWorld-V2 harness; V2 tasks once the gated data
is available): the benchmark VM is presented to agent.run as a KVM (screenshots in, HID-style actions out, turned
into pyautogui code), so the planner / grounder / verifier / rails are measured unchanged.

    cd /tmp/osworld-v2 && uv run python -m agent.osworld --tasks chrome:3 os:3 --result-dir results/run1

Each task: env.reset(config) -> run(...) -> env.evaluate(); score, steps, seconds and the trajectory log land in
<result-dir>/<domain>/<id>/. Needs the OSWorld checkout on sys.path (run from it) and its provider (docker VM).
"""
import argparse
import io
import json
import os
import sys
import time

from PIL import Image

from .run import run
from .vlm import VLM, load_key

PYKEY = {"cmd": "ctrl", "win": "win", "super": "win", "esc": "escape", "return": "enter", "pageup": "pageup",
         "pagedown": "pagedown"}


def py_actions(actions, w, h):
    """HID-style actions (fractions 0-1 for pointer) -> one pyautogui script for the VM."""
    lines = ["import pyautogui, time", "pyautogui.FAILSAFE = False"]
    for a in actions:
        t = a["type"]
        if t == "move":
            lines.append(f"pyautogui.moveTo({a['x'] * w:.0f}, {a['y'] * h:.0f})")
        elif t == "click":
            lines.append(f"pyautogui.click(button={a.get('button', 'left')!r}, clicks={a.get('count', 1)}, interval=0.08)")
        elif t == "type":
            lines.append(f"pyautogui.write({a['text']!r}, interval=0.01)")
        elif t == "key":
            keys = [PYKEY.get(k.strip().lower(), k.strip().lower()) for k in a["combo"].split("+")]
            lines.append(f"pyautogui.hotkey({', '.join(repr(k) for k in keys)})" if len(keys) > 1 else f"pyautogui.press({keys[0]!r})")
        elif t == "scroll":
            lines.append(f"pyautogui.scroll({int(a['ticks']) * 3}, x={a['x'] * w:.0f}, y={a['y'] * h:.0f})")
        elif t == "wait":
            lines.append(f"time.sleep({a['ms'] / 1000:.2f})")
    return "\n".join(lines)


class EnvKVM:
    """What agent.run needs from a KVM, backed by an OSWorld DesktopEnv."""

    def __init__(self, env, log):
        self.env, self.log, self.steps = env, log, 0
        self.size = tuple(env.vm_screen_size) if hasattr(env, "vm_screen_size") else (1920, 1080)
        self._obs = None

    def screenshot(self):
        png = self.env.controller.get_screenshot()
        img = Image.open(io.BytesIO(png)).convert("RGB")
        self.size = img.size
        return img

    def actions(self, actions):
        code = py_actions(actions, *self.size)
        self.steps += 1
        self.env.step(code, pause=0.3)


def load_tasks(root, specs):
    """"chrome:3" -> the first 3 chrome task configs (sorted by id) under evaluation_examples/examples; "chrome" -> all;
    "chrome:<id>" -> that one task."""
    out = []
    for spec in specs:
        domain, _, sel = spec.partition(":")
        d = os.path.join(root, "evaluation_examples", "examples", domain)
        ids = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))
        if sel and sel.isdigit():
            ids = ids[: int(sel)]
        elif sel:
            ids = [sel]
        for tid in ids:
            with open(os.path.join(d, f"{tid}.json")) as f:
                out.append((domain, tid, json.load(f)))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m agent.osworld", description=__doc__.splitlines()[0])
    p.add_argument("--osworld", default=os.environ.get("OSWORLD_ROOT", "/tmp/osworld-v2"))
    p.add_argument("--tasks", nargs="+", default=["chrome:2"])
    p.add_argument("--result-dir", default="results/ai-kvm")
    p.add_argument("--provider", default="docker")
    p.add_argument("--llm", default=os.environ.get("AIKVM_PLANNER_BASE", "http://127.0.0.1:4001/v1"))
    p.add_argument("--model", default=os.environ.get("AIKVM_PLANNER_MODEL", "anthropic/claude-opus-5-5"))
    p.add_argument("--key-file", default="/etc/gemma4-vllm-api-key")
    p.add_argument("--grounder", default="http://127.0.0.1:8010/v1")
    p.add_argument("--verifier", default="http://127.0.0.1:8000/v1")
    p.add_argument("--max-steps", type=int, default=30)
    a = p.parse_args(argv)
    sys.path.insert(0, a.osworld)
    from desktop_env.desktop_env import DesktopEnv
    if os.uname().machine == "aarch64" and a.provider == "docker":
        arm64_docker_provider()

    key = load_key(a.key_file)
    vlm = VLM(a.llm, a.model, key=key)
    verifier = VLM(a.verifier, "qwen27", key=key)
    grounder = {"url": a.grounder.rstrip("/") + "/chat/completions", "model": "holo4", "key": key}
    tasks = load_tasks(a.osworld, a.tasks)
    env = DesktopEnv(provider_name=a.provider, action_space="pyautogui", screen_size=(1920, 1080), headless=True,
                     os_type="Ubuntu", require_a11y_tree=False)
    scores = []
    try:
        for domain, tid, cfg in tasks:
            out_dir = os.path.join(a.result_dir, domain, tid)
            os.makedirs(out_dir, exist_ok=True)
            lines = []

            def log(msg, _l=lines):
                print(msg, flush=True)
                _l.append(msg)
            log(f"== {domain}/{tid}: {cfg['instruction']}")
            t0 = time.monotonic()
            env.reset(task_config=cfg)
            kvm = EnvKVM(env, log)
            try:
                result = run(cfg["instruction"], kvm, vlm, os_name="Ubuntu", max_steps=a.max_steps, save_dir=out_dir,
                             log=log, coords="pixels", grounder=grounder, isolate=False, verifier=verifier)
            except Exception as e:
                result = {"done": False, "fail": f"{type(e).__name__}: {e}", "steps": kvm.steps}
            if result.get("done"):
                env.step("DONE")
            else:
                env.step("FAIL")
            try:
                score = env.evaluate()
                score = float(score.get("score", 0.0)) if isinstance(score, dict) else float(score)
            except Exception as e:
                log(f"evaluate failed: {e}")
                score = 0.0
            scores.append(score)
            rec = {"domain": domain, "id": tid, "score": score, "seconds": round(time.monotonic() - t0, 1), **result}
            log(f"-- score {score} {json.dumps(result)}")
            with open(os.path.join(out_dir, "result.json"), "w") as f:
                json.dump(rec, f, indent=1)
            with open(os.path.join(out_dir, "log.txt"), "w") as f:
                f.write("\n".join(lines) + "\n")
            with open(os.path.join(a.result_dir, "scores.jsonl"), "a") as f:
                f.write(json.dumps(rec) + "\n")
    finally:
        env.close()
    print(f"\n{sum(scores)}/{len(scores)} tasks solved ({100 * sum(scores) / max(1, len(scores)):.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---- arm64 host: run the x86 VM image through qemux/qemu (multi-arch, TCG emulation) instead of the amd64-only
# container the harness hardcodes. Same port contract (5000 server, 9222 chromium, 8080 vlc, 8006 web VNC); the
# pristine image stays untouched behind a qcow2 overlay that is recreated for every task.
def arm64_docker_provider():
    import subprocess
    from desktop_env.providers.docker import provider as P

    def start_emulator(self, path_to_vm, headless, os_type):
        from filelock import FileLock
        lock = FileLock(str(self.lock_file), timeout=P.LOCK_TIMEOUT)
        base = os.path.abspath(path_to_vm)
        overlay = os.path.join(os.path.dirname(base), f"overlay-{os.getpid()}.qcow2")
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", "/vm/base.qcow2", overlay], check=True)
        with lock:
            self.vnc_port = self._get_available_port(8006)
            self.server_port = self._get_available_port(5000)
            self.chromium_port = self._get_available_port(9222)
            self.vlc_port = self._get_available_port(8080)
            env = dict(self.environment, KVM="N", ARGUMENTS="-cpu max")
            self.container = self.client.containers.run(
                os.environ.get("OSWORLD_QEMU_IMAGE", "qemux/qemu:latest"), environment=env, cap_add=["NET_ADMIN"],
                devices=["/dev/net/tun"],
                volumes={base: {"bind": "/vm/base.qcow2", "mode": "ro"}, overlay: {"bind": "/boot.qcow2", "mode": "rw"}},
                ports={8006: self.vnc_port, 5000: self.server_port, 9222: self.chromium_port, 8080: self.vlc_port},
                detach=True)
        self._overlay = overlay
        try:
            self._wait_for_vm_ready(timeout=int(os.environ.get("OSWORLD_BOOT_TIMEOUT", "1800")))
        except Exception:
            try:
                print(self.container.logs().decode()[-2000:])
                self.container.stop(); self.container.remove()
            except Exception:
                pass
            raise

    orig_stop = P.DockerProvider.stop_emulator

    def stop_emulator(self, path_to_vm, region=None, *a, **kw):
        orig_stop(self, path_to_vm, region, *a, **kw)
        ov = getattr(self, "_overlay", None)
        if ov and os.path.exists(ov):
            os.remove(ov)

    P.DockerProvider.start_emulator = start_emulator
    P.DockerProvider.stop_emulator = stop_emulator
