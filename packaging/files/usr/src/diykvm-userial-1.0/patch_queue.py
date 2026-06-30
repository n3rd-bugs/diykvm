#!/usr/bin/env python3
"""Rewrite u_serial.c's `#define QUEUE_SIZE <n>` into a runtime module parameter.

The gadget serial (CDC-ACM) pre-queues only QUEUE_SIZE (16) bulk-OUT USB requests; a bursty host->device
transfer exhausts the pool faster than gs_read_complete re-queues it, so the endpoint NAK-stalls. This is a
software constant, not a hardware limit -- the dwc2 FIFO + DMA sustain a far deeper queue. Making it a
parameter (default 256) absorbs the burst.

    python3 patch_queue.py u_serial.c [default_depth]
"""
import re
import sys

path = sys.argv[1]
depth = sys.argv[2] if len(sys.argv) > 2 else "256"
src = open(path).read()
repl = ("static unsigned int queue_size = %s;\n"
        "module_param(queue_size, uint, 0644);\n"
        'MODULE_PARM_DESC(queue_size, "RX/TX request queue depth in packets (raise for bursty bulk-OUT)");\n'
        "#define QUEUE_SIZE\t\t(queue_size)") % depth
out = re.sub(r"#define QUEUE_SIZE\s+\d+", repl, src, count=1)
if out == src:
    sys.exit("patch_queue: '#define QUEUE_SIZE <n>' not found -- wrong source for this kernel?")
open(path, "w").write(out)
print("patch_queue: QUEUE_SIZE -> module param queue_size (default %s)" % depth)
