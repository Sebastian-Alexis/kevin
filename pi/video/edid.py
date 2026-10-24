#!/usr/bin/env python3
"""Print edid.hex with this unit's serial number patched in (bytes 12-15, block 0 checksum fixed).

macOS remembers a display's mirroring setup by EDID identity, so a serial that is stable per device (taken
from the Pi's own serial number) makes the first-plug setup a one-time event on each target computer.
AIKVM_EDID_SERIAL (hex) overrides it, e.g. to make targets treat the KVM as a new display again.
Usage: edid.py <template.hex>  >  /run/ai-kvm/edid.hex
"""
import os
import sys


def device_serial():
    if os.environ.get("AIKVM_EDID_SERIAL"):
        return int(os.environ["AIKVM_EDID_SERIAL"], 16)
    try:
        with open("/proc/device-tree/serial-number") as f:
            return int(f.read().strip("\x00\n ")[-8:], 16)
    except (OSError, ValueError):
        return 1


def main(path):
    with open(path) as f:
        edid = bytearray(bytes.fromhex("".join(f.read().split())))
    if len(edid) % 128 or edid[:8] != bytes.fromhex("00ffffffffffff00"):
        sys.exit(f"{path}: not an EDID")
    edid[12:16] = (device_serial() or 1).to_bytes(4, "little")
    edid[127] = (-sum(edid[:127])) % 256
    print("\n".join(edid[i:i + 16].hex() for i in range(0, len(edid), 16)))


if __name__ == "__main__":
    main(sys.argv[1])
