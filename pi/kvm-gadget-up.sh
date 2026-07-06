#!/bin/bash
# DIY KVM USB gadget: a BOOT keyboard + a mouse + mass storage.
#   /dev/hidg0  boot keyboard (subclass=1 protocol=1, NO Report ID -> works in BIOS/UEFI):
#                 8 bytes  [mods][0x00][k1..k6]
#   /dev/hidg1  mouse (Report-ID multiplexed):
#                 abs = [0x02][btns][Xlo][Xhi][Ylo][Yhi][wheel]  (7 bytes)
#                 rel = [0x03][btns][dx][dy][wheel]              (5 bytes)
# Each HID is IN-endpoint-only (no_out_endpoint=1) so two HID functions + mass storage stay within
# the Pi dwc2 endpoint/FIFO budget -- this is how PiKVM avoids the multi-HID re-enumeration loop.
# Optional (usb.usb_serial=true): also present a CDC-ACM serial port to the target (-> /dev/ttyGS0).
set -e
G=/sys/kernel/config/usb_gadget/kvm

modprobe libcomposite

# Optional CDC-ACM serial: the target sees a USB COM port; the Pi gets /dev/ttyGS0.
ACM=false
if command -v kvm-conf-get >/dev/null 2>&1; then
  # Accept any BOOL spelling the config helper allows (1/true/yes/on), case-insensitively.
  case "$(kvm-conf-get usb usb_serial false | tr '[:upper:]' '[:lower:]')" in 1|true|yes|on) ACM=true ;; esac
fi

# Optional 2nd mass-storage LUN: a scratch read-write block store the connected host can WRITE(10) to and
# you read back via the web API (GET /api/store/region). Generic: logs, captures, bulk data offload, etc.
# Size and an optional SCSI INQUIRY (so a specific host can identify the LUN) come from config.
STORE=false
if command -v kvm-conf-get >/dev/null 2>&1; then
  case "$(kvm-conf-get usb store_lun false | tr '[:upper:]' '[:lower:]')" in 1|true|yes|on) STORE=true ;; esac
fi
STORE_IMG=/opt/kvm/store.img
STORE_SIZE="$(command -v kvm-conf-get >/dev/null 2>&1 && kvm-conf-get usb store_size 64M || echo 64M)"
STORE_INQ="$(command -v kvm-conf-get >/dev/null 2>&1 && kvm-conf-get usb store_inquiry '' || echo '')"

# --- teardown if it already exists ---
if [ -d "$G" ]; then
  echo "" > "$G/UDC" 2>/dev/null || true
  for l in "$G"/configs/c.1/hid.usb0 "$G"/configs/c.1/hid.usb1 "$G"/configs/c.1/mass_storage.usb0 "$G"/configs/c.1/acm.usb0; do if [ -L "$l" ]; then rm -f "$l"; fi; done
  [ -d "$G/configs/c.1/strings/0x409" ] && rmdir "$G/configs/c.1/strings/0x409" 2>/dev/null || true
  [ -d "$G/configs/c.1" ] && rmdir "$G/configs/c.1" 2>/dev/null || true
  [ -d "$G/functions/hid.usb0" ] && rmdir "$G/functions/hid.usb0" 2>/dev/null || true
  [ -d "$G/functions/hid.usb1" ] && rmdir "$G/functions/hid.usb1" 2>/dev/null || true
  if [ -d "$G/functions/acm.usb0" ]; then rmdir "$G/functions/acm.usb0" || true; fi
  if [ -d "$G/functions/mass_storage.usb0" ]; then
    echo "" > "$G/functions/mass_storage.usb0/lun.0/file" 2>/dev/null || true
    # extra LUNs (e.g. lun.1 trace) must be removed before the function dir can rmdir
    if [ -d "$G/functions/mass_storage.usb0/lun.1" ]; then
      echo "" > "$G/functions/mass_storage.usb0/lun.1/file" 2>/dev/null || true
      rmdir "$G/functions/mass_storage.usb0/lun.1" 2>/dev/null || true
    fi
    rmdir "$G/functions/mass_storage.usb0" 2>/dev/null || true
  fi
  [ -d "$G/strings/0x409" ] && rmdir "$G/strings/0x409" 2>/dev/null || true
  rmdir "$G" 2>/dev/null || true     # never let a teardown failure (e.g. a still-busy lun.1) abort the rebuild
