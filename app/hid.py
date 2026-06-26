"""HID report translation for the DIY KVM.

Two gadget HID devices:
  /dev/hidg0  BOOT keyboard  -> 8 bytes, NO report id:  [mods, 0x00, k1..k6]   (works in BIOS/UEFI)
  /dev/hidg1  mouse          -> report-id multiplexed:
                 abs = [0x02, btns, Xlo,Xhi, Ylo,Yhi, wheel]   (7 bytes, X/Y absolute 0..32767)
                 rel = [0x03, btns, dx, dy, wheel]             (5 bytes)
"""
import os
import threading

HIDG_KBD = "/dev/hidg0"     # boot keyboard, 8-byte reports, no report id
HIDG_MOUSE = "/dev/hidg1"   # mouse, report-id multiplexed
MOUSE_ABS_ID = 0x02         # desktop: absolute positioning
MOUSE_REL_ID = 0x03         # touch:   relative/trackpad
ABS_MAX = 32767

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
    def __init__(self, kbd_path: str = HIDG_KBD, mouse_path: str = HIDG_MOUSE):
        self._paths = {"kbd": kbd_path, "mouse": mouse_path}
        self._fds = {"kbd": None, "mouse": None}
        self._lock = threading.Lock()
        # keyboard state
        self._mods = 0
        self._keys: list[int] = []  # up to 6 pressed usage ids, in order
        # mouse state
        self._btn = 0
        self._x = 0
        self._y = 0
        self._open("kbd")
        self._open("mouse")

    def _open(self, which: str):
        try:
            self._fds[which] = os.open(self._paths[which], os.O_WRONLY)
        except OSError:
            self._fds[which] = None

    def _write(self, which: str, data: bytes):
        with self._lock:
            if self._fds[which] is None:
                self._open(which)
            if self._fds[which] is None:
                return
            try:
                os.write(self._fds[which], data)
            except OSError:
                # gadget may have been rebuilt; reopen once and retry
                try:
                    os.close(self._fds[which])
                except OSError:
                    pass
                self._fds[which] = None
                self._open(which)
                if self._fds[which] is not None:
                    try:
                        os.write(self._fds[which], data)
                    except OSError:
                        pass

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

    # ---- mouse (report-id prefixed) -> /dev/hidg1 ----
    def _abs_report(self, wheel: int = 0) -> bytes:
        x = max(0, min(ABS_MAX, self._x))
        y = max(0, min(ABS_MAX, self._y))
        return bytes([MOUSE_ABS_ID, self._btn, x & 0xFF, (x >> 8) & 0xFF,
                      y & 0xFF, (y >> 8) & 0xFF, wheel & 0xFF])

    def _rel_report(self, dx: int = 0, dy: int = 0, wheel: int = 0) -> bytes:
        return bytes([MOUSE_REL_ID, self._btn, dx & 0xFF, dy & 0xFF, wheel & 0xFF])

    def move_abs(self, xn: float, yn: float):
        """Absolute move (desktop): xn, yn normalized 0..1 across the target's primary display."""
        self._x = int(max(0.0, min(1.0, xn)) * ABS_MAX)
        self._y = int(max(0.0, min(1.0, yn)) * ABS_MAX)
        self._write("mouse", self._abs_report())

    def move_rel(self, dx: int, dy: int):
        """Relative move (touch/trackpad): nudges the cursor from its current position."""
        self._write("mouse", self._rel_report(max(-127, min(127, int(dx))),
                                               max(-127, min(127, int(dy)))))

    def button(self, index: int, down: bool):
        # Sent via the RELATIVE report (dx=dy=0) so a click never moves the cursor.
        # index follows JS MouseEvent.button: 0=left, 1=middle, 2=right.
        # HID button bits: 0x01=left(btn1), 0x02=right(btn2), 0x04=middle(btn3).
        bit = {0: 0x01, 1: 0x04, 2: 0x02}.get(index)
        if bit is None:
            return
        self._btn = (self._btn | bit) if down else (self._btn & ~bit)
        self._write("mouse", self._rel_report())

    def wheel(self, dy: int):
        dy = max(-127, min(127, dy))
        self._write("mouse", self._rel_report(wheel=dy))
        self._write("mouse", self._rel_report(wheel=0))   # auto-release the wheel axis

    # ---- safety ----
    def release_all(self):
        self._mods = 0
        self._keys = []
        self._btn = 0
        self._write("kbd", self._kbd_report())
        self._write("mouse", self._rel_report())
