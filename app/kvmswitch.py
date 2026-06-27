"""External KVM-switch control over GPIO.

Drive a hardware KVM switch (the kind that physically toggles the display + USB between two or more
target machines) by pulsing a GPIO line wired to each of its front-panel select buttons — one
momentary "press" per target. Same unprivileged GPIO path as power.py: udev gives /dev/gpiochip*
group `gpio` mode 0660, so the service (a member of `gpio`) drives the line via the gpiod CLI, no sudo.

Settings live in the [kvmswitch] section of the config file and are read fresh on each press, so
changes from the Config page take effect immediately. Buttons are listed as Label:BCMpin pairs:

    ports = PC1:5, PC2:6, Cycle:13
"""
import configparser
import os
import shutil
import subprocess
import threading

CONF = os.environ.get("KVM_CONF", "/etc/kvm/kvm.conf")


class SwitchError(Exception):
    pass


def _cp():
    c = configparser.ConfigParser()
    c.read(CONF)
    return c


def _get(c, key, default):
    return c.get("kvmswitch", key, fallback=default)


def _bool(c, key, default):
    return str(_get(c, key, default)).strip().lower() in ("1", "true", "yes", "on")


def _int(c, key, default):
    try:
        return int(str(_get(c, key, default)).strip())
    except ValueError:
        return int(default)


def _parse_ports(spec):
    """'PC1:5, PC2:6' -> [{'label':'PC1','pin':5}, ...]; entries without a numeric pin are skipped."""
    ports = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        label, _, pin = item.rpartition(":")
        label, pin = label.strip(), pin.strip()
        if label and pin.isdigit():
            ports.append({"label": label, "pin": int(pin)})
    return ports


class KvmSwitch:
    def __init__(self):
        self._lock = threading.Lock()      # serialize GPIO access

    def settings(self) -> dict:
        c = _cp()
        return {
            "enabled": _bool(c, "enabled", "false"),
            "chip": (_get(c, "chip", "gpiochip0").strip() or "gpiochip0"),
            "active_low": _bool(c, "active_low", "false"),
            "pulse_ms": max(1, _int(c, "pulse_ms", "300")),
            "ports": _parse_ports(_get(c, "ports", "")),
        }

    def status(self) -> dict:
        s = self.settings()
        return {
            "enabled": s["enabled"],
            "available": shutil.which("gpioset") is not None,
            "ports": [{"label": p["label"]} for p in s["ports"]],   # labels only; pins stay server-side
        }

    def press(self, index) -> dict:
        with self._lock:
            s = self.settings()
            if not s["enabled"]:
                raise SwitchError("KVM switch is disabled (enable it on the Config page)")
            try:
                port = s["ports"][int(index)]
            except (ValueError, TypeError, IndexError):
                raise SwitchError("no such switch button")
            level = 0 if s["active_low"] else 1            # physical level that "presses" the button
            argv = ["gpioset", "--mode=time", "--usec=%d" % (s["pulse_ms"] * 1000),
                    "--", s["chip"], "%d=%d" % (port["pin"], level)]
            try:
                r = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=s["pulse_ms"] / 1000.0 + 10)
            except FileNotFoundError:
                raise SwitchError("gpiod tools not installed (sudo apt install gpiod)")
            except subprocess.TimeoutExpired:
                raise SwitchError("gpio command timed out")
            if r.returncode != 0:
                raise SwitchError((r.stderr or "gpio command failed").strip())
        return {"ok": True, "switched": port["label"]}
