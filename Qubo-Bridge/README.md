# Qubo Bridge — local RTSP from a Qubo HCM01B (no cloud)

Pulls a Qubo Wi‑Fi camera's **encrypted P2P stream directly over the LAN**,
decrypts it on the fly, and republishes it as a normal **`rtsp://` stream** for
Home Assistant. No cloud media hop, no ONVIF (the camera has none).

Two containers:

| service | what it does |
|---|---|
| `qubo-bridge` | P2P signalling + ICE + AES‑128‑GCM decrypt → feeds ffmpeg |
| `mediamtx` | RTSP/WebRTC server HA connects to: `rtsp://<vm-ip>:8554/qubo` |

> Protocol was reverse‑engineered from the camera + Android app; the full
> derivation lives in the private `Projects` repo (`internal/qubo-camera/
> notes/p2p-protocol.md`). This folder is just the runtime.

---

## Requirements

- An always‑on Linux host (your Ubuntu VM) with **Docker + Docker Compose v2**.
- The VM must be on the **same LAN/Wi‑Fi segment the camera can reach** (the
  camera sends P2P UDP to the VM). This is why both containers use
  **`network_mode: host`** — Docker's bridge NAT breaks the P2P return path.
- Outbound HTTPS to Qubo's cloud (for the P2P call signalling only).
- A Qubo account that owns the camera.

---

## Install

**1. Get the files onto the VM** — either pull the repo:

```bash
cd ~/Docker-Property-Automation-v1 && git pull
cd Qubo-Bridge
```

…or copy just this folder over with scp:

```bash
scp -r Qubo-Bridge user@your-vm:~/Docker-Property-Automation-v1/
ssh user@your-vm
cd ~/Docker-Property-Automation-v1/Qubo-Bridge
```

**2. Configure:** settings live in the repo's shared root `.env`, in the "Qubo Bridge"
section (copy any missing lines from the root `.env.example`):

```bash
nano ../.env           # set QUBO_RTSP_READ_USER + QUBO_RTSP_READ_PASS, and
                       # QUBO_EMAIL + QUBO_PASSWORD (or seed a session — see below)
```

**3. Build + start:**

```bash
docker compose --env-file ../.env up -d --build
docker compose --env-file ../.env logs -f qubo-bridge      # watch it connect
```

You want to see, within a few seconds:

```
qubo.service: starting ffmpeg -> rtsp://127.0.0.1:8554/qubo
qubo.service: media flowing (high quality)
```

Once a minute it also logs `last 60s: N video packets received, M lost`, which is
the real Wi‑Fi loss rate between the camera and the VM.

**4. Add it to Home Assistant** — Settings → Devices & Services → **Add
Integration → Generic Camera**, stream source (with the read credentials you set
in the root `.env`):

```
rtsp://<QUBO_RTSP_READ_USER>:<QUBO_RTSP_READ_PASS>@<vm-ip>:8554/qubo
```

(Leave still‑image URL blank; HA will snapshot from the stream. HA's built‑in
go2rtc gives you WebRTC/low‑latency automatically.)

The stream is **password-protected for reading**; only the bridge (publishing
from localhost) can publish, so nothing on the LAN can hijack the path.

---

## Seeding a session instead of storing the password

If you'd rather not keep `QUBO_PASSWORD` in the root `.env` (where every service
using that file can read it), generate a session once
and drop it in `./data` (its refresh token keeps it alive):

```bash
pip install qubopy==0.2.0
python -m qubopy login            # prompts for email + password
mkdir -p data && cp ~/.qubopy_session.json data/qubopy_session.json
```

Then leave `QUBO_EMAIL`/`QUBO_PASSWORD` blank. (If the refresh token ever
expires, the bridge falls back to email/password if you've set them — so
setting both is the most hands‑off option.)

---

## Audio

The camera sends mic audio (AAC) alongside the video. `QUBO_AUDIO=1` in the root
`.env` includes it; `0` (or leaving it unset) publishes video only. After changing it:

