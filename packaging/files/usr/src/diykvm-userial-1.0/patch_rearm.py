#!/usr/bin/env python3
"""Add a periodic OUT-endpoint re-arm watchdog to u_serial.c (gadget serial / CDC-ACM).

WHY: after a gadget re-enumeration / USB reset while an OUT endpoint is under load, the bulk-OUT request
queue can drain to empty and is never re-armed (gs_start_rx isn't re-driven), so the endpoint "dies out"
until the tty is closed+reopened. This adds a global delayed_work that, every `rearm_ms`, kicks each open,
connected port's existing `push` work -- which re-arms the OUT queue via gs_rx_push()->gs_start_rx() in its
normal context. Idempotent (gs_start_rx tops up to QUEUE_SIZE), cause-agnostic, self-heals without an app
reopen. rearm_ms=0 disables it. Safe locking: read ports[] under the per-port mutex (which gserial_free_line
holds while clearing the slot), never call gs_start_rx under serial_port_lock.

    python3 patch_rearm.py u_serial.c
"""
import sys

path = sys.argv[1]
s = open(path).read()
orig = s


def sub(anchor, new, why):
    global s
    if anchor not in s:
        sys.exit("patch_rearm: anchor not found (%s) -- wrong source for this kernel?" % why)
    if s.count(anchor) != 1:
        sys.exit("patch_rearm: anchor not unique (%s)" % why)
    s = s.replace(anchor, new, 1)


# 1. module param + work declaration, right after the ports[] array.
A1 = "} ports[MAX_U_SERIAL_PORTS];\n"
sub(A1, A1 + (
    "\n/* DIY-KVM: periodic OUT-endpoint re-arm watchdog. After a re-enumeration/USB reset while an OUT\n"
    " * endpoint is under load, its bulk-OUT request queue can drain to empty and never get re-armed, so the\n"
    " * endpoint dies out until the tty is closed+reopened. This tops up every open+connected port's OUT\n"
    " * queue every rearm_ms so it self-heals without an app reopen. rearm_ms=0 disables it (load-time). */\n"
    "static unsigned int rearm_ms = 250;\n"
    "module_param(rearm_ms, uint, 0444);\n"   # 0444 = load-time only: the watchdog is (re)scheduled only at
    "MODULE_PARM_DESC(rearm_ms,\n"            # init/from itself, so a runtime 0->N write would never re-arm it.
    "\t\"DIY-KVM: ms between gadget OUT-endpoint re-arm sweeps (0=off, default 250; set via modprobe option)\");\n"
    "static struct delayed_work gs_rearm_dwork;\n"
    "/* DIY-KVM: keep an open ttyGS port across a transient gadget re-enumeration. The stock disconnect\n"
    " * hangs up the tty (zeroing port.count), so an open port can't re-arm its OUT endpoint on reconnect\n"
    " * and \"dies out\" until the app closes+reopens. Skipping the hangup lets the open fd survive the\n"
    " * re-enumerate; gserial_connect()/the re-arm watchdog then re-arm the OUT queue transparently. */\n"
    "static bool survive_reenum = true;\n"
    "module_param(survive_reenum, bool, 0644);\n"
    "MODULE_PARM_DESC(survive_reenum, \"DIY-KVM: don't tty-hangup on gadget disconnect so an open port survives a re-enumerate (default 1)\");\n"
    "/* DIY-KVM: IN/TX-side loss fix. gs_start_tx consumes up to maxpacket bytes from the write fifo into a\n"
    " * request BEFORE usb_ep_queue; on a transient queue failure the stock code returns that request to the\n"
    " * free pool, silently dropping its payload (observed as exact-512B gaps under sustained TX flood).\n"
    " * Park the filled request instead and send it first on the next TX kick. tx_qfail/tx_retried expose\n"
    " * live counters; tx_keep_on_fail=0 restores the stock (lossy) behaviour for A/B testing. */\n"
    "static struct usb_request *gs_tx_pending[MAX_U_SERIAL_PORTS];\n"
    "static bool tx_keep_on_fail = true;\n"
    "module_param(tx_keep_on_fail, bool, 0644);\n"
    "MODULE_PARM_DESC(tx_keep_on_fail, \"DIY-KVM: park+retry a TX request whose usb_ep_queue failed instead of dropping its data (default 1)\");\n"
    "static unsigned long tx_qfail;\n"
    "module_param(tx_qfail, ulong, 0444);\n"
    "MODULE_PARM_DESC(tx_qfail, \"DIY-KVM: count of transient usb_ep_queue failures on the serial IN/TX path\");\n"
    "static unsigned long tx_retried;\n"
    "module_param(tx_retried, ulong, 0444);\n"
    "MODULE_PARM_DESC(tx_retried, \"DIY-KVM: count of parked TX requests successfully retried\");\n"
), "ports[] decl")

