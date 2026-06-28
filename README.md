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
  end to end (raw bytes both directions) for agents/automation. Use a USB/RS‑232 adapter, **or** have the
  Pi present its **own USB serial (CDC‑ACM) COM port** to the target over the same cable — no extra wiring.
- **Target power** — connect or cut power to two or more targets from the browser, each via a Raspberry
  Pi **GPIO** wired to a relay (latched on/off per target; **push‑pull or open‑drain** output).
- **External KVM switch** — drive a hardware KVM switch (display + USB) across two or more targets with
  one button per target, by pulsing a GPIO wired to each of its select buttons.
- **Configuration UI** — a **Config** page (and API) to edit settings in `/etc/kvm/kvm.conf` from the
  browser; every value is validated server‑side before it's written.
- **Keep‑awake** — optional periodic mouse nudges so the target's display doesn't sleep.
- **Latency tool** — a `/pingtest` page shows live browser↔Pi round‑trip time over the input WebSocket.
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

Install the Debian package on Raspberry Pi OS (Bookworm):

```sh
sudo apt install ./diykvm_0.4.0_all.deb
sudo reboot            # first install enables USB gadget mode (dtoverlay=dwc2,dr_mode=peripheral)
```

The installer prints a generated admin password (also change it any time):

```sh
sudo /opt/kvm/venv/bin/python /opt/kvm/app/setup_auth.py <user> <password>
sudo systemctl restart kvm-web
```

Then open **http://&lt;pi-ip&gt;:8000/** and sign in.

## Configuration

All options live in **`/etc/kvm/kvm.conf`** (preserved across upgrades). After editing:

```sh
sudo systemctl restart kvm-web ustreamer kvm-gadget
```

| Section | Key | Default | Meaning |
|---|---|---|---|
| `web` | `host`, `port` | `0.0.0.0`, `8000` | web UI / API bind |
| `web` | `tls`, `tls_cert`, `tls_key` | `false` | serve HTTPS (and `wss://` for input) |
| `web` | `allowed_origins` | _(blank)_ | extra browser origins permitted to connect |
| `video` | `device`, `resolution`, `fps` | `/dev/video0`, `1920x1080`, `30` | capture + stream |
| `usb` | `image_path`, `image_size` | `/opt/kvm/images/drive.img`, `1G` | virtual drive |
| `usb` | `usb_serial` | `false` | also present a USB serial (CDC‑ACM) COM port to the target (Pi side `/dev/ttyGS0`) |
| `serial` | `default_baud` | `115200` | serial console default |
| `ui` | `capture_exit` | `Ctrl+Space` | shortcut to release input capture (blank = on‑screen button only) |

### Enabling HTTPS

```sh
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout /etc/kvm/tls/key.pem -out /etc/kvm/tls/cert.pem -subj "/CN=$(hostname)"
sudo sed -i 's/^tls = false/tls = true/' /etc/kvm/kvm.conf
sudo systemctl restart kvm-web
```

## EFI boot media

1. In the UI open **Files**, click **Detach & edit**, go to `EFI/BOOT`, and upload your
   `BOOTX64.EFI` (+ any payload).
2. Click **Attach to target**.
3. Reboot the target (use the KVM keyboard) and pick the USB device in its firmware boot menu.
   If the target has Secure Boot enabled, either use a signed bootloader or disable Secure Boot.

## Agents / API

Agents authenticate with the API key (exchange credentials once):

```sh
curl -s -X POST http://<pi>:8000/api/login -d username=admin -d password=PASS   # -> {"api_key": "..."}
```

Then send `X-API-Key: <key>` (HTTP) or `?token=<key>` (WebSocket). Full reference, with copy‑paste
examples for input, screen capture, the virtual drive and serial, is at **`/api-guide`**.

## Security

LAN‑only by design. Authentication (session cookie, `SameSite=strict`, or API key) is required for
every endpoint; WebSockets and unsafe requests are **Origin‑checked** (blocks cross‑site hijacking);
logins are **rate‑limited**; the serial console only opens **enumerated** ports; the video stream is
proxied behind auth. The web app itself runs **unprivileged** (user `diykvm`) — only the two
virtual‑drive transitions are privileged, through a no‑argument `sudo` helper. There is no internet
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

- **`kvm-gadget`** builds a composite USB gadget via configfs: a **boot‑protocol keyboard**
  interface (no Report ID, so it works in the target's BIOS/UEFI), a separate **mouse** interface
  (absolute + relative, multiplexed by report IDs), a mass‑storage function backed by the drive image,
  and — when `usb.usb_serial` is set — a **CDC‑ACM serial** interface the target sees as a COM port (the
  Pi side is `/dev/ttyGS0`). Each HID interface is IN‑endpoint‑only so they fit the Pi's dwc2 endpoint
  budget alongside mass storage and the optional serial.
- **`ustreamer`** serves the capture as MJPEG on localhost.
- **`kvm-web`** (FastAPI) serves the UI, proxies the video behind auth, turns WebSocket input
  events into HID reports, manages the virtual drive (configfs + loop‑mounted ESP), bridges the
  serial console, controls target power over GPIO, and edits the config file through a
  validating root helper.

## License

See `LICENSE`.
