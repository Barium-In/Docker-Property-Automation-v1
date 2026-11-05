"""Entrypoint: python -m bridge"""
from __future__ import annotations

import asyncio
import logging
import os

from .config import Config
from .service import run


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config()
    cfg.validate()
    logging.getLogger("qubo").info(
        "bridge up: advertising %s:%s, quality=%s, audio=%s -> %s",
        cfg.local_ip, cfg.local_port, cfg.quality, cfg.audio, cfg.rtsp_publish,
    )
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
