#!/bin/bash
# DIY KVM USB gadget: a composite of selectable functions, each toggled in [usb] config so a target only
# gets what it needs. Each function costs dwc2 endpoints and the Pi controller has only 7 total, so
# disabling what you don't need keeps the gadget well under budget (fewer functions = more robust):
#   keyboard=true     /dev/hidg0  BOOT keyboard (subclass=1 protocol=1, NO Report ID -> BIOS/UEFI)  (1 IN)
#                       8 bytes  [mods][0x00][k1..k6]
#   mouse=true        /dev/hidg1  ABSOLUTE pointer (Report ID 2)                                    (1 IN)
#                       [0x02][btns][Xlo][Xhi][Ylo][Yhi][wheel]   X/Y 0..32767
#   mouse_rel=true    /dev/hidg2  RELATIVE pointer (Report ID 3)                                    (1 IN)
#                       [0x03][btns][dx][dy][wheel]
#   mass_storage=true             virtual USB drive (+ optional 2nd store LUN)               (1 IN + 1 OUT)
#   usb_serial=true               CDC-ACM COM port -> /dev/ttyGS0                        (1 IN + 1 OUT + 1 IN)
# The absolute and relative pointers are SEPARATE HID functions on purpose: packed as two collections in
# one interface, Linux hosts (hid-generic) merge them into ONE input device and silently DROP the second
# collection's buttons -- relative-mode clicks never reach the target. As separate interfaces every OS
# binds two complete mice (Windows split per-collection anyway, so it sees the same devices as before).
# Each HID is IN-endpoint-only (no_out_endpoint=1). All HID functions are ALWAYS created so /dev/hidg
# numbering stays stable (kbd=hidg0, abs=hidg1, rel=hidg2 -- minors are reserved at creation, in order);
# they are only LINKED into the config when enabled, and an unlinked function uses no endpoints.
set -e
G=/sys/kernel/config/usb_gadget/kvm

modprobe libcomposite

# --- which functions to present (from [usb] config; defaults = the classic kbd+mouse+mass-storage set,
#     so existing setups are unchanged) ---
cfg_bool() {   # cfg_bool KEY DEFAULT -> echoes true/false (any 1/true/yes/on spelling, case-insensitive)
  local v="$2"
  command -v kvm-conf-get >/dev/null 2>&1 && v="$(kvm-conf-get usb "$1" "$2")"
  case "${v,,}" in 1|true|yes|on) echo true ;; *) echo false ;; esac
}
KBD=$(cfg_bool keyboard true)          # HID boot keyboard      -> /dev/hidg0
MOUSE=$(cfg_bool mouse true)           # HID absolute pointer   -> /dev/hidg1
MREL=$(cfg_bool mouse_rel true)        # HID relative pointer   -> /dev/hidg2
MSD=$(cfg_bool mass_storage true)      # virtual USB drive
ACM=$(cfg_bool usb_serial false)       # CDC-ACM serial         -> /dev/ttyGS0
STORE=$(cfg_bool store_lun false)      # 2nd mass-storage LUN (scratch block store)
[ "$MSD" = "true" ] || STORE=false     # the store LUN lives on the mass_storage function
if [ "$KBD" != "true" ] && [ "$MOUSE" != "true" ] && [ "$MREL" != "true" ] && [ "$MSD" != "true" ] && [ "$ACM" != "true" ]; then
  echo "ERROR: [usb] config disables ALL gadget functions -- enable at least one" >&2; exit 1
fi
# Endpoint budget: dwc2 has 7. kbd/abs/rel = 1 each, mass_storage = 2, serial = 3; everything on = 8.
# Rather than fail the bind (dead KVM), shed the relative pointer -- the one nicety -- and say so.
EP=0
[ "$KBD" = "true" ]   && EP=$((EP + 1))
[ "$MOUSE" = "true" ] && EP=$((EP + 1))
[ "$MREL" = "true" ]  && EP=$((EP + 1))
[ "$MSD" = "true" ]   && EP=$((EP + 2))
[ "$ACM" = "true" ]   && EP=$((EP + 3))
if [ "$EP" -gt 7 ] && [ "$MREL" = "true" ]; then
  echo "WARN: $EP endpoints wanted but dwc2 has 7 -- dropping the relative mouse (disable usb_serial or mass_storage in [usb] to keep it)" >&2
  MREL=false
