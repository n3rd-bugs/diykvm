#!/bin/bash
# DIY KVM USB gadget: a BOOT keyboard + a mouse + mass storage.
#   /dev/hidg0  boot keyboard (subclass=1 protocol=1, NO Report ID -> works in BIOS/UEFI):
#                 8 bytes  [mods][0x00][k1..k6]
#   /dev/hidg1  mouse (Report-ID multiplexed):
#                 abs = [0x02][btns][Xlo][Xhi][Ylo][Yhi][wheel]  (7 bytes)
#                 rel = [0x03][btns][dx][dy][wheel]              (5 bytes)
# Each HID is IN-endpoint-only (no_out_endpoint=1) so two HID functions + mass storage stay within
# the Pi dwc2 endpoint/FIFO budget -- this is how PiKVM avoids the multi-HID re-enumeration loop.
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
echo 0x0108 > "$G/idProduct"         # boot-kbd + mouse split (bumped to force fresh host enumeration)
echo 0x0100 > "$G/bcdDevice"
echo 0x0200 > "$G/bcdUSB"
echo 0x00   > "$G/bDeviceClass"      # composite; class per-interface
echo 0x00   > "$G/bDeviceSubClass"
echo 0x00   > "$G/bDeviceProtocol"

mkdir -p "$G/strings/0x409"
echo "kvm-0001"          > "$G/strings/0x409/serialnumber"
echo "DIY-PiKVM"         > "$G/strings/0x409/manufacturer"
echo "PiKVM HID Console" > "$G/strings/0x409/product"

# hid.usb0: BOOT KEYBOARD -> /dev/hidg0 (8-byte report, NO Report ID, BIOS/UEFI compatible)
mkdir -p "$G/functions/hid.usb0"
echo 1 > "$G/functions/hid.usb0/protocol"        # 1 = Keyboard
echo 1 > "$G/functions/hid.usb0/subclass"        # 1 = Boot Interface Subclass
echo 8 > "$G/functions/hid.usb0/report_length"   # fixed 8-byte boot report
echo 1 > "$G/functions/hid.usb0/no_out_endpoint" 2>/dev/null || true   # IN-only; LEDs via SET_REPORT on EP0
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' > "$G/functions/hid.usb0/report_desc"

# hid.usb1: MOUSE -> /dev/hidg1 (abs = Report ID 2, rel = Report ID 3)
mkdir -p "$G/functions/hid.usb1"
echo 0 > "$G/functions/hid.usb1/protocol"
echo 0 > "$G/functions/hid.usb1/subclass"
echo 7 > "$G/functions/hid.usb1/report_length"   # largest mouse report (abs = id + 6 bytes)
echo 1 > "$G/functions/hid.usb1/no_out_endpoint" 2>/dev/null || true
printf '\x05\x01\x09\x02\xa1\x01\x85\x02\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x16\x00\x00\x26\xff\x7f\x75\x10\x95\x02\x81\x02\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x01\x81\x06\xc0\xc0\x05\x01\x09\x02\xa1\x01\x85\x03\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x15\x81\x25\x7f\x75\x08\x95\x02\x81\x06\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x01\x81\x06\xc0\xc0' > "$G/functions/hid.usb1/report_desc"

# Mass storage (virtual USB drive). Backing image; present even if missing (= no media).
# Runtime attach/detach is done by the web app writing lun.0/file (path = attached, "" = ejected).
IMG="$(command -v kvm-conf-get >/dev/null 2>&1 && kvm-conf-get usb image_path /opt/kvm/images/drive.img || echo /opt/kvm/images/drive.img)"
mkdir -p "$G/functions/mass_storage.usb0"
echo 1 > "$G/functions/mass_storage.usb0/lun.0/removable"
echo 0 > "$G/functions/mass_storage.usb0/lun.0/ro"
echo 0 > "$G/functions/mass_storage.usb0/lun.0/cdrom"
[ -f "$IMG" ] && echo "$IMG" > "$G/functions/mass_storage.usb0/lun.0/file"

# Config + link the functions (link order sets /dev/hidg numbering: kbd=hidg0, mouse=hidg1)
mkdir -p "$G/configs/c.1/strings/0x409"
echo "Keyboard + Mouse + Mass Storage" > "$G/configs/c.1/strings/0x409/configuration"
echo 250 > "$G/configs/c.1/MaxPower"
ln -s "$G/functions/hid.usb0"          "$G/configs/c.1/"
ln -s "$G/functions/hid.usb1"          "$G/configs/c.1/"
ln -s "$G/functions/mass_storage.usb0" "$G/configs/c.1/"

# Bind to the USB Device Controller (wait for it — dwc2 may not be ready at early boot)
UDC=""
for _ in $(seq 1 50); do
  for u in /sys/class/udc/*; do [ -e "$u" ] && { UDC=${u##*/}; break; }; done
  [ -n "$UDC" ] && break
  sleep 0.2
done
[ -n "$UDC" ] || { echo "ERROR: no UDC found (is dtoverlay=dwc2,dr_mode=peripheral set?)" >&2; exit 1; }
echo "$UDC" > "$G/UDC"

# Let the unprivileged web-service group write the HID endpoints. The udev rule
# (99-kvm-hidg.rules) is the source of truth for /dev/hidg0 and /dev/hidg1; this loop is a
# best-effort fast-path, so briefly wait for the nodes the kernel creates asynchronously after bind.
if getent group diykvm >/dev/null 2>&1; then
  for _ in $(seq 1 25); do [ -e /dev/hidg1 ] && break; sleep 0.1; done
  for d in /dev/hidg*; do [ -e "$d" ] && chgrp diykvm "$d" 2>/dev/null && chmod 0660 "$d" 2>/dev/null; done
fi

echo "GADGET_UP udc=$UDC"
ls -l /dev/hidg* 2>/dev/null