```bash
docker compose --env-file ../.env up -d
```

Audio and video are muxed by the bridge into one timestamped FLV stream
(`bridge/flv.py`) and stream‑copied by ffmpeg, so the AAC is not re‑encoded. If the
mic stops sending, video carries on and audio resumes when it returns.

Two‑way (talk‑back to the camera speaker) is **not** implemented here — it's a
separate build (outbound AAC + GCM‑encrypt + go2rtc back‑channel). See the RE
notes.

---

## Key settings (root `.env`)

| var | default | notes |
|---|---|---|
| `QUBO_EMAIL` / `QUBO_PASSWORD` | — | account login (or seed a session file) |
| `QUBO_QUALITY` | `high` | `high`=1080p, `low`=640×480 |
| `QUBO_AUDIO` | `0` if unset | `1` adds mic audio |
| `QUBO_DEVICE` | first camera | camera UUID or name substring |
| `QUBO_LOCAL_IP` | auto | the VM LAN IP the camera streams to |
| `QUBO_LOCAL_PORT` | `49700` | UDP port for the media |
| `QUBO_RTSP_READ_USER` / `QUBO_RTSP_READ_PASS` | required | login HA uses to read the stream |
| `QUBO_RTSP_PUBLISH_URL` | `rtsp://127.0.0.1:8554/qubo` | where ffmpeg publishes |
| `QUBO_LOG_LEVEL` | `INFO` | bridge log level |
| `MEDIAMTX_VERSION` | see `.env.example` | mediamtx image tag |

---

## Troubleshooting

- **`no media within watchdog window` looping** → the camera can't reach the VM.
  Confirm the VM is on the camera's LAN and `network_mode: host` is in effect
  (`docker inspect qubo-bridge | grep NetworkMode` → `host`). Check `QUBO_LOCAL_IP`
  is the VM's real LAN address.
- **Port clash on 8554/8889/etc.** → another service (Frigate also uses 8554!)
  owns it. Change the ports in `mediamtx.yml` **and** `QUBO_RTSP_PUBLISH_URL`, or run
  this on a host where they're free. (Frigate in this repo maps `8554` too — pick
  a different host port if they coexist.)
- **Phone app can't see live view while this runs** → expected; the camera
  serves one live session at a time. The bridge reconnects automatically, so if
  you open the app the bridge will just re‑grab the stream after.
- **`ignored N packets that failed authentication`** → normal for a few seconds
  after a restart: the previous call is still streaming to the UDP port. Those
  packets are discarded and the new call connects on its own. If video stops, the
  watchdog places a new call after 8 s, so this line should never run for long.
- **Stream stutters / artifacts** → UDP loss; usually Wi‑Fi (see the `last 60s` log
  line). The camera is
  Wi‑Fi‑only; a stronger AP near it helps. IDR interval is ~1–2 s so it recovers.
- **Logs**: `docker logs -f qubo-bridge` (bridge) and `docker logs -f qubo-mediamtx`
  (server).
- **`required variable QUBO_RTSP_READ_USER is missing`** → run compose with
  `--env-file ../.env` and check the variable is set there.

---

## How it works (one paragraph)

The bridge asks Qubo's cloud to place a P2P "call", advertising the VM's UDP
endpoint and an AES‑128 key it generates. The camera (signalled over its cloud
link) runs an RFC‑3489 STUN connectivity check against the VM; the bridge
answers it with integrity signed by that key and sends a `USE‑CANDIDATE`
nomination, which makes the camera stream **RTP/H.264(+AAC)** straight to the VM.
Each RTP payload is AES‑128‑GCM, with `IV = aesIV` XOR‑folded with the RTP header
and a per‑stream sequence rollover counter, the header as AAD and the tag in the
last 16 bytes. The bridge authenticates and
decrypts each packet, depacketizes it, muxes video and audio into one FLV stream
and hands that to ffmpeg → mediamtx → Home Assistant.
