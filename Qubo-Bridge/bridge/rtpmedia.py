"""Decrypt + depacketize the Qubo P2P media (reverse-engineered).

Per RTP packet (12-byte header + payload):
  iv  = aesIV with iv[0:12] ^= header[0:12], iv[12:16] ^= roc (little-endian)
  pt  = AES-128-GCM(aesKey, iv, aad=header).decrypt(payload[:-16])   # last 16 = tag
roc is a per-stream rollover counter: the sender adds 1 after sending seq 0xFFFF
(BaseElemStream::encryptRtpPacket in libP2pMedia.so).
Video (PT 96): H.264 in RFC-6184 (FU-A / single-NAL / STAP-A) → Annex-B.
Audio (PT 97): AAC-LC 16 kHz mono in RFC-3640 → raw ADTS frames.

Loss-aware: over Wi-Fi, UDP loses/reorders packets. The reorder buffer below
restores order and skips gaps. When a fragmented NAL hits a gap, the video
depacketizer keeps the slice up to the gap and discards the rest: the decoder
conceals the missing lower part of the picture. Discarding the whole NAL is not
an option, since an IDR spans about 100 packets and at a few percent loss almost
none would arrive whole, so the stream would never get a keyframe.
"""
from __future__ import annotations

import logging
import time

from Crypto.Cipher import AES

log = logging.getLogger("qubo.rtp")

SC = b"\x00\x00\x00\x01"


def decrypt_payload(pkt: bytes, aes_key: bytes, aes_iv: bytes, roc: int = 0) -> bytes:
    """Return the cleartext RTP payload. Raises ValueError if the GCM tag fails: the
    packet was sealed with another key (a stream still arriving from an earlier call)
    or a different roc. Without this check such packets decrypt to random bytes that
    reach ffmpeg as H.264."""
    hdr = pkt[:12]
    iv = bytes(hdr[i] ^ aes_iv[i] for i in range(12)) + bytes(
        aes_iv[12 + i] ^ ((roc >> (8 * i)) & 0xFF) for i in range(4))
    cipher = AES.new(aes_key, AES.MODE_GCM, nonce=iv)
    cipher.update(hdr)
    return cipher.decrypt_and_verify(pkt[12:-16], pkt[-16:])


class RtpDecryptor:
    """Tracks each stream's rollover counter. The tag proves which counter is right,
    so a guess only decides what gets tried first."""

    def __init__(self, aes_key: bytes, aes_iv: bytes):
        self._key = aes_key
        self._iv = aes_iv
        self._streams: dict[int, list] = {}  # pt -> [roc, highest seq seen]

    def decrypt(self, pkt: bytes) -> bytes:
        if len(pkt) < 28:  # 12-byte header + 16-byte tag
            raise ValueError("too short")
        pt = pkt[1] & 0x7F
        seq = int.from_bytes(pkt[2:4], "big")
        state = self._streams.setdefault(pt, [0, None])
        roc, high = state
        if high is not None and seq < 0x4000 and high >= 0xC000:
            order = (roc + 1, roc)           # just wrapped
        elif high is not None and seq >= 0xC000 and high < 0x4000:
            order = (roc - 1, roc)           # a late packet from before the wrap
        else:
            order = (roc, roc + 1, roc - 1)
        for candidate in order:
            if candidate < 0:
                continue
            try:
                payload = decrypt_payload(pkt, self._key, self._iv, candidate)
            except ValueError:
                continue
            if candidate > roc:
                log.info("RTP sequence rolled over on PT %d (counter now %d)", pt, candidate)
                state[0], state[1] = candidate, seq
            elif candidate == roc and (high is None or ((seq - high) & 0xFFFF) < 0x8000):
                state[1] = seq
            return payload
        raise ValueError("authentication failed")


