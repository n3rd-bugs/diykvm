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

if s == orig:
    sys.exit("patch_rearm: no changes applied")
open(path, "w").write(s)
print("patch_rearm: added OUT re-arm watchdog (module param rearm_ms, default 250)")
