"""DIY PiKVM web control plane.

- Login (session cookie) or API key (agents).
- Single-page UI; WebSocket /ws -> HID gadget (keyboard /dev/hidg0, mouse /dev/hidg1).
- Video proxied from uStreamer (localhost) behind auth.
- Serial console bridge over /ws/serial (device allow-listed).
- Agent API guide at /api-guide.

Runtime options come from a config file (default /etc/kvm/kvm.conf); every value
falls back to a sane default, so the app also runs with no config file present.
"""
import io
import os
import time
import asyncio
import subprocess
import threading
import configparser
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import (FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File,
                     Query, HTTPException, Request, Depends, Form, Response, Body)
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

import auth
from hid import HIDController
from msd import MSD, MSDError
from power import PowerController, PowerError
from kvmswitch import KvmSwitch, SwitchError
from serialbridge import list_serial_ports, COMMON_BAUDS
from serialport import SerialManager, SerialError, FLOW_MODES
from events import EventBus
import ocr

HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------- config -----------------------------
CONF_PATH = os.environ.get("KVM_CONF", "/etc/kvm/kvm.conf")
CONF_HELPER = "/usr/local/sbin/kvm-conf-helper"
GADGET_HELPER = "/usr/local/sbin/kvm-gadget-helper"
_reenum_lock = threading.Lock()      # app-level single-flight for the gadget re-enumerate (it bounces all USB)
_cp = configparser.ConfigParser()
_cp.read(CONF_PATH)


def _conf(section, key, default):
    return _cp.get(section, key, fallback=default)


def _conf_bool(section, key, default):
    return str(_conf(section, key, default)).strip().lower() in ("1", "true", "yes", "on")


WEB_HOST = _conf("web", "host", "0.0.0.0")
WEB_PORT = int(_conf("web", "port", "8000"))
TLS = _conf_bool("web", "tls", "false")
TLS_CERT = _conf("web", "tls_cert", "/etc/kvm/tls/cert.pem")
TLS_KEY = _conf("web", "tls_key", "/etc/kvm/tls/key.pem")
# HTTPS is actually served only when TLS is on AND the cert+key exist (see __main__). Bind the cookie Secure
# flag to that, not to the config flag alone: tls=true with a missing/failed cert serves plain HTTP, and a
# Secure cookie over HTTP is dropped by the browser -> login bounces back forever.
TLS_ACTIVE = TLS and os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY)
USTREAMER = "http://%s:%s" % (_conf("video", "ustreamer_host", "127.0.0.1"),
                              _conf("video", "ustreamer_port", "8080"))
MSD_IMAGE = _conf("usb", "image_path", "/opt/kvm/images/drive.img")
# Extra browser origins allowed to open WebSockets / send unsafe requests (comma-separated host[:port]).
EXTRA_ORIGINS = {o.strip() for o in _conf("web", "allowed_origins", "").split(",") if o.strip()}

cfg = auth.load_config()
app = FastAPI(title="DIY PiKVM")
app.add_middleware(SessionMiddleware, secret_key=cfg["secret_key"], max_age=86400,
                   same_site="lax", https_only=TLS_ACTIVE)
hid = HIDController()
msd = MSD(image=MSD_IMAGE)
power = PowerController()
kvmswitch = KvmSwitch()
_serial_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="serial")
# Single worker so HID reports stay strictly ordered, and so the blocking os.write() to /dev/hidg*
# (which waits for the target to poll the USB endpoint) never runs on the async event loop.
_hid_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hid")
# One owner per serial device: drains continuously into a ring buffer and fans out to many WS clients.
SERIAL = SerialManager(_serial_pool)
_ocr_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ocr")   # OCR is CPU-heavy; keep it off-loop
EVENTS = EventBus()                  # state-change events fanned out to /ws/events subscribers


# ----------------------------- origin / auth -----------------------------
def _origin_ok(origin, host):
    """True if a browser Origin is acceptable for this request (anti-CSRF / anti-CSWSH)."""
    if not origin:
        return True                      # non-browser client (curl/agent); auth is enforced separately
    netloc = urlparse(origin).netloc
    return netloc == host or netloc in EXTRA_ORIGINS or urlparse(origin).hostname in EXTRA_ORIGINS


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not _origin_ok(request.headers.get("origin"), request.headers.get("host", "")):
            return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
    return await call_next(request)


def _key_from_headers(request: Request) -> str:
    a = request.headers.get("authorization", "")
    if a.lower().startswith("bearer "):
        return a[7:]
    return request.headers.get("x-api-key", "")


def require_auth(request: Request) -> bool:
    if request.session.get("user"):
        return True
    if auth.check_api_key(cfg, _key_from_headers(request)):
        return True
    raise HTTPException(status_code=401, detail="authentication required")


def ws_authed(sock: WebSocket) -> bool:
    # Origin check first (blocks cross-site WebSocket hijacking of a cookie session).
    if not _origin_ok(sock.headers.get("origin"), sock.headers.get("host", "")):
        return False
    if sock.session.get("user"):
        return True
    return auth.check_api_key(cfg, sock.query_params.get("token", ""))


# ----------------------------- login (rate-limited) -----------------------------
_FAILS = {}                              # ip -> [count, locked_until]
_MAX_FAILS = 5
_LOCK_BASE = 5                           # seconds, doubled each fail past the threshold (cap 300)


def _locked_for(ip):
    c, until = _FAILS.get(ip, (0, 0))
    return max(0, int(until - time.time()))


def _note_fail(ip):
    c, _ = _FAILS.get(ip, (0, 0))
    c += 1
    until = time.time() + min(300, _LOCK_BASE * (2 ** (c - _MAX_FAILS))) if c >= _MAX_FAILS else 0
    _FAILS[ip] = (c, until)


def _note_ok(ip):
    _FAILS.pop(ip, None)


