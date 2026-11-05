"""Long-running bridge: hold the P2P session, decrypt live, feed ffmpeg→RTSP."""
from __future__ import annotations

import asyncio
import logging
import queue
import socket
import subprocess
import threading
import time

from .config import Config
from .flv import FlvMuxer
from .ice import binding_request, binding_response, is_stun, msg_type, verify, BIND_REQ
from .rtpmedia import AudioDepacketizer, RtpDecryptor, RtpReorder, VideoDepacketizer
from .session import build_client, resolve_targets

log = logging.getLogger("qubo.service")


class FFmpeg:
    """Owns one ffmpeg child that publishes RTSP. Video and audio are muxed into a
    single FLV stream (see flv.py) and written to its stdin by a dedicated thread,
    so a slow or just-starting ffmpeg can never block the async packet loop."""

    # About two minutes of frames. A backlog this deep means ffmpeg has stopped
    # consuming, so it gets restarted instead of growing memory without limit.
    MAX_BACKLOG = 4096
    # FLV timestamps are 32-bit milliseconds; restart well before they wrap.
    MAX_RUN_SECS = 20 * 86400

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self._mux: FlvMuxer | None = None
        self._q: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        self._started_at = 0.0

    def _cmd(self) -> list[str]:
        c = self.cfg
        # -max_interleave_delta: the muxer holds packets until every track has one,
        # 10 s by default. mediamtx drops a publisher that is silent for 10 s, so a
        # mic outage would take the video down with it. 500 ms lets video carry on.
        return [c.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-analyzeduration", "2000000", "-f", "flv", "-i", "pipe:0",
                "-c", "copy", "-max_interleave_delta", "500000",
                "-f", "rtsp", "-rtsp_transport", "tcp", c.rtsp_publish]

    def start(self) -> None:
        log.info("starting ffmpeg -> %s%s", self.cfg.rtsp_publish,
                 " (+audio)" if self.cfg.audio else "")
        self.proc = subprocess.Popen(self._cmd(), stdin=subprocess.PIPE)
        self._mux = FlvMuxer(self.cfg.audio)
        self._q = queue.Queue()
        self._q.put(self._mux.header())
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._writer, args=(self.proc.stdin, self._q), daemon=True)
        self._thread.start()

    @staticmethod
    def _writer(stdin, q: queue.Queue) -> None:
        try:
            while (data := q.get()) is not None:
                stdin.write(data)
                stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass  # ffmpeg gone; the main loop restarts it
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    def write_video(self, data: bytes) -> None:
        """data: one Annex-B NAL unit (4-byte start code + NAL)."""
        if self._mux and self._q:
            out = self._mux.video(data[4:], time.monotonic())
            if out:
                self._q.put(out)

    def write_audio(self, data: bytes) -> None:
        """data: ADTS AAC."""
        if self.cfg.audio and self._mux and self._q:
            out = self._mux.audio(data, time.monotonic())
            if out:
                self._q.put(out)

    def alive(self) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            return False
        if self._q is not None and self._q.qsize() > self.MAX_BACKLOG:
            log.warning("ffmpeg stopped consuming (%d frames queued)", self._q.qsize())
            return False
        return time.monotonic() - self._started_at < self.MAX_RUN_SECS

    def ensure(self) -> None:
        if not self.alive():
            self.restart()

    def restart(self) -> None:
        self.stop()
        self.start()

    def stop(self) -> None:
        q, self._q, self._mux = self._q, None, None
        if q is not None:
            q.put(None)
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self.proc = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


OUR_TXID = b"\x24\x4a\x21\x12" + b"bridgeP2P001"  # 16 bytes


