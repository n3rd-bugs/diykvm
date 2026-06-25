#!/bin/bash
# DIY PiKVM USB gadget: single combined HID interface (keyboard + absolute mouse via Report IDs).
# One interrupt endpoint -> stable on the Pi's dwc2 (two separate HID funcs cause a re-enum loop).
#   /dev/hidg0 reports:  keyboard = [0x01][mods][resv][k1..k6]               (9 bytes)
#                        abs mouse= [0x02][btns][Xlo][Xhi][Ylo][Yhi][wheel]  (7 bytes)  -> desktop
#                        rel mouse= [0x03][btns][dx][dy][wheel]              (5 bytes)  -> touch/trackpad
set -e
G=/sys/kernel/config/usb_gadget/kvm

modprobe libcomposite

# --- teardown if it already exists ---
if [ -d "$G" ]; then
  echo "" > "$G/UDC" 2>/dev/null || true
  for l in "$G"/configs/c.1/hid.usb0 "$G"/configs/c.1/hid.usb1 "$G"/configs/c.1/mass_storage.usb0; do [ -L "$l" ] && rm -f "$l"; done
  [ -d "$G/configs/c.1/strings/0x409" ] && rmdir "$G/configs/c.1/strings/0x409"
  [ -d "$G/configs/c.1" ] && rmdir "$G/configs/c.1"
  [ -d "$G/functions/hid.usb0" ] && rmdir "$G/functions/hid.usb0"
  [ -d "$G/functions/hid.usb1" ] && rmdir "$G/functions/hid.usb1"
  [ -d "$G/functions/mass_storage.usb0" ] && { echo "" > "$G/functions/mass_storage.usb0/lun.0/file" 2>/dev/null; rmdir "$G/functions/mass_storage.usb0" 2>/dev/null; }
  [ -d "$G/strings/0x409" ] && rmdir "$G/strings/0x409"
  rmdir "$G"
fi

# --- create gadget ---
mkdir -p "$G"
echo 0x1d6b > "$G/idVendor"          # Linux Foundation
echo 0x0107 > "$G/idProduct"         # +relative-mouse report (bumped to force fresh Windows enumeration)
echo 0x0100 > "$G/bcdDevice"
echo 0x0200 > "$G/bcdUSB"
echo 0x00   > "$G/bDeviceClass"      # composite; class per-interface
echo 0x00   > "$G/bDeviceSubClass"
echo 0x00   > "$G/bDeviceProtocol"

mkdir -p "$G/strings/0x409"
echo "kvm-0001"          > "$G/strings/0x409/serialnumber"
echo "DIY-PiKVM"         > "$G/strings/0x409/manufacturer"
echo "PiKVM HID Console" > "$G/strings/0x409/product"

# Single HID function: keyboard (Report ID 1) + absolute mouse (Report ID 2) -> /dev/hidg0
mkdir -p "$G/functions/hid.usb0"
echo 0 > "$G/functions/hid.usb0/protocol"      # 0 = no boot protocol (report-ID multiplexed)
echo 0 > "$G/functions/hid.usb0/subclass"
echo 9 > "$G/functions/hid.usb0/report_length" # largest report incl. ID byte (keyboard = 1+8)
printf '\x05\x01\x09\x06\xa1\x01\x85\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0\x05\x01\x09\x02\xa1\x01\x85\x02\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x16\x00\x00\x26\xff\x7f\x75\x10\x95\x02\x81\x02\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x01\x81\x06\xc0\xc0\x05\x01\x09\x02\xa1\x01\x85\x03\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x15\x81\x25\x7f\x75\x08\x95\x02\x81\x06\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x01\x81\x06\xc0\xc0' > "$G/functions/hid.usb0/report_desc"

# Mass storage (virtual USB drive). Backing image; present even if missing (= no media).
# Runtime attach/detach is done by the web app writing lun.0/file (path = attached, "" = ejected).
IMG="$(command -v kvm-conf-get >/dev/null 2>&1 && kvm-conf-get usb image_path /opt/kvm/images/drive.img || echo /opt/kvm/images/drive.img)"
mkdir -p "$G/functions/mass_storage.usb0"
echo 1 > "$G/functions/mass_storage.usb0/lun.0/removable"
echo 0 > "$G/functions/mass_storage.usb0/lun.0/ro"
echo 0 > "$G/functions/mass_storage.usb0/lun.0/cdrom"
[ -f "$IMG" ] && echo "$IMG" > "$G/functions/mass_storage.usb0/lun.0/file"

# Config + link the functions
mkdir -p "$G/configs/c.1/strings/0x409"
echo "HID Keyboard + Absolute Mouse + Mass Storage" > "$G/configs/c.1/strings/0x409/configuration"
echo 250 > "$G/configs/c.1/MaxPower"
ln -s "$G/functions/hid.usb0" "$G/configs/c.1/"
ln -s "$G/functions/mass_storage.usb0" "$G/configs/c.1/"

# Bind to the USB Device Controller (wait for it — dwc2 may not be ready at early boot)
UDC=""
for i in $(seq 1 50); do UDC=$(ls /sys/class/udc 2>/dev/null | head -n1); [ -n "$UDC" ] && break; sleep 0.2; done
[ -n "$UDC" ] || { echo "ERROR: no UDC found (is dtoverlay=dwc2,dr_mode=peripheral set?)" >&2; exit 1; }
echo "$UDC" > "$G/UDC"

# Let the unprivileged web-service group write the HID endpoints (udev also sets this).
if getent group diykvm >/dev/null 2>&1; then
  for d in /dev/hidg*; do [ -e "$d" ] && chgrp diykvm "$d" 2>/dev/null && chmod 0660 "$d" 2>/dev/null; done
fi

echo "GADGET_UP udc=$UDC"
ls -l /dev/hidg* 2>/dev/null
