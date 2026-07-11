"""HID report translation for the DIY KVM.

Three gadget HID devices (the two pointers are SEPARATE USB interfaces: merged as two collections in one
interface, Linux hosts fold them into a single input device and drop the second collection's buttons --
relative-mode clicks never arrive; as separate interfaces every OS binds two complete mice):
  /dev/hidg0  BOOT keyboard    -> 8 bytes, NO report id:  [mods, 0x00, k1..k6]   (works in BIOS/UEFI)
  /dev/hidg1  absolute pointer -> [0x02, btns, Xlo,Xhi, Ylo,Yhi, wheel]   (7 bytes, X/Y 0..32767)
  /dev/hidg2  relative pointer -> [0x03, btns, dx, dy, wheel]             (5 bytes)
The relative pointer is optional (mouse_rel toggle / endpoint budget): if its node is absent, relative
input is dropped and everything else keeps working.
"""
import os
import select
import threading
import time

HIDG_KBD = "/dev/hidg0"        # boot keyboard, 8-byte reports, no report id
HIDG_MOUSE = "/dev/hidg1"      # absolute pointer (Report ID 2)
HIDG_MOUSE_REL = "/dev/hidg2"  # relative pointer (Report ID 3), optional
MOUSE_ABS_ID = 0x02            # desktop: absolute positioning
MOUSE_REL_ID = 0x03            # touch:   relative/trackpad
ABS_MAX = 32767
# Max time to wait for the target to accept a HID report before dropping it. A gadget write to /dev/hidg*
# blocks until the target polls the interrupt-IN endpoint; a healthy target does so within ~1-10ms. If the
# target has stopped polling (asleep/rebooting/hung), a BLOCKING write would hang forever holding the HID
# lock -- and the event-loop poller reads HID state, so that froze the whole KVM. We open non-blocking and
# bound the wait: past this, the report is dropped (input to a non-listening target is worthless anyway).
WRITE_TIMEOUT = 0.2

# JS KeyboardEvent.code -> modifier bit
MODIFIERS = {
    "ControlLeft": 0x01, "ShiftLeft": 0x02, "AltLeft": 0x04, "MetaLeft": 0x08,
    "ControlRight": 0x10, "ShiftRight": 0x20, "AltRight": 0x40, "MetaRight": 0x80,
}

