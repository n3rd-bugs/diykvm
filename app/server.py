"""DIY PiKVM web control plane.

- Login (session cookie) or API key (agents).
- Single-page UI; WebSocket /ws -> HID gadget (keyboard /dev/hidg0, mouse /dev/hidg1).
- Video proxied from uStreamer (localhost) behind auth.
- Serial console bridge over /ws/serial (device allow-listed).
- Agent API guide at /api-guide.

Runtime options come from a config file (default /etc/kvm/kvm.conf); every value
falls back to a sane default, so the app also runs with no config file present.
"""
import os
import time
import asyncio
import configparser
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import httpx
import serial
from fastapi import (FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File,
                     Query, HTTPException, Request, Depends, Form, Response)
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

import auth
from hid import HIDController
from msd import MSD, MSDError
from serialbridge import list_serial_ports, COMMON_BAUDS

HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------- config -----------------------------
_cp = configparser.ConfigParser()
_cp.read(os.environ.get("KVM_CONF", "/etc/kvm/kvm.conf"))


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

    async def reader():
        while not stop.is_set():
            data = await loop.run_in_executor(_serial_pool, ser.read, 4096)
            if data and not stop.is_set():
                try:
                    await sock.send_text(data.decode("utf-8", "replace"))
                except Exception:
                    break

    async def writer():
        while True:
            msg = await sock.receive_text()
            await loop.run_in_executor(_serial_pool, ser.write, msg.encode("utf-8", "replace"))

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


if __name__ == "__main__":
    import uvicorn
    kw = dict(host=WEB_HOST, port=WEB_PORT)
    if TLS and os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
        kw["ssl_certfile"], kw["ssl_keyfile"] = TLS_CERT, TLS_KEY
    uvicorn.run(app, **kw)
