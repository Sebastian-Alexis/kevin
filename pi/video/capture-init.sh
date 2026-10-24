#!/bin/sh
# Program the TC358743 HDMI->CSI bridge: push an EDID so the host sees a 1080p60 monitor,
# then latch whatever timings the host actually sends.
set -eu
DEV=${1:-/dev/video0}
for _ in $(seq 1 30); do [ -e "$DEV" ] && break; sleep 1; done
v4l2-ctl -d "$DEV" --set-edid=file=/opt/ai-kvm/video/edid.hex --fix-edid-checksums
sleep 1
v4l2-ctl -d "$DEV" --set-dv-bt-timings query || true
v4l2-ctl -d "$DEV" --query-dv-timings || true
