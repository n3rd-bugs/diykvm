# DIY KVM-over-IP for Raspberry Pi

Control a target machine from your browser: see its screen, drive its keyboard and
mouse, hand it a virtual USB drive (including EFI boot media), and reach its serial
console — all over your LAN, from a Raspberry Pi.

The Pi presents itself to the target as a USB **keyboard + mouse + mass‑storage**
device, captures the target's **HDMI** through a USB capture dongle, and serves a
single‑page web UI plus an HTTP/WebSocket API.

---

## Features

- **Video** — low‑latency MJPEG of the target's screen (USB HDMI capture via µStreamer).
- **Keyboard & mouse** — full keyboard, switchable **absolute (point)** or **relative (trackpad, adjustable
  speed)** mouse, on‑screen keyboard, and touchscreen gestures (tap = click, drag = move, two‑finger
  scroll, double‑tap‑hold = drag, two‑finger = right button). Mouse motion is coalesced server‑side so it
  stays smooth even with high‑DPI / high‑polling mice. Release input capture with a **configurable
  shortcut** (default **Ctrl+Space**). The keyboard is a USB **boot keyboard**, so it also works in the
  target's **BIOS/UEFI** firmware and boot menus, not just the OS.
- **Virtual USB drive** — present boot media to the target: **upload a disk image or ISO** and attach it
  (ISOs as a read‑only CD‑ROM), or use the built‑in editable GPT/FAT32 **EFI System Partition** and manage
  its files from the browser. Attach/detach safely (the Pi and target never mount it at once).
- **Serial console** — talk to a serial port from the browser (line or raw‑key mode); **binary‑clean**
  end to end. Selectable **flow control** (RTS/CTS or XON/XOFF) and DTR; the port is **buffered** server‑side
  while detached (replayed once on reconnect, then dropped — no duplication), with multiple simultaneous
  viewers and explicit open/close. The **other end drives it over the API** — request **open** on demand, or
  **re‑enumerate** the USB to hand the target a fresh COM port (`POST /api/serial/reenumerate`). Use a
  USB/RS‑232 adapter, **or** have the Pi present its **own USB serial (CDC‑ACM) COM port** to the target over
  the same cable — no extra wiring.
- **Target power** — connect or cut power to two or more targets from the browser, each via a Raspberry
  Pi **GPIO** wired to a relay (latched on/off per target; **push‑pull or open‑drain** output).
- **External KVM switch** — drive a hardware KVM switch (display + USB) across two or more targets with
  one button per target, by pulsing a GPIO wired to each of its select buttons.
- **Configuration UI** — a **Config** page (and API) to edit settings in `/etc/kvm/kvm.conf` from the
  browser; every value is validated server‑side before it's written.
- **Keep‑awake** — optional periodic mouse nudges so the target's display doesn't sleep.
- **Latency tool** — a `/pingtest` page shows live browser↔Pi round‑trip time over the input WebSocket.
- **Screen OCR** — read the target's screen as structured JSON (`GET /api/ocr`): text **line‑by‑line** and
  grouped into **layout blocks**, each with a bounding box and confidence. Local (Tesseract), on‑demand —
  handy for scripting and agents.
- **Event stream** — a WebSocket (`/ws/events`) and snapshot (`GET /api/events/state`) that report device
  state in real time: **USB‑gadget state** (whether the target has the gadget *configured* vs *not attached* —
  so tooling sees the target lose/regain the keyboard, mouse, drive and COM port), HID open, serial
  ports/reconnects, and re‑enumerate actions. Fold the stream into a live state object — no polling.
- **Auth** — login (session cookie) for humans, API key for agents/automation.
- **Agent API** — a machine-readable endpoint list at `/api` (JSON, public for discovery) plus a human
  guide at `/api-guide`; drive everything programmatically.

## Hardware

- Raspberry Pi 4 / CM4 (a USB‑OTG‑capable port is required for the gadget; tested on a Pi 4B).
- A USB **HDMI capture** dongle (UVC, MJPEG — e.g. an MS2109/MS2130 class device).
- A **data** USB‑C → USB‑A cable from the Pi's USB‑C/OTG port to the target.
- HDMI cable from the target to the capture dongle.
- Network to the Pi (the example below uses a wired LAN).
- Power the Pi independently (PoE or a 5 V supply) so its USB‑C port is free for data.

## Install

Install the Debian package on Raspberry Pi OS (Bookworm, **64‑bit** — the OCR dependency Pillow has no
prebuilt 32‑bit/armhf wheel, so a 32‑bit OS would need build tools to compile it):

```sh
sudo apt install ./diykvm_0.7.3_all.deb
sudo reboot            # first install enables USB gadget mode (dtoverlay=dwc2,dr_mode=peripheral)
```

