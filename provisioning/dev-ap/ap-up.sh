#!/bin/sh
# Dev AP for the Pi on the workstation's TP-Link Archer T2U Plus (rtw88_8821au).
# NetworkManager's AP mode (wpa_supplicant) on this driver never completed client auth in testing;
# hostapd does (auth frames answered), so run hostapd + dnsmasq directly and NAT out of the default route.
# Usage: sudo ./ap-up.sh <iface> <psk-file>      (foreground; Ctrl-C tears down)
set -eu
IF=${1:?iface}
PSK=$(cat "${2:?psk file}")
UPLINK=$(ip -4 route show default | awk '{print $5; exit}')
NET=10.42.0
RUN=/run/ai-kvm-ap
mkdir -p "$RUN"

cleanup() {
  kill "$(cat "$RUN/dnsmasq.pid" 2>/dev/null)" 2>/dev/null || true
  iptables -t nat -D POSTROUTING -s $NET.0/24 -o "$UPLINK" -j MASQUERADE 2>/dev/null || true
  iptables -D FORWARD -i "$IF" -o "$UPLINK" -j ACCEPT 2>/dev/null || true
  iptables -D FORWARD -i "$UPLINK" -o "$IF" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
  ip addr flush dev "$IF" || true
  nmcli dev set "$IF" managed yes || true
}
trap cleanup EXIT INT TERM

nmcli dev set "$IF" managed no
ip link set "$IF" down
iw dev "$IF" set type __ap 2>/dev/null || true
ip addr flush dev "$IF"
ip addr add $NET.1/24 dev "$IF"
ip link set "$IF" up

cat > "$RUN/hostapd.conf" <<EOF
interface=$IF
driver=nl80211
ssid=ai-kvm-net
country_code=US
ieee80211d=1
hw_mode=g
channel=6
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$PSK
EOF
chmod 600 "$RUN/hostapd.conf"

sysctl -qw net.ipv4.ip_forward=1
iptables -t nat -A POSTROUTING -s $NET.0/24 -o "$UPLINK" -j MASQUERADE
iptables -I FORWARD -i "$IF" -o "$UPLINK" -j ACCEPT
iptables -I FORWARD -i "$UPLINK" -o "$IF" -m state --state RELATED,ESTABLISHED -j ACCEPT

dnsmasq --interface="$IF" --bind-interfaces --except-interface=lo --port=0 \
  --dhcp-range=$NET.10,$NET.254,12h --dhcp-option=6,1.1.1.1,8.8.8.8 \
  --dhcp-leasefile="$RUN/leases" --pid-file="$RUN/dnsmasq.pid" --log-dhcp

echo "AP up on $IF, NAT via $UPLINK; leases: $RUN/leases"
hostapd "$RUN/hostapd.conf"
