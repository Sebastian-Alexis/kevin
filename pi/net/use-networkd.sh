#!/bin/bash
# Bring up Wi-Fi with wpa_supplicant + systemd-networkd instead of NetworkManager (run once, as root, after the
# first boot has Wi-Fi working). Pi OS's NetworkManager is built with netplan integration: every start it runs
# `netplan generate` + `systemctl daemon-reload` (3-13 s), so the Pi joined Wi-Fi ~22 s after boot. With this it
# joins at ~15 s and the controller sees the KVM ~7 s sooner after each plug-in.
#
# Usage: use-networkd.sh <ssid> [psk]   (psk defaults to the one NetworkManager already has for <ssid>)
# Revert: systemctl disable --now wpa_supplicant@wlan0 systemd-networkd; systemctl enable --now NetworkManager
set -eu
SSID=${1:?ssid}
PSK=${2:-}
if [ -z "$PSK" ]; then
  PSK=$(grep -lx "ssid=$SSID" /etc/NetworkManager/system-connections/*.nmconnection \
          /run/NetworkManager/system-connections/*.nmconnection 2>/dev/null | head -1 | xargs -r sed -n 's/^psk=//p')
fi
[ -n "$PSK" ] || { echo "no PSK for $SSID: pass it as the second argument" >&2; exit 1; }

install -d -m 755 /etc/wpa_supplicant /etc/systemd/network /etc/systemd/system/wpa_supplicant@wlan0.service.d
umask 077
{
  echo "ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev"
  echo "update_config=0"
  echo "country=US"
  wpa_passphrase "$SSID" "$PSK" | grep -v '#psk='
} > /etc/wpa_supplicant/wpa_supplicant-wlan0.conf
umask 022

for ifc in wlan0:25:600 eth0:20:100; do
  IFS=: read -r name prio metric <<< "$ifc"
  cat > "/etc/systemd/network/$prio-$name.network" <<EOF
[Match]
Name=$name

[Network]
DHCP=ipv4
IPv6AcceptRA=no
LinkLocalAddressing=no

[DHCPv4]
RouteMetric=$metric
# MAC-based client id: keeps the lease (and IP) NetworkManager had
ClientIdentifier=mac
EOF
done

cat > /etc/systemd/system/wpa_supplicant@wlan0.service.d/aikvm.conf <<'EOF'
[Unit]
# start as soon as the interface exists; don't wait for the rest of the network stack
DefaultDependencies=no
After=sys-subsystem-net-devices-wlan0.device
Before=network.target

[Service]
ExecStartPre=-/usr/sbin/rfkill unblock wifi
EOF

systemctl daemon-reload
systemctl disable NetworkManager.service NetworkManager-wait-online.service 2>/dev/null || true
systemctl enable systemd-networkd.service wpa_supplicant@wlan0.service
# cloud-init only matters for the first boot and costs ~5 s on every later one
touch /etc/cloud/cloud-init.disabled
echo "networkd configured for $SSID; takes effect on next boot"
