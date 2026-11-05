"""qubopy auth + client bootstrap for headless/container use.

Prefers a mounted session file (its refresh token keeps access alive); falls
back to email/password login and then persists the session so restarts and
token rotation survive. A stable device id is kept on the data volume so the
account does not accumulate a new "logged-in device" on every restart.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from qubopy.auth import QuboAuth
from qubopy.client import QuboClient
from qubopy.devices import QuboCamera

from .config import Config

log = logging.getLogger("qubo.session")


def _data_dir(cfg: Config) -> Path:
    d = Path(cfg.session_file).parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stable_device_id(cfg: Config) -> str:
    f = _data_dir(cfg) / "qubopy_device_id"
    if f.exists():
        v = f.read_text(encoding="utf-8").strip()
        if v:
            return v
    v = str(uuid.uuid4())
    fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as fh:
        fh.write(v)
    return v


def save_session(cfg: Config, auth: QuboAuth) -> None:
    t = auth.tokens
    if not t:
        return
    payload = {
        "accessToken": t.access_token,
        "refreshToken": getattr(t, "refresh_token", None),
        "uuid": getattr(t, "user_uuid", ""),
    }
    try:
        fd = os.open(cfg.session_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except Exception as err:  # noqa: BLE001
        log.warning("could not persist session: %s", err)


async def build_client(cfg: Config) -> QuboClient:
    auth = QuboAuth(device_id=_stable_device_id(cfg))
    loaded = False
    if os.path.exists(cfg.session_file):
        try:
            s = json.loads(Path(cfg.session_file).read_text(encoding="utf-8"))
            if s.get("accessToken"):
                auth.set_tokens(
                    access_token=s["accessToken"],
                    refresh_token=s.get("refreshToken"),
                    user_uuid=s.get("uuid", ""),
                )
                loaded = True
                log.info("loaded session from %s", cfg.session_file)
        except Exception as err:  # noqa: BLE001
            log.warning("session file unreadable (%s); will try login", err)
    if not loaded:
        if not (cfg.email and cfg.password):
            await auth.close()
            raise RuntimeError("No usable session and no QUBO_EMAIL/QUBO_PASSWORD to log in.")
        log.info("logging in as %s", cfg.email)
        try:
            await auth.login_with_email(cfg.email, cfg.password)
        except Exception:
            await auth.close()  # don't leak the aiohttp session on failure
            raise
        save_session(cfg, auth)
    return QuboClient(auth)


def _is_camera(d) -> bool:
    return isinstance(d, QuboCamera)


async def resolve_targets(client: QuboClient, cfg: Config):
    """Return (camera, mobile_uuid). Auto-pick if not configured."""
    devices = await client.get_all_devices()
    # also refresh + persist tokens now that a call succeeded
    save_session(cfg, client.auth)

    cams = [d for d in devices if _is_camera(d)]
    if cfg.device:
        want = cfg.device.lower()
        cams = [c for c in cams if want in c.device_id.lower() or want in (c.name or "").lower()]
    if not cams:
        raise SystemExit(f"No camera matched QUBO_DEVICE={cfg.device!r}")
    cam = cams[0]

    mobile = cfg.mobile_uuid
    if not mobile:
        mobs = [d for d in devices if getattr(d.info, "device_type", "") == "mobile"]
        if not mobs:
            raise SystemExit("No registered mobile on the account for the P2P call identity.")
        mobile = mobs[0].device_id
    log.info("camera=%s (%s)  mobile=%s", cam.name, cam.model, mobile)
    return cam, mobile
