"""Authentication for the DIY PiKVM.

Two ways to authenticate:
  * Browser  -> POST /login (username + password) -> signed session cookie.
  * Agents   -> send the API key as `X-API-Key: <key>` or `Authorization: Bearer <key>`
               (HTTP), or `?token=<key>` (WebSocket).

Config lives in /opt/kvm/config.json (created on first run with a random password,
printed once to the service log). Use setup_auth.py to (re)set credentials.
"""
import os
import json
import hashlib
import secrets

CONFIG = os.environ.get("KVM_CONFIG", "/opt/kvm/config.json")
_ITER = 200_000


def _hash(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _ITER).hex()


def make_config(username: str, password: str, path: str = CONFIG) -> dict:
    salt = secrets.token_hex(16)
    cfg = {
        "username": username,
        "salt": salt,
        "password_hash": _hash(password, salt),
        "secret_key": secrets.token_hex(32),
        "api_key": secrets.token_hex(24),
    }
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(path, 0o600)
    return cfg


def load_config(path: str = CONFIG) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    pw = secrets.token_urlsafe(9)
    cfg = make_config("admin", pw, path)
    print(f"[auth] created {path}: username=admin password={pw}  (change with setup_auth.py)",
          flush=True)
    return cfg


def verify_password(cfg: dict, password: str) -> bool:
    try:
        return secrets.compare_digest(_hash(password, cfg["salt"]), cfg["password_hash"])
    except (KeyError, ValueError, TypeError):
        return False                     # fail closed on a malformed config


def check_api_key(cfg: dict, key: str) -> bool:
    try:
        return bool(key) and secrets.compare_digest(key, cfg["api_key"])
    except (KeyError, ValueError, TypeError):
        return False
