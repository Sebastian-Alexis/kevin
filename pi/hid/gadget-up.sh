#!/bin/sh
# Create a USB composite gadget: boot-protocol keyboard (hidg0) + absolute-pointer mouse (hidg1).
# Absolute pointer (tablet-style) so the agent can address screen coordinates directly.
set -eu

G=/sys/kernel/config/usb_gadget/ai-kvm
[ -d "$G" ] && exit 0

modprobe libcomposite
mkdir -p "$G"
cd "$G"

echo 0x1d6b > idVendor   # Linux Foundation
echo 0x0104 > idProduct  # Multifunction Composite Gadget
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

mkdir -p strings/0x409
echo "0000000000001" > strings/0x409/serialnumber
echo "ai-kvm"        > strings/0x409/manufacturer
echo "AI KVM HID"    > strings/0x409/product

mkdir -p configs/c.1/strings/0x409
echo "HID keyboard + mouse" > configs/c.1/strings/0x409/configuration
echo 250 > configs/c.1/MaxPower

# --- keyboard (standard 8-byte boot report) ---
mkdir -p functions/hid.kbd
echo 1 > functions/hid.kbd/protocol
echo 1 > functions/hid.kbd/subclass
echo 8 > functions/hid.kbd/report_length
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' \
  > functions/hid.kbd/report_desc

# --- absolute mouse: buttons(1 byte) + X(2 bytes 0..32767) + Y(2 bytes) + wheel(1 byte) = 6 bytes ---
mkdir -p functions/hid.mouse
echo 0 > functions/hid.mouse/protocol
echo 0 > functions/hid.mouse/subclass
echo 6 > functions/hid.mouse/report_length
printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x15\x00\x26\xff\x7f\x75\x10\x95\x02\x81\x02\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x01\x81\x06\xc0\xc0' \
  > functions/hid.mouse/report_desc

ln -sf functions/hid.kbd   configs/c.1/
ln -sf functions/hid.mouse configs/c.1/

UDC=$(find /sys/class/udc -mindepth 1 -maxdepth 1 -printf '%f\n' | head -n1)
echo "$UDC" > UDC
for _ in $(seq 1 50); do [ -e /dev/hidg0 ] && [ -e /dev/hidg1 ] && break; sleep 0.1; done
chmod 666 /dev/hidg0 /dev/hidg1
