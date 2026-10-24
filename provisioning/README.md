# Pi provisioning

Target: Raspberry Pi 4B + TC358743 HDMI-to-CSI bridge. Base OS: Raspberry Pi OS Lite arm64 (Trixie, 2026-10-06).
The Pi Zero W (armhf image) also runs the stack, but its CSI link to the bridge was unreliable in testing.
First boot is driven by cloud-init (NoCloud, seed = bootfs).

Flash:

    xzcat 2026-10-06-raspios-trixie-arm64-lite.img.xz | sudo dd of=/dev/sdX bs=4M conv=fsync
    sudo mount /dev/sdX1 /mnt/boot
    cp user-data network-config /mnt/boot/          # fill in placeholders first
    touch /mnt/boot/ssh
    # append the ai-kvm lines to the image's own config.txt / cmdline.txt (see below);
    # do not copy cmdline.txt from here: its root=PARTUUID is specific to one image
    sudo umount /mnt/boot

Boot config deltas (the `config.txt` / `cmdline.txt` here are reference copies):

- `config.txt`: `dtparam=i2c_arm=on`, `camera_auto_detect=0`, `display_auto_detect=0`, `dtparam=audio=off`, and
  under `[all]`: `dtoverlay=dwc2,dr_mode=peripheral` (USB HID gadget),
  `dtoverlay=tc358743,link-frequency=297000000` (at the default 486 MHz the bridge drives 720p60 on one lane
  while unicam expects two, and no frames arrive) and `dtoverlay=disable-bt` (power).
- `cmdline.txt`: append `modules-load=dwc2,libcomposite cma=128M` on the same line.

`user-data`: user `kvm` (sudo NOPASSWD, ssh key + password), avahi, ustreamer/v4l-utils/i2c-tools/python3,
wifi powersave off, persistent journal. `network-config`: joins the 2.4 GHz `ai-kvm-net` AP; on boards with
ethernet it also DHCPs on `eth0`.

After first boot, install the stack and switch to the fast boot path:

    rsync -a pi/ kvm@<pi>:ai-kvm/
    ssh kvm@<pi> 'cd ai-kvm && sudo ./install.sh && sudo ./net/use-networkd.sh ai-kvm-net && sudo reboot'

- `install.sh`: services, capture-module preload, and `fast-boot.sh` (no initramfs, `boot_delay=0`, `panic=5`;
  only safe after the first boot, which used the initramfs to grow the root partition).
- `net/use-networkd.sh`: Wi-Fi via wpa_supplicant + systemd-networkd instead of NetworkManager. Pi OS's
  NetworkManager is built with netplan integration and runs `netplan generate` + `systemctl daemon-reload` on
  every start (3-13 s). Also pins the scan to the AP's channel and rescans every second while disconnected.

Measured boot (kernel + userspace): 37.7 s with the stock image, 12.9 s with all of the above. Wi-Fi is up
~12-15 s after power-on; the capture bridge is configured by then.

On the 4B the USB-C port is both the power input and the HID gadget port, so the target computer it plugs into
also powers it. Hub USB-A ports leave the Pi under-voltage (`vcgencmd get_throttled` = 0x50005); a direct USB-C
port or a powered hub is safer.

## Identity the target sees

- USB: an Apple ANSI keyboard + absolute mouse (`05ac:024f`, per-device serial). macOS knows Apple keyboard
  layouts, so it never opens Keyboard Setup Assistant (which steals the keystrokes the first-plug setup sends).
  Trade-off: on macOS (and Linux `hid-apple`) F1-F12 act as media keys (F3 = Mission Control).
- HDMI: `pi/video/edid.hex`, a TV-class EDID (CTA-861 extension) without the HDMI vendor block, 720p60, with a
  per-device serial (`edid.py`; `AIKVM_EDID_SERIAL` in `/etc/default/ai-kvm` overrides it to force re-setup).
  - TV-class: macOS 26 asks "What do you want to show on ai-kvm?" with Entire Screen / Start Mirroring as the
    default, which the controller accepts with a blind Return. A plain monitor EDID instead gets a "Choose to
    Mirror or Extend Display" card that only a person can answer from the menu bar.
  - No HDMI vendor block: the source signals DVI. In HDMI mode the Mac held AV-mute and sent black video.

## Dev AP on the controller

TP-Link Archer T2U Plus (rtw88_8821au): NetworkManager's AP mode never let clients finish auth on this driver,
so `dev-ap/ap-up.sh` runs hostapd + dnsmasq directly (NAT out of any other interface, 802.11n/WMM off; with them
on, the AP dropped client data frames). Install it as the `ai-kvm-ap` systemd service (starts at boot, restarts
if the dongle drops; leases persist in `/var/lib/ai-kvm-ap/leases`, so the Pi keeps its address):

    sudo dev-ap/install.sh <wlx...> <psk-file>
