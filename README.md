# ai-kvm

Hardware computer use for agents. A Raspberry Pi 4B plugs into a computer like a person would: its USB-C port
is a keyboard + mouse, and an HDMI-to-CSI bridge captures the screen. A controller on the local network (here
the GB10, which also serves the vision model) watches the KVM and drives the computer. Nothing is installed on
the target.

```mermaid
graph LR
  T["Target computer"] -- "USB-C: power + keyboard/mouse" --> P["Pi 4B (pi/)"]
  T -- "HDMI out" --> B["TC358743 bridge"] -- CSI --> P
  P -- "Wi-Fi: /health, /screenshot, /actions" --> C["Controller (agent/)"]
  C -- "OpenAI-compatible API" --> M["Local VLM (vLLM, qwen122)"]
```

## Plug and play

Plug the KVM's USB-C and HDMI into a Mac. The Pi boots from the Mac's USB power (~13 s to Wi-Fi) and
`ai-kvm-agent` on the controller notices the new session (new boot, USB host, or HDMI signal) and runs
`agent.setup.first_contact`:

1. First plug-in on a given Mac: macOS asks "What do you want to show on ai-kvm?" (the EDID is TV-class). The
   capture shows the "Choose to Mirror or Extend" card, so the controller presses Return: Entire Screen.
2. macOS then mirrors with the KVM as master, putting the Mac's own panel at 720p. The controller opens
   Apple menu > System Settings (fixed 720p positions), Hide Others, cmd+F "Displays", sets "Optimize for" to the
   Mac itself (two vision-grounded clicks), Show All, and closes the window. The panel is back at native
   resolution and the KVM mirrors it (pillarboxed). macOS remembers this per display, so later plug-ins into
   the same Mac skip steps 1-2.
3. Pointer check: the cursor is located in the capture by frame differencing and must land where it was sent.

The state is in `/run/ai-kvm/state.json` (`ready` / `needs attention` with a report) and the journal
(`journalctl -u ai-kvm-agent@<user>`). Failed setups are retried while the session lasts (click-free failures up to
5 times, failures that clicked around System Settings once).

Measured on a MacBook Air (macOS 26.4):

| Path | Setup time | Mac panel at 720p |
|---|---|---|
| Known Mac (re-plug) | 3.5-4 s after the Pi is reachable | never |
| New Mac (first plug) | 20-25 s after the Pi is reachable | 16-18 s |

The Pi is reachable ~24-28 s after a warm reboot (12.9 s boot + Wi-Fi association; a cold plug-in skips the
shutdown). Automatic new-display trials: 5/5 and 3/3 after the last fixes.

## Using it

    python3 -m agent "In the web browser, search for raspberry pi kvm"

One model call per step: the model sees the screen picture (pillarbox removed) and replies with batched
actions; clicks carry a target description and a 0-1000 `point_2d`. Typical step: 1.2-2 s of model time plus
execution. `--dry-run` asks the model without sending input; `--save-dir` keeps every frame.

The Pi's own API (`pi/api/server.py`) is usable directly: `GET /health`, `GET /screenshot`,
`POST /actions` with move/click/drag/scroll/type/key/wait batches.

## Setup

- Pi: `provisioning/README.md` (flash, `pi/install.sh`, `pi/net/use-networkd.sh`).
- Controller: `sudo provisioning/dev-ap/install.sh <wlx...> <psk-file>` (Wi-Fi AP the Pi joins) and
  `sudo agent/install.sh <user>` (the auto-setup daemon; reads the vLLM key via systemd `LoadCredential`).
- `host/macos/kvm-mirror.swift` does step 2 through CoreGraphics when you have a shell on the Mac (dev/recovery).

## Limits

- Auto-setup is macOS-only. Windows/Linux extend a new display by default: set the KVM display to Duplicate once
  (Windows: Win+P). The setup recognizes a non-Mac and reports this instead of clicking.
- "KVM is master" is judged by aspect ratio (the Mac panel is pillarboxed when it is master), which fits every
  MacBook; a 16:9 iMac looks the same either way.
- The keyboard identifies as an Apple keyboard (no Keyboard Setup Assistant), so F1-F12 are media keys on macOS.
- Capture is 720p60 over 2 CSI lanes.
