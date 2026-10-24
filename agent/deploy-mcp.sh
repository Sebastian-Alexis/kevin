#!/bin/sh
# Install/update the MCP server on the KVM: the agent package, a venv with mcp+pillow (offline wheels from
# /tmp/piwheels, the Pi has no internet), the model API key, and the systemd unit.
#   bash agent/deploy-mcp.sh [kvm@10.42.0.101]
set -eu
PI=${1:-kvm@10.42.0.101}
cd "$(dirname "$0")/.."
rsync -a --delete --exclude __pycache__ agent/ "$PI:/tmp/agent/"
rsync -a /tmp/piwheels/ "$PI:/tmp/piwheels/" 2>/dev/null || true
sudo -n cat /etc/gemma4-vllm-api-key | ssh "$PI" 'sudo install -d -m 755 /etc/ai-kvm && sudo tee /etc/ai-kvm/llm-key >/dev/null && sudo chmod 644 /etc/ai-kvm/llm-key'
ssh "$PI" 'set -e
  sudo rsync -a --delete /tmp/agent/ /opt/ai-kvm/agent/
  [ -x /opt/ai-kvm/venv/bin/python ] || sudo python3 -m venv --system-site-packages /opt/ai-kvm/venv
  sudo /opt/ai-kvm/venv/bin/pip install -q --no-index --find-links /tmp/piwheels "mcp<2" pillow
  sudo install -d -m 755 /var/lib/ai-kvm/runs
  sudo cp /opt/ai-kvm/agent/ai-kvm-mcp.service /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now ai-kvm-mcp && sudo systemctl restart ai-kvm-mcp
  sleep 2; systemctl is-active ai-kvm-mcp'