fi

# --- create gadget ---
mkdir -p "$G"
echo 0x1d6b > "$G/idVendor"          # Linux Foundation
if [ "$ACM" = "true" ]; then echo 0x0109 > "$G/idProduct"; else echo 0x0108 > "$G/idProduct"; fi  # PID tracks the interface set
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

# lun.1: optional scratch RW block store. FUA is honored (nofua=0) so a host WRITE(10) with FUA fsyncs the
# backing file -> a Pi page-cache read is coherent. An optional INQUIRY string from config lets a specific
# host identify the LUN. Backed by STORE_IMG; accessible to the web service (group diykvm).
if [ "$STORE" = "true" ]; then
  # Fully guarded: a store-LUN problem must never abort gadget-up (the keyboard/mouse/boot LUN must come up).
  [ -f "$STORE_IMG" ] || truncate -s "$STORE_SIZE" "$STORE_IMG" 2>/dev/null || truncate -s 64M "$STORE_IMG" 2>/dev/null || true
  chgrp diykvm "$STORE_IMG" 2>/dev/null || true; chmod 0640 "$STORE_IMG" 2>/dev/null || true
  L1="$G/functions/mass_storage.usb0/lun.1"
  if [ -f "$STORE_IMG" ] && mkdir -p "$L1" 2>/dev/null; then
    echo 1 > "$L1/removable" 2>/dev/null || true
    echo 0 > "$L1/ro"        2>/dev/null || true
    echo 0 > "$L1/cdrom"     2>/dev/null || true
    echo 0 > "$L1/nofua"     2>/dev/null || true          # FUA -> fsync (REQUIRED for read coherency)
    [ -n "$STORE_INQ" ] && { echo "$STORE_INQ" > "$L1/inquiry_string" 2>/dev/null || true; }   # blank -> kernel default
    echo "$STORE_IMG" > "$L1/file" 2>/dev/null || true
  fi
fi

# acm.usb0: optional CDC-ACM serial -> target sees a USB COM port; the Pi gets /dev/ttyGS0.
if [ "$ACM" = "true" ]; then mkdir -p "$G/functions/acm.usb0"; fi

# Config + link the functions (link order sets /dev/hidg numbering: kbd=hidg0, mouse=hidg1)
mkdir -p "$G/configs/c.1/strings/0x409"
echo "Keyboard + Mouse + Mass Storage" > "$G/configs/c.1/strings/0x409/configuration"
echo 250 > "$G/configs/c.1/MaxPower"
ln -s "$G/functions/hid.usb0"          "$G/configs/c.1/"
ln -s "$G/functions/hid.usb1"          "$G/configs/c.1/"
ln -s "$G/functions/mass_storage.usb0" "$G/configs/c.1/"
if [ "$ACM" = "true" ]; then ln -s "$G/functions/acm.usb0" "$G/configs/c.1/"; fi

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

# Optional gadget serial: let the web service (dialout group) open the target-facing COM port.
if [ "$ACM" = "true" ]; then
  for _ in $(seq 1 25); do [ -e /dev/ttyGS0 ] && break; sleep 0.1; done
  if [ -e /dev/ttyGS0 ]; then chgrp dialout /dev/ttyGS0 2>/dev/null || true; chmod 0660 /dev/ttyGS0 2>/dev/null || true; fi
fi

echo "GADGET_UP udc=$UDC acm=$ACM"
ls -l /dev/hidg* /dev/ttyGS* 2>/dev/null || true