def _do_login(request: Request, username, password):
    ip = request.client.host if request.client else "?"
    wait = _locked_for(ip)
    if wait:
        raise HTTPException(status_code=429, detail=f"too many attempts; retry in {wait}s")
    if username == cfg["username"] and auth.verify_password(cfg, password):
        _note_ok(ip)
        return True
    _note_fail(ip)
    time.sleep(0.5)                      # small constant delay on failure
    return False


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(HERE, "static", "login.html"))


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if _do_login(request, username, password):
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?e=1", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/api/login")
async def api_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if _do_login(request, username, password):
        return {"api_key": cfg["api_key"]}
    raise HTTPException(status_code=401, detail="invalid credentials")


# ----------------------------- pages -----------------------------
@app.get("/")
def index(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/api-guide")
def api_guide(_: bool = Depends(require_auth)):
    return FileResponse(os.path.join(HERE, "static", "api-guide.html"))


@app.get("/pingtest")
def pingtest_page(request: Request):
    # Diagnostic: live browser<->Pi round-trip latency over the same /ws transport the mouse uses.
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(os.path.join(HERE, "static", "pingtest.html"))


# Machine-readable API descriptor for agents (public, no secrets — aids discovery before auth).
API_INFO = {
    "name": "DIY PiKVM",
    "description": "KVM-over-IP for a target machine, drivable by humans and agents. Capabilities: "
                   "keyboard & mouse over USB HID -- a BIOS/UEFI-capable boot keyboard, and an absolute or "
                   "relative mouse whose motion is coalesced server-side so high-DPI/high-polling devices "
                   "stay smooth; the input WebSocket also echoes ping->pong for round-trip latency. "
                   "Screen as MJPEG (snapshot or live stream), proxied behind auth. Virtual USB boot media: "
                   "upload whole disk images/ISOs and attach them, or edit a built-in GPT/FAT32 EFI drive "
                   "from the browser. Serial console that is binary-clean, buffered (ring-buffer scrollback, "
                   "no RX lost while detached, replayed once on connect then dropped so reconnecting doesn't "
                   "duplicate; configured ports auto-drain from boot), multi-client, with "
                   "selectable flow control (none/RTS-CTS/"
                   "XON-XOFF), DTR control and explicit open/close lifecycle -- over a USB/RS-232 adapter "
                   "OR the Pi's own CDC-ACM COM port presented to the target. Target power via latched GPIO "
                   "relays and an external hardware KVM switch. A browser<->Pi latency meter. Settings are "
                   "read/written via a validated config API. Auth by session cookie (humans) or API key "
                   "(agents); WebSockets and unsafe requests are Origin-checked; LAN-only.",
    "human_guide": "/api-guide",
    "auth": {
        "type": "api_key",
        "obtain": "POST /api/login (form fields: username, password) -> {\"api_key\": \"...\"}",
        "send": "HTTP header 'X-API-Key: <key>' or 'Authorization: Bearer <key>'; "
                "for WebSockets append '?token=<key>' to the URL.",
    },
    "endpoints": [
        {"method": "POST", "path": "/api/login", "auth": False, "body": "form: username, password",
         "returns": "{api_key}", "desc": "Exchange credentials for an API key."},
        {"method": "WS", "path": "/ws", "auth": True, "query": "token",
         "desc": "Keyboard & mouse. Send JSON: {t:'kd'|'ku',code} key down/up (JS KeyboardEvent.code); "
                 "{t:'mm',x,y} absolute move (0..1); {t:'mr',dx,dy} relative move; "
                 "{t:'mb',button,down} button (0=left,1=middle,2=right); {t:'mw',dy} wheel; "
                 "{t:'reset'} release everything; {t:'ping',ts} -> server replies {t:'pong',ts} "
                 "(echoes ts) to measure round-trip latency over this socket."},
        {"method": "GET", "path": "/snapshot", "auth": True, "desc": "JPEG of the target screen now."},
        {"method": "GET", "path": "/stream", "auth": True, "desc": "Live MJPEG stream."},
        {"method": "GET", "path": "/api/ocr", "auth": True, "query": "lang, psm, min_conf, region (x,y,w,h), scale, words",
         "desc": "OCR the current screen -> {image:{w,h}, text, lines[], blocks[]}. Each line/block has text, a "
                 "bbox [x,y,w,h] in image pixels and a confidence; lines are in reading order and blocks group "
                 "them as laid out on screen (columns/panels stay separate). On-demand, local (Tesseract)."},
        {"method": "GET", "path": "/api/msd/status", "auth": True, "desc": "Virtual USB drive status."},
        {"method": "POST", "path": "/api/msd/detach", "auth": True, "desc": "Eject from target; mount the EFI drive on the Pi to edit files."},
        {"method": "POST", "path": "/api/msd/attach", "auth": True, "desc": "Hand the EFI drive back to the target."},
        {"method": "GET", "path": "/api/msd/ls", "auth": True, "query": "path", "desc": "List EFI drive directory (while editing)."},
        {"method": "POST", "path": "/api/msd/upload", "auth": True, "query": "path", "body": "multipart file", "desc": "Upload a file into the EFI drive."},
        {"method": "GET", "path": "/api/msd/download", "auth": True, "query": "path", "desc": "Download a file from the EFI drive."},
        {"method": "POST", "path": "/api/msd/mkdir", "auth": True, "query": "path", "desc": "Make a directory."},
        {"method": "DELETE", "path": "/api/msd/rm", "auth": True, "query": "path", "desc": "Delete a file/empty directory."},
        {"method": "GET", "path": "/api/msd/images", "auth": True, "desc": "List boot images (uploaded disk images / ISOs)."},
        {"method": "POST", "path": "/api/msd/images/upload", "auth": True, "body": "multipart file (.img/.iso/.bin/.raw)", "desc": "Upload a whole boot image / ISO."},
        {"method": "POST", "path": "/api/msd/images/attach", "auth": True, "query": "name", "desc": "Attach an image to the target (ISOs as a read-only CD-ROM)."},
        {"method": "POST", "path": "/api/msd/eject", "auth": True, "desc": "Remove the medium from the target."},
        {"method": "DELETE", "path": "/api/msd/images", "auth": True, "query": "name", "desc": "Delete a boot image."},
        {"method": "GET", "path": "/api/serial/ports", "auth": True, "desc": "List serial ports (including the gadget /dev/ttyGS0 when usb.usb_serial is on) and common baud rates."},
        {"method": "GET", "path": "/api/serial/status", "auth": True, "desc": "Open ports with settings, buffered byte count and client count; plus the supported flow_modes."},
        {"method": "POST", "path": "/api/serial/open", "auth": True, "body": "{device, baud, flow:none|rtscts|xonxoff, dtr, rts, reconnect, reopen}",
         "desc": "Open a port (raw, 8N1, echo off, DTR asserted) and keep it draining even with no client (bytes held as a backlog while detached). reconnect=true auto-reopens it if the target disconnects/re-enumerates. Idempotent."},
        {"method": "POST", "path": "/api/serial/close", "auth": True, "body": "{device}", "desc": "Close a serial port and stop buffering."},
        {"method": "POST", "path": "/api/serial/reenumerate", "auth": True,
         "desc": "Re-enumerate the USB gadget (unbind + re-bind its UDC) so the target re-detects the device and gets a fresh CDC-ACM COM port. Lets the other end request a fresh port on demand. Because USB enumerates per device, the keyboard, mouse and mass-storage interface also briefly re-appear (~1s); an open Pi-side serial port self-heals."},
        {"method": "WS", "path": "/ws/serial", "auth": True, "query": "device, baud, flow, dtr, rts, reconnect, token",
         "desc": "Serial console, binary-clean. Subscribes to the shared port (auto-opens it with the given "
                 "settings if needed), replays the backlog accumulated while detached (then dropped, so "
                 "reconnecting doesn't duplicate), then streams live. Serial RX arrives as WebSocket BINARY "
                 "frames (raw bytes); transmit BINARY frames (verbatim) or TEXT frames (UTF-8). Status lines "
                 "are TEXT frames. Disconnecting leaves the port open; release it with POST /api/serial/close."},
        {"method": "GET", "path": "/api/events/state", "auth": True,
         "desc": "Point-in-time state snapshot: {usb:{udc,bound,state}, hid:{keyboard,mouse}, serial:[...], msd, power, kvmswitch}."},
        {"method": "WS", "path": "/ws/events", "auth": True, "query": "replay (0-256), token",
         "desc": "Live state-event stream as JSON (one event per message). On connect: {type:'snapshot',...} with full state, then events {seq,ts,type,...}: 'usb' (gadget/target enumeration -- state 'configured'/'not attached', so tooling sees the target losing/regaining HID), 'hid' (keyboard/mouse device open), 'serial' (ports: open/clients/reconnects), 'reenumerate'. ?replay=N replays the last N events from before connecting."},
        {"method": "GET", "path": "/api/power/state", "auth": True, "desc": "Per-target power relay state: {targets:[{label, on}]}."},
        {"method": "POST", "path": "/api/power", "auth": True, "body": "{index, on}", "desc": "Connect (on=true) or cut (on=false) a target's power (latched relay)."},
        {"method": "GET", "path": "/api/kvmswitch/state", "auth": True, "desc": "External KVM-switch buttons: {ports:[{label}]}."},
        {"method": "POST", "path": "/api/kvmswitch", "auth": True, "body": "{index}", "desc": "Press a select button (switch display + USB to that target)."},
        {"method": "GET", "path": "/api/config", "auth": True, "desc": "Read settings (sections/fields with current values)."},
        {"method": "POST", "path": "/api/config", "auth": True, "body": "{section:{key:value}}", "desc": "Update settings (validated server-side)."},
        {"method": "POST", "path": "/api/config/restart-streamer", "auth": True, "desc": "Restart the video streamer to apply [video] changes."},
        {"method": "GET", "path": "/pingtest", "auth": True, "desc": "Browser page: live browser<->Pi WebSocket round-trip latency meter (uses the /ws ping/pong)."},
    ],
}


@app.get("/api")
def api_index():
    return API_INFO


@app.get("/config")
def config_page(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(os.path.join(HERE, "static", "config.html"))


@app.get("/healthz")
def healthz():
    return {"ok": True}                  # no internal state leaked to unauthenticated callers


# ----------------------------- video proxy (behind auth) -----------------------------
@app.get("/stream")
async def stream(_: bool = Depends(require_auth)):
    client = httpx.AsyncClient(timeout=None)
    try:
        r = await client.send(client.build_request("GET", f"{USTREAMER}/stream"), stream=True)
    except Exception:
        await client.aclose()            # don't leak the client if uStreamer is unreachable
        raise HTTPException(status_code=502, detail="stream source unavailable")

    async def gen():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return StreamingResponse(gen(), media_type=r.headers.get(
        "content-type", "multipart/x-mixed-replace;boundary=boundarydonotcross"))


@app.get("/snapshot")
async def snapshot(_: bool = Depends(require_auth)):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{USTREAMER}/snapshot")
    except Exception:
        raise HTTPException(status_code=502, detail="stream source unavailable")
    return Response(r.content, media_type=r.headers.get("content-type", "image/jpeg"))


@app.get("/api/ocr")
async def api_ocr(_: bool = Depends(require_auth),
                  lang: str = "eng", psm: int | None = None, min_conf: float = 0.0,
                  region: str | None = None, scale: float = 2.0, words: bool = False):
    """OCR the current screen -> JSON: lines in reading order + layout blocks, each with bbox & confidence."""
    if not ocr.available():
        raise HTTPException(status_code=503, detail="OCR unavailable (install tesseract-ocr + pytesseract + Pillow)")
    if not lang or len(lang) > 32 or not all(c.isalnum() or c in "_+" for c in lang):
        lang = "eng"
    ps = None
    if psm is not None:
        try:
            ps = max(0, min(13, int(psm)))
        except (TypeError, ValueError):
            ps = None
    try:
        mc = max(0.0, min(100.0, float(min_conf)))
    except (TypeError, ValueError):
        mc = 0.0
    try:
        sc = max(1.0, min(3.0, float(scale)))      # cap upscaling: scale=3 on 1080p is already ~18MP/frame
    except (TypeError, ValueError):
        sc = 2.0
    reg = None
    if region:
        try:
            parts = [int(p) for p in region.split(",")]
            if len(parts) == 4 and all(p >= 0 for p in parts) and parts[2] > 0 and parts[3] > 0:
                reg = tuple(parts)
        except (TypeError, ValueError):
            reg = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{USTREAMER}/snapshot")
    except Exception:
        raise HTTPException(status_code=502, detail="stream source unavailable")
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(status_code=502, detail="stream source returned no image")
    jpeg = r.content
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(_ocr_pool, lambda: ocr.ocr_image(
            jpeg, lang=lang, psm=ps, min_conf=mc, region=reg, scale=sc, words=bool(words)))
    except ocr.OCRUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        print("[ocr] failed: %r" % (e,), flush=True)      # log internals server-side, don't leak to caller
        raise HTTPException(status_code=500, detail="ocr failed")


# ----------------------------- input (HID) -----------------------------
@app.websocket("/ws")
async def ws(sock: WebSocket):
    if not ws_authed(sock):
        await sock.close(code=1008)
        return
    await sock.accept()
    loop = asyncio.get_event_loop()
    # Server-side mouse coalescing. The receive loop only records the LATEST intent (absolute position,
    # or summed relative delta) and never blocks on a HID write; a flusher pushes it to the gadget at a
    # steady rate that self-paces to the target's USB poll capacity. This decouples a fast / high-DPI
    # client from the HID write rate, so the pointer reflects where you are now instead of trailing a
    # growing backlog, and a burst can never stall the event loop (which would delay every connection).
    pend = {"abs": None, "dx": 0, "dy": 0}
    stop = asyncio.Event()

    async def flush_mouse():
        ax, dx, dy = pend["abs"], pend["dx"], pend["dy"]
        pend["abs"] = None; pend["dx"] = 0; pend["dy"] = 0
        if ax is not None:
            await loop.run_in_executor(_hid_pool, hid.move_abs, ax[0], ax[1])
        if dx or dy:
            await loop.run_in_executor(_hid_pool, hid.move_rel, dx, dy)

    async def flusher():
        try:
            while not stop.is_set():
                await asyncio.sleep(0.008)         # ~125 Hz target; self-paces to the USB write rate
                await flush_mouse()
        except asyncio.CancelledError:
            pass

    ftask = asyncio.create_task(flusher())
    try:
        while True:
            msg = await sock.receive_json()
            try:
                t = msg.get("t")
                if t == "mm":
                    pend["abs"] = (float(msg["x"]), float(msg["y"]))
                elif t == "mr":
                    pend["dx"] += int(msg["dx"]); pend["dy"] += int(msg["dy"])
                elif t == "kd":
                    await flush_mouse(); await loop.run_in_executor(_hid_pool, hid.key, str(msg["code"]), True)
                elif t == "ku":
                    await flush_mouse(); await loop.run_in_executor(_hid_pool, hid.key, str(msg["code"]), False)
                elif t == "mb":
                    await flush_mouse()           # flush pending moves first so a click lands in place
                    await loop.run_in_executor(_hid_pool, hid.button, int(msg["button"]), bool(msg["down"]))
                elif t == "mw":
                    await flush_mouse()           # flush pending moves first so the scroll lands in place
                    await loop.run_in_executor(_hid_pool, hid.wheel, int(msg["dy"]))
                elif t == "reset":
                    pend["abs"] = None; pend["dx"] = 0; pend["dy"] = 0
                    await loop.run_in_executor(_hid_pool, hid.release_all)
                elif t == "ping":
                    # Latency probe: echo the client's timestamp so it can measure RTT over this exact
                    # WebSocket (the same transport mouse/keyboard input uses).
                    await sock.send_json({"t": "pong", "ts": msg.get("ts")})
            except (KeyError, ValueError, TypeError):
                continue                 # ignore one malformed message; keep the channel open
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        stop.set()
        ftask.cancel()
        try:
            await ftask
        except Exception:
            pass
        try:
            await loop.run_in_executor(_hid_pool, hid.release_all)
        except Exception:
            pass


# ----------------------------- mass storage -----------------------------
def _msd(fn, *a, **k):
    try:
        return fn(*a, **k)
    except MSDError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/msd/status")
def msd_status(_: bool = Depends(require_auth)):
    return msd.status()


@app.post("/api/msd/detach")
def msd_detach(_: bool = Depends(require_auth)):
    return _msd(msd.detach)


@app.post("/api/msd/attach")
def msd_attach(_: bool = Depends(require_auth)):
    return _msd(msd.attach)


@app.get("/api/msd/ls")
def msd_ls(path: str = Query(""), _: bool = Depends(require_auth)):
    return _msd(msd.listdir, path)


@app.post("/api/msd/mkdir")
def msd_mkdir(path: str = Query(...), _: bool = Depends(require_auth)):
    _msd(msd.mkdir, path)
    return {"ok": True}


@app.delete("/api/msd/rm")
def msd_rm(path: str = Query(...), _: bool = Depends(require_auth)):
    _msd(msd.delete, path)
    return {"ok": True}


@app.get("/api/msd/download")
def msd_download(path: str = Query(...), _: bool = Depends(require_auth)):
    full = _msd(msd.resolve_for_download, path)
    return FileResponse(full, filename=os.path.basename(full))


@app.post("/api/msd/upload")
def msd_upload(path: str = Query(""), file: UploadFile = File(...), _: bool = Depends(require_auth)):
    _msd(msd.save_upload, path, file.filename, file.file)
    return {"ok": True, "name": file.filename}


# ---- image library: upload a whole disk image / ISO and hand it to the target ----
@app.get("/api/msd/images")
def msd_images(_: bool = Depends(require_auth)):
    return _msd(msd.list_images)


@app.post("/api/msd/images/upload")
def msd_image_upload(request: Request, file: UploadFile = File(...), _: bool = Depends(require_auth)):
    try:
        declared = max(0, int(request.headers.get("content-length", "0")))
    except ValueError:
        declared = 0
    return _msd(msd.save_image_upload, file.filename, file.file, declared)


@app.post("/api/msd/images/attach")
def msd_image_attach(name: str = Query(...), _: bool = Depends(require_auth)):
    return _msd(msd.attach_image, name)


@app.delete("/api/msd/images")
def msd_image_delete(name: str = Query(...), _: bool = Depends(require_auth)):
    return _msd(msd.delete_image, name)


@app.post("/api/msd/eject")
def msd_eject(_: bool = Depends(require_auth)):
    return _msd(msd.eject)


# ----------------------------- serial console -----------------------------
@app.get("/api/serial/ports")
def serial_ports(_: bool = Depends(require_auth)):
    return {"ports": list_serial_ports(), "bauds": COMMON_BAUDS}


def _valid_serial_device(device: str):
    """Only allow opening a port that is actually enumerated as a serial port."""
    if not device:
        return None
    want = os.path.realpath(device)
    for p in list_serial_ports():
        if os.path.realpath(p["device"]) == want:
            return want
    return None


def _flag(v, default=True):
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def _serial_params(qp):
    """Parse baud/flow/dtr/rts/reconnect from query params or a JSON body; fall back to configured defaults."""
    try:
        baud = int(qp.get("baud", _conf("serial", "default_baud", "115200")))
    except (TypeError, ValueError):
        baud = 115200
    if baud not in COMMON_BAUDS:
        baud = 115200
    flow = (qp.get("flow", _conf("serial", "default_flow", "none")) or "none").strip().lower()
    if flow not in FLOW_MODES:                       # accept the case-insensitive forms the conf-helper allows
        flow = "none"
    rc = qp.get("reconnect")
    reconnect = _conf_bool("serial", "reconnect", "true") if rc is None else _flag(rc, True)
    return baud, flow, _flag(qp.get("dtr"), True), _flag(qp.get("rts"), True), reconnect


@app.get("/api/serial/status")
def serial_status(_: bool = Depends(require_auth)):
    return {"ports": SERIAL.status(), "flow_modes": list(FLOW_MODES)}


@app.post("/api/serial/open")
async def serial_open(payload: dict = Body(default={}), _: bool = Depends(require_auth)):
    body = payload if isinstance(payload, dict) else {}
    device = _valid_serial_device(body.get("device", ""))
    if device is None:
        raise HTTPException(status_code=400, detail="unknown serial device")
    baud, flow, dtr, rts, rc = _serial_params(body)
    try:
        p = await SERIAL.open(device, baud, flow, dtr, rts, reconnect=rc, reopen=bool(body.get("reopen")))
    except SerialError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"device": device, "baud": p.baud, "flow": p.flow, "dtr": p.dtr, "rts": p.rts, "open": p.alive}


@app.post("/api/serial/close")
async def serial_close(payload: dict = Body(default={}), _: bool = Depends(require_auth)):
    body = payload if isinstance(payload, dict) else {}
    device = _valid_serial_device(body.get("device", ""))
    if device is None:
        raise HTTPException(status_code=400, detail="unknown serial device")
    return {"device": device, "closed": await SERIAL.close(device)}


@app.post("/api/serial/reenumerate")
def serial_reenumerate(_: bool = Depends(require_auth)):
    """Let the other end request a fresh COM port: re-enumerate the USB gadget (unbind + re-bind its UDC)
    so the target re-detects the device. Because USB enumerates per device, this also briefly re-creates
    the keyboard, mouse and mass-storage interface. A Pi-side serial port that is open self-heals if its
    [serial] reconnect is on. Sync route: FastAPI runs it in a worker thread, off the event loop.
    Single-flight (it bounces ALL USB): overlapping requests get 409, not a second, compounding bounce."""
    if not _reenum_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="a re-enumerate is already in progress")
    try:
        try:
            r = subprocess.run(["sudo", "-n", GADGET_HELPER, "reenumerate"],
                               capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=502, detail="reenumerate timed out")
        if r.returncode == 3:               # helper's own flock: another bounce is mid-flight
            raise HTTPException(status_code=409, detail="a re-enumerate is already in progress")
        if r.returncode != 0:
            raise HTTPException(status_code=502, detail=(r.stderr or r.stdout or "reenumerate failed").strip())
        # The gadget was re-enumerated -> /dev/hidg* were recreated; reopen them now so the keyboard/mouse
        # keep working instead of waiting for (and possibly dropping) the next input event.
        hid_ok = hid.reopen()
        if not hid_ok:
            print("[reenumerate] HID re-open incomplete; will recover on next input", flush=True)
        EVENTS.publish("reenumerate", ok=True, hid_reopened=hid_ok, detail=(r.stdout or "").strip())
        return {"reenumerated": True, "hid_reopened": hid_ok, "detail": (r.stdout or "").strip()}
    finally:
        _reenum_lock.release()