# JS KeyboardEvent.code -> HID usage id (Keyboard/Keypad page 0x07)
KEYMAP = {
    **{f"Key{c}": 0x04 + i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")},
    "Digit1": 0x1E, "Digit2": 0x1F, "Digit3": 0x20, "Digit4": 0x21, "Digit5": 0x22,
    "Digit6": 0x23, "Digit7": 0x24, "Digit8": 0x25, "Digit9": 0x26, "Digit0": 0x27,
    "Enter": 0x28, "Escape": 0x29, "Backspace": 0x2A, "Tab": 0x2B, "Space": 0x2C,
    "Minus": 0x2D, "Equal": 0x2E, "BracketLeft": 0x2F, "BracketRight": 0x30,
    "Backslash": 0x31, "Semicolon": 0x33, "Quote": 0x34, "Backquote": 0x35,
    "Comma": 0x36, "Period": 0x37, "Slash": 0x38, "CapsLock": 0x39,
    **{f"F{n}": 0x3A + (n - 1) for n in range(1, 13)},
    "PrintScreen": 0x46, "ScrollLock": 0x47, "Pause": 0x48,
    "Insert": 0x49, "Home": 0x4A, "PageUp": 0x4B, "Delete": 0x4C, "End": 0x4D,
    "PageDown": 0x4E, "ArrowRight": 0x4F, "ArrowLeft": 0x50, "ArrowDown": 0x51,
    "ArrowUp": 0x52, "NumLock": 0x53,
    "NumpadDivide": 0x54, "NumpadMultiply": 0x55, "NumpadSubtract": 0x56,
    "NumpadAdd": 0x57, "NumpadEnter": 0x58,
    **{f"Numpad{n}": 0x59 + (n - 1) for n in range(1, 10)},
    "Numpad0": 0x62, "NumpadDecimal": 0x63, "ContextMenu": 0x65,
}


class HIDController:
    # Device keys: "kbd" and "mabs" are the KVM essentials; "mrel" is optional (the mouse_rel toggle may be
    # off, or the endpoint budget may have shed it) -- everywhere below treats an absent mrel node as normal.
    DEVICES = ("kbd", "mabs", "mrel")

    def __init__(self, kbd_path: str = HIDG_KBD, mouse_path: str = HIDG_MOUSE,
                 mouse_rel_path: str = HIDG_MOUSE_REL):
        self._paths = {"kbd": kbd_path, "mabs": mouse_path, "mrel": mouse_rel_path}
        self._fds = {"kbd": None, "mabs": None, "mrel": None}
        self._lock = threading.Lock()
        # keyboard state
        self._mods = 0
        self._keys: list[int] = []  # up to 6 pressed usage ids, in order
        # mouse state
        self._btn = 0               # desired button bits (what the operator is holding right now)
        # Last button bits actually WRITTEN to each pointer device. The two mice are independent devices at
        # the host, each with its own button state -- a release must reach every device that still shows a
        # pressed bit, not just the currently-active one, or the target is left with a stuck button.
        self._btn_sent = {"mabs": 0, "mrel": 0}
        # Absolute position is UNKNOWN until the first move_abs: sending an absolute report before then
        # would warp the target cursor to a made-up position (e.g. a blur-triggered release_all right after
        # a service restart must never yank the cursor to the top-left corner).
        self._x: int | None = None
        self._y: int | None = None
        # Which pointer device input is currently flowing through: absolute (hidg1) or relative (hidg2) --
        # two separate USB mice as far as the target is concerned. A click/scroll must ride the SAME device
        # as the last move, or its button lands on the other mouse (not under the visible cursor). Default
        # absolute to match the UI's default desktop mode.
        self._mmode = "abs"
        self._rel_warned = False    # one-shot log when relative input has to be emulated/dropped
        for which in self.DEVICES:
            self._open(which)
        if self._fds["kbd"] is None or self._fds["mabs"] is None:
            missing = ", ".join(self._paths[k] for k in ("kbd", "mabs") if self._fds[k] is None)
            # Not fatal: _write reopens lazily, and reopen() recovers after a gadget re-enumerate. Log it so a
            # silently-dead HID (no input reaching the target) is diagnosable. An absent mrel is not worth a
            # warning -- it is a config choice.
            print("[hid] warning: could not open %s at startup (will retry on first write)" % missing, flush=True)

    def _open(self, which: str):
        try:
            # Non-blocking so a target that stops polling the HID endpoint can never wedge a write() (which
            # would freeze the KVM -- see WRITE_TIMEOUT). _try_write bounds the wait with select().
            self._fds[which] = os.open(self._paths[which], os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            self._fds[which] = None

    def _try_write(self, fd: int, data: bytes):
        """Bounded non-blocking write of one HID report. Waits up to WRITE_TIMEOUT for the endpoint to
        accept it, then writes. Returns True if the report was written, None if it was dropped (the target
        isn't draining -- the fd is still fine), and False if the fd errored and should be reopened.
        Never blocks unbounded."""
        try:
            _, wready, _ = select.select([], [fd], [], WRITE_TIMEOUT)
            if not wready:
                return None                # target not accepting reports now -> drop this one, keep the fd
            os.write(fd, data)
            return True
        except BlockingIOError:
            return None                    # endpoint momentarily full -> drop, fd is fine
        except OSError:
            return False                   # real error (e.g. gadget rebuilt) -> caller reopens

    def _write(self, which: str, data: bytes):
        # Held only for the bounded write (<= WRITE_TIMEOUT), never indefinitely, so reopen() and the
        # (now lock-free) status reads are never starved. The single _hid_pool worker serializes writes.
        # Returns True (sent) / None (dropped) / False (unrecoverable) so a chunked caller can bail early.
        with self._lock:
            if self._fds[which] is None:
                self._open(which)
            fd = self._fds[which]
            if fd is None:
                return False
            r = self._try_write(fd, data)
            if r is False:
                # gadget may have been rebuilt; reopen once and retry
                try:
                    os.close(fd)
                except OSError:
                    pass
                self._fds[which] = None
                self._open(which)
                r = self._try_write(self._fds[which], data) if self._fds[which] is not None else False
            return r

    # ---- keyboard (8 bytes, NO report id) -> /dev/hidg0 ----
    def _kbd_report(self) -> bytes:
        keys = (self._keys + [0, 0, 0, 0, 0, 0])[:6]
        return bytes([self._mods, 0x00, *keys])

    def key(self, code: str, down: bool):
        if code in MODIFIERS:
            bit = MODIFIERS[code]
            self._mods = (self._mods | bit) if down else (self._mods & ~bit)
        elif code in KEYMAP:
            usage = KEYMAP[code]
            if down:
                if usage not in self._keys and len(self._keys) < 6:
                    self._keys.append(usage)
            else:
                if usage in self._keys:
                    self._keys.remove(usage)
        else:
            return  # unknown key
        self._write("kbd", self._kbd_report())

    # ---- mouse: absolute -> /dev/hidg1, relative -> /dev/hidg2 (two separate USB mice) ----
    def _abs_report(self, wheel: int = 0) -> bytes:
        x = max(0, min(ABS_MAX, self._x or 0))
        y = max(0, min(ABS_MAX, self._y or 0))
        return bytes([MOUSE_ABS_ID, self._btn, x & 0xFF, (x >> 8) & 0xFF,
                      y & 0xFF, (y >> 8) & 0xFF, wheel & 0xFF])

    def _rel_report(self, dx: int = 0, dy: int = 0, wheel: int = 0) -> bytes:
        return bytes([MOUSE_REL_ID, self._btn, dx & 0xFF, dy & 0xFF, wheel & 0xFF])

    # The _send_* wrappers keep _btn_sent (last button bits written per device) in step with every report,
    # since every report embeds the current button byte. They pass through _write's sent/dropped status.
    def _send_abs(self, wheel: int = 0):
        r = self._write("mabs", self._abs_report(wheel))
        self._btn_sent["mabs"] = self._btn
        return r

    def _send_rel(self, dx: int = 0, dy: int = 0, wheel: int = 0):
        r = self._write("mrel", self._rel_report(dx, dy, wheel))
        self._btn_sent["mrel"] = self._btn
        return r

    def _rel_available(self) -> bool:
        # The relative pointer is optional (mouse_rel toggle / endpoint budget). An open fd, or a node that
        # exists for the lazy open in _write, counts as available.
        return self._fds["mrel"] is not None or os.path.exists(self._paths["mrel"])

    def _write_buttons(self, wheel: int = 0):
        """Send a no-motion report (buttons + wheel only) on the pointer device the cursor is riding, so a
        click or scroll lands on the SAME mouse the cursor last moved on. The absolute report re-sends the
        current (x, y) -- same position, so it does not move -- while the relative one carries dx=dy=0.
        Never invents a position: with no absolute position known yet, buttons ride the relative device
        (which clicks at the target's current cursor without moving it); with neither usable, the report
        is dropped -- a click at a made-up position is worse than no click."""
        abs_ok = self._x is not None
        if self._mmode == "abs" and abs_ok:
            self._send_abs(wheel)
        elif self._rel_available():
            self._send_rel(wheel=wheel)
        elif abs_ok:
            self._send_abs(wheel)      # rel device absent: buttons ride the absolute mouse
        # else: drop (no known position and no relative device)

    def move_abs(self, xn: float, yn: float):
        """Absolute move (desktop): xn, yn normalized 0..1 across the target's primary display."""
        self._mmode = "abs"
        self._x = int(max(0.0, min(1.0, xn)) * ABS_MAX)
        self._y = int(max(0.0, min(1.0, yn)) * ABS_MAX)
        self._send_abs()

    def move_rel(self, dx: int, dy: int):
        """Relative move (touch/trackpad): nudges the cursor from its current position.

        The relative report carries one signed byte per axis (-127..127). Coalesced or high-DPI moves
        can exceed that, so split a large delta into <=127 steps rather than clamp it (which would lose
        motion and make a fast pointer trail). The cap allows a 50x-speed flick (UI speed sliders go to
        50x); one call still can't stall the writer, because the chunk loop ABORTS on the first dropped
        report -- a target that stopped draining gets no further chunks (they'd only queue stale motion).

        If the relative pointer is absent (mouse_rel off, or shed by the endpoint budget), relative input
        is EMULATED on the absolute mouse by advancing the tracked position -- touch input, Relative mode
        and keep-awake nudges keep working instead of dying silently. Starts from the screen centre if no
        absolute position is known yet.
        """
        dx = max(-16000, min(16000, int(dx)))
        dy = max(-16000, min(16000, int(dy)))
        if not self._rel_available():
            if not self._rel_warned:
                print("[hid] relative pointer absent (%s): emulating relative input on the absolute mouse"
                      % self._paths["mrel"], flush=True)
                self._rel_warned = True
            if self._x is None:
                self._x = self._y = ABS_MAX // 2
            self._mmode = "abs"        # keep buttons riding the mouse that is actually moving
            self._x = max(0, min(ABS_MAX, self._x + dx))
            self._y = max(0, min(ABS_MAX, self._y + dy))
            self._send_abs()
            return
        self._mmode = "rel"
        if not dx and not dy:
            self._send_rel()
            return
        while dx or dy:
            sx = max(-127, min(127, dx)); dx -= sx
            sy = max(-127, min(127, dy)); dy -= sy
            if self._send_rel(sx, sy) is not True:
                break                      # dropped/errored: stop the stroke, don't queue stale motion

    def _sync_stale_buttons(self):
        """Clear pressed bits still latched on the OTHER pointer device. Each USB mouse keeps its own
        button state at the host; a press that rode one device must not survive on the other after
        release. The relative clear (dx=dy=0) never moves the cursor; the absolute clear re-sends the
        last known position and only fires in the cross-mode-drag edge case (a button pressed in
        absolute mode and released in relative mode)."""
        if self._btn_sent["mrel"] & ~self._btn:
            self._send_rel()
        if self._btn_sent["mabs"] & ~self._btn and self._x is not None:
            self._send_abs()

    def button(self, index: int, down: bool):
        # Ride the ACTIVE pointer device (see _write_buttons) so the button lands under the visible cursor;
        # dx=dy=0 / unchanged x,y means the click never moves the cursor.
        # index follows JS MouseEvent.button: 0=left, 1=middle, 2=right.
        # HID button bits: 0x01=left(btn1), 0x02=right(btn2), 0x04=middle(btn3).
        bit = {0: 0x01, 1: 0x04, 2: 0x02}.get(index)
        if bit is None:
            return
        self._btn = (self._btn | bit) if down else (self._btn & ~bit)
        self._write_buttons()
        if not down:
            self._sync_stale_buttons()

    def wheel(self, dy: int):
        dy = max(-127, min(127, dy))
        self._write_buttons(wheel=dy)
        self._write_buttons(wheel=0)   # auto-release the wheel axis

    # ---- state (LOCK-FREE advisory reads; called from the event loop by the events poller) ----
    # These deliberately do NOT take self._lock: a single dict read is atomic under the GIL, and a
    # momentarily-stale bool is harmless for the status stream. Taking the lock here was the freeze bug --
    # a write wedged on a non-polling target held the lock, and the poller acquiring it on the loop thread
    # blocked the ENTIRE event loop. Bounded writes now cap the hold, but keep these lock-free regardless.
    def is_open(self) -> bool:
        # The KVM essentials: keyboard + absolute mouse. The relative mouse is an optional extra and never
        # gates "HID is up".
        fds = self._fds
        return fds["kbd"] is not None and fds["mabs"] is not None

    def devices_open(self) -> dict:
        fds = self._fds
        return {"keyboard": fds["kbd"] is not None, "mouse": fds["mabs"] is not None,
                "mouse_rel": fds["mrel"] is not None}

    # ---- safety ----
    def release_all(self):
        self._mods = 0
        self._keys = []
        self._btn = 0
        self._write("kbd", self._kbd_report())
        # Clear ONLY pointer devices that still show a pressed button (per _btn_sent). When nothing is
        # held this writes no pointer report at all -- a blur/disconnect/reset must never move the cursor
        # (and before the first absolute move there is no sane position to send anyway).
        self._sync_stale_buttons()

    def close(self):
        """Close the HID fds. Call this BEFORE a gadget re-enumerate (UDC unbind): the /dev/hidg* char
        devices are torn down by hidg_unbind during the unbind, and holding them open across that has
        triggered a kernel refcount underflow / use-after-free in usb_f_hid (hidg_unbind -> cdev_device_del)
        that leaves HID dead (opens then fail with ENXIO). reopen() re-acquires them afterward."""
        with self._lock:
            for which in self.DEVICES:
                if self._fds[which] is not None:
                    try:
                        os.close(self._fds[which])
                    except OSError:
                        pass
                    self._fds[which] = None

    def reopen(self, retries: int = 12, delay: float = 0.3) -> bool:
        """Re-open the HID devices after the USB gadget was re-enumerated (the /dev/hidg* nodes are
        recreated, so the old fds go stale). Clears any stuck keys/buttons, then retries the open while udev
        re-creates the nodes and applies their group permissions. Returns True once every device whose node
        EXISTS is open -- a node that is absent altogether (its function is disabled in config) is not
        counted as a failure.

        Without this, the keyboard/mouse only recover on the NEXT input event (the lazy reopen in _write),
        which leaves a window where input is silently dropped after a re-enumerate."""
        with self._lock:
            self._mods = 0
            self._keys = []
            self._btn = 0
            # The re-enumeration itself released every button at the host (the devices disconnected), so
            # the per-device latches restart clean. Keep _x/_y: the target cursor did not move.
            self._btn_sent = {"mabs": 0, "mrel": 0}
            self._rel_warned = False   # the rel node may (re)appear with the rebuilt gadget; re-log if not
            for which in self.DEVICES:
                if self._fds[which] is not None:
                    try:
                        os.close(self._fds[which])
                    except OSError:
                        pass
                    self._fds[which] = None
        for _ in range(retries):
            with self._lock:
                ok = True
                for which in self.DEVICES:
                    if self._fds[which] is None:
                        self._open(which)
                    if self._fds[which] is None and os.path.exists(self._paths[which]):
                        ok = False       # node exists but is not openable yet (udev still applying perms)
                if ok and (self._fds["kbd"] is not None or self._fds["mabs"] is not None):
                    return True          # everything present is open (and at least one essential exists)
            time.sleep(delay)        # wait (unlocked) for udev to recreate + chgrp the nodes
        return False
