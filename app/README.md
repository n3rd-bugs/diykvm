# Web control plane (`/opt/kvm/app`)

FastAPI app that serves the KVM web UI and API, proxies the video behind auth, turns browser
input into USB‑HID reports, manages the virtual USB drive, and bridges the serial console.
For install/usage see the top‑level [README](../README.md); the agent API is at `/api-guide`.

## Modules
- `server.py` — FastAPI: pages, auth (session + API key, Origin‑checked, rate‑limited), video
  proxy (`/stream`, `/snapshot`), input WebSocket `/ws`, mass‑storage REST `/api/msd/*`, serial
  WebSocket `/ws/serial`. Reads `/etc/kvm/kvm.conf` (host/port/TLS, ustreamer URL, image path).
- `hid.py` — report‑ID‑multiplexed writes to `/dev/hidg0`: keyboard (`KeyboardEvent.code`→HID
  usage), absolute mouse (0..1 → 0..32767), relative mouse, clicks/wheel; `release_all()` safety.
- `msd.py` — virtual drive: attach/detach via configfs `lun.0/file`, loop‑mount the ESP for
  editing, file ops. All transitions are lock‑serialized so the Pi and target never mount it at once.
- `auth.py` — `config.json` (pbkdf2 password hash, API key, session secret); `setup_auth.py` resets it.
- `serialbridge.py` — serial‑port enumeration (the WS handler only opens enumerated ports).
- `static/` — `index.html` (single‑page UI), `login.html`, `api-guide.html`.

## Input WebSocket (`/ws`, JSON client → server)
Authenticate via the session cookie (browser) or `?token=<api_key>` (agent).

| msg | meaning |
|---|---|
| `{"t":"kd","code":"KeyA"}` / `{"t":"ku",...}` | key down / up (`KeyboardEvent.code`) |
| `{"t":"mm","x":0.5,"y":0.5}` | absolute move, normalized 0..1 over the target's primary display |
| `{"t":"mr","dx":10,"dy":-4}` | relative move (trackpad), −127..127 |
| `{"t":"mb","button":0,"down":true}` | button — 0=left, 1=middle, 2=right (JS `MouseEvent.button`) |
| `{"t":"mw","dy":1}` | wheel (±1) |
| `{"t":"reset"}` | release all keys/buttons |

## Notes
- The service runs as **root** (needs `/dev/hidg0`, configfs and `mount`/`losetup`). A udev rule
  (`hidg*` → `kvm` group) ships as groundwork for future non‑root operation.
- The absolute mouse targets the captured display's **primary**; use relative `mr` for multi‑monitor.
