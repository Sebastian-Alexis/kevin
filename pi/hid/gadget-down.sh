#!/bin/sh
set -eu
G=/sys/kernel/config/usb_gadget/ai-kvm
[ -d "$G" ] || exit 0
cd "$G"
echo "" > UDC || true
rm -f configs/c.1/hid.kbd configs/c.1/hid.mouse
rmdir configs/c.1/strings/0x409 configs/c.1 functions/hid.kbd functions/hid.mouse strings/0x409
cd / && rmdir "$G"