fi
STORE_IMG=/opt/kvm/store.img
STORE_SIZE="$(command -v kvm-conf-get >/dev/null 2>&1 && kvm-conf-get usb store_size 64M || echo 64M)"
STORE_INQ="$(command -v kvm-conf-get >/dev/null 2>&1 && kvm-conf-get usb store_inquiry '' || echo '')"

# --- teardown if it already exists ---
if [ -d "$G" ]; then
  echo "" > "$G/UDC" 2>/dev/null || true
  for l in "$G"/configs/c.1/hid.usb0 "$G"/configs/c.1/hid.usb1 "$G"/configs/c.1/hid.usb2 "$G"/configs/c.1/mass_storage.usb0 "$G"/configs/c.1/acm.usb0; do if [ -L "$l" ]; then rm -f "$l"; fi; done
  rmdir "$G/configs/c.1/strings/0x409" 2>/dev/null || true
  rmdir "$G/configs/c.1" 2>/dev/null || true
  rmdir "$G/functions/hid.usb0" 2>/dev/null || true
  rmdir "$G/functions/hid.usb1" 2>/dev/null || true
  rmdir "$G/functions/hid.usb2" 2>/dev/null || true
  if [ -d "$G/functions/acm.usb0" ]; then rmdir "$G/functions/acm.usb0" || true; fi
  if [ -d "$G/functions/mass_storage.usb0" ]; then
    echo "" > "$G/functions/mass_storage.usb0/lun.0/file" 2>/dev/null || true
    # extra LUNs (e.g. lun.1 store) must be removed before the function dir can rmdir
    if [ -d "$G/functions/mass_storage.usb0/lun.1" ]; then
      echo "" > "$G/functions/mass_storage.usb0/lun.1/file" 2>/dev/null || true
      rmdir "$G/functions/mass_storage.usb0/lun.1" 2>/dev/null || true
    fi
    rmdir "$G/functions/mass_storage.usb0" 2>/dev/null || true
  fi
  rmdir "$G/strings/0x409" 2>/dev/null || true
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

# hid.usb0/1/2: ALWAYS created, in this order, so /dev/hidg numbering is stable (minors are reserved at
# creation: kbd=hidg0, abs=hidg1, rel=hidg2); LINKED into the config below only when enabled. An unlinked
# function uses no endpoints (its /dev node simply never appears).
# hid.usb0: BOOT KEYBOARD -> /dev/hidg0 (8-byte report, NO Report ID, BIOS/UEFI compatible)
mkdir -p "$G/functions/hid.usb0"
echo 1 > "$G/functions/hid.usb0/protocol"        # 1 = Keyboard
echo 1 > "$G/functions/hid.usb0/subclass"        # 1 = Boot Interface Subclass
echo 8 > "$G/functions/hid.usb0/report_length"   # fixed 8-byte boot report
echo 1 > "$G/functions/hid.usb0/no_out_endpoint" 2>/dev/null || true   # IN-only; LEDs via SET_REPORT on EP0
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' > "$G/functions/hid.usb0/report_desc"

# hid.usb1: ABSOLUTE MOUSE -> /dev/hidg1 (single collection, Report ID 2 kept so app report bytes are
# unchanged: [0x02][btns][Xlo][Xhi][Ylo][Yhi][wheel], X/Y logical 0..32767, wheel relative)
mkdir -p "$G/functions/hid.usb1"
echo 0 > "$G/functions/hid.usb1/protocol"
echo 0 > "$G/functions/hid.usb1/subclass"
echo 7 > "$G/functions/hid.usb1/report_length"   # id + 6 data bytes
echo 1 > "$G/functions/hid.usb1/no_out_endpoint" 2>/dev/null || true
printf '\x05\x01\x09\x02\xa1\x01\x85\x02\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x16\x00\x00\x26\xff\x7f\x75\x10\x95\x02\x81\x02\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x01\x81\x06\xc0\xc0' > "$G/functions/hid.usb1/report_desc"