# 2. the watchdog function, just before userial_init().
A2 = "static int __init userial_init(void)\n"
sub(A2, (
    "/* DIY-KVM: kick each open, connected port's push work so a drained OUT queue is re-armed\n"
    " * (gs_rx_push -> gs_start_rx). ports[] is read under the per-port mutex that gserial_free_line() holds\n"
    " * while clearing the slot, so the port can't be freed under us; we never hold serial_port_lock here. */\n"
    "static void gs_rearm_fn(struct work_struct *w)\n"
    "{\n"
    "\tint i;\n"
    "\n"
    "\tfor (i = 0; i < MAX_U_SERIAL_PORTS; i++) {\n"
    "\t\tstruct gs_port *port;\n"
    "\n"
    "\t\tmutex_lock(&ports[i].lock);\n"
    "\t\tport = ports[i].port;\n"
    "\t\tif (port) {\n"
    "\t\t\tunsigned long flags;\n"
    "\t\t\tbool active;\n"
    "\n"
    "\t\t\tspin_lock_irqsave(&port->port_lock, flags);\n"
    "\t\t\tactive = port->port_usb && port->port.count;\n"
    "\t\t\tif (port->port_usb && gs_tx_pending[port->port_num])\n"
    "\t\t\t\tgs_start_tx(port);\t/* retry a parked TX request (see gs_start_tx) */\n"
    "\t\t\tspin_unlock_irqrestore(&port->port_lock, flags);\n"
    "\t\t\tif (active)\n"
    "\t\t\t\tschedule_delayed_work(&port->push, 0);\n"
    "\t\t}\n"
    "\t\tmutex_unlock(&ports[i].lock);\n"
    "\t}\n"
    "\n"
    "\tif (rearm_ms)\n"
    "\t\tschedule_delayed_work(&gs_rearm_dwork, msecs_to_jiffies(rearm_ms));\n"
    "}\n"
    "\n"
) + A2, "userial_init")

# 3. start the watchdog in userial_init (after the tty driver is live).
A3 = "\tgs_tty_driver = driver;\n"
sub(A3, A3 + (
    "\n\tINIT_DELAYED_WORK(&gs_rearm_dwork, gs_rearm_fn);\n"
    "\tif (rearm_ms)\n"
    "\t\tschedule_delayed_work(&gs_rearm_dwork, msecs_to_jiffies(rearm_ms));\n"
), "userial_init body")

# 4. stop the watchdog in userial_cleanup.
A4 = "static void __exit userial_cleanup(void)\n{\n"
sub(A4, A4 + "\tcancel_delayed_work_sync(&gs_rearm_dwork);\n", "userial_cleanup")

# 5. in gserial_disconnect(), skip the tty hangup when survive_reenum is set, so an open port survives a
#    transient re-enumerate (its port.count stays non-zero -> the OUT endpoint can re-arm on reconnect).
A5 = "\t\tif (port->port.tty)\n\t\t\ttty_hangup(port->port.tty);\n"
sub(A5, "\t\tif (port->port.tty && !survive_reenum)\n\t\t\ttty_hangup(port->port.tty);\n", "gserial_disconnect hangup")

# 6. gs_start_tx: also enter the send loop when a parked (queue-failed) request is waiting, even if the
#    free pool is momentarily empty.
A6 = "\twhile (!port->write_busy && !list_empty(pool)) {\n"
sub(A6, "\twhile (!port->write_busy &&\n"
        "\t       (gs_tx_pending[port->port_num] || !list_empty(pool))) {\n",
    "gs_start_tx loop condition")