The installer prints a generated admin password (also change it any time):

```sh
sudo /opt/kvm/venv/bin/python /opt/kvm/app/setup_auth.py <user> <password>
sudo systemctl restart kvm-web
```

Then open **https://&lt;pi-ip&gt;:8000/** and sign in (HTTPS is on by default with a self-signed cert — accept
the browser warning once; see [HTTPS](#https-on-by-default)).

## Configuration

All options live in **`/etc/kvm/kvm.conf`** (preserved across upgrades). After editing:

```sh
sudo systemctl restart kvm-web ustreamer kvm-gadget
```

| Section | Key | Default | Meaning |
|---|---|---|---|
| `web` | `host`, `port` | `0.0.0.0`, `8000` | web UI / API bind |
| `web` | `tls`, `tls_cert`, `tls_key` | `true` | serve HTTPS (and `wss://` for input); on by default with a self‑signed cert |
| `web` | `allowed_origins` | _(blank)_ | extra browser origins permitted to connect |
| `video` | `device`, `resolution`, `fps` | `/dev/video0`, `1920x1080`, `30` | capture + stream |
| `video` | `devices` | _(blank)_ | multi‑PC: `Label:/dev/videoN, …` capture cards; UI shows a source picker (blank = just `device`) |
| `video` | `quality` | `80` | base JPEG quality 1‑100 (the bandwidth knob the capture encodes at) |
| `video` | `adaptive` | `true` | per‑viewer: on a slow link, auto re‑encode that viewer's `/ws/video` frames smaller to hold latency at ~1 frame |
| `usb` | `keyboard`, `mouse`, `mouse_rel`, `mass_storage` | `true` | which gadget functions to present (boot keyboard / absolute mouse / relative mouse / virtual drive); see the endpoint budget below |
| `usb` | `image_path`, `image_size` | `/opt/kvm/images/drive.img`, `1G` | virtual drive |
| `usb` | `usb_serial` | `false` | also present a USB serial (CDC‑ACM) COM port to the target (Pi side `/dev/ttyGS0`) |
| `usb` | `store_lun`, `store_size`, `store_inquiry` | `false`, `64M`, _(blank)_ | also present a 2nd mass‑storage LUN (scratch RW block store) the host can WRITE(10) to and you read back via `GET /api/store/region` — a generic one‑way host→Pi bulk data store (logs, captures, firmware dumps …). `store_inquiry` sets the SCSI INQUIRY so a specific host identifies the LUN |
| `serial` | `default_baud`, `default_flow`, `autostart`, `reconnect` | `115200`, `none`, _(blank)_, `true` | serial console: UI defaults (flow; raw/8N1/DTR/buffered); `autostart` auto‑opens listed ports from boot (blank = the other end opens on demand via `POST /api/serial/open`); `reconnect` auto‑reopens a dropped port. `POST /api/serial/reenumerate` re‑enumerates the gadget so the target gets a fresh COM port |
| `power` | `enabled`, `mode`, `targets`, … | `false`, `relay`, _(blank)_ | per‑target GPIO power — `relay` (latched connect/cut) or `button` (momentary front‑panel button, `hold_on_sec`/`hold_off_sec`); `targets = Label:BCMpin, …` |
| `kvmswitch` | `enabled`, `ports`, `pulse_ms` | `false`, _(blank)_, `300` | external hardware KVM switch — pulse a GPIO per select button; `ports = Label:BCMpin, …` |
| `ui` | `capture_exit` | `Ctrl+Space` | shortcut to release input capture (blank = on‑screen button only) |

**USB endpoint budget.** The Pi's **dwc2** controller has **7** endpoints. Each `[usb]` function costs some,
so the enabled set must fit — with everything on the total is 8, one over, and `kvm-gadget` **auto‑sheds the
relative mouse** (the one nicety) and logs a `WARN` (relative input then falls back onto the absolute mouse):

| Function | Endpoints |
|---|---|
| `keyboard` | 1 |
| `mouse` (absolute) | 1 |
| `mouse_rel` (relative) | 1 |
| `mass_storage` | 2 |
| `usb_serial` (CDC‑ACM) | 3 |
| **all on** | **8 → over budget, relative mouse dropped** |

### HTTPS (on by default)

The installer enables HTTPS (`tls = true`) and generates a **self-signed** cert at `/etc/kvm/tls/`, so you
reach the UI at **`https://<pi-ip>:8000/`** (accept the browser's self-signed warning once). HTTPS is the
default because browsers auto-upgrade `http://` to `https://` (HTTPS-First / a cached HSTS pin) and treat
`http` vs `https` as different sites for cookies (*schemeful same-site*) — over plain HTTP that breaks the
session so login bounces back to the sign-in page.

Drop in your own cert/key any time (replace `/etc/kvm/tls/{cert,key}.pem`, keep them readable by group
`diykvm`), then `sudo systemctl restart kvm-web`. To regenerate the self-signed cert by hand:

```sh
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout /etc/kvm/tls/key.pem -out /etc/kvm/tls/cert.pem \
  -subj "/CN=$(hostname)" -addext "subjectAltName=DNS:$(hostname),IP:$(hostname -I | awk '{print $1}')"
sudo chown root:diykvm /etc/kvm/tls/key.pem /etc/kvm/tls/cert.pem   # the web service (diykvm) must read them
sudo chmod 640 /etc/kvm/tls/key.pem
sudo systemctl restart kvm-web
```

To run plain HTTP instead (e.g. behind a TLS-terminating proxy), set `tls = false` in `/etc/kvm/kvm.conf`.

## EFI boot media

1. In the UI open **Files**, click **Detach & edit**, go to `EFI/BOOT`, and upload your
   `BOOTX64.EFI` (+ any payload).
2. Click **Attach to target**.
3. Reboot the target (use the KVM keyboard) and pick the USB device in its firmware boot menu.
   If the target has Secure Boot enabled, either use a signed bootloader or disable Secure Boot.

## Agents / API

Agents authenticate with the API key (exchange credentials once):

```sh
curl -sk -X POST https://<pi>:8000/api/login -d username=admin -d password=PASS  # -k: self-signed cert; -> {"api_key": "..."}
```

Then send `X-API-Key: <key>` (HTTP) or `?token=<key>` (WebSocket). Full reference, with copy‑paste
examples for input, screen capture, the virtual drive and serial, is at **`/api-guide`**.

## Security

LAN‑only by design. Authentication (session cookie, `SameSite=lax`, or API key) is required for
every endpoint; WebSockets and unsafe requests are **Origin‑checked** (blocks cross‑site hijacking);
logins are **rate‑limited**; the serial console only opens **enumerated** ports; the video stream is
proxied behind auth. The web app itself runs **unprivileged** (user `diykvm`); the only root it can reach is
a tiny, fixed set of `sudo` helper verbs with **no path arguments** — `kvm-msd-helper {attach|detach|eject|import|delete}`,
`kvm-conf-helper {write|restart-streamer}` and `kvm-gadget-helper {reenumerate|recover}` (nine verbs across
three helpers, enumerated in `sudoers.d/diykvm`). There is no internet
hardening — do not expose port 8000 directly; use a VPN (WireGuard/Tailscale) for remote access.

## Building from source

```sh
bash packaging/build-deb.sh        # produces diykvm_<version>_all.deb (Architecture: all)
```

CI (GitHub Actions, `.github/workflows/build-deb.yml`) lints the Python, shellchecks the maintainer
scripts, builds the `.deb`, uploads it as an artifact, and attaches it to releases on `v*` tags.

## How it works

```
TARGET ── HDMI ──▶ USB capture ──▶ /dev/video0 ──▶ ustreamer ──┐
       ── USB ───▶ Pi USB-C (dwc2 gadget): HID kbd+mouse, MSC   │  /opt/kvm (FastAPI/uvicorn)
                                                                ├─▶ web UI + API ──▶ operator browser
operator ◀── LAN ───────────────────────────────────────────────┘
```

- **`kvm-gadget`** builds a composite USB gadget via configfs, out of **three separate HID functions**:
  a **boot‑protocol keyboard** (`/dev/hidg0`, no Report ID, so it works in the target's BIOS/UEFI), an
  **absolute mouse** (`/dev/hidg1`) and an optional **relative mouse** (`/dev/hidg2`) — plus a mass‑storage
  function backed by the drive image, and, when `usb.usb_serial` is set, a **CDC‑ACM serial** interface the
  target sees as a COM port (the Pi side is `/dev/ttyGS0`). The two pointers are **separate interfaces on
  purpose**: packed as two collections in one interface, Linux targets (hid‑generic) merge them into one
  input device and silently **drop the second collection's buttons**, so relative‑mode clicks never reached
  Linux — as their own interfaces every OS binds two complete mice. Each HID function is **IN‑endpoint‑only**
  (`no_out_endpoint=1`) so the set fits the Pi's dwc2 endpoint budget alongside mass storage and the optional
  serial, and enumerates once instead of resetting every ~10&nbsp;s.
- **`ustreamer`** serves the capture as MJPEG on localhost.
- **`kvm-web`** (FastAPI) serves the UI, proxies the video behind auth, turns WebSocket input
  events into HID reports, manages the virtual drive (configfs + loop‑mounted ESP), bridges the
  serial console, controls target power over GPIO, and edits the config file through a
  validating root helper.

## License

See `LICENSE`.
