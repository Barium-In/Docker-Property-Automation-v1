"""Mux H.264 NAL units + ADTS AAC frames into one timestamped FLV byte stream.

ffmpeg gets a single input with timestamps we set. Feeding it two live inputs
(raw H.264 + a FIFO of AAC) with read-time wall-clock stamps does not work: its
scheduler holds back whichever input is ahead, and a raw pipe stamped at read
time is always ahead, so video gets read at a fraction of the camera's bitrate
and backs up without limit.

Clock: the video RTP timestamp is always 0, so video is stamped on arrival.
Audio is evenly spaced by frame duration and kept within AUDIO_SLACK_MS of the
same clock, because arrival times alone are too jittery to play back smoothly.
"""
from __future__ import annotations

import struct

NAL_SLICE, NAL_IDR, NAL_SEI, NAL_SPS, NAL_PPS = 1, 5, 6, 7, 8
TAG_AUDIO, TAG_VIDEO = 8, 9
AAC_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)
AUDIO_SLACK_MS = 300


def _tag(kind: int, ts_ms: int, body: bytes) -> bytes:
    ts_ms &= 0xFFFFFFFF
    head = (
        bytes([kind])
        + len(body).to_bytes(3, "big")
        + (ts_ms & 0xFFFFFF).to_bytes(3, "big")
        + bytes([ts_ms >> 24])
        + b"\x00\x00\x00"
    )
    return head + body + struct.pack(">I", len(head) + len(body))


def _avcc(sps: bytes, pps: bytes) -> bytes:
    return (
        bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1])
        + struct.pack(">H", len(sps)) + sps
        + b"\x01" + struct.pack(">H", len(pps)) + pps
    )


class FlvMuxer:
    """Each method returns the bytes to send to ffmpeg (b"" if nothing is ready).

    Nothing is emitted until the first IDR with a known SPS and PPS, so a fresh
    ffmpeg never has to probe frames it cannot decode. Audio starts with it.
    """

    def __init__(self, audio: bool):
        self._audio = audio
        self._t0: float | None = None
        self._video_last = -1
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._sent_config: tuple[bytes, bytes] | None = None
        self._prefix: list[bytes] = []   # SPS/PPS/SEI waiting for the next slice
        self._started = False
        self._asc: bytes | None = None
        self._audio_next: float | None = None
        self._audio_dropping = False

    def header(self) -> bytes:
        flags = 0x05 if self._audio else 0x01
        return b"FLV\x01" + bytes([flags]) + struct.pack(">I", 9) + b"\x00\x00\x00\x00"

    def _ms(self, now: float) -> float:
        if self._t0 is None:
            self._t0 = now
        return (now - self._t0) * 1000.0

    def video(self, nal: bytes, now: float) -> bytes:
        if not nal:
            return b""
        kind = nal[0] & 0x1F
        if kind == NAL_SPS and len(nal) >= 4:
            self._sps = nal
        elif kind == NAL_PPS:
            self._pps = nal
        if kind in (NAL_SPS, NAL_PPS, NAL_SEI):
            self._prefix.append(nal)
            return b""
        if kind not in (NAL_SLICE, NAL_IDR):
            return b""

        out = b""
        idr = kind == NAL_IDR
        if not self._started:
            if not (idr and self._sps and self._pps):
                self._prefix.clear()
                return b""
            self._started = True
        prefix, self._prefix = self._prefix, []

        ts = max(self._video_last + 1, int(self._ms(now)))
        self._video_last = ts
        if idr and self._sps and self._pps and self._sent_config != (self._sps, self._pps):
            self._sent_config = (self._sps, self._pps)
            out += _tag(TAG_VIDEO, ts, b"\x17\x00\x00\x00\x00" + _avcc(self._sps, self._pps))
        body = bytearray(b"\x17\x01\x00\x00\x00" if idr else b"\x27\x01\x00\x00\x00")
        for n in (*prefix, nal):
            body += struct.pack(">I", len(n)) + n
        return out + _tag(TAG_VIDEO, ts, bytes(body))

    def audio(self, adts: bytes, now: float) -> bytes:
        if not self._started:  # keep the two tracks starting together
            return b""
        out = bytearray()
        i = 0
        while i + 7 <= len(adts):
            if adts[i] != 0xFF or adts[i + 1] & 0xF6 != 0xF0:
                break  # not an ADTS sync word
            frame_len = ((adts[i + 3] & 3) << 11) | (adts[i + 4] << 3) | (adts[i + 5] >> 5)
            head_len = 7 if adts[i + 1] & 1 else 9
            if frame_len <= head_len or i + frame_len > len(adts):
                break
            out += self._audio_frame(adts[i : i + head_len], adts[i + head_len : i + frame_len], now)
            i += frame_len
        return bytes(out)

    def _audio_frame(self, head: bytes, raw: bytes, now: float) -> bytes:
        aot = (head[2] >> 6) + 1
        rate_idx = (head[2] >> 2) & 0xF
        channels = ((head[2] & 1) << 2) | (head[3] >> 6)
        if rate_idx >= len(AAC_RATES):
            return b""
        wall = self._ms(now)
        if self._audio_next is None:
            self._audio_next = wall
        ahead = self._audio_next - wall
        if ahead < -AUDIO_SLACK_MS:   # frames were lost or stopped: skip the gap
            self._audio_next = wall
        elif ahead > AUDIO_SLACK_MS:  # the camera's start-of-call burst: drop the excess
            self._audio_dropping = True
        if self._audio_dropping:
            if self._audio_next > wall:
                return b""
            self._audio_dropping = False

        ts = int(self._audio_next)
        self._audio_next += 1024 * 1000.0 / AAC_RATES[rate_idx]
        out = b""
        asc = struct.pack(">H", (aot << 11) | (rate_idx << 7) | (channels << 3))
        if asc != self._asc:
            self._asc = asc
            out += _tag(TAG_AUDIO, ts, b"\xAF\x00" + asc)
        return out + _tag(TAG_AUDIO, ts, b"\xAF\x01" + raw)
