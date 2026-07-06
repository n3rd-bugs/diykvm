"""Per-target power control over GPIO — two modes.

Each target has ONE GPIO. Which style of device it drives is selected by [power] mode:

* mode = relay  (default): a LATCHED relay/switch that CONNECTS or CUTS power. 'on' holds the line at
  its active level, 'off' at the inactive level; the level is driven register-level with pinctrl so it
  latches and survives an app restart, and can be read back — so status reports the real on/off state.

* mode = button: a MOMENTARY front-panel power button (a GPIO wired across the motherboard's power
  header, via a relay/opto). There is no readback, so power is a soft, ATX-style press-and-hold:
  'on' presses briefly (hold_on_sec, default 1s) to power up; 'off' holds long (hold_off_sec, default
  30s) to force a hard power-off. Because the button gives no feedback, the commanded state is TRACKED
  in software and assumed OFF at startup. The long hold runs in a background thread so the request
  returns immediately; status() reports whether a hold is in progress and its remaining seconds.

Both modes drive the line with pinctrl (or raspi-gpio) through the unprivileged `gpio` group
(/dev/gpiomem) — no sudo. Output style is selectable: push-pull (default; drives high or low per
active_low) or open_drain (active sinks the line to ground, inactive releases it to high-Z and never
drives high — provide an external pull-up; handy for 5 V relay/opto inputs and shared lines).

Settings live in the [power] section and are read fresh on each call, so Config-page changes take
effect immediately. Targets are listed as Label:BCMpin pairs:

    targets = PC1:5, PC2:6

Safety: power is only ever changed in response to an explicit, authenticated request; nothing is
driven on startup, so restarting the service never power-cycles a target. Cutting power (relay off) or
a long force-off hold (button) is destructive and is confirmed in the UI. Caveat for button mode: if
the service is hard-killed (SIGKILL) mid-hold the finally-release cannot run and the line stays
pressed until the process is restarted (which does not drive pins) or the pin is otherwise released.
"""
import configparser
import os
import shutil
import subprocess
import threading
import time

CONF = os.environ.get("KVM_CONF", "/etc/kvm/kvm.conf")

_HOLD_MIN, _HOLD_MAX = 1, 300          # clamp configured button holds to a sane range (seconds)


class PowerError(Exception):
    pass


def _cp():
    c = configparser.ConfigParser()
    c.read(CONF)
    return c


def _get(c, key, default):
    return c.get("power", key, fallback=default)


def _bool(c, key, default):
    return str(_get(c, key, default)).strip().lower() in ("1", "true", "yes", "on")


def _int(c, key, default):
    try:
        return int(str(_get(c, key, default)).strip())
    except ValueError:
        return int(default)


def _parse_targets(spec):
    """'PC1:5, PC2:6' -> [{'label':'PC1','pin':5}, ...]; entries without a numeric pin are skipped."""
    out = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        label, _, pin = item.rpartition(":")
        label, pin = label.strip(), pin.strip()
        if label and pin.isdigit():
            out.append({"label": label, "pin": int(pin)})
    return out


