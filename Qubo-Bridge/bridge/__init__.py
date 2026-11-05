"""Qubo HCM01B → RTSP local bridge.

Pulls the camera's encrypted P2P stream directly over the LAN (no cloud media
hop), decrypts it (AES-128-GCM with the session key we mint), depacketizes
RTP → H.264 (+ AAC), and feeds a local RTSP server for Home Assistant.

Protocol reverse-engineered in Barium-In/Projects internal/qubo-camera; see
that repo's notes/p2p-protocol.md for the full derivation.
"""

__all__ = ["main"]
