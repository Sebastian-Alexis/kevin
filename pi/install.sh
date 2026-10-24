#!/bin/sh
# Install ai-kvm onto the Pi. Run from the repo checkout on the Pi: sudo ./pi/install.sh
set -eu
cd "$(dirname "$0")"

apt-get update
apt-get install -y --no-install-recommends python3 v4l-utils ustreamer iw

install -d /opt/ai-kvm
cp -r hid video api /opt/ai-kvm/
chmod +x /opt/ai-kvm/hid/*.sh /opt/ai-kvm/video/*.sh /opt/ai-kvm/video/edid.py
install -m 644 systemd/*.service /etc/systemd/system/
install -m 644 video/modules-load.conf /etc/modules-load.d/ai-kvm-video.conf

systemctl daemon-reload
# first boot (which needed the initramfs to resize the root partition) is behind us: boot without it from now on
sh ./fast-boot.sh
systemctl enable --now ai-kvm-hid.service ai-kvm-api.service ai-kvm-video.service
systemctl --no-pager status ai-kvm-hid ai-kvm-api ai-kvm-video | sed -n '/●\|Active:/p'