class PowerController:
    def __init__(self):
        self._lock = threading.Lock()
        # Button mode has no hardware readback (a momentary press leaves no readable state), so state is
        # tracked here, keyed by the target's BCM pin -- the physical actuator -- so it follows the
        # target across [power] targets-list edits/reorders. Empty at startup => every target reads OFF.
        self._state = {}          # pin -> last commanded power state (True/False)
        self._holding = set()     # pins with a press-and-hold currently in progress
        self._hold_until = {}     # pin -> monotonic deadline of the current hold
        self._fault = {}          # pin -> message if a release drive failed (line may be stuck asserted)

    def settings(self) -> dict:
        c = _cp()
        mode = (_get(c, "mode", "relay") or "relay").strip().lower()
        if mode not in ("relay", "button"):
            mode = "relay"
        return {
            "enabled": _bool(c, "enabled", "false"),
            "mode": mode,
            "active_low": _bool(c, "active_low", "false"),
            "open_drain": _bool(c, "open_drain", "false"),
            "hold_on_sec": min(_HOLD_MAX, max(_HOLD_MIN, _int(c, "hold_on_sec", "1"))),
            "hold_off_sec": min(_HOLD_MAX, max(_HOLD_MIN, _int(c, "hold_off_sec", "30"))),
            "targets": _parse_targets(_get(c, "targets", "")),
        }

    def _tool(self):
        return shutil.which("pinctrl") or shutil.which("raspi-gpio")

    def status(self) -> dict:
        s = self.settings()
        tool = self._tool()
        button = s["mode"] == "button"
        targets = []
        if button:
            now = time.monotonic()
            with self._lock:
                for t in s["targets"]:
                    pin = t["pin"]
                    holding = pin in self._holding
                    rem = max(0, int(round(self._hold_until.get(pin, 0) - now))) if holding else 0
                    targets.append({"label": t["label"], "on": bool(self._state.get(pin, False)),
                                    "holding": holding, "holding_secs": rem, "fault": self._fault.get(pin)})
        else:
            for t in s["targets"]:
                on = None
                if tool:
                    try:
                        on = self._read(tool, t["pin"], s)
                    except PowerError:
                        on = None
                targets.append({"label": t["label"], "on": on})   # on True/False, or None = not yet set
        out = {"enabled": s["enabled"], "available": tool is not None, "mode": s["mode"], "targets": targets}
        if button:
            out["hold_on_sec"] = s["hold_on_sec"]
            out["hold_off_sec"] = s["hold_off_sec"]
        return out

    # ---- low level ----
    def _run(self, tool, args):
        try:
            r = subprocess.run([tool] + args, capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            raise PowerError("pinctrl/raspi-gpio not installed")
        except subprocess.TimeoutExpired:
            raise PowerError("gpio command timed out")
        if r.returncode != 0:
            raise PowerError((r.stderr or r.stdout or "gpio command failed").strip())
        return r.stdout

    def _drive(self, tool, pin, active: bool, s):
        """Drive one pin to its ACTIVE (True) or INACTIVE (False) level, honoring active_low/open_drain.
        Relay mode holds this level; button mode drives active to press then inactive to release."""
        pin = str(pin)
        if s["open_drain"]:
            # open-drain: active sinks the line to ground; inactive releases it to high-Z (input, NO
            # internal pull) so an external pull-up sets the level. The line is never driven high --
            # safe for 5 V relay/opto inputs and shared lines.
            args = ["set", pin, "op", "dl"] if active else ["set", pin, "ip", "pn"]
        else:
            drive = "dh" if (bool(active) != s["active_low"]) else "dl"   # push-pull: active -> active level
            args = ["set", pin, "op", drive]
        self._run(tool, args)

    def _read(self, tool, pin, s):
        """Return True (on), False (off), or None if the pin's state is unknown."""
        out = self._run(tool, ["get", str(pin)])
        if "level=" in out:                       # raspi-gpio: "GPIO 5: level=1 fsel=1 func=OUTPUT"
            is_output = ("func=OUTPUT" in out) or ("func=OP" in out)
            level_high = "level=1" in out
        else:                                     # pinctrl: "5: op dh pd | hi // GPIO5 = output"
            toks = out.split()
            is_output = len(toks) >= 2 and toks[1] == "op"
            level_high = True if "hi" in toks else (False if "lo" in toks else None)
        if s["open_drain"]:
            # on = actively drained LOW; off = released (input / high-Z)
            if not is_output:
                return False
            return (not level_high) if level_high is not None else None
        # push-pull: only a driven output pin has a definite state
        if not is_output or level_high is None:
            return None
        return level_high != s["active_low"]      # active-low: a low level means 'on'

    def set(self, index, on: bool) -> dict:
        s = self.settings()
        if not s["enabled"]:
            raise PowerError("power control is disabled (enable it on the Config page)")
        try:
            idx = int(index)
            t = s["targets"][idx]
        except (ValueError, TypeError, IndexError):
            raise PowerError("no such target")
        tool = self._tool()
        if not tool:
            raise PowerError("pinctrl/raspi-gpio not installed (sudo apt install raspi-gpio)")
        if s["mode"] == "button":
            return self._press(t["pin"], bool(on), s, tool)
        with self._lock:                          # relay: hold a latched level
            self._drive(tool, t["pin"], bool(on), s)
        return self.status()

    # ---- button (momentary front-panel power button) ----
    def _press(self, pin, on, s, tool) -> dict:
        """Press-and-hold the power button: active for hold_on_sec (power up) or hold_off_sec (force
        off), then release. The initial press is driven SYNCHRONOUSLY so a failure to actuate the pin
        surfaces to the caller as a PowerError (409) -- parity with relay mode -- rather than being
        swallowed by the background thread; only the long hold + release run in the thread. A second
        press for the same pin while one is in flight is rejected."""
        hold = s["hold_on_sec"] if on else s["hold_off_sec"]
        with self._lock:
            if pin in self._holding:
                raise PowerError("a power-button hold is already in progress for this target")
            self._holding.add(pin)
            self._hold_until[pin] = time.monotonic() + hold
        try:
            with self._lock:
                self._drive(tool, pin, True, s)        # press the button (fails loudly, like relay)
        except Exception:
            with self._lock:                           # press never landed -> unregister, record nothing
                self._holding.discard(pin)
                self._hold_until.pop(pin, None)
            raise
        with self._lock:
            self._state[pin] = bool(on)                # press landed -> record the commanded state
            self._fault.pop(pin, None)
        threading.Thread(target=self._hold_worker, args=(pin, hold, s, tool), daemon=True).start()
        return self.status()

    def _hold_worker(self, pin, hold, s, tool):
        """Hold the (already-pressed) button for `hold` seconds, then release it -- off the request
        thread. The release is retried hard: a front-panel button line left stuck asserted keeps the
        power button 'pressed' forever, so on repeated failure we record a fault on the pin (surfaced in
        status) and log it, instead of silently reporting a clean state."""
        try:
            time.sleep(hold)
        finally:
            released, err = False, None
            for _ in range(5):
                try:
                    with self._lock:
                        self._drive(tool, pin, False, s)   # release the button
                    released = True
                    break
                except Exception as e:
                    err = e
                    time.sleep(0.5)
            with self._lock:
                self._holding.discard(pin)
                self._hold_until.pop(pin, None)
                if released:
                    self._fault.pop(pin, None)
                else:
                    self._fault[pin] = ("power-button release may have failed -- the line could be stuck "
                                        "asserted; retry the target")
            if not released:
                print("[power] failed to release power button on pin %s after retries: %r"
                      % (pin, err), flush=True)
