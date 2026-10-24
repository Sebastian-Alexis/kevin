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
python3 /opt/ai-kvm/video/edid.py /opt/ai-kvm/video/edid.hex > /run/ai-kvm/edid.hex
for _ in $(seq 1 30); do [ -e "$DEV" ] && break; sleep 1; done
v4l2-ctl -d "$DEV" --set-edid=pad=0,file=/run/ai-kvm/edid.hex,format=hex
v4l2-ctl -d "$DEV" --set-dv-bt-timings query || true
v4l2-ctl -d "$DEV" --query-dv-timings || true
