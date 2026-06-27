"""Latched per-target power control over GPIO.

Each target has ONE GPIO wired to a relay (or power switch) that CONNECTS or CUTS its power. Unlike a
momentary front-panel button, this is a held (latched) on/off level: 'on' connects power, 'off' cuts it.
The level is driven with pinctrl (register-level, so it latches and survives an app restart) via the
unprivileged `gpio` group (/dev/gpiomem); raspi-gpio is used if pinctrl is absent.

Output style is selectable: push-pull (default; drives high or low per active_low) or open_drain (on
sinks the line to ground, off releases it to high-Z and never drives high — provide an external
pull-up to set the off level; handy for 5 V relay/opto inputs and shared lines).

Settings live in the [power] section and are read fresh on each call, so Config-page changes take effect
immediately. Targets are listed as Label:BCMpin pairs:

    targets = PC1:5, PC2:6

Safety: power is only ever changed in response to an explicit, authenticated request; nothing is driven
on startup, so restarting the service never power-cycles a target. Cutting power is destructive (a hard
power loss) and is confirmed in the UI.
"""
import configparser
import os
import shutil
import subprocess
import threading

CONF = os.environ.get("KVM_CONF", "/etc/kvm/kvm.conf")


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

    def settings(self) -> dict:
        c = _cp()
        return {
            "enabled": _bool(c, "enabled", "false"),
            "active_low": _bool(c, "active_low", "false"),
            "open_drain": _bool(c, "open_drain", "false"),
            "targets": _parse_targets(_get(c, "targets", "")),
        }

    def _tool(self):
        return shutil.which("pinctrl") or shutil.which("raspi-gpio")

    def status(self) -> dict:
        s = self.settings()
        tool = self._tool()
        targets = []
        for t in s["targets"]:
            on = None
            if tool:
                try:
                    on = self._read(tool, t["pin"], s)
                except PowerError:
                    on = None
            targets.append({"label": t["label"], "on": on})   # on True/False, or None = not yet set
        return {"enabled": s["enabled"], "available": tool is not None, "targets": targets}

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
        with self._lock:
            s = self.settings()
            if not s["enabled"]:
                raise PowerError("power control is disabled (enable it on the Config page)")
            try:
                t = s["targets"][int(index)]
            except (ValueError, TypeError, IndexError):
                raise PowerError("no such target")
            tool = self._tool()
            if not tool:
                raise PowerError("pinctrl/raspi-gpio not installed (sudo apt install raspi-gpio)")
            pin = str(t["pin"])
            if s["open_drain"]:
                # open-drain: 'on' sinks the line to ground; 'off' releases it to high-Z (input, NO
                # internal pull) so an external pull-up sets the off level. The line is never driven
                # high -- safe for 5 V relay/opto inputs and shared lines.
                args = ["set", pin, "op", "dl"] if on else ["set", pin, "ip", "pn"]
            else:
                drive = "dh" if (bool(on) != s["active_low"]) else "dl"   # push-pull: on -> active level
                args = ["set", pin, "op", drive]
            self._run(tool, args)
        return self.status()
