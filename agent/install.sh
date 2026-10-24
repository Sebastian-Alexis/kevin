#!/bin/sh
# Install the controller daemon (auto-setup on plug-in) as ai-kvm-agent@<user>.service.
# Usage: sudo agent/install.sh [user]        (default: the sudo-ing user; runs from this repo checkout)
set -eu
USER_NAME=${1:-${SUDO_USER:?pass the user to run as}}
REPO=$(cd "$(dirname "$0")/.." && pwd)
ln -sfn "$REPO" /opt/ai-kvm-controller
install -m 644 "$REPO/agent/ai-kvm-agent@.service" /etc/systemd/system/ai-kvm-agent@.service
systemctl daemon-reload
systemctl enable --now "ai-kvm-agent@$USER_NAME.service"
systemctl --no-pager status "ai-kvm-agent@$USER_NAME.service" | sed -n '1,3p'
