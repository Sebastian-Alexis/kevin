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

- `config.txt`: `dtparam=i2c_arm=on`, `camera_auto_detect=0`, and under `[all]`:
  `dtoverlay=dwc2,dr_mode=peripheral` (USB HID gadget) and
  `dtoverlay=tc358743,link-frequency=297000000`. At the default 486 MHz the bridge drives 720p60 on one
  lane while unicam expects two, and no frames arrive; 297 MHz makes it use both lanes.
- `cmdline.txt`: append `modules-load=dwc2,libcomposite cma=128M` on the same line.

`user-data`: user `kvm` (sudo NOPASSWD, ssh key + password), avahi, ustreamer/v4l-utils/i2c-tools/python3,
wifi powersave off, persistent journal. `network-config`: joins the 2.4 GHz `ai-kvm-net` AP; on boards with
ethernet it also DHCPs on `eth0`.

After first boot, install the stack from the repo root:

    rsync -a pi/ kvm@<pi>:ai-kvm/ && ssh kvm@<pi> 'cd ai-kvm && sudo ./install.sh'

On the 4B the USB-C port is both the power input and the HID gadget port, so the target computer it plugs
into also powers it.

Dev AP on the workstation (TP-Link Archer T2U Plus, rtw88_8821au): NetworkManager's AP mode never let
clients finish auth on this driver, so run hostapd + dnsmasq directly (NAT out of the default route,
802.11n/WMM off; with them on, the AP dropped client data frames):

    sudo dev-ap/ap-up.sh <wlx...> <psk-file>      # foreground; Ctrl-C tears down; leases in /run/ai-kvm-ap/leases
