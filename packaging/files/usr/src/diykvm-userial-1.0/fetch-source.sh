#!/bin/bash
# DKMS PRE_BUILD hook: fetch the u_serial source matching the target kernel and patch its QUEUE_SIZE
# #define into a module parameter (default 256). Runs in the DKMS build directory; needs curl + python3.
set -euo pipefail
KVER="${1:-$(uname -r)}"
QUEUE="${2:-256}"
MM=$(echo "$KVER" | grep -oE '^[0-9]+\.[0-9]+')        # e.g. 6.12.25+rpt-rpi-v8 -> 6.12
BR="rpi-${MM}.y"                                       # raspberrypi/linux branch for that series
BASE="https://raw.githubusercontent.com/raspberrypi/linux/${BR}/drivers/usb/gadget/function"
echo "diykvm-userial: fetching u_serial.{c,h} from ${BR} for kernel ${KVER}"
curl -fsSL "${BASE}/u_serial.c" -o u_serial.c
curl -fsSL "${BASE}/u_serial.h" -o u_serial.h
python3 patch_queue.py u_serial.c "${QUEUE}"
