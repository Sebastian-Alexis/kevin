#!/bin/sh
# Install the dev AP as a systemd service on the controller (the machine the Pi joins over Wi-Fi).
# Usage: sudo ./install.sh <wifi-iface> <psk-file>
set -eu
IF=${1:?wifi iface (the USB dongle, e.g. wlx...)}
PSK_FILE=${2:?psk file}
cd "$(dirname "$0")"
install -D -m 755 ap-up.sh /usr/local/lib/ai-kvm/ap-up.sh
install -D -m 600 "$PSK_FILE" /etc/ai-kvm/wifi.psk
install -d -m 755 /etc/ai-kvm
printf 'AP_IFACE=%s\n' "$IF" > /etc/ai-kvm/ap.env
# a leftover lease file from the old /run location keeps the Pi on its current address
if [ -f /run/ai-kvm-ap/leases ] && [ ! -f /var/lib/ai-kvm-ap/leases ]; then
  install -D -m 644 /run/ai-kvm-ap/leases /var/lib/ai-kvm-ap/leases
fi
install -m 644 ai-kvm-ap.service /etc/systemd/system/ai-kvm-ap.service
systemctl daemon-reload
systemctl enable --now ai-kvm-ap.service
systemctl --no-pager status ai-kvm-ap.service | sed -n '1,3p'
