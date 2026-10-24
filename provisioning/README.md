# Pi provisioning

Target: Raspberry Pi Zero 2 W + PiSugar UPS + TC358743 HDMI-to-CSI bridge.
Base OS: Raspberry Pi OS Lite arm64 (Trixie, 2026-10-06). First boot is driven by cloud-init (NoCloud, seed = bootfs).

Flash:

    xzcat 2026-10-06-raspios-trixie-arm64-lite.img.xz | sudo dd of=/dev/sdX bs=4M conv=fsync
    sudo mount /dev/sdX1 /mnt/boot
    cp user-data network-config config.txt cmdline.txt /mnt/boot/   # fill in placeholders first
    touch /mnt/boot/ssh
    sudo umount /mnt/boot

What the config does:

- `user-data`: hostname `ai-kvm`, user `kvm` (sudo NOPASSWD, ssh key + password), avahi (`ai-kvm.local`), loads `libcomposite`/`dwc2` for the USB HID gadget, disables wifi powersave.
- `network-config`: joins the 2.4 GHz `ai-kvm-net` AP (the Zero 2 W has no 5 GHz).
- `config.txt`: `dwc2` in peripheral mode (HID gadget to the target), `tc358743` overlay (HDMI capture via CSI), `i2c_arm=on` (PiSugar), `camera_auto_detect=0`, `gpu_mem=128`.
- `cmdline.txt`: `modules-load=dwc2 cma=128M`.

Dev AP on the workstation (TP-Link Archer T2U Plus, NetworkManager shared mode, NAT to upstream):

    nmcli con add type wifi ifname <wlx...> con-name ai-kvm-ap autoconnect yes ssid ai-kvm-net \
      802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
      ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method disabled \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk <WIFI-PSK>
