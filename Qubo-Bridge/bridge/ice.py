"""Minimal RFC-3489 STUN used by Qubo's P2P connectivity check.

The camera signs Binding Requests with MESSAGE-INTEGRITY = HMAC-SHA1(aesKey).
We hold aesKey (we mint it in the p2pCall offer), so we answer its checks and
send our own Binding Request carrying USE-CANDIDATE — that nomination is what
makes the camera start streaming media.
"""
from __future__ import annotations

import hashlib
import hmac
import socket
import struct

BIND_REQ, BIND_RESP = 0x0001, 0x0101
ATTR_MAPPED, ATTR_MI, ATTR_USE_CAND = 0x0001, 0x0008, 0x0025


def is_stun(p: bytes) -> bool:
    if len(p) < 20:
        return False
    mt, ml = struct.unpack(">HH", p[:4])
    return mt in (BIND_REQ, BIND_RESP) and 20 + ml <= len(p) + 4


def msg_type(p: bytes) -> int:
    return struct.unpack(">H", p[:2])[0]


def _mi(msg_with_len: bytes, key: bytes) -> bytes:
    return struct.pack(">HH", ATTR_MI, 20) + hmac.new(key, msg_with_len, hashlib.sha1).digest()


def _mapped(ip: str, port: int) -> bytes:
    val = b"\x00\x01" + struct.pack(">H", port) + socket.inet_aton(ip)
    return struct.pack(">HH", ATTR_MAPPED, len(val)) + val


def build(mtype: int, txid: bytes, key: bytes, attrs_before_mi: bytes = b"") -> bytes:
    total = len(attrs_before_mi) + 24  # + MI attr (4 + 20)
    hdr = struct.pack(">HH", mtype, total) + txid
    return hdr + attrs_before_mi + _mi(hdr + attrs_before_mi, key)


def binding_response(request: bytes, src, key: bytes) -> bytes:
    return build(BIND_RESP, request[4:20], key, _mapped(src[0], src[1]))


def binding_request(txid: bytes, key: bytes, nominate: bool = True) -> bytes:
    extra = struct.pack(">HH", ATTR_USE_CAND, 0) if nominate else b""
    return build(BIND_REQ, txid, key, extra)


def verify(p: bytes, key: bytes) -> bool:
    i = p.rfind(struct.pack(">HH", ATTR_MI, 20))
    if i < 0 or i + 24 > len(p):
        return False
    return p[i + 4 : i + 24] == hmac.new(key, p[:i], hashlib.sha1).digest()
