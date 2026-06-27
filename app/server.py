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
import configparser
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import httpx
import serial
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

HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------- config -----------------------------
CONF_PATH = os.environ.get("KVM_CONF", "/etc/kvm/kvm.conf")
CONF_HELPER = "/usr/local/sbin/kvm-conf-helper"
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
USTREAMER = "http://%s:%s" % (_conf("video", "ustreamer_host", "127.0.0.1"),
                              _conf("video", "ustreamer_port", "8080"))
MSD_IMAGE = _conf("usb", "image_path", "/opt/kvm/images/drive.img")
# Extra browser origins allowed to open WebSockets / send unsafe requests (comma-separated host[:port]).
EXTRA_ORIGINS = {o.strip() for o in _conf("web", "allowed_origins", "").split(",") if o.strip()}

cfg = auth.load_config()
app = FastAPI(title="DIY PiKVM")
app.add_middleware(SessionMiddleware, secret_key=cfg["secret_key"], max_age=86400,
                   same_site="strict", https_only=TLS)
hid = HIDController()
msd = MSD(image=MSD_IMAGE)
power = PowerController()
kvmswitch = KvmSwitch()
_serial_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="serial")


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


# Machine-readable API descriptor for agents (public, no secrets — aids discovery before auth).
API_INFO = {
    "name": "DIY PiKVM",
    "description": "KVM-over-IP: drive the target's keyboard & mouse, watch its screen, serve virtual "
                   "USB boot media, bridge a serial console, switch GPIO power and an external KVM "
                   "switch. LAN-only.",
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
                 "{t:'reset'} release everything."},
        {"method": "GET", "path": "/snapshot", "auth": True, "desc": "JPEG of the target screen now."},
        {"method": "GET", "path": "/stream", "auth": True, "desc": "Live MJPEG stream."},
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
        {"method": "GET", "path": "/api/serial/ports", "auth": True, "desc": "List serial ports and common baud rates."},
        {"method": "WS", "path": "/ws/serial", "auth": True, "query": "device, baud, token",
         "desc": "Serial console, binary-clean. Serial RX arrives as WebSocket BINARY frames (raw bytes); "
                 "to transmit, send BINARY frames (written verbatim) or TEXT frames (UTF-8). Status/info "
                 "lines (connected/closed) arrive as TEXT frames, so distinguish them by frame type."},
        {"method": "GET", "path": "/api/power/state", "auth": True, "desc": "Per-target power relay state: {targets:[{label, on}]}."},
        {"method": "POST", "path": "/api/power", "auth": True, "body": "{index, on}", "desc": "Connect (on=true) or cut (on=false) a target's power (latched relay)."},
        {"method": "GET", "path": "/api/kvmswitch/state", "auth": True, "desc": "External KVM-switch buttons: {ports:[{label}]}."},
        {"method": "POST", "path": "/api/kvmswitch", "auth": True, "body": "{index}", "desc": "Press a select button (switch display + USB to that target)."},
        {"method": "GET", "path": "/api/config", "auth": True, "desc": "Read settings (sections/fields with current values)."},
        {"method": "POST", "path": "/api/config", "auth": True, "body": "{section:{key:value}}", "desc": "Update settings (validated server-side)."},
        {"method": "POST", "path": "/api/config/restart-streamer", "auth": True, "desc": "Restart the video streamer to apply [video] changes."},
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


# ----------------------------- input (HID) -----------------------------
@app.websocket("/ws")
async def ws(sock: WebSocket):
    if not ws_authed(sock):
        await sock.close(code=1008)
        return
    await sock.accept()
    try:
        while True:
            msg = await sock.receive_json()
            try:
                t = msg.get("t")
                if t == "kd":
                    hid.key(str(msg["code"]), True)
                elif t == "ku":
                    hid.key(str(msg["code"]), False)
                elif t == "mm":
                    hid.move_abs(float(msg["x"]), float(msg["y"]))
                elif t == "mr":
                    hid.move_rel(int(msg["dx"]), int(msg["dy"]))
                elif t == "mb":
                    hid.button(int(msg["button"]), bool(msg["down"]))
                elif t == "mw":
                    hid.wheel(int(msg["dy"]))
                elif t == "reset":
                    hid.release_all()
            except (KeyError, ValueError, TypeError):
                continue                 # ignore one malformed message; keep the channel open
    except WebSocketDisconnect:
        hid.release_all()
    except Exception:
        hid.release_all()


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
    try:
        baud = int(sock.query_params.get("baud", "115200"))
    except ValueError:
        baud = 115200
    if baud not in COMMON_BAUDS:
        baud = 115200
    try:
        ser = serial.Serial(device, baudrate=baud, timeout=0.1)
    except Exception as e:
        await sock.send_text(f"\r\n[open failed: {e}]\r\n")
        await sock.close()
        return
    await sock.send_text(f"\r\n[connected {device} @ {baud} baud]\r\n")
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()

    # Binary-clean both ways: serial RX is sent as WebSocket BINARY frames (raw bytes, lossless), and
    # TX accepts BINARY frames (written verbatim) or TEXT frames (UTF-8 encoded — convenient for typing).
    # Status/info lines above/below are TEXT frames, so clients tell them apart from serial data by type.
    async def reader():
        while not stop.is_set():
            data = await loop.run_in_executor(_serial_pool, ser.read, 4096)
            if data and not stop.is_set():
                try:
                    await sock.send_bytes(data)
                except Exception:
                    break

    async def writer():
        while True:
            m = await sock.receive()
            if m.get("type") == "websocket.disconnect":
                stop.set()                  # also stop the reader so the serial port is released
                break
            data = m.get("bytes")
            if data is None:
                txt = m.get("text")
                if txt is None:
                    continue
                data = txt.encode("utf-8", "replace")
            await loop.run_in_executor(_serial_pool, ser.write, data)

    try:
        await asyncio.gather(reader(), writer())
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        stop.set()
        try:
            ser.close()
        except Exception:
            pass


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
    {"section": "usb", "title": "Virtual USB drive",
     "note": "Applied after: sudo systemctl restart kvm-gadget (briefly re-enumerates USB).",
     "fields": [
         {"key": "image_path", "label": "Drive image path", "type": "text", "default": "/opt/kvm/images/drive.img"},
         {"key": "image_size", "label": "Image size (e.g. 1G, 512M)", "type": "text", "default": "1G"},
     ]},
    {"section": "serial", "title": "Serial console",
     "note": "Default baud for the console UI.",
     "fields": [
         {"key": "default_baud", "label": "Default baud", "type": "number", "default": "115200"},
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
    kw = dict(host=WEB_HOST, port=WEB_PORT)
    if TLS and os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
        kw["ssl_certfile"], kw["ssl_keyfile"] = TLS_CERT, TLS_KEY
    uvicorn.run(app, **kw)
