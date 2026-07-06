#!/bin/bash
# Tear down the DIY PiKVM USB gadget cleanly.
G=/sys/kernel/config/usb_gadget/kvm
[ -d "$G" ] || exit 0
echo "" > "$G/UDC" 2>/dev/null || true
for l in "$G"/configs/c.1/hid.usb0 "$G"/configs/c.1/hid.usb1 "$G"/configs/c.1/mass_storage.usb0 "$G"/configs/c.1/acm.usb0; do [ -L "$l" ] && rm -f "$l"; done
[ -d "$G/configs/c.1/strings/0x409" ] && rmdir "$G/configs/c.1/strings/0x409"
[ -d "$G/configs/c.1" ] && rmdir "$G/configs/c.1"
[ -d "$G/functions/hid.usb0" ] && rmdir "$G/functions/hid.usb0"
[ -d "$G/functions/hid.usb1" ] && rmdir "$G/functions/hid.usb1"
[ -d "$G/functions/acm.usb0" ] && rmdir "$G/functions/acm.usb0" 2>/dev/null
if [ -d "$G/functions/mass_storage.usb0" ]; then
  echo "" > "$G/functions/mass_storage.usb0/lun.0/file" 2>/dev/null
  # remove any extra LUN (lun.1 trace) before the function dir, else the function (and gadget) leak in configfs
  [ -d "$G/functions/mass_storage.usb0/lun.1" ] && { echo "" > "$G/functions/mass_storage.usb0/lun.1/file" 2>/dev/null; rmdir "$G/functions/mass_storage.usb0/lun.1" 2>/dev/null; }
  rmdir "$G/functions/mass_storage.usb0" 2>/dev/null
fi
[ -d "$G/strings/0x409" ] && rmdir "$G/strings/0x409"
rmdir "$G" 2>/dev/null || true
exit 0
