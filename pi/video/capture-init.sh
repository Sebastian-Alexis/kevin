#!/bin/sh
# Program the TC358743 HDMI->CSI bridge, then latch whatever timings the host actually sends.
# edid.hex is a TV-class EDID (CTA-861 extension) WITHOUT the HDMI vendor block, 720p60 preferred:
# - TV-class: macOS 26 answers a new TV with a "What do you want to show on ai-kvm?" dialog whose default is
#   Entire Screen / Start Mirroring, so the KVM can accept it with a blind Return (a plain monitor instead gets a
#   "Choose to Mirror or Extend" placeholder that only a person can answer from the menu bar).
# - no HDMI vendor block: the source then signals DVI. In HDMI mode the Mac held AV-mute with black video.
# - 720p60 only (plus mandatory 640x480): the Pi 4's 2-lane CSI can't carry 1080p60.
# edid.py stamps this unit's serial so each Mac remembers the KVM after its first setup.
set -eu
DEV=${1:-/dev/video0}
mkdir -p /run/ai-kvm
# optional overrides, e.g. AIKVM_EDID_SERIAL=4b4d5800 to make targets treat the KVM as a new display
if [ -r /etc/default/ai-kvm ]; then set -a; . /etc/default/ai-kvm; set +a; fi
python3 /opt/ai-kvm/video/edid.py /opt/ai-kvm/video/edid.hex > /run/ai-kvm/edid.hex
for _ in $(seq 1 300); do [ -e "$DEV" ] && break; sleep 0.1; done
# The bridge keeps its EDID (and hotplug) across a warm reboot. If it holds a different EDID than this boot's
# (e.g. AIKVM_EDID_SERIAL changed), hold hotplug low briefly so the target treats this as a new display and
# re-reads it. A cold plug-in has no EDID loaded (hotplug was low already) and an unchanged EDID needs no reset.
current=$(v4l2-ctl -d "$DEV" --get-edid=format=hex 2>/dev/null | tr -d ' \n' || true)
if [ -n "$current" ] && [ "$current" != "$(tr -d ' \n' < /run/ai-kvm/edid.hex)" ]; then
  v4l2-ctl -d "$DEV" --clear-edid || true
  sleep 2
fi
v4l2-ctl -d "$DEV" --set-edid=pad=0,file=/run/ai-kvm/edid.hex,format=hex
v4l2-ctl -d "$DEV" --set-dv-bt-timings query || true
v4l2-ctl -d "$DEV" --query-dv-timings || true