# ----------------------------- events (state stream) -----------------------------
_UDC_DIR = "/sys/class/udc"
_GADGET_UDC = "/sys/kernel/config/usb_gadget/kvm/UDC"
_events_poller_task = None


def _usb_state():
    """Gadget/USB state from the kernel: which UDC the gadget is bound to, and the controller's USB state
    -- 'configured' once the target has enumerated the gadget, 'not attached' when the target is gone or has
    no USB/HID driver (e.g. a dead OS), etc. This is the signal that exposes the target re-enumerating or
    losing the gadget (HID/serial go dead while it is not 'configured')."""
    try:
        with open(_GADGET_UDC) as f:
            udc = f.read().strip()
    except OSError:
        udc = ""
    state = "unbound"
    if udc:
        try:
            with open("%s/%s/state" % (_UDC_DIR, udc)) as f:
                state = f.read().strip()
        except OSError:
            state = "unknown"
    return {"udc": udc or None, "bound": bool(udc), "state": state}


def _hid_state():
    return hid.devices_open()


def _events_snapshot():
    """A full point-in-time state snapshot -- the same shape a client can build by folding the event stream."""
    snap = {"usb": _usb_state(), "hid": _hid_state(), "serial": SERIAL.status()}
    for key, fn in (("msd", msd.status), ("power", power.status), ("kvmswitch", kvmswitch.status)):
        try:
            snap[key] = fn()
        except Exception:
            pass
    return snap


