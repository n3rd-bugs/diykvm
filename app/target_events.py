"""Derive semantic 'target' events from raw domain-state diffs.

The events poller samples each domain's state (USB/gadget, power, serial, HID, drive) and, when a value
changes, publishes a coarse full-state event (back-compat) AND the fine-grained transition events this
module derives -- the things an operator/tooling actually cares about: the target powering up/down,
enumerating the gadget, the COM port appearing, a serial port opening, HID coming online, a drive
attaching, etc.

Every function here is PURE: given the previous and current state for one domain it returns a list of
event dicts, each shaped {"type": <dotted name>, "message": <human text>, ...fields}. `prev is None`
means "no baseline yet" (first poll / just connected) and yields [] so a fresh start doesn't emit a
flood of synthetic transitions. The poller publishes each returned dict via EVENTS.publish(type, ...).

Event catalogue (all `type` values that can appear):
    usb.enumerated  usb.enumerating  usb.suspended  usb.resumed  usb.detached
    gadget.bound    gadget.unbound
    power.on        power.off
    serial.enumerated  serial.deenumerated
    serial.opened   serial.closed   serial.reconnected   serial.error   serial.error_cleared
    hid.online      hid.offline
    drive.attached  drive.detached  drive.editing
"""

# All transition types this module can emit -- handy for docs/consumers that want to enumerate them.
EVENT_TYPES = (
    "usb.enumerated", "usb.enumerating", "usb.suspended", "usb.resumed", "usb.detached",
    "gadget.bound", "gadget.unbound",
    "power.on", "power.off",
    "serial.enumerated", "serial.deenumerated",
    "serial.opened", "serial.closed", "serial.reconnected", "serial.error", "serial.error_cleared",
    "hid.online", "hid.offline",
    "drive.attached", "drive.detached", "drive.editing",
)

# UDC states that are steps in enumeration but not yet 'configured'.
_ENUMERATING = ("attached", "powered", "default", "addressed")
_PRE_LINK = ("not attached", "unbound", "unknown", None)


def _ev(type, message, **fields):
    return dict(type=type, message=message, **fields)


def usb_events(prev, cur):
    """prev/cur = {'udc','bound','state'} from _usb_state(). Emits gadget bind + USB link transitions."""
    if prev is None or cur is None:
        return []
    out = []
    if cur.get("bound") and not prev.get("bound"):
        out.append(_ev("gadget.bound", "Gadget bound to UDC %s." % (cur.get("udc") or "?"),
                       udc=cur.get("udc")))
    elif prev.get("bound") and not cur.get("bound"):
        out.append(_ev("gadget.unbound", "Gadget unbound from its UDC.", udc=prev.get("udc")))

    ps, cs = prev.get("state"), cur.get("state")
    if cs == ps:
        return out
    if cs == "configured":
        if ps == "suspended":
            out.append(_ev("usb.resumed", "Target resumed from USB suspend.", **{"from": ps, "to": cs}))
        else:
            out.append(_ev("usb.enumerated",
                           "Target enumerated the USB gadget (HID, serial and drive are live).",
                           **{"from": ps, "to": cs}))
    elif cs == "suspended":
        out.append(_ev("usb.suspended", "Target suspended the USB bus (sleep / port powered down).",
                       **{"from": ps, "to": cs}))
    elif cs == "not attached":
        out.append(_ev("usb.detached", "Target detached (powered off, unplugged, or USB dropped).",
                       **{"from": ps, "to": cs}))
    elif cs in _ENUMERATING and ps in _PRE_LINK:
        out.append(_ev("usb.enumerating", "Target began enumerating the gadget.",
                       **{"from": ps, "to": cs}))
    return out


def com_events(prev, cur):
    """prev/cur = bool: is a CDC-ACM COM port currently presented to the host (gadget configured + acm)."""
    if prev is None:
        return []
    if cur and not prev:
        return [_ev("serial.enumerated", "COM port enumerated to the target (host now sees a serial device).")]
    if prev and not cur:
        return [_ev("serial.deenumerated", "COM port no longer presented to the target.")]
    return []


def power_events(prev, cur):
    """prev/cur = list of power targets [{'label','on',...}]. Emits per-target on/off transitions
    (matched by label; None/unknown states never emit)."""
    if prev is None or cur is None:
        return []
    prevmap = {t.get("label"): t for t in prev}
    out = []
    for t in cur:
        label = t.get("label")
        on = t.get("on")
        was = prevmap.get(label, {}).get("on") if label in prevmap else None
        if on is None or on == was:
            continue
        if on:
            out.append(_ev("power.on", "%s powered on." % label, label=label, mode=t.get("mode")))
        else:
            out.append(_ev("power.off", "%s powered off." % label, label=label, mode=t.get("mode")))
    return out


def hid_events(prev, cur):
    """prev/cur = {'keyboard':bool,'mouse':bool,'mouse_rel':bool} from hid.devices_open()."""
    if prev is None or cur is None:
        return []
    out = []
    for dev, label in (("keyboard", "Keyboard"), ("mouse", "Mouse"), ("mouse_rel", "Relative mouse")):
        was, now = bool(prev.get(dev)), bool(cur.get(dev))
        if now and not was:
            out.append(_ev("hid.online", "%s HID online." % label, device=dev))
        elif was and not now:
            out.append(_ev("hid.offline", "%s HID offline." % label, device=dev))
    return out


def serial_events(prev, cur):
    """prev/cur = list of serial ports from SERIAL.status(). Emits per-device open/close, silent
    reconnects, and error set/clear (matched by device path)."""
    if prev is None or cur is None:
        return []
    prevmap = {p.get("device"): p for p in prev}
    curmap = {p.get("device"): p for p in cur}
    out = []
    for dev, p in curmap.items():
        pp = prevmap.get(dev)
        was_open = bool(pp.get("open")) if pp else False
        if bool(p.get("open")) != was_open:
            if p.get("open"):
                out.append(_ev("serial.opened", "Serial port %s opened." % dev, device=dev))
            else:
                out.append(_ev("serial.closed", "Serial port %s closed." % dev, device=dev))
        prev_rec = pp.get("reconnects") if pp else p.get("reconnects")
        if pp and (p.get("reconnects") or 0) > (prev_rec or 0):
            out.append(_ev("serial.reconnected",
                           "Serial port %s re-opened (target re-enumerated the COM port)." % dev,
                           device=dev, reconnects=p.get("reconnects")))
        prev_err = pp.get("error") if pp else None
        if p.get("error") != prev_err:
            if p.get("error"):
                out.append(_ev("serial.error", "Serial port %s error: %s" % (dev, p.get("error")),
                               device=dev, error=p.get("error")))
            elif pp is not None:
                out.append(_ev("serial.error_cleared", "Serial port %s recovered." % dev, device=dev))
    for dev, pp in prevmap.items():
        if dev not in curmap and pp.get("open"):        # port removed while open
            out.append(_ev("serial.closed", "Serial port %s closed." % dev, device=dev))
    return out


def drive_events(prev, cur):
    """prev/cur = msd.status() dict with a 'state' of attached|editing|detached."""
    if prev is None or cur is None:
        return []
    ps, cs = prev.get("state"), cur.get("state")
    if cs == ps:
        return []
    if cs == "attached":
        img = cur.get("attached_image")
        return [_ev("drive.attached", "Virtual drive attached to the target%s." %
                    ((" (%s)" % img) if img else ""), image=img)]
    if cs == "detached":
        return [_ev("drive.detached", "Virtual drive detached from the target.")]
    if cs == "editing":
        return [_ev("drive.editing", "Virtual drive mounted on the Pi for editing (detached from target).")]
    return []
