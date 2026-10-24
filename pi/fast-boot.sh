#!/bin/sh
# Shave ~3 s off every boot: skip the initramfs (the kernel mounts the SD root itself: MMC_SDHCI_IPROC, MMC_BLOCK
# and EXT4 are built in) and the firmware's default 1 s boot_delay. panic=5 reboots instead of hanging if root
# ever fails to mount. Run only after the first boot: that one needs the initramfs to grow the root partition.
# Verified first with a one-shot `reboot '0 tryboot'` (kernel stage 4.6 s -> 2.6 s).
set -eu
CFG=/boot/firmware/config.txt
CMD=/boot/firmware/cmdline.txt
sed -i "s/^auto_initramfs=1$/auto_initramfs=0/" "$CFG"
grep -q "^boot_delay=" "$CFG" || printf '\n# ai-kvm fast boot\nboot_delay=0\ndisable_splash=1\n' >> "$CFG"
grep -q "panic=5" "$CMD" || sed -i "s/$/ panic=5/" "$CMD"
echo "fast boot configured; takes effect on next boot"