class VideoDepacketizer:
    """RFC-6184 H.264 → Annex-B, loss-aware. feed(seq, payload): fragments are
    joined while their RTP sequence numbers are contiguous (prev+1). On a gap, a
    slice is emitted truncated at the gap; fragments after it are dropped until
    the next start, because they cannot be placed without what was lost."""

    SLICE_TYPES = (1, 5)

    def __init__(self, sink):
        self._sink = sink                   # callable(bytes)
        self._nal: bytearray | None = None  # FU-A assembly buffer (None = idle)
        self._nal_seq = 0                   # seq of the last fragment appended

    def _flush_partial(self) -> None:
        if self._nal is not None and (self._nal[0] & 0x1F) in self.SLICE_TYPES:
            self._sink(SC + bytes(self._nal))
        self._nal = None

    def feed(self, seq: int, payload: bytes) -> None:
        if not payload:
            return
        t = payload[0] & 0x1F
        if 1 <= t <= 23:  # single NAL unit (complete in one packet)
            self._flush_partial()
            self._sink(SC + payload)
        elif t == 28:  # FU-A fragment
            if len(payload) < 2:
                self._flush_partial()
                return
            start = payload[1] >> 7
            end = (payload[1] >> 6) & 1
            ntype = payload[1] & 0x1F
            if start:
                self._flush_partial()
                self._nal = bytearray(bytes([(payload[0] & 0xE0) | ntype]) + payload[2:])
                self._nal_seq = seq
            else:
                if self._nal is None:
                    return  # the start was lost; nothing to attach this to
                if seq != (self._nal_seq + 1) & 0xFFFF:
                    self._flush_partial()
                    return
                self._nal += payload[2:]
                self._nal_seq = seq
            if end and self._nal is not None:
                self._sink(SC + bytes(self._nal))
                self._nal = None
        elif t == 24:  # STAP-A (several whole NALs in one packet)
            self._flush_partial()
            i = 1
            while i + 2 <= len(payload):
                sz = int.from_bytes(payload[i : i + 2], "big")
                i += 2
                self._sink(SC + payload[i : i + sz])
                i += sz


class AudioDepacketizer:
    """RFC-3640 AAC-hbr → ADTS frames. AAC frames are single-packet, so loss just
    means a prior frame is gone; the current frame is still whole."""

    def __init__(self, sink):
        self._sink = sink

    def feed(self, seq: int, payload: bytes) -> None:
        if len(payload) < 4:
            return
        au_headers_bits = int.from_bytes(payload[0:2], "big")
        n = au_headers_bits // 16  # each AU-header is 16 bits (size13|idx3)
        self._sink(payload[2 + 2 * n :])


class RtpReorder:
    """Per-stream RTP sequence reorder buffer (a small jitter buffer).

    Emits payloads in sequence order via emit(payload, gap); gap=True when the
    buffer had to skip a lost packet right before this one. Waits a bounded time
    for a missing packet, then skips it so the stream never stalls. One instance
    per stream (video and audio have separate sequence spaces).
    """

    def __init__(self, emit, max_depth: int = 256, max_age: float = 0.3):
        self._emit = emit                    # callable(seq: int, payload: bytes)
        self._buf: dict[int, tuple[bytes, float]] = {}
        self._next: int | None = None
        self._max_depth = max_depth
        self._max_age = max_age
        self.lost = 0                        # packets skipped as lost (for the logs)
        self.received = 0

    @staticmethod
    def _ahead(a: int, b: int) -> bool:      # is seq a at/after seq b (16-bit wrap)
        return ((a - b) & 0xFFFF) < 0x8000

    def push(self, seq: int, payload: bytes) -> None:
        if self._next is None:
            self._next = seq
        if seq != self._next and not self._ahead(seq, self._next):
            return  # late/duplicate — already emitted or skipped
        self.received += 1
        self._buf[seq] = (payload, time.monotonic())
        self._drain()
        if len(self._buf) > self._max_depth:
            self._skip_gap()

    def flush_stale(self) -> None:
        """Call periodically: if the head packet is missing and buffered data has
        aged past max_age, skip the gap so we don't stall on loss."""
        if not self._buf or self._next in self._buf:
            return
        oldest = min(v[1] for v in self._buf.values())
        if time.monotonic() - oldest > self._max_age:
            self._skip_gap()

    def _drain(self) -> None:
        while self._next in self._buf:
            payload, _ = self._buf.pop(self._next)
            self._emit(self._next, payload)
            self._next = (self._next + 1) & 0xFFFF

    def _skip_gap(self) -> None:
        if not self._buf:
            return
        head = min(self._buf.keys(), key=lambda s: (s - self._next) & 0xFFFF)
        self.lost += (head - self._next) & 0xFFFF
        self._next = head
        self._drain()
