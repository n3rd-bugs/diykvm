#!/bin/bash
# Build a u_serial.ko whose RX/TX request queue depth is a tunable module parameter (gadget serial / CDC-ACM).
#
# WHY
#   The in-tree u_serial hardcodes `#define QUEUE_SIZE 16`: only 16 bulk-OUT (host->device) USB requests are
#   pre-queued, and gs_read_complete re-queues each one only after copying it to the tty. A back-to-back
#   High-Speed bulk-OUT burst (e.g. a bursty host->device trace/log stream) consumes the pool faster than it
#   so the OUT endpoint NAK-stalls after ~16-40 packets.
#
#   This is a SOFTWARE default, NOT a hardware limit. Each extra request is just one MaxPacket (512 B HS) of
#   host RAM; the dwc2 RX FIFO (g-rx-fifo-size, ~558 words on a Pi 4) + DMA sustain a far deeper queue. The
#   patch turns QUEUE_SIZE into a module parameter (default 256) so a burst is absorbed without re-queue
#   pressure: 256 packets = 128 KB of OUT buffering; an 87 x 80 B handshake (~7 KB) fits in the first ~14.
#
# WHAT
#   Fetches the matching u_serial.{c,h}, rewrites the QUEUE_SIZE #define into `module_param(queue_size,...)`,
#   and builds out-of-tree against the running kernel's headers. It does NOT load or install anything --
#   deploy / persist / recover steps are in README.md.
#
# USAGE
#   ./build-u_serial-queue.sh [kernel-source-branch] [default-depth]
#   e.g. ./build-u_serial-queue.sh rpi-6.12.y 256
#
# REQUIRES: linux-headers-$(uname -r), build-essential, curl. Run on the Pi.
set -euo pipefail
KREL="$(uname -r)"
BRANCH="${1:-rpi-6.12.y}"          # raspberrypi/linux branch that matches $KREL
DEPTH="${2:-256}"
KBUILD="/lib/modules/$KREL/build"
BASE="https://raw.githubusercontent.com/raspberrypi/linux/${BRANCH}/drivers/usb/gadget/function"

[ -e "$KBUILD/Makefile" ] || { echo "ERROR: no kernel build tree at $KBUILD (install linux-headers-$KREL)"; exit 1; }
command -v make >/dev/null && command -v gcc >/dev/null || { echo "ERROR: install build-essential"; exit 1; }

WORK="$(mktemp -d)"
echo "kernel=$KREL  branch=$BRANCH  default queue_size=$DEPTH  workdir=$WORK"
curl -fsSL "$BASE/u_serial.c" -o "$WORK/u_serial.c"
curl -fsSL "$BASE/u_serial.h" -o "$WORK/u_serial.h"

python3 - "$WORK/u_serial.c" "$DEPTH" <<'PY'
import re, sys
path, depth = sys.argv[1], sys.argv[2]
s = open(path).read()
repl = ("static unsigned int queue_size = %s;\n"
        "module_param(queue_size, uint, 0644);\n"
        'MODULE_PARM_DESC(queue_size, "RX/TX request queue depth in packets (raise for bursty bulk-OUT)");\n'
        "#define QUEUE_SIZE\t\t(queue_size)") % depth
s2 = re.sub(r'#define QUEUE_SIZE\s+\d+', repl, s, count=1)
assert s2 != s, "QUEUE_SIZE #define not found -- wrong source branch for this kernel?"
open(path, "w").write(s2)
PY

printf 'obj-m += u_serial.o\nall:\n\t$(MAKE) -C %s M=%s modules\n' "$KBUILD" "$WORK" > "$WORK/Makefile"
make -C "$KBUILD" "M=$WORK" modules

echo "=== built: $WORK/u_serial.ko ==="
modinfo "$WORK/u_serial.ko" | grep -iE 'vermagic|^parm'
echo "in-tree vermagic: $(modinfo u_serial | awk '/vermagic/{print $2,$3,$4,$5,$6}')"
echo "Artifact: $WORK/u_serial.ko  (see README.md to deploy / persist / recover)"