def _serial_key(ports):
    # Diff serial on the stable fields, ignoring the ever-changing 'buffered' byte count (data flowing would
    # otherwise emit an event every poll). open/clients/reconnects/reconnecting/error DO signal real changes.
    return [{k: p.get(k) for k in ("device", "baud", "flow", "open", "clients",
                                   "reconnect", "reconnects", "reconnecting", "error")} for p in ports]


async def _events_poller():
    """Publish an event per domain that changed. Catches state that changes WITHOUT an API call -- the target
    enumerating/dropping the gadget (usb), HID re-opening, serial reconnects -- so /ws/events reflects
    reality, not just operator actions."""
    last = {}
    errs = 0
    loop = asyncio.get_event_loop()
    while True:
        try:
            usb = _usb_state()
            if usb != last.get("usb"):
                prev = (last.get("usb") or {}).get("state")
                EVENTS.publish("usb", **usb); last["usb"] = usb
                # When the target (re)configures the gadget (a fresh enumeration, e.g. it just booted an
                # OS/BIOS with HID), reopen HID so the keyboard/mouse come back immediately instead of
                # dropping the first keystroke. Only on the transition -- no churn if it stays unopenable.
                if usb.get("state") == "configured" and prev != "configured":
                    # Target (re)configured the gadget. Rebind BOTH directions to the live host so a single
                    # boot works (no re-enumeration needed): reopen HID if it isn't open, and reopen any open
                    # /dev/ttyGS* so the serial TX/write path binds (an open-before-configure leaves TX dead).
                    if not hid.is_open():
                        await loop.run_in_executor(_hid_pool, lambda: hid.reopen(retries=4, delay=0.2))
                    SERIAL.reopen_gadget_ports()
            h = _hid_state()
            if h != last.get("hid"):
                EVENTS.publish("hid", **h); last["hid"] = h
            ports = SERIAL.status()
            key = _serial_key(ports)
            if key != last.get("serial"):
                EVENTS.publish("serial", ports=ports); last["serial"] = key
            errs = 0
        except Exception as e:
            errs += 1
            if errs == 1 or errs % 120 == 0:     # log the first error + occasionally; don't flood a wedged poll
                print("[events] poller error: %r" % (e,), flush=True)
        await asyncio.sleep(0.5)


