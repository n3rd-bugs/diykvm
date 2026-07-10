# Web control plane (`/opt/kvm/app`)

FastAPI app that serves the KVM web UI and API, proxies the video behind auth, turns browser
input into USB‑HID reports, manages the virtual USB drive, and bridges the serial console.
For install/usage see the top‑level [README](../README.md); the agent API is at `/api-guide`.

## Modules
- `server.py` — FastAPI: pages, auth (session + API key, Origin‑checked, rate‑limited), video
  proxy (`/stream`, `/snapshot`), input WebSocket `/ws` (HID writes run off the event loop and mouse
  motion is **coalesced** server‑side, so a fast/high‑DPI mouse can't back up or stall the loop),
  mass‑storage REST `/api/msd/*`, binary‑clean serial WebSocket `/ws/serial`, target power `/api/power`,
  external KVM switch `/api/kvmswitch`, config read/write `/api/config` (+ the `/config` page), a public
  machine‑readable API descriptor `/api`, and a `/pingtest` latency meter (`/ws` `ping`/`pong`).
  Reads `/etc/kvm/kvm.conf` (host/port/TLS, ustreamer URL, image path, USB serial, capture shortcut).
- `power.py` — latched per-target power over GPIO: drives a relay line on/off (`pinctrl`) to connect or
  cut each target's power, and reads the line back for state.
- `kvmswitch.py` — external KVM switch over GPIO: pulses a configured line per target (`gpioset`) to
  press a hardware KVM switch's select buttons (display + USB).
- `hid.py` — three HID devices: an 8‑byte **no‑Report‑ID boot keyboard** on `/dev/hidg0`
  (`KeyboardEvent.code`→HID usage; BIOS/UEFI‑compatible), an **absolute mouse** on `/dev/hidg1`
  (0..1 → 0..32767) and an optional **relative mouse** on `/dev/hidg2` — separate USB interfaces
  because Linux targets drop the buttons of a second pointer collection merged into one interface.
  Clicks/wheel ride whichever pointer moved last; `release_all()` safety.
- `msd.py` — virtual drive: an image **library** under `/opt/kvm/images` (upload whole disk images /
  ISOs, list, select, delete) plus the built‑in editable EFI drive. Attaches the chosen image to the
  target via configfs `lun.0/file` (ISOs read‑only as a CD‑ROM), or loop‑mounts the EFI ESP for file
  editing. All transitions are lock‑serialized so the Pi and target never mount it at once.
- `ocr.py` — on‑demand **screen OCR**: snapshot → Tesseract (`pytesseract` + Pillow, with grayscale/upscale/
  autocontrast) → JSON of lines (reading order) and layout blocks, each with a bounding box + confidence
  (`GET /api/ocr`). Local, CPU‑only; deps optional at import (degrades to 503 if missing).
- `auth.py` — `config.json` (pbkdf2 password hash, API key, session secret); `setup_auth.py` resets it.
- `serialbridge.py` — serial‑port enumeration, including the optional **gadget serial** `/dev/ttyGS0`
  (the CDC‑ACM COM port the Pi presents to the target when `usb.usb_serial` is set); only enumerated ports
  may be opened.
- `serialport.py` — persistent serial **port manager**: one owner per device drains it **continuously** and
  fans bytes out to any number of WS clients (no "multiple access on port" error). While detached it keeps a
  bounded **backlog** that a connecting client receives once and is then dropped — and bytes delivered live
  are never retained — so reconnecting never duplicates. Opens **raw, 8N1, echo off, DTR asserted** with
  selectable **RTS/CTS or XON/XOFF** flow control; explicit **open/close** lifecycle decoupled from clients.
- `static/` — `index.html` (single‑page UI), `config.html` (settings page), `login.html`, `api-guide.html`,
  `pingtest.html` (browser↔Pi latency meter).

## Input WebSocket (`/ws`, JSON client → server)
Authenticate via the session cookie (browser) or `?token=<api_key>` (agent).

| msg | meaning |
|---|---|
| `{"t":"kd","code":"KeyA"}` / `{"t":"ku",...}` | key down / up (`KeyboardEvent.code`) |
| `{"t":"mm","x":0.5,"y":0.5}` | absolute move, normalized 0..1 over the target's primary display |
| `{"t":"mr","dx":10,"dy":-4}` | relative move (trackpad); large deltas are split across reports |
| `{"t":"mb","button":0,"down":true}` | button — 0=left, 1=middle, 2=right (JS `MouseEvent.button`) |
| `{"t":"mw","dy":1}` | wheel (±1) |
| `{"t":"reset"}` | release all keys/buttons |
| `{"t":"ping","ts":N}` | server replies `{"t":"pong","ts":N}` — round‑trip latency probe (see `/pingtest`) |

Mouse `mm`/`mr` are **coalesced server‑side**: the latest position / summed delta is flushed to the
gadget at ~125 Hz, so a fast/high‑DPI client can't outrun the USB write rate or stall the event loop.

## Notes
- The web service runs **unprivileged** (user `diykvm`): it writes `/dev/hidg*` via the `diykvm`
  group (udev rule), opens serial ports via `dialout`, drives the power GPIO via the `gpio` group
  (`/dev/gpiochip*`), and performs the privileged virtual‑drive and config transitions through
  `sudo /usr/local/sbin/kvm-msd-helper {attach|detach|eject|import|delete}` and
  `sudo /usr/local/sbin/kvm-conf-helper {write|restart-streamer}` (fixed verbs, no path arguments).
  The image library `/opt/kvm/images` is **root‑owned** — the app reads it, but uploads/deletes go
  through the helper (an upload is streamed in over the helper's stdin), so the directory the helper
  trusts can never be tampered with. The config helper validates every value against a strict allowlist
  before writing the root‑owned `/etc/kvm/kvm.conf`. The gadget and streamer units run as root. EFI
  file edits happen on the ESP, which the helper mounts owned by `diykvm`.
- The absolute mouse targets the captured display's **primary**; use relative `mr` for multi‑monitor.