async def _one_session(cfg: Config, cam, mobile: str, ff: FFmpeg) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", cfg.local_port))
    sock.setblocking(False)
    loop = asyncio.get_event_loop()
    try:
        res = await cam.initiate_p2p_call(
            local_ip=cfg.local_ip, local_port=cfg.local_port,
            quality=cfg.quality, mobile_uuid=mobile,
        )
        key, aes_iv = res["aes_key"], res["aes_iv"]
        # Reorder per stream (video/audio have separate seq spaces) so FU-A
        # reassembly sees packets in sequence order — out-of-order UDP otherwise
        # yields corrupt H.264.
        rv = RtpReorder(VideoDepacketizer(ff.write_video).feed)
        ra = RtpReorder(AudioDepacketizer(ff.write_audio).feed) if cfg.audio else None
        # The camera can keep streaming an earlier call to this port for a while,
        # sealed with that call's key (there is no hang-up API). So nothing counts
        # until it authenticates under this session's key: not the address we
        # nominate (nominating the old call starves the new one), and not liveness.
        # Only video counts as liveness: audio alone must not keep a dead picture up.
        decryptor = RtpDecryptor(key, aes_iv)
        cam_addr = None
        last_media = last_req = last_report = last_stats = time.monotonic()
        got_media = False
        rejected = 0
        while True:
            now = time.monotonic()
            if rejected and now - last_report > 5:
                log.warning("ignored %d packets that failed authentication "
                            "(normal for a few seconds after a restart: the previous call)",
                            rejected)
                rejected = 0
                last_report = now
            if now - last_stats > 60:
                log.info("last 60s: %d video packets received, %d lost", rv.received, rv.lost)
                rv.received = rv.lost = 0
                last_stats = now
            if now - last_media > cfg.watchdog_secs:
                raise TimeoutError("no video within watchdog window")
            if cam_addr and now - last_req > 0.5:
                sock.sendto(binding_request(OUR_TXID, key), cam_addr)
                last_req = now
            rv.flush_stale()
            if ra:
                ra.flush_stale()
            if not ff.alive():
                log.warning("ffmpeg exited; restarting it (session stays up)")
                await loop.run_in_executor(None, ff.restart)  # off-loop: don't stall the reorder
                last_media = time.monotonic()
            try:
                data, addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), timeout=0.4)
            except asyncio.TimeoutError:
                continue
            if is_stun(data):
                if not verify(data, key):
                    rejected += 1
                    continue
                cam_addr = addr
                if msg_type(data) == BIND_REQ:
                    sock.sendto(binding_response(data, addr, key), addr)
                continue
            try:
                payload = decryptor.decrypt(data)
            except ValueError:  # wrong key or truncated packet
                rejected += 1
                continue
            pt = data[1] & 0x7F
            seq = int.from_bytes(data[2:4], "big")
            cam_addr = addr
            if pt == cfg.video_pt:
                last_media = now
                if not got_media:
                    got_media = True
                    log.info("media flowing (%s quality)", cfg.quality)
                rv.push(seq, payload)
            elif pt == cfg.audio_pt and ra:
                ra.push(seq, payload)
    finally:
        sock.close()


async def run(cfg: Config) -> None:
    ff = FFmpeg(cfg)
    backoff = cfg.reconnect_min
    try:
        while True:
            try:
                client = await build_client(cfg)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001  (auth/config: retry, don't crash)
                log.error("auth/connect failed: %s; retrying in %.0fs", err, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, cfg.reconnect_max)
                continue
            try:
                async with client:
                    cam, mobile = await resolve_targets(client, cfg)
                    ff.ensure()
                    backoff = cfg.reconnect_min
                    while True:
                        try:
                            await _one_session(cfg, cam, mobile, ff)
                            backoff = cfg.reconnect_min
                        except asyncio.CancelledError:
                            raise
                        except Exception as err:  # noqa: BLE001
                            log.warning("session ended: %s", err)
                        ff.ensure()
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, cfg.reconnect_max)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001  (lost cloud/client: reconnect)
                log.warning("client loop ended: %s; reconnecting in %.0fs", err, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, cfg.reconnect_max)
    finally:
        ff.stop()