# 7. gs_start_tx: send a parked request FIRST (its bytes were already consumed from the write fifo --
#    re-filling from the fifo would silently skip them), otherwise fill a fresh request as stock does.
A7 = (
    "\t\treq = list_entry(pool->next, struct usb_request, list);\n"
    "\t\tlen = gs_send_packet(port, req->buf, in->maxpacket);\n"
    "\t\tif (len == 0) {\n"
    "\t\t\twake_up_interruptible(&port->drain_wait);\n"
    "\t\t\tbreak;\n"
    "\t\t}\n"
    "\t\tdo_tty_wake = true;\n"
    "\t\tport->icount.tx += len;\n"
    "\n"
    "\t\treq->length = len;\n"
    "\t\tlist_del(&req->list);\n"
)
sub(A7, (
    "\t\tif (gs_tx_pending[port->port_num]) {\n"
    "\t\t\t/* DIY-KVM: retry the parked request first -- its payload was already consumed\n"
    "\t\t\t * from the write fifo and would otherwise be lost (see the queue-failure path). */\n"
    "\t\t\treq = gs_tx_pending[port->port_num];\n"
    "\t\t\tgs_tx_pending[port->port_num] = NULL;\n"
    "\t\t\tlen = req->length;\n"
    "\t\t\ttx_retried++;\n"
    "\t\t\tdo_tty_wake = true;\n"
    "\t\t} else {\n"
    "\t\t\treq = list_entry(pool->next, struct usb_request, list);\n"
    "\t\t\tlen = gs_send_packet(port, req->buf, in->maxpacket);\n"
    "\t\t\tif (len == 0) {\n"
    "\t\t\t\twake_up_interruptible(&port->drain_wait);\n"
    "\t\t\t\tbreak;\n"
    "\t\t\t}\n"
    "\t\t\tdo_tty_wake = true;\n"
    "\t\t\tport->icount.tx += len;\n"
    "\n"
    "\t\t\treq->length = len;\n"
    "\t\t\tlist_del(&req->list);\n"
    "\t\t}\n"
), "gs_start_tx fill/retry")

# 8. gs_start_tx: on usb_ep_queue failure, park the filled request (data preserved, retried by the next
#    write/completion/watchdog kick) instead of returning it to the pool (which drops its payload).
A8 = (
    "\t\tif (status) {\n"
    "\t\t\tpr_debug(\"%s: %s %s err %d\\n\",\n"
    "\t\t\t\t\t__func__, \"queue\", in->name, status);\n"
    "\t\t\tlist_add(&req->list, pool);\n"
    "\t\t\tbreak;\n"
    "\t\t}\n"
)
sub(A8, (
    "\t\tif (status) {\n"
    "\t\t\tpr_debug(\"%s: %s %s err %d\\n\",\n"
    "\t\t\t\t\t__func__, \"queue\", in->name, status);\n"
    "\t\t\t/* DIY-KVM: req->buf holds bytes already consumed from the write fifo; returning the\n"
    "\t\t\t * request straight to the pool silently drops them (one maxpacket per transient\n"
    "\t\t\t * failure). Park it for retry instead. */\n"
    "\t\t\ttx_qfail++;\n"
    "\t\t\tif (tx_keep_on_fail && !gs_tx_pending[port->port_num])\n"
    "\t\t\t\tgs_tx_pending[port->port_num] = req;\n"
    "\t\t\telse\n"
    "\t\t\t\tlist_add(&req->list, pool);\n"
    "\t\t\tbreak;\n"
    "\t\t}\n"
), "gs_start_tx queue-failure park")

# 9. gserial_disconnect: return a parked request to the pool (under the same port_lock) right before the
#    write_pool is freed, so it can't leak across a disconnect.
A9 = "\tgs_free_requests(gser->in, &port->write_pool, NULL);\n"
sub(A9, (
    "\tif (gs_tx_pending[port->port_num]) {\t/* DIY-KVM: don't leak a parked TX request */\n"
    "\t\tlist_add(&gs_tx_pending[port->port_num]->list, &port->write_pool);\n"
    "\t\tgs_tx_pending[port->port_num] = NULL;\n"
    "\t}\n"
) + A9, "gserial_disconnect parked-req cleanup")

if s == orig:
    sys.exit("patch_rearm: no changes applied")
open(path, "w").write(s)
print("patch_rearm: added OUT re-arm watchdog (rearm_ms), survive_reenum, and TX park-on-queue-failure (tx_keep_on_fail/tx_qfail/tx_retried)")
