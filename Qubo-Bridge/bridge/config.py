"""Environment-driven configuration for the Qubo bridge."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass


def _auto_lan_ip() -> str:
    """Best-effort primary LAN IPv4 (the address the camera must reach us on)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # auth: a mounted qubopy session file is preferred; email/password is the
    # self-heal fallback (used on first run or when the refresh token lapses).
    session_file: str = os.getenv("QUBO_SESSION_FILE", "/data/qubopy_session.json")
    email: str = os.getenv("QUBO_EMAIL", "")
    password: str = os.getenv("QUBO_PASSWORD", "")

    # which camera + which registered mobile identity to call as (defaults: the
    # first camera on the account / the first mobile device).
    device: str = os.getenv("QUBO_DEVICE", "")          # uuid or name substring
    mobile_uuid: str = os.getenv("QUBO_MOBILE_UUID", "")
    quality: str = os.getenv("QUBO_QUALITY", "high")    # high=1080p, low=640x480

    # our advertised UDP endpoint (needs host networking so the camera can reach it)
    local_ip: str = os.getenv("QUBO_LOCAL_IP") or _auto_lan_ip()
    local_port: int = int(os.getenv("QUBO_LOCAL_PORT", "49700"))

    video_pt: int = int(os.getenv("QUBO_VIDEO_PT", "96"))
    audio_pt: int = int(os.getenv("QUBO_AUDIO_PT", "97"))
    audio: bool = _b("QUBO_AUDIO", False)

    # output: ffmpeg publishes here (mediamtx accepts the RTSP publish)
    rtsp_publish: str = os.getenv("RTSP_PUBLISH_URL", "rtsp://127.0.0.1:8554/qubo")
    ffmpeg: str = os.getenv("FFMPEG_BIN", "ffmpeg")

    # liveness: re-initiate the P2P session if no media for this long
    watchdog_secs: float = float(os.getenv("QUBO_WATCHDOG_SECS", "8"))
    reconnect_min: float = float(os.getenv("QUBO_RECONNECT_MIN", "2"))
    reconnect_max: float = float(os.getenv("QUBO_RECONNECT_MAX", "30"))

    def validate(self) -> None:
        if self.quality not in ("high", "low", "medium", "auto"):
            raise SystemExit(f"QUBO_QUALITY invalid: {self.quality}")
        has_session = os.path.exists(self.session_file)
        if not has_session and not (self.email and self.password):
            raise SystemExit(
                "No credentials: mount a qubopy session at QUBO_SESSION_FILE "
                f"({self.session_file}) or set QUBO_EMAIL + QUBO_PASSWORD."
            )