@app.get("/api/events/state")
def events_state(_: bool = Depends(require_auth)):
    """Point-in-time snapshot of all tracked state (USB/gadget, HID, serial, drive, power, switch)."""
    return _events_snapshot()


@app.websocket("/ws/events")
async def ws_events(sock: WebSocket):
    """Stream state-change events as JSON. On connect: a {type:'snapshot', ...} with full current state,
    then live events -- usb (gadget/target enumeration), hid (keyboard/mouse open), serial (ports,
    reconnects), reenumerate, and operator actions. ?replay=N replays up to N events from just before
    connecting. Auth via ?token=<API key> (or session cookie)."""
    if not ws_authed(sock):
        await sock.close(code=1008)
        return
    await sock.accept()
    try:
        replay = max(0, min(256, int(sock.query_params.get("replay", "0"))))
    except (TypeError, ValueError):
        replay = 0
    q, recent = EVENTS.subscribe(replay=replay)
    loop = asyncio.get_event_loop()

    async def send_loop():
        # Build the snapshot OFF the loop: power.status()/msd.status() fork subprocesses / hit the fs and
        # would otherwise freeze the whole event loop (and every other client + input) on each connect.
        snap = await loop.run_in_executor(None, _events_snapshot)
        await sock.send_json({"seq": EVENTS.seq, "ts": round(time.time(), 3), "type": "snapshot", **snap})
        for ev in recent:
            await sock.send_json(ev)
        while True:
            await sock.send_json(await q.get())

    async def recv_drain():            # clients aren't expected to send; this just detects a disconnect
        while True:
            await sock.receive_text()

    st = asyncio.create_task(send_loop())
    rt = asyncio.create_task(recv_drain())
    try:
        await asyncio.wait({st, rt}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (st, rt):
            t.cancel()
        for t in (st, rt):           # await the cancellations so exceptions aren't left unretrieved
            try:
                await t
            except Exception:
                pass
        EVENTS.unsubscribe(q)


@app.websocket("/ws/serial")
async def ws_serial(sock: WebSocket):
    if not ws_authed(sock):
        await sock.close(code=1008)
        return
    await sock.accept()
    device = _valid_serial_device(sock.query_params.get("device", ""))
    if device is None:
        await sock.send_text("\r\n[refused: unknown serial device]\r\n")
        await sock.close()
        return
    baud, flow, dtr, rts, rc = _serial_params(sock.query_params)
    # Subscribe to the shared port owner; open it with the requested settings if it isn't already. The
    # port keeps draining into its ring buffer after this client leaves -- it is released only via
    # POST /api/serial/close. This is what makes RX buffered, multi-client and free of the
    # two-readers-on-one-tty error.
    port = SERIAL.get(device)
    if port is None:
        try:
            port = await SERIAL.open(device, baud, flow, dtr, rts, reconnect=rc)
        except SerialError as e:
            await sock.send_text(f"\r\n[open failed: {e}]\r\n")
            await sock.close()
            return
    await sock.send_text(f"\r\n[connected {device} @ {port.baud} flow={port.flow} "
                         f"dtr={int(port.dtr)} rts={int(port.rts)}]\r\n")
    q, backlog = port.subscribe()
    if not port.alive:                 # closed in the race window between open/get and subscribe()
        port.unsubscribe(q)            # the drain's finally already fired; we'd never get the sentinel
        try:
            await sock.send_text("\r\n[port closed]\r\n")
        finally:
            await sock.close()
        return
    stop = asyncio.Event()

    # Send only the backlog that arrived while NO client was attached, then stream live. Bytes already
    # delivered to a live client are not retained, so reconnecting never replays what you've already seen.
    # Serial bytes are BINARY frames (lossless); status/info lines are TEXT frames.
    if backlog:
        try:
            await sock.send_bytes(backlog)
        except Exception:
            pass

    async def reader():           # port ring buffer -> this websocket
        while not stop.is_set():
            data = await q.get()
            if data is None:      # the port was closed underneath us
                try:
                    await sock.send_text("\r\n[port closed]\r\n")
                except Exception:
                    pass
                break
            try:
                await sock.send_bytes(data)
            except Exception:
                break
        stop.set()

    async def writer():           # this websocket -> port
        while not stop.is_set():
            m = await sock.receive()
            if m.get("type") == "websocket.disconnect":
                break
            data = m.get("bytes")
            if data is None:
                txt = m.get("text")
                if txt is None:
                    continue
                data = txt.encode("utf-8", "replace")
            try:
                await port.write(data)
            except Exception:
                try:
                    await sock.send_text("\r\n[write failed]\r\n")
                except Exception:
                    pass
        stop.set()

    rt = asyncio.create_task(reader())
    wt = asyncio.create_task(writer())
    try:
        await asyncio.wait({rt, wt}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        for t in (rt, wt):
            t.cancel()
        for t in (rt, wt):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        port.unsubscribe(q)       # leave the port OPEN + buffering for other clients / lifecycle control


# --- socat-style persistent reader: keep configured ports open & draining so RX is never dropped ---
_serial_autostart_task = None


def _autostart_devices():
    # Default OFF: the Pi does not auto-open any port. The other end requests it via POST /api/serial/open
    # (or the operator via the UI). List ports in [serial] autostart only to have the Pi grab them on boot.
    return [d.strip() for d in _conf("serial", "autostart", "").split(",") if d.strip()]


async def _serial_autostart():
    """Keep each configured port open and continuously drained into its ring buffer, independent of any
    client. On a CDC-ACM gadget RX only flows while the tty is open, so without this the target's output
    is dropped until someone connects. Also (re)opens a port that appears later (e.g. /dev/ttyGS0 once the
    gadget binds) or that dropped (USB re-enumerate)."""
    while True:
        for dev in _autostart_devices():
            try:
                if SERIAL.get(dev) is None:                 # not currently open/draining
                    rdev = _valid_serial_device(dev)        # exists + is an enumerated serial port?
                    if rdev:
                        baud, flow, dtr, rts, rc = _serial_params({})
                        await SERIAL.open(rdev, baud, flow, dtr, rts, reconnect=rc)
            except Exception:
                pass                                        # transient (e.g. opened by something else); retry next tick
        await asyncio.sleep(5)


@app.on_event("startup")
async def _serial_startup():
    global _serial_autostart_task, _events_poller_task
    EVENTS.bind(asyncio.get_event_loop())     # let publish() be safe from worker threads (sync routes)
    if _autostart_devices():
        _serial_autostart_task = asyncio.create_task(_serial_autostart())
    _events_poller_task = asyncio.create_task(_events_poller())


@app.on_event("shutdown")
async def _serial_shutdown():
    if _serial_autostart_task:
        _serial_autostart_task.cancel()
    if _events_poller_task:
        _events_poller_task.cancel()
    await SERIAL.close_all()


# ----------------------------- target power (GPIO) -----------------------------
@app.get("/api/power/state")
def power_state(_: bool = Depends(require_auth)):
    return power.status()


@app.post("/api/power")
def power_action(payload: dict = Body(default={}), _: bool = Depends(require_auth)):
    # body: {"index": N, "on": true|false} -> connect/cut that target's power (latched relay)
    p = payload or {}
    try:
        return power.set(p.get("index"), bool(p.get("on")))
    except PowerError as e:
        raise HTTPException(status_code=409, detail=str(e))


# ----------------------------- external KVM switch (GPIO) -----------------------------
@app.get("/api/kvmswitch/state")
def kvmswitch_state(_: bool = Depends(require_auth)):
    return kvmswitch.status()


@app.post("/api/kvmswitch")
def kvmswitch_press(payload: dict = Body(default={}), _: bool = Depends(require_auth)):
    # body: {"index": N} -> momentary press of the Nth configured select button
    try:
        return kvmswitch.press((payload or {}).get("index"))
    except SwitchError as e:
        raise HTTPException(status_code=409, detail=str(e))


# ----------------------------- configuration management -----------------------------
# The editable schema, also used to render the Config page. Only these sections/keys are exposed;
# the privileged kvm-conf-helper re-validates every value before writing /etc/kvm/kvm.conf.
CONFIG_FIELDS = [
    {"section": "web", "title": "Web server",
     "note": "Applied after: sudo systemctl restart kvm-web",
     "fields": [
         {"key": "host", "label": "Bind address", "type": "text", "default": "0.0.0.0"},
         {"key": "port", "label": "Port", "type": "number", "default": "8000"},
         {"key": "tls", "label": "Enable HTTPS (TLS)", "type": "bool", "default": "false"},
         {"key": "tls_cert", "label": "TLS certificate path", "type": "text", "default": "/etc/kvm/tls/cert.pem"},
         {"key": "tls_key", "label": "TLS key path", "type": "text", "default": "/etc/kvm/tls/key.pem"},
         {"key": "allowed_origins", "label": "Extra allowed origins (host[:port], comma-separated)",
          "type": "text", "default": ""},
     ]},
    {"section": "video", "title": "Video capture",
     "note": "Apply with the “Restart streamer” button.",
     "fields": [
         {"key": "device", "label": "Capture device", "type": "text", "default": "/dev/video0"},
         {"key": "resolution", "label": "Resolution (WxH)", "type": "text", "default": "1920x1080"},
         {"key": "fps", "label": "Frames per second", "type": "number", "default": "30"},
         {"key": "ustreamer_host", "label": "uStreamer host", "type": "text", "default": "127.0.0.1"},
         {"key": "ustreamer_port", "label": "uStreamer port", "type": "number", "default": "8080"},
     ]},
    {"section": "usb", "title": "Virtual USB drive & serial",
     "note": "Applied after: sudo systemctl restart kvm-gadget (briefly re-enumerates USB).",
     "fields": [
         {"key": "image_path", "label": "Drive image path", "type": "text", "default": "/opt/kvm/images/drive.img"},
         {"key": "image_size", "label": "Image size (e.g. 1G, 512M)", "type": "text", "default": "1G"},
         {"key": "usb_serial", "label": "Present a USB serial (COM) port to the target", "type": "bool", "default": "false"},
     ]},
    {"section": "serial", "title": "Serial console",
     "note": "Defaults for the console UI; the per-session controls override these. Ports open raw, 8N1, echo off, DTR asserted.",
     "fields": [
         {"key": "default_baud", "label": "Default baud", "type": "number", "default": "115200"},
         {"key": "default_flow", "label": "Default flow control (none / rtscts / xonxoff)", "type": "text", "default": "none"},
         {"key": "autostart", "label": "Auto-open & drain these ports from boot (comma-separated, e.g. /dev/ttyGS0); blank (default) leaves the gadget COM closed until the other end opens it via /api/serial/open", "type": "text", "default": ""},
         {"key": "reconnect", "label": "Auto-reopen a port if it disconnects (survive the target re-enumerating; no console churn)", "type": "bool", "default": "true"},
     ]},
    {"section": "power", "title": "Target power (GPIO relays)",
     "note": "Applied immediately. Each target's GPIO drives a relay that connects or cuts its power "
             "(latched on/off); list targets as Label:BCMpin pairs.",
     "fields": [
         {"key": "enabled", "label": "Enable power control", "type": "bool", "default": "false"},
         {"key": "active_low", "label": "Active-low relays (drive line low = power on)", "type": "bool", "default": "false"},
         {"key": "open_drain", "label": "Open-drain output (on = sink to ground, off = release/high-Z, never drives high)", "type": "bool", "default": "false"},
         {"key": "targets", "label": "Targets — Label:pin, comma-separated (e.g. PC1:5, PC2:6)",
          "type": "text", "default": ""},
     ]},
    {"section": "kvmswitch", "title": "External KVM switch (GPIO)",
     "note": "Applied immediately. Wire a GPIO to each select button of a hardware KVM switch that "
             "toggles display + USB between targets; list buttons as Label:BCMpin pairs.",
     "fields": [
         {"key": "enabled", "label": "Enable KVM switch", "type": "bool", "default": "false"},
         {"key": "chip", "label": "GPIO chip", "type": "text", "default": "gpiochip0"},
         {"key": "active_low", "label": "Active-low wiring", "type": "bool", "default": "false"},
         {"key": "pulse_ms", "label": "Button press (ms)", "type": "number", "default": "300"},
         {"key": "ports", "label": "Buttons — Label:pin, comma-separated (e.g. PC1:5, PC2:6)",
          "type": "text", "default": ""},
     ]},
    {"section": "ui", "title": "Web UI",
     "note": "Keyboard shortcut that releases input capture in the browser (returns control to your local machine).",
     "fields": [
         {"key": "capture_exit", "label": "Release-capture shortcut", "type": "keycombo", "default": "Ctrl+Space"},
     ]},
]


def _read_config_values():
    c = configparser.ConfigParser()
    c.read(CONF_PATH)                            # read fresh from disk (reflects edits)
    out = []
    for sec in CONFIG_FIELDS:
        fields = [dict(f, value=c.get(sec["section"], f["key"], fallback=f["default"]))
                  for f in sec["fields"]]
        out.append({"section": sec["section"], "title": sec["title"], "note": sec["note"], "fields": fields})
    return out


@app.get("/api/config")
def config_get(_: bool = Depends(require_auth)):
    return {"sections": _read_config_values()}


@app.post("/api/config")
def config_save(payload: dict = Body(default={}), _: bool = Depends(require_auth)):
    # Rebuild the config from the known schema only (every key written with the submitted value or
    # its default); the privileged helper re-validates each value before it touches the real file.
    data = payload if isinstance(payload, dict) else {}
    c = configparser.ConfigParser()
    for sec in CONFIG_FIELDS:
        name = sec["section"]
        c.add_section(name)
        incoming = data.get(name) if isinstance(data.get(name), dict) else {}
        for f in sec["fields"]:
            v = incoming.get(f["key"], f["default"])
            if isinstance(v, bool):
                v = "true" if v else "false"
            c.set(name, f["key"], str(v))
    buf = io.StringIO()
    c.write(buf)
    proc = subprocess.run(["sudo", "-n", CONF_HELPER, "write"],
                          input=buf.getvalue(), capture_output=True, text=True)
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=(proc.stderr or proc.stdout or "config rejected").strip())
    return {"ok": True}


@app.post("/api/config/restart-streamer")
def config_restart_streamer(_: bool = Depends(require_auth)):
    proc = subprocess.run(["sudo", "-n", CONF_HELPER, "restart-streamer"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=(proc.stderr or "restart failed").strip())
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    # Bound graceful shutdown so a restart doesn't hang waiting on an open WebSocket (input/serial); after
    # this many seconds uvicorn force-closes connections. Clients just reconnect.
    # access_log=False: WS endpoints take the API key as ?token=..., which uvicorn's access log would write
    # to journald in cleartext. Auth failures/HTTP errors still surface via app logging.
    kw = dict(host=WEB_HOST, port=WEB_PORT, timeout_graceful_shutdown=5, access_log=False)
    if TLS_ACTIVE:
        kw["ssl_certfile"], kw["ssl_keyfile"] = TLS_CERT, TLS_KEY
    uvicorn.run(app, **kw)