# hid.usb2: RELATIVE MOUSE -> /dev/hidg2 (single collection, Report ID 3 kept:
# [0x03][btns][dx][dy][wheel], all axes -127..127)
mkdir -p "$G/functions/hid.usb2"
echo 0 > "$G/functions/hid.usb2/protocol"
echo 0 > "$G/functions/hid.usb2/subclass"
echo 5 > "$G/functions/hid.usb2/report_length"   # id + 4 data bytes
echo 1 > "$G/functions/hid.usb2/no_out_endpoint" 2>/dev/null || true
printf '\x05\x01\x09\x02\xa1\x01\x85\x03\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x15\x81\x25\x7f\x75\x08\x95\x02\x81\x06\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x01\x81\x06\xc0\xc0' > "$G/functions/hid.usb2/report_desc"

# Mass storage (virtual USB drive), only when enabled. Backing image; present even if missing (= no media).
# Runtime attach/detach is done by the web app writing lun.0/file (path = attached, "" = ejected).
if [ "$MSD" = "true" ]; then
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
fi

# acm.usb0: optional CDC-ACM serial -> target sees a USB COM port; the Pi gets /dev/ttyGS0.
if [ "$ACM" = "true" ]; then mkdir -p "$G/functions/acm.usb0"; fi

# Config + link the ENABLED functions (/dev/hidg numbering is fixed by the creation order above)
mkdir -p "$G/configs/c.1/strings/0x409"
echo "DIY PiKVM" > "$G/configs/c.1/strings/0x409/configuration"
echo 250 > "$G/configs/c.1/MaxPower"
if [ "$KBD" = "true" ];   then ln -s "$G/functions/hid.usb0"          "$G/configs/c.1/"; fi
if [ "$MOUSE" = "true" ]; then ln -s "$G/functions/hid.usb1"          "$G/configs/c.1/"; fi
if [ "$MREL" = "true" ];  then ln -s "$G/functions/hid.usb2"          "$G/configs/c.1/"; fi
if [ "$MSD" = "true" ];   then ln -s "$G/functions/mass_storage.usb0" "$G/configs/c.1/"; fi
if [ "$ACM" = "true" ];   then ln -s "$G/functions/acm.usb0"          "$G/configs/c.1/"; fi

# Bind to the USB Device Controller (wait up to ~30s for it -- at early boot the dwc2/udc-core modules may
# not have probed yet, and this unit has no ConditionPathExists guard, so the wait must cover that window)
UDC=""
for _ in $(seq 1 150); do
  for u in /sys/class/udc/*; do [ -e "$u" ] && { UDC=${u##*/}; break; }; done
  [ -n "$UDC" ] && break
  sleep 0.2
done
[ -n "$UDC" ] || { echo "ERROR: no UDC found (is dtoverlay=dwc2,dr_mode=peripheral set?)" >&2; exit 1; }
echo "$UDC" > "$G/UDC"

# Let the unprivileged web-service group write the HID endpoints. The udev rule
# (99-kvm-hidg.rules, KERNEL=="hidg[0-9]*") is the source of truth for ALL /dev/hidg* nodes; this loop is a
# best-effort fast-path, so briefly wait for the enabled HID node(s) the kernel creates asynchronously.
if getent group diykvm >/dev/null 2>&1; then
  for _ in $(seq 1 25); do ls /dev/hidg* >/dev/null 2>&1 && break; sleep 0.1; done
  for d in /dev/hidg*; do [ -e "$d" ] && chgrp diykvm "$d" 2>/dev/null && chmod 0660 "$d" 2>/dev/null; done
fi

# Optional gadget serial: let the web service (dialout group) open the target-facing COM port.
if [ "$ACM" = "true" ]; then
  for _ in $(seq 1 25); do [ -e /dev/ttyGS0 ] && break; sleep 0.1; done
  if [ -e /dev/ttyGS0 ]; then chgrp dialout /dev/ttyGS0 2>/dev/null || true; chmod 0660 /dev/ttyGS0 2>/dev/null || true; fi
fi

echo "GADGET_UP udc=$UDC kbd=$KBD mouse=$MOUSE mouse_rel=$MREL msd=$MSD acm=$ACM"
ls -l /dev/hidg* /dev/ttyGS* 2>/dev/null || true
