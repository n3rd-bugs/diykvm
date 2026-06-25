"""Serial-port discovery for the DIY PiKVM serial console.

The actual byte-pumping happens in the /ws/serial WebSocket handler in server.py;
this module just enumerates the ports that are present.
"""
import glob
import os

try:
    from serial.tools import list_ports
except Exception:                       # pyserial not importable
    list_ports = None

COMMON_BAUDS = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]


def list_serial_ports() -> list:
    seen = {}
    if list_ports is not None:
        for p in list_ports.comports():
            seen[p.device] = {
                "device": p.device,
                "desc": (p.description or "").strip(),
                "hwid": (p.hwid or "").strip(),
            }
    # Fallback / also include stable by-id symlinks and raw nodes.
    for pat in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA*", "/dev/serial/by-id/*"):
        for dev in glob.glob(pat):
            real = os.path.realpath(dev)
            seen.setdefault(real, {"device": real, "desc": "", "hwid": ""})
            if dev != real:
                seen[real]["by_id"] = dev
    return sorted(seen.values(), key=lambda d: d["device"])
