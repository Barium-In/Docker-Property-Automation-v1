#!/usr/bin/env python3
"""Test D: can the landline carry a second call while the first is on SIP hold?

Runs on den-ubuntu from Home-Phone/, beside the home-phone container, with ARI on
(HOME_PHONE_ARI_PASSWORD set in the root .env). A test caller presses 4 in the menu, and the
dialplan hands the call to this script's ARI app with the forward number as its argument.
No number or password is in this file.

Modes, one per run (--moh can be added to any of them):
  (default)     D1: hold the caller (leg1), dial the forward number (leg2) on the same line;
                when leg2 answers, play hello-world to it, hang it up, take leg1 off hold and
                play hello-world to leg1.
  --moh         D2: also play our music on hold to leg1 while it is held.
  --mix         D3: when leg2 answers, use a phone's order: hold leg2, unhold leg1, unhold
                leg2, then join both in a mixing bridge.
  --mix-direct  D3b: unhold leg1 while leg2 stays active, then join both.
  --wait-in     E2: hold leg1 and wait 60 s for a call in to the landline from the forward
                number or a test number (leg2); answer it, unhold leg1, join both.
  --probe       D4a: no caller and no phone. INVITE each likely conference factory address
                once, log Airtel's answer and whether a conference focus answered (;isfocus
                in the 200 OK Contact), and hang up.
  --conf        D4: as D1 up to leg2's answer, then hold leg2, INVITE the conference factory
                (leg3), and send a REFER with Replaces inside leg3's dialog for leg2, then for
                leg1, so that Airtel moves both calls into its conference. The script then
                asks at the terminal whether the testers heard each other, hangs up leg3 and
                asks whether they are still connected. Run it in a real terminal.
  --conf --flash  D4 by TS 183 043 C.14.3 (option 2, tight coupling), with no REFER: while
                leg1 is held and leg2 is active, INVITE a switching-order address (default
                sip:flash@<domain>) as leg3, watch for Airtel's re-INVITEs, then ask the same
                questions.
  --conf --no-drop --hold-leg3 [S]  D4, then stay on the conference; on Enter, hold leg3 for
                S s (default 10), take it off hold, beep into the conference and ask what the
                testers heard: whether a held leg3 rejoins Airtel's conference.
  --conf --watch-end [S]  D4, then the end-of-merge check, with no drop test: after the heard
                question both testers hang up and press Enter, and the script keeps leg3 up
                for S s (default 180) and logs whether and when Airtel ends it. The key
                check's recording of leg3 runs from just after the merge to the end of the
                watch; it is summarised as per-second levels (RMS dBFS), with a count of the
                seconds of digital silence and the time of the last switch to all-zero audio,
                and deleted.
  --conf --in2 [--in2-wait S]  D4-in2: as D4, but leg2 is a call IN, in call-waiting order.
                leg1 stays on an active call (no hold) while the script waits 60 s for a call
                in from the forward number. That call is not answered: the script sends it
                ringing, plays leg1 three beeps and waits S s (default 20) for a key from leg1.
                1 adds the caller: hold leg1, answer leg2, then as D4 from the hello-world to
                leg2 (hold it, the factory INVITE and the REFERs for leg2, then leg1). 2, or no
                key in time, refuses it with busy (cause 17, which chan_pjsip should send as
                486) and leg1 hears two beeps. Keys from leg2 are ignored. If leg2 stops
                ringing first, the script says when.
In a 3-way, * on either phone plays a beep and # ends the test.
If the carrier holds a leg from its side (a ChannelHold event: its answer to an unhold, or its
own re-INVITE, carried a=sendonly or a=inactive) and the leg is still held when the test
checks, the result is FAIL: that leg never had two-way audio.
D4 sends each REFER through the [testd-refer] context in extensions.conf, which runs
Transfer() on the channel and returns it to this script with the result.
D4 key check (--conf, on unless --no-key-check): during the talk a snoop channel records the
audio the conference sends to leg3, inside the container. When the talk ends the script
downloads it into memory, deletes it in Asterisk and searches it for DTMF tones. The testers
press 1 (test phone) and 2 (forward number) three times each, so a tone shows whose key got
through. Keypad events on leg3 (RFC 4733) are logged as before. With --watch-end the same
recording runs on until the watch ends (with --no-key-check too, then only for the levels).

  export HOME_PHONE_ARI_PASSWORD="$(grep '^HOME_PHONE_ARI_PASSWORD=' ../.env | cut -d= -f2-)"
  python3 tests/testd.py [--moh] [--mix | --mix-direct | --wait-in | --conf [--flash]]
  python3 tests/testd.py --conf [--in2 [--in2-wait S]] [--watch-end [S]] [--conf-uri URI]
  python3 tests/testd.py --probe [--wide] [--all] [--icsi] [--conf-uri URI ...]
  (HOME_PHONE_TEST_PLMN=MCC-MNC in the shell, or --plmn, fills the mobile-form address)

Every step prints a "TESTD ..." line with the ARI result, and the Q.850 cause when a leg ends.
The last line is "TESTD RESULT: ...". Numbers show only their prefix and last 4 digits.
A copy goes to tests/testd-<date>-<time>.log, which git ignores (*.log).
Python 3 standard library only.
"""
import argparse
import base64
import hashlib
import io
import json
import math
import operator
import os
import re
import select
import signal
import socket
import struct
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from array import array

HOST, PORT = "127.0.0.1", 8088
APP, USER = "home-phone-testd", "homephone"
RING_TIMEOUT = 30   # s the forward number rings before Asterisk gives up
WAIT_IN = 60        # s E2 waits for a call in
TALK_LIMIT = 300    # s a 3-way may run before the script ends it
# D4 --in2, call waiting: how long leg2 rings while the script waits for 1 (add) or 2 (refuse)
# from leg1. The default is the plan's second_call.decide_s.
IN2_WAIT = 20
IN2_WAIT_RANGE = (5, 120)
IN2_CUE = "beep,beep,beep"   # leg1 hears this when a call is waiting (one playback, three beeps)
IN2_REFUSED = "beep,beep"    # and this once the waiting call has been refused

# Test D4. Conference factory addresses: {domain} is the SIP_DOMAIN global, {parent} the domain
# without its first label, {mcc} and {mnc} come from --plmn (MNC padded to 3 digits). No real
# value is in this file.
DEFAULT_CONF_URIS = (
    "sip:mmtel@conf-factory.{domain}",                                 # TS 23.003 13.10
    "sip:mmtel@conf-factory.ims.mnc{mnc}.mcc{mcc}.3gppnetwork.org",    # the mobile default
    "sip:mmtel@conf-factory.{parent}",
)
WIDE_CONF_URIS = (
    "sip:conference-factory@{domain}", "sip:conf-factory@{domain}", "sip:conference@{domain}",
    "sip:conf@{domain}", "sip:n-way_voice@{domain}", "sip:flash@{domain}",
)
# --flash (TS 183 043 C.14.3, tight coupling): the switching-order address, when no --conf-uri
# is given. C.14.3.1 gives flash@ as the form for a flash with no collected command.
FLASH_URI = "sip:flash@{domain}"
PLACEHOLDERS = ("domain", "parent", "mcc", "mnc")
FOCUS_RING = 20     # s Asterisk lets the factory INVITE run
REFER_WAIT = 45     # s the script waits for a REFER's result
BYE_WAIT = 8        # s it waits after a REFER's success for Airtel to end the old call
FLASH_WATCH = 5     # s --flash watches for Airtel's re-INVITEs after leg3 answers
PROBE_GAP = 3       # s between D4a tries
ASK_WAIT = 120      # s a question at the terminal waits for an answer
REFUSED_REFER = {401, 403, 405, 407, 489, 501}   # codes that can mean the REFER itself was refused
# The codes after which no other Refer-To form is tried. 403 is not one: Asterisk reports a 403
# answer to the REFER and a 403 in a NOTIFY sipfrag (the focus's INVITE to the party was
# refused) the same way, so the next form gets its chance.
STOP_FORMS = REFUSED_REFER - {403}
ICSI = "urn:urn-7:3gpp-service.ims.icsi.mmtel"
ICSI_BODY = {"variables": {
    "PJSIP_HEADER(add,Accept-Contact)":
        '*;+g.3gpp.icsi-ref="urn%3Aurn-7%3A3gpp-service.ims.icsi.mmtel"',
    "PJSIP_HEADER(add,P-Preferred-Service)": ICSI,
}}
# What each cause means as the answer to a conference factory INVITE.
FOCUS_HINT = {
    1: "the IMS core does not know this address", 21: "the address is known but refused for "
    "this line", 28: "digit collection: the network read it as dialled digits (tight coupling "
    "for flash@)", 58: "a session cap or SDP", 17: "busy, or a session cap", 34: "unavailable, "
    "or a session cap", 3: "an option tag is needed (see Unsupported in the trace)",
    29: "not implemented", 38: "server error", 19: "no final answer",
    127: "another 4xx, see the trace for the code",
}
# What a REFER result code usually means (the code comes from TRANSFERSTATUSPROTOCOL).
REFER_HINT = {
    1: "the focus ended the subscription without a final answer, or Asterisk could not send "
       "the REFER at all (then the trace has no REFER on that Call-ID)",
    405: "the REFER itself was refused",
    403: "the REFER itself, or the focus's INVITE to the party, was refused: the trace says "
         "which",
    501: "the REFER itself was refused", 489: "the REFER itself was refused",
    401: "the REFER was challenged, and Asterisk does not answer that",
    407: "the REFER was challenged, and Asterisk does not answer that",
    481: "Replaces did not match where it was checked", 486: "the phone was busy",
    500: "Asterisk's REFER subscription failed or timed out",
}
# A URI the script may dial or put in a Refer-To: nothing that breaks the dialplan or SIP.
URI_OK = re.compile(r"sips?:[^\s<>\"'(),\\{}]+")
REFER_TO_OK = re.compile(r"<[^\s<>\"'(),\\{}]+>")
NUMBER_OK = re.compile(r"\+?\d{6,15}")
PLMN_OK = re.compile(r"(\d{3})-(\d{2,3})")
ISFOCUS = re.compile(r"(?i);\s*isfocus\b")

# The SIP responses Asterisk 20.6 turns into each Q.850 cause (ast_sip_hangup_sip2cause in
# res/res_pjsip.c). ARI reports only the cause; the SIP code itself is in the trace.
SIP_HINT = {
    1: "SIP 404/485/604", 3: "SIP 420", 16: "normal hang-up", 17: "SIP 486/600",
    18: "SIP 408", 19: "SIP 480/483", 21: "SIP 401/403/407/603", 22: "SIP 410",
    27: "SIP 502", 28: "SIP 484", 29: "SIP 501", 34: "SIP 503 or another 5xx",
    38: "SIP 500", 41: "SIP 409", 58: "SIP 488/606", 102: "SIP 504",
    127: "SIP 405/481/487/491 or another 4xx/6xx",
}
# How a leg's end was found when ARI gave no cause for it.
END_HOW = {
    "poll": "found by polling: ARI sent no event for it",
    "404": "it had gone when the script went to hang it up, and ARI sent no event for it",
    "stasis-end": "it left the app and ARI gave no hang-up cause",
    "hung-up": "the script hung it up, and ARI sent no event for it",
}

# D4 key check. A snoop channel on leg3 with spy=in hears the audio Airtel sends us (the
# conference mix). res_stasis_snoop maps "in" to AST_AUDIOHOOK_DIRECTION_READ, which ast_read()
# feeds with the frames leg3 receives. Its recording is ARI's format=wav: 8 kHz 16-bit mono.
KEY_WAIT = 5                  # s for the snoop channel, RecordingStarted and RecordingFinished
KEY_MAX_SECS = TALK_LIMIT + 30  # maxDurationSeconds: the recording ends by itself after this
KEY_MAX_BYTES = 16 << 20      # the largest download accepted (330 s of 8 kHz audio is 5.3 MB,
#                               WATCH_REC_SECS is 16.0 MB)
KEY_FOLDER = "/var/spool/asterisk/recording"
KEY_RM = ("docker exec home-phone sh -c 'rm -f " + KEY_FOLDER + "/testd-keys-*'")
# The DTMF detector, modelled on Asterisk's main/dsp.c dtmf_detect(). Each value is in the
# units dsp.c uses: |X|^2 of a 102-sample block of 16-bit samples.
DTMF_RATE = 8000
DTMF_ROWS = (697.0, 770.0, 852.0, 941.0)
DTMF_COLS = (1209.0, 1336.0, 1477.0, 1633.0)
DTMF_KEYS = ("123A", "456B", "789C", "*0#D")
DTMF_N = 102            # block length, dsp.c DTMF_GSIZE: 12.75 ms, wide enough to part the rows
DTMF_HOP = 51           # blocks start every 6.4 ms (half overlap), so tone edges are found finely
DTMF_FLOOR = 8.0e7      # each tone at least this (dsp.c DTMF_THRESHOLD): about -42 dBm0 per tone
DTMF_PEAK = 6.3         # the strongest row and column 8 dB above the rest of their group (dsp.c)
DTMF_TWIST_ROW = 6.31   # the row tone may be up to 8 dB above the column tone (dsp.c normal twist)
DTMF_TWIST_COL = 3.98   # the column tone up to 6 dB above the row tone (dsp.c relaxed reverse
#                         twist; the strict 4 dB is for analogue lines, and these tones may have
#                         passed through a mobile codec and Airtel's mixer)
DTMF_SHARE = 42.0       # row + column above 42 x the block energy: the pair carries over 82% of it
#                         (a pure pair gives N/2 = 51; dsp.c DTMF_TO_TOTAL_ENERGY). As in dsp.c, a
#                         pair more than about 1.5% off its frequencies is missed, which Q.24
#                         allows a receiver; a digital tone generator is far closer than that
DTMF_MIN_HITS = 5       # 5 matching blocks: a tone of 42 ms or more always passes, one of 31 ms
#                         or less never does
DTMF_GAP = 7            # 7 blocks without the digit end a press: a gap of 40 ms or more always
#                         does and one under 20 ms never does (ES 201 235-3, cited in dsp.c)
DTMF_DBM0 = 20 * math.log10(22300.0 * DTMF_N / 2)   # |X|^2 in dB of a 0 dBm0 tone, whose peak
#                         is 22300 in 16-bit G.711 audio: dBm0 = 10 log10(|X|^2) - DTMF_DBM0
# D4 --watch-end, the end-of-merge check. These are fixed for this test script; the service
# keeps its own limits as settings.
WATCH_DEFAULT = 180           # s leg3 stays up after Enter when --watch-end has no number
WATCH_RANGE = (10, 900)       # s --watch-end accepts
WATCH_HANGUP_WAIT = ASK_WAIT  # s the script waits for Enter once it has asked both to hang up
WATCH_NOTE = 30               # s between "leg3 is still up" lines during the watch
WATCH_REC_SECS = 1000         # maxDurationSeconds cap: 8 kHz 16-bit audio is 16 000 bytes a
#                               second, so the 16 MB tmpfs holds about 1048 s
WATCH_SILENT_DBFS = -55.0     # a second below this is silent: a quiet line or comfort noise
#                               sits near -60 to -70 dBFS, and mu-law's smallest step is -72
WATCH_DIGITAL_DBFS = -90.0    # a second below this is digital silence: almost every sample is
#                               0 (an all-zero second reads LEVEL_FLOOR). It sits 18 dB under a
#                               second of mu-law's smallest step, so line noise never reaches it
WATCH_ZERO_RUN = 1.0          # s: a stretch of all-zero samples this long or longer is a switch
#                               to digital silence; quiet line noise holds shorter runs of zero
#                               samples (mu-law's two codes for 0)
WATCH_LOUD_DBFS = -40.0       # a second at or above this is at tone or speech level: a network
#                               tone of -10 dBm0 is about -16 dBFS, and a second in which
#                               someone talks is about -20 to -35 dBFS
WATCH_GAP = 2.0               # s: a file this much shorter than the time it ran means stretches
#                               with no audio frames (no RTP from Airtel)
WATCH_RUNS = 8                # runs of silent / low / loud seconds shown in the RESULT
LEVEL_FLOOR = -99.0           # dBFS given to digital silence
_sumprod = getattr(math, "sumprod", None)      # Python 3.12 and later
if _sumprod is None:
    def _sumprod(a, b, _mul=operator.mul):
        return sum(map(_mul, a, b))

# A phone number: optional +91, 91 or 0, then 10 digits. Shown as the prefix, XXXXXX and the
# last 4 digits. Digits next to a '.digit' (Asterisk channel ids) are left alone.
_NUMBER = re.compile(r"(?<!\d)(?<!\d\.)(\+91|91|0)?\d{6}(\d{4})(?!\d|\.\d)")


def redact(text):
    return _NUMBER.sub(lambda m: (m.group(1) or "") + "XXXXXX" + m.group(2), text)


def cause_str(cause, text=None):
    if cause is None:
        return "cause unknown"
    hint = SIP_HINT.get(cause)
    out = f"cause {cause}"
    if text:
        out += f" {text}"
    return out + (f" ({hint})" if hint else "")


# ---- D4 key check: the recording and the DTMF detector ---------------------------------------

def read_wav(data):
    """(samples, unclosed) of the WAV that ARI's record wrote with format=wav: samples is an
    array of 16-bit ints. format_wav writes 0 in the RIFF and data sizes and fills them in only
    when it closes the file, so a file with a 0 data size is read by its length (unclosed True).
    ValueError when it is not a WAV the wave module reads, or not 8 kHz 16-bit mono PCM."""
    unclosed = (len(data) > 44 and data[:4] == b"RIFF" and data[8:16] == b"WAVEfmt "
                and data[16:20] == b"\x10\0\0\0" and data[36:40] == b"data"
                and data[40:44] == b"\0\0\0\0")
    if unclosed:
        data = (data[:4] + struct.pack("<I", len(data) - 8) + data[8:40]
                + struct.pack("<I", len(data) - 44) + data[44:])
    try:
        with wave.open(io.BytesIO(data)) as w:
            shape = (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getcomptype())
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError, struct.error, ValueError) as e:
        raise ValueError(f"the recording is not a WAV file the script can read ({e})")
    if shape != (1, 2, DTMF_RATE, "NONE"):
        ch, width, rate, comp = shape
        raise ValueError(f"the recording is {rate} Hz, {8 * width}-bit, {ch} channel(s), "
                         f"compression {comp}; the key check needs 8000 Hz 16-bit mono PCM")
    samples = array("h")
    samples.frombytes(frames[:len(frames) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples, unclosed


def _dtmf_tables():
    """cos and sin of each DTMF frequency over one block, for a DFT at exactly that frequency
    (the same |X|^2 that dsp.c's Goertzel filters give)."""
    out = []
    for f in DTMF_ROWS + DTMF_COLS:
        w = 2.0 * math.pi * f / DTMF_RATE
        out.append(([math.cos(w * n) for n in range(DTMF_N)],
                    [math.sin(w * n) for n in range(DTMF_N)]))
    return out


def find_key_tones(samples):
    """DTMF presses in 8 kHz 16-bit audio: [(digit, start s, duration s, low tone dBm0,
    high tone dBm0), ...]. Each level is the median over the press's blocks.

    Each 102-sample block (every 51 samples) is a hit for a digit when, as in dsp.c: the
    strongest row and the strongest column tone each reach DTMF_FLOOR, the twist is within
    limits, each is 8 dB above the other tones of its group, and the pair holds over 82% of the
    block's energy (speech spreads its energy over many harmonics and fails this). A press is
    DTMF_MIN_HITS hits of the same digit or more, with gaps of fewer than DTMF_GAP blocks.
    Blocks too quiet to hold a tone at the floor are skipped, which keeps silence cheap.
    """
    tables = _dtmf_tables()
    x = array("d", samples)
    n, size, hop, sp = len(x), DTMF_N, DTMF_HOP, _sumprod
    quiet = DTMF_FLOOR / size           # below this block energy no tone can reach the floor
    floor, share, peak = DTMF_FLOOR, DTMF_SHARE, DTMF_PEAK
    twist_row, twist_col = DTMF_TWIST_ROW, DTMF_TWIST_COL
    presses, run, misses = [], None, 0  # run: [digit, first block, last block, hits, rows, cols]

    def dbm0(powers):
        return 10 * math.log10(sorted(powers)[len(powers) // 2]) - DTMF_DBM0

    def close(r):
        if r is not None and r[3] >= DTMF_MIN_HITS:
            presses.append((r[0], r[1] * hop / DTMF_RATE,
                            ((r[2] - r[1]) * hop + size) / DTMF_RATE, dbm0(r[4]), dbm0(r[5])))

    for i, start in enumerate(range(0, n - size + 1, hop)):
        blk = x[start:start + size]
        energy = sp(blk, blk)
        hit = None
        if energy >= quiet:
            p = []
            for cos_t, sin_t in tables:
                re_, im = sp(blk, cos_t), sp(blk, sin_t)
                p.append(re_ * re_ + im * im)
            rows, cols = p[:4], p[4:]
            pr, pc = max(rows), max(cols)
            r, c = rows.index(pr), cols.index(pc)
            if (pr >= floor and pc >= floor and pc < pr * twist_col and pr < pc * twist_row
                    and pr + pc > share * energy
                    and all(k == r or rows[k] * peak <= pr for k in range(4))
                    and all(k == c or cols[k] * peak <= pc for k in range(4))):
                hit = DTMF_KEYS[r][c]
        if run is not None:
            if hit == run[0]:
                run[2], run[3], misses = i, run[3] + 1, 0
                run[4].append(pr)
                run[5].append(pc)
                continue
            misses += 1
            if hit is not None or misses >= DTMF_GAP:
                close(run)
                run = None
        if hit is not None and run is None:
            run, misses = [hit, i, i, 1, [pr], [pc]], 0
    close(run)
    return presses


def second_levels(samples):
    """The RMS level of each second of 8 kHz 16-bit audio, in dBFS (full scale 32768, so a
    full-scale sine is -3 dBFS). A last part second counts if it holds half a second or more.
    Digital silence is LEVEL_FLOOR."""
    out = []
    for start in range(0, len(samples), DTMF_RATE):
        blk = samples[start:start + DTMF_RATE]
        if len(blk) < DTMF_RATE // 2:
            break
        energy = _sumprod(blk, blk)
        level = 10 * math.log10(energy / len(blk) / 32768.0 ** 2) if energy else LEVEL_FLOOR
        out.append(max(LEVEL_FLOOR, level))
    return out


def level_runs(levels):
    """[(class, first second, last second), ...] for a list of per-second levels: 'silent'
    below WATCH_SILENT_DBFS, 'loud' at or above WATCH_LOUD_DBFS, 'low' between."""
    runs = []
    for i, level in enumerate(levels):
        kind = ("silent" if level < WATCH_SILENT_DBFS else "loud" if level >= WATCH_LOUD_DBFS
                else "low")
        if runs and runs[-1][0] == kind:
            runs[-1][2] = i
        else:
            runs.append([kind, i, i])
    return [tuple(r) for r in runs]


def last_zero_switch(samples):
    """The last switch from audio to digital silence in 8 kHz 16-bit audio: (start s, length s,
    True if it runs to the end), or None. A switch is a stretch of all-zero samples at least
    WATCH_ZERO_RUN s long that follows a non-zero sample; it starts just after that sample.
    The audio is looked at in 10 ms blocks, then to the sample at the stretch's start."""
    blk, n = DTMF_RATE // 100, len(samples)
    zero = [not any(samples[i:i + blk]) for i in range(0, n, blk)]
    need = math.ceil(WATCH_ZERO_RUN * 100)
    found, i = None, 0
    while i < len(zero):
        if not zero[i]:
            i += 1
            continue
        j = i
        while j < len(zero) and zero[j]:
            j += 1
        if i > 0 and j - i >= need:          # block i-1 holds a non-zero sample
            found = (i, j)
        i = j
    if found is None:
        return None
    i, j = found
    prev = samples[(i - 1) * blk:i * blk]
    k = len(prev)
    while k and prev[k - 1] == 0:
        k -= 1
    start, end = (i - 1) * blk + k, min(n, j * blk)
    return start / DTMF_RATE, (end - start) / DTMF_RATE, end >= n


def tone_summary(presses):
    """'1 x3, 2 x3, * x1': each digit with its count, in the order it was first heard."""
    counts = {}
    for digit, *_ in presses:
        counts[digit] = counts.get(digit, 0) + 1
    return ", ".join(f"{d} x{k}" for d, k in counts.items())


class SetupError(Exception):
    """The test could not start (ARI off, wrong password, ...)."""


class WsClosed(Exception):
    """Asterisk closed the event connection."""


class Refused(Exception):
    """Asterisk refused a hold or unhold, so that step never reached the carrier."""


class CallEnded(Exception):
    """A leg the current step depends on has gone."""

    def __init__(self, leg, cause, text):
        super().__init__(f"{leg} ended, {cause_str(cause, text)}")
        self.leg, self.cause, self.text = leg, cause, text   # cause None: ARI gave none


class Log:
    def __init__(self, path):
        self.path = path
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.file = os.fdopen(fd, "a", encoding="utf-8")
        self.console = True

    def __call__(self, *parts):
        line = time.strftime("%H:%M:%S ") + "TESTD " + redact(" ".join(str(p) for p in parts))
        if self.console:
            try:
                print(line, flush=True)
            except (OSError, ValueError):
                # The terminal has gone (a closed SSH session): keep logging to the file so the
                # cleanup still runs and the RESULT line is kept.
                self.console = False
        self.file.write(line + "\n")
        self.file.flush()


class Rest:
    """ARI's REST side. Every call returns (http status, reason, body); status 0 = no answer."""

    def __init__(self, auth):
        self.auth = auth

    def call(self, method, path, http_timeout=10, body=None, **params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"http://{HOST}:{PORT}/ari{path}" + (f"?{query}" if query else "")
        headers = {"Authorization": self.auth}
        if body is None:
            data = None if method == "GET" else b""
        else:
            # Only D4's originate with --icsi sends a body (channel variables as JSON).
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                raw = resp.read()
                return resp.status, resp.reason, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                message = json.loads(raw).get("message", "")
            except (ValueError, AttributeError):
                message = raw[:200].decode("utf-8", "replace")
            return e.code, e.reason, {"message": message}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, str(getattr(e, "reason", e)), {}

    def raw(self, method, path, http_timeout=30, limit=KEY_MAX_BYTES):
        """A call whose answer is a file (GET /recordings/stored/{name}/file): (http status,
        reason, the bytes, the Content-Type or ARI's error message). Status 0 = no answer, or
        a file larger than `limit`. The bytes stay in memory."""
        url = f"http://{HOST}:{PORT}/ari{path}"
        req = urllib.request.Request(url, method=method, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                data = resp.read(limit + 1)
                if len(data) > limit:
                    return 0, f"the file is larger than {limit >> 20} MB", b"", ""
                return resp.status, resp.reason, data, resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                message = json.loads(raw).get("message", "")
            except (ValueError, AttributeError):
                message = raw[:200].decode("utf-8", "replace")
            return e.code, e.reason, b"", message
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, str(getattr(e, "reason", e)), b"", ""


class Events:
    """A minimal websocket client for /ari/events (RFC 6455).

    Our frames are masked, as a client's must be. Server frames are unmasked if a mask is
    present, fragmented messages are joined, pings get a pong with the same payload, and a
    close frame is answered before the connection ends.
    """

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, auth):
        try:
            self.sock = socket.create_connection((HOST, PORT), timeout=5)
        except ConnectionRefusedError:
            raise SetupError(f"nothing listens on {HOST}:{PORT}. Is ARI on? Set "
                             "HOME_PHONE_ARI_PASSWORD in ../.env and recreate the container.")
        except OSError as e:
            raise SetupError(f"cannot reach {HOST}:{PORT}: {e}")
        key = base64.b64encode(os.urandom(16)).decode()
        query = urllib.parse.urlencode({"app": APP, "subscribeAll": "false"})
        self.sock.sendall((
            f"GET /ari/events?{query} HTTP/1.1\r\n"
            f"Host: {HOST}:{PORT}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: ari\r\n"
            f"Authorization: {auth}\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                raise SetupError("ARI did not answer the websocket request")
            if not chunk or len(self.buf) > 65536:
                raise SetupError("ARI closed the connection during the websocket handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ", 2)
        if len(status) < 2 or status[1] != "101":
            self.sock.close()
            if len(status) > 1 and status[1] == "401":
                raise SetupError("ARI refused the password (401). Export the same "
                                 "HOME_PHONE_ARI_PASSWORD the container was started with.")
            raise SetupError(f"ARI refused the websocket: {lines[0]}")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        accept = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != accept:
            self.sock.close()
            raise SetupError("bad Sec-WebSocket-Accept from ARI")
        self.sock.settimeout(None)
        self.fragments = None   # (opcode, bytearray) while a fragmented message arrives
        self.closed = False     # the connection is finished
        self.close_sent = False

    def _send(self, opcode, payload=b""):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _next_frame(self):
        """Take one complete frame off the buffer: (fin, opcode, payload), or None."""
        buf = self.buf
        if len(buf) < 2:
            return None
        fin, opcode = buf[0] & 0x80, buf[0] & 0x0F
        masked, n, pos = buf[1] & 0x80, buf[1] & 0x7F, 2
        if n == 126:
            if len(buf) < 4:
                return None
            n, pos = struct.unpack("!H", buf[2:4])[0], 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n, pos = struct.unpack("!Q", buf[2:10])[0], 10
        mask = b""
        if masked:
            if len(buf) < pos + 4:
                return None
            mask, pos = buf[pos:pos + 4], pos + 4
        if len(buf) < pos + n:
            return None
        payload, self.buf = buf[pos:pos + n], buf[pos + n:]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _message(self):
        """Handle the complete frames in the buffer; return the next text message, or None."""
        while True:
            frame = self._next_frame()
            if frame is None:
                return None
            fin, opcode, payload = frame
            if opcode == 0x9:                       # ping: pong with the same payload
                try:
                    self._send(0xA, payload)
                except OSError:
                    pass                            # the read side reports the drop
            elif opcode == 0xA:                     # pong
                continue
            elif opcode == 0x8:                     # close
                if not self.close_sent:
                    self.close_sent = True
                    try:
                        self._send(0x8, payload[:2])
                    except OSError:
                        pass
                self.closed = True
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                raise WsClosed(f"Asterisk closed the event connection (code {code})")
            elif opcode in (0x1, 0x2):              # text or binary, maybe the first fragment
                if fin:
                    if opcode == 0x1:
                        return payload.decode("utf-8", "replace")
                else:
                    self.fragments = (opcode, bytearray(payload))
            elif opcode == 0x0 and self.fragments:  # continuation
                self.fragments[1].extend(payload)
                if fin:
                    first, data = self.fragments
                    self.fragments = None
                    if first == 0x1:
                        return data.decode("utf-8", "replace")

    def recv(self, timeout):
        """The next text message, or None once `timeout` seconds pass without one."""
        end = time.monotonic() + max(timeout, 0)
        while True:
            message = self._message()
            if message is not None:
                return message
            if self.closed:
                raise WsClosed("the event connection is closed")
            left = end - time.monotonic()
            if left <= 0:
                return None
            ready, _, _ = select.select([self.sock], [], [], left)
            if not ready:
                return None
            chunk = self.sock.recv(65536)
            if not chunk:
                self.closed = True
                raise WsClosed("Asterisk dropped the event connection")
            self.buf += chunk

    def close(self):
        """Send a close frame, wait up to 2 s for Asterisk's, then drop the socket."""
        if self.sock is None:
            return
        try:
            if not self.closed and not self.close_sent:
                self.close_sent = True
                self._send(0x8, struct.pack("!H", 1000))
                end = time.monotonic() + 2
                while not self.closed and time.monotonic() < end:
                    self.recv(end - time.monotonic())
        except (WsClosed, OSError):
            pass
        finally:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None


def channel_of(ev):
    return ev.get("channel") or ev.get("peer") or {}


def first_arg(ev):
    return (ev.get("args") or [""])[0]


class TestD:
    def __init__(self, opt, rest, log):
        self.opt, self.rest, self.log = opt, rest, log
        self.mode = ("D4a" if opt.probe else "D4-flash" if opt.conf and opt.flash
                     else "D4-in2" if opt.conf and opt.in2
                     else "D4" if opt.conf else "D3" if opt.mix
                     else "D3b" if opt.mix_direct else "E2" if opt.wait_in
                     else "D2" if opt.moh else "D1")
        if opt.moh and self.mode != "D2":
            self.mode += "+moh"
        self.ws = None
        self.legs = {}         # "leg1" / "leg2" -> channel id
        self.ended = {}        # label -> (cause, cause text); cause None when ARI gave none
        self.ended_at = {}     # label -> time.monotonic() when it ended
        self.end_how = {}      # label -> "destroyed", "stasis-end", "poll" or "404"
        self.hangup_cause = {}  # label -> Q.850 cause of the carrier's hang-up request
        self.subscribed = set()  # dialplan-entered legs the app subscribed to explicitly
        self.answered_at = None
        self.last_error = ""
        self.last_status = 0
        self.required = set()  # legs whose end stops the current step
        self.bridge = None
        self.flag_set = False  # TESTD_WAIT may be 1
        self.moh_on = False
        self.dtmf_from = set()
        self.net_held = set()  # legs the carrier holds from its side (ChannelHold, no Unhold yet)
        self.step = "waiting for a test call"
        self.verdict = None
        self.detail = ""
        # Test D4
        self.domain = ""
        self.uris = []         # conference factory addresses, filled in
        self.outside = set()   # legs running [testd-refer]: ARI sends no events for them
        self.progress = set()  # legs that had early media (a Dial event with PROGRESS)
        self.typed = ""        # terminal input not yet used
        self.stdin_eof = False
        self.keys = []         # digits that arrived on leg3
        self.kc = None         # the D4 key check while it runs (see key_check_start)
        self.talk_started = False
        self.hung_up = set()   # labels cleanup() has already hung up
        self.merge_at = None   # time.monotonic() when both REFERs had worked
        self.watch = None      # --watch-end while it runs and after (see end_watch)
        self.in2 = None        # --in2, call waiting (see call_in_conf)

    # ---- results -------------------------------------------------------------------------

    def fail(self, detail, verdict="FAIL"):
        if self.verdict is None:
            self.verdict, self.detail = verdict, detail
        else:
            self.detail += f"; then {detail}"
        self.log(f"{verdict}: {detail}")

    def passed(self, detail, verdict="PASS"):
        if self.verdict is None:
            self.verdict, self.detail = verdict, detail

    def add_sentence(self, text):
        """Add a sentence to the RESULT detail, closing the one before it."""
        if self.detail and not self.detail.endswith("."):
            self.detail += "."
        self.detail = f"{self.detail} {text}" if self.detail else text

    # ---- ARI -----------------------------------------------------------------------------

    def ari(self, step, method, path, http_timeout=10, body=None, **params):
        status, reason, body = self.rest.call(method, path, http_timeout=http_timeout, body=body,
                                              **params)
        shown = path
        for label, cid in self.legs.items():
            shown = shown.replace(cid, label)
        if self.bridge:
            shown = shown.replace(self.bridge, "bridge")
        ok = 200 <= status < 300
        note = "" if ok or not body.get("message") else f" ({body['message']})"
        self.last_status = status
        self.last_error = "" if ok else f"HTTP {status or 'no answer'} {reason}{note}"
        self.log(f"{step}: {method} {shown} -> {status or 'no answer'} {reason}{note}")
        return ok, body

    def must(self, step, method, path):
        """A hold or unhold the test depends on. If Asterisk refuses it, nothing reached the
        carrier, so the result would mean nothing: stop. A leg that just hung up is reported
        as such (CallEnded) rather than as a refusal."""
        ok, _ = self.ari(step, method, path)
        if not ok:
            error = self.last_error
            self.wait_for(1.0)
            raise Refused(f"Asterisk refused {step.split(' (')[0]} ({error}); "
                          "no re-INVITE went to the carrier for it")

    def states(self):
        """Each leg's channel state, or its cause if it has ended."""
        parts = []
        for label in list(self.legs):
            cid = self.legs.get(label)
            if not cid:
                continue
            if label in self.ended:
                parts.append(self.end_str(label))
                continue
            status, _, body = self.rest.call("GET", f"/channels/{cid}", http_timeout=5)
            parts.append(f"{label} {body.get('state', '?')}" if status == 200
                         else f"{label} not found (HTTP {status})")
        return "; ".join(parts)

    def end_str(self, label):
        cause, text = self.ended[label]
        if cause is None:
            return f"{label} ended ({END_HOW.get(self.end_how.get(label), 'ARI gave no cause')})"
        return f"{label} ended, {cause_str(cause, text)}"

    def mark_ended(self, label, cause, text, how):
        """Record that a leg has ended. The first sign sets the time; ChannelDestroyed, which
        carries the channel's own cause, replaces a cause found another way. A leg the current
        step depends on raises CallEnded."""
        first = label not in self.ended
        if first or how == "destroyed":
            self.ended[label] = (cause, text)
            self.end_how[label] = how
        if first:
            self.ended_at[label] = time.monotonic()
        self.outside.discard(label)
        if label in self.required:
            self.required.discard(label)
            cause, text = self.ended[label]
            raise CallEnded(label, cause, text)

    def subscribe(self, label):
        """Subscribe the app to a leg that entered through the dialplan. In 20.6 StasisEnd
        unsubscribes the app from such a channel (res_stasis app_send_end_msg), so its
        ChannelDestroyed never arrives; an ARI-originated channel keeps the subscription the
        originate made (resource_channels.c), which is why leg2 and leg3 always report theirs."""
        ok, _ = self.ari(f"setup: subscribe the app to {label}, so ARI reports its end after it "
                         "leaves the app", "POST", f"/applications/{APP}/subscription",
                         eventSource=f"channel:{self.legs[label]}")
        if ok:
            self.subscribed.add(label)
        else:
            self.log(f"note: no subscription for {label}: the script learns of its end from "
                     "StasisEnd, and by polling while it runs a REFER")

    def label_of(self, cid):
        for label, x in self.legs.items():
            if x == cid:
                return label
        return None

    # ---- events --------------------------------------------------------------------------

    def note(self, ev):
        """Log one event and track legs that end."""
        kind = ev.get("type", "?")
        ch = channel_of(ev)
        label = self.label_of(ch.get("id"))
        if label is None and kind.startswith(("Playback", "Recording")):
            target = ev.get("playback" if kind.startswith("Playback") else "recording",
                            {}).get("target_uri", "")
            label = ("bridge" if self.bridge and target == f"bridge:{self.bridge}"
                     else self.label_of(target.partition(":")[2]))
        if label is None:
            label = "new call" if kind == "StasisStart" else "other"
        bits = [f"event {kind}", label]
        if ch.get("name"):
            bits.append(ch["name"])
        if ch.get("state"):
            bits.append(ch["state"])
        for key in ("args", "dialstatus", "digit", "variable", "value"):
            if ev.get(key) not in (None, "", []):
                bits.append(f"{key}={ev[key]}")
        if kind.startswith("Playback"):
            bits.append(ev.get("playback", {}).get("media_uri", ""))
        rec = ev.get("recording") if kind.startswith("Recording") else None
        if isinstance(rec, dict):
            bits.append(f"{rec.get('name', '')} {rec.get('state', '')}")
            if rec.get("cause"):
                bits.append(f"({rec['cause']})")
        if kind == "ChannelHangupRequest" and ev.get("soft"):
            bits.append("(a soft hang-up, such as the script's own)")
        elif "cause" in ev:
            bits.append(cause_str(ev.get("cause"), ev.get("cause_txt")))
        self.log(" ".join(str(b) for b in bits))
        kc = self.kc
        if isinstance(rec, dict) and kc and rec.get("name") == kc["name"]:
            kc["state"] = str(rec.get("state", ""))
            if kind == "RecordingStarted":
                kc["t0"] = time.monotonic()     # --watch-end places Enter in the file by this
            elif kind in ("RecordingFinished", "RecordingFailed"):
                kc["t_end"] = time.monotonic()
            if kind == "RecordingFailed":
                kc["fail_cause"] = str(rec.get("cause") or "no cause given")
        # Asterisk raises ChannelHold only for a hold from the far end (ast_queue_hold); the
        # script's own holds raise none. So this is the carrier holding the leg.
        if kind == "ChannelHold" and label in self.legs:
            self.net_held.add(label)
            self.log(f"note: the carrier now holds {label} from its side (its SDP carried "
                     "a=sendonly or a=inactive)")
        elif kind == "ChannelUnhold" and label in self.legs and label in self.net_held:
            self.net_held.discard(label)
            self.log(f"note: the carrier took {label} off hold from its side")
        elif kind == "StasisStart" and label in self.outside:
            self.outside.discard(label)      # back from [testd-refer]
        elif kind == "Dial" and ev.get("dialstatus") == "PROGRESS" and label in self.legs:
            self.progress.add(label)
        elif kind == "ChannelDtmfReceived" and label == "leg3":
            # Counted here, so a key that arrives during a playback or the key-check setup
            # counts too. ask() handles * and # in the talk.
            self.keys.append(str(ev.get("digit")))
            self.dtmf_from.add("leg3")
        elif (kind == "ChannelHangupRequest" and label in self.legs and not ev.get("soft")
              and isinstance(ev.get("cause"), int) and ev["cause"] > 0):
            # The carrier's hang-up (ast_queue_hangup_with_cause) carries a Q.850 cause. A
            # soft one (ast_softhangup, as for ARI's DELETE) carries a flag in that field.
            self.hangup_cause[label] = ev["cause"]
        if (kind == "ChannelDtmfReceived" and self.in2 is not None
                and label in ("leg1", "leg2")):
            # --in2: kept here, so a key during a playback counts too; decide() reads them.
            self.in2["keys"].append((label, str(ev.get("digit")), time.monotonic()))
        if label not in self.legs:
            return
        if kind == "StasisEnd" and label not in self.outside and label not in self.ended:
            # The script sent this leg nowhere (a continue into [testd-refer] puts it in
            # self.outside first, and send_back() only takes unlabelled calls), so it has
            # hung up. A dialplan-entered leg sends nothing after this unless subscribed.
            self.mark_ended(label, self.hangup_cause.get(label), None, "stasis-end")
        elif kind == "ChannelDestroyed":
            self.mark_ended(label, ev.get("cause"), ev.get("cause_txt"), "destroyed")

    def wait_for(self, timeout, match=None):
        """Handle events until match(event) is true (returns it) or `timeout` s pass (None).

        timeout=None waits until Ctrl-C. A call that enters the app unasked is sent back to
        the dialplan.
        """
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            left = 3600.0 if end is None else end - time.monotonic()
            if left <= 0:
                return None
            raw = self.ws.recv(left)
            if raw is None:
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                self.log("event that is not JSON, ignored")
                continue
            self.note(ev)
            if match and match(ev):
                return ev
            if ev.get("type") == "StasisStart":
                self.send_back(ev)

    def pause(self, step):
        self.wait_for(self.opt.pause)
        self.log(f"{step}, {self.opt.pause:g} s later: {self.states()}")

    def send_back(self, ev):
        cid = channel_of(ev).get("id")
        if not cid or self.label_of(cid):
            return
        self.log("another call reached the script: sending it back to the stage-1 dialplan")
        ok, _ = self.ari("send back", "POST", f"/channels/{cid}/continue",
                         context="inbound", extension="s", label="start")
        if not ok:
            self.ari("send back", "DELETE", f"/channels/{cid}", reason="normal")

    # ---- building blocks -----------------------------------------------------------------

    def play(self, step, label, sound, wait=8.0):
        """Play a sound, or several in one playback ("beep,beep"), to a leg."""
        ok, body = self.ari(step, "POST", f"/channels/{self.legs[label]}/play",
                            media=",".join(f"sound:{s}" for s in sound.split(",")))
        if ok and body.get("id") and wait:
            pid = body["id"]
            self.wait_for(wait, lambda e: e.get("type") == "PlaybackFinished"
                          and e.get("playback", {}).get("id") == pid)
        return ok

    def play_bridge(self, step, sound, wait=8.0):
        ok, body = self.ari(step, "POST", f"/bridges/{self.bridge}/play", media=f"sound:{sound}")
        if ok and body.get("id") and wait:
            pid = body["id"]
            self.wait_for(wait, lambda e: e.get("type") == "PlaybackFinished"
                          and e.get("playback", {}).get("id") == pid)

    def hangup(self, step, label, wait=5.0, reason="normal"):
        """Hang up a leg and wait up to `wait` s for its end. Returns whether ARI accepted the
        DELETE: False if the leg had already gone or ARI refused, and True does not mean the
        leg has ended (check self.ended). reason="busy" asks Asterisk to end an unanswered call
        in with AST_CAUSE_BUSY (17), which chan_pjsip should send as 486 (from the 20.6
        source; the trace confirms it)."""
        cid = self.legs.get(label)
        if not cid or label in self.ended:
            return False
        self.required.discard(label)
        ok, _ = self.ari(step, "DELETE", f"/channels/{cid}", reason=reason)
        if self.last_status == 404 and label not in self.ended:
            self.mark_ended(label, None, None, "404")
            self.log(f"{label} had already gone (DELETE -> 404), and ARI had sent no event for "
                     "its end")
            return False
        end = time.monotonic() + wait
        while label not in self.ended:
            left = end - time.monotonic()
            if left <= 0:
                break
            if label in self.outside:
                # A dialplan-entered leg inside [testd-refer] may send no events: look as well.
                self.wait_for(min(0.5, left), lambda e: label in self.ended)
                self.poll_outside()
            else:
                self.wait_for(left, lambda e: label in self.ended)
        return ok

    def poll_outside(self):
        """Find legs that ended while they ran a REFER. In 20.6 StasisEnd unsubscribes the app
        from a dialplan-entered channel (res_stasis app_send_end_msg), so unless subscribe()
        worked such a leg sends no events in [testd-refer]: ask for each one."""
        for label in list(self.outside):
            if label in self.ended:
                self.outside.discard(label)
                continue
            status, _, _ = self.rest.call("GET", f"/channels/{self.legs[label]}", http_timeout=5)
            if status == 404 and label not in self.ended:
                self.required.discard(label)
                self.mark_ended(label, None, None, "poll")
                self.log(f"{label} has gone (GET /channels -> 404). It ended while it ran the "
                         "REFER, so ARI sent no event and the cause is not known; see the trace.")

    def set_flag(self, value, step):
        if value == "1":
            self.flag_set = True
        ok, _ = self.ari(step, "POST", "/asterisk/variable", variable="TESTD_WAIT", value=value,
                         http_timeout=3 if value == "0" else 10)
        if value == "0" and ok:
            self.flag_set = False
        return ok

    def stop_moh(self):
        if self.moh_on:
            self.moh_on = False
            self.ari("D2 stop our music on hold", "DELETE", f"/channels/{self.legs['leg1']}/moh")

    def return_caller(self, failed, leg2_early=None):
        """Take leg1 off hold and play it a sound: the consult-and-return (decline) path.

        leg2_early: seconds between leg2 answering and leg2 ending on its own, when it ended
        before the script hung it up.
        """
        self.step = "D1c unhold leg1"
        self.required = {"leg1"}
        self.stop_moh()
        self.must("D1c unhold leg1 (re-INVITE a=sendrecv)", "DELETE",
                  f"/channels/{self.legs['leg1']}/hold")
        self.pause("D1c")
        self.play("D1c play " + ("beeperr" if failed else "hello-world") + " to leg1", "leg1",
                  "beeperr" if failed else "hello-world")
        if failed:
            return
        if "leg1" in self.net_held:
            self.fail("the carrier kept leg1 on hold from its side after the unhold, so the "
                      "caller could not hear anything. Check the trace: the 200 OK to the "
                      "a=sendrecv re-INVITE carries a=sendonly or a=inactive.")
            return
        detail = ("hold sent, second call answered, leg1 still up after the unhold. "
                  "Confirm both phones heard hello-world"
                  + (" and note what leg1 heard while held." if self.opt.moh else "."))
        if leg2_early is None:
            self.passed(detail)
        else:
            self.passed(detail + f" But leg2 ended {leg2_early:.1f} s after it answered, before "
                        "the script hung it up: check the trace for who sent the BYE.",
                        verdict="UNCLEAR")

    # ---- the test ------------------------------------------------------------------------

    def check_passthrough(self):
        """The hold must reach the carrier as a re-INVITE, which needs moh_passthrough on the
        airtel endpoint; without it Asterisk plays local music and sends nothing. Only the one
        attribute is read and logged (the endpoint also carries the line number)."""
        status, _, body = self.rest.call(
            "GET", "/asterisk/config/dynamic/res_pjsip/endpoint/airtel", http_timeout=5)
        value = None
        if status == 200 and isinstance(body, list):
            for item in body:
                if isinstance(item, dict) and item.get("attribute") == "moh_passthrough":
                    value = item.get("value")
        if value is None:
            self.log(f"setup: could not read moh_passthrough (HTTP {status or 'no answer'}). "
                     "Check it: docker exec home-phone asterisk -rx \"pjsip show endpoint airtel\"")
        elif value not in ("true", "yes"):
            raise SetupError(f"moh_passthrough is {value} on the airtel endpoint, so a hold would "
                             "play local music and send no re-INVITE. Recreate the container "
                             "with HOME_PHONE_ARI_PASSWORD set.")
        else:
            self.log(f"setup: airtel endpoint moh_passthrough={value}")

    def run(self):
        self.ws = Events(self.rest.auth)
        self.log("connected to ARI as app", APP)
        self.check_passthrough()
        self.set_flag("0", "setup: clear TESTD_WAIT")
        if self.opt.probe or self.opt.conf:
            self.domain = self.sip_domain()
            self.uris = self.conf_uris()
        if self.opt.probe:
            return self.probe()
        if self.opt.conf:
            what = ("the switching-order address" if self.opt.flash
                    else "the conference factory")
            self.log(f"D4: {what} for this run is {self.uris[0]}"
                     + (f" (D4 uses the first address only; {len(self.uris) - 1} more in the "
                        "list are not used)" if len(self.uris) > 1 else ""))
            if sys.stdin is None or not os.isatty(0):
                self.log("note: the terminal input is not a terminal. D4 asks up to three "
                         "questions there; with no answer the result is UNCLEAR.")
        self.log(f"ready for {self.mode}. Call the landline from a test number and press 4 "
                 "after the beep. Ctrl-C stops.")
        ev = self.wait_for(None, lambda e: e.get("type") == "StasisStart" and first_arg(e)
                           not in ("second", "incoming", "focus", "referred", "keycheck"))
        self.legs["leg1"] = channel_of(ev)["id"]
        self.required = {"leg1"}
        forward = first_arg(ev)
        self.subscribe("leg1")
        self.step = "D1a hold leg1"
        if not self.opt.wait_in and not re.fullmatch(r"\+?\d{6,15}", forward):
            self.fail("no usable forward number came from the dialplan. "
                      "Set HOME_PHONE_TEST_FORWARD in ../.env.", verdict="NOT RUN")
            self.play("tell leg1", "leg1", "beeperr")
            return
        if self.opt.conf and self.opt.in2:
            self.call_in_conf(forward)          # leg1 stays active until a key adds the caller
            return
        self.play("start: beep to leg1", "leg1", "beep", wait=3)
        self.hold_leg1("D1a")
        if self.opt.wait_in:
            self.call_in()
        else:
            self.dial_out(forward)

    def hold_leg1(self, tag, after=None):
        """Hold leg1 (and start our music on hold with --moh). `after` names the pause line."""
        self.must(f"{tag} hold leg1 (re-INVITE a=sendonly)", "POST",
                  f"/channels/{self.legs['leg1']}/hold")
        self.pause(after or tag)
        if self.opt.moh:
            ok, _ = self.ari("D2 start our music on hold to leg1", "POST",
                             f"/channels/{self.legs['leg1']}/moh", mohClass="default")
            self.moh_on = ok

    def dial_out(self, forward):
        self.step = "D1b second call"
        leg2 = "testd-" + uuid.uuid4().hex[:12]
        self.legs["leg2"] = leg2    # known before the originate, so its events get the label
        ok, _ = self.ari("D1b dial the forward number on the same line", "POST", "/channels",
                         endpoint=f"PJSIP/{forward}@airtel", app=APP, appArgs="second",
                         timeout=RING_TIMEOUT, channelId=leg2)
        if not ok:
            del self.legs["leg2"]
            self.fail("D1b: Asterisk refused the originate (see the ARI line above)")
            self.return_caller(failed=True)
            return
        ev = self.wait_for(RING_TIMEOUT + 15, lambda e: "leg2" in self.ended or (
            e.get("type") == "StasisStart" and self.label_of(channel_of(e).get("id")) == "leg2"))
        if ev is None:
            self.fail(f"D1b: the second call neither answered nor ended in {RING_TIMEOUT + 15} s")
            self.hangup("D1b give up on leg2", "leg2")
            self.return_caller(failed=True)
            return
        if "leg2" in self.ended:
            self.fail("D1b: the second call failed, " + cause_str(*self.ended["leg2"]))
            self.return_caller(failed=True)
            return
        self.log("D1b PASS: the second call was answered while leg1 was held")
        self.answered_at = time.monotonic()
        if self.opt.mix:
            self.required = {"leg1", "leg2"}
            self.mix(phone_order=True)
        elif self.opt.mix_direct:
            self.required = {"leg1", "leg2"}
            self.mix(phone_order=False)
        elif self.opt.conf:
            self.required = {"leg1", "leg2"}
            self.conference(forward)
        else:
            # leg2's part ends here anyway: if it hangs up first, note it and still run D1c.
            self.required = {"leg1"}
            self.step = "D1 hello to leg2"
            self.play("D1 play hello-world to leg2", "leg2", "hello-world")
            self.step = "D1 hang up leg2"
            early = None
            if not self.hangup("D1 hang up leg2", "leg2") and "leg2" in self.ended_at:
                early = self.ended_at["leg2"] - self.answered_at
                self.log(f"D1: leg2 ended on its own {early:.1f} s after it answered, "
                         + cause_str(*self.ended["leg2"]))
            self.pause("D1 after leg2 ended")
            self.return_caller(failed=False, leg2_early=early)

    def mix(self, phone_order):
        tag = "D3" if phone_order else "D3b"
        leg1, leg2 = self.legs["leg1"], self.legs["leg2"]
        n = 1
        if phone_order:
            self.step = "D3 step 1, hold leg2"
            self.must("D3 step 1: hold leg2 (re-INVITE a=sendonly)", "POST",
                      f"/channels/{leg2}/hold")
            self.pause("D3 step 1")
            n = 2
        self.stop_moh()
        self.step = f"{tag} step {n}, unhold leg1"
        self.must(f"{tag} step {n}: unhold leg1 (re-INVITE a=sendrecv)", "DELETE",
                  f"/channels/{leg1}/hold")
        self.pause(f"{tag} step {n}")
        if phone_order:
            self.step = "D3 step 3, unhold leg2"
            self.must("D3 step 3: unhold leg2 (re-INVITE a=sendrecv)", "DELETE",
                      f"/channels/{leg2}/hold")
            self.pause("D3 step 3")
        self.join(tag)

    def call_in(self):
        self.step = "E2 wait for a call in"
        if not self.set_flag("1", "E2: set TESTD_WAIT=1 so the next call in comes here"):
            self.fail("E2: ARI would not set TESTD_WAIT")
            self.return_caller(failed=True)
            return
        self.log(f"E2: ring the landline now from the forward number or another test "
                 f"number. Waiting {WAIT_IN} s.")
        ev = self.wait_for(WAIT_IN, lambda e: e.get("type") == "StasisStart"
                           and first_arg(e) == "incoming")
        self.set_flag("0", "E2: clear TESTD_WAIT")
        if ev is None:
            self.fail(f"E2: no call in reached the script in {WAIT_IN} s. Check the trace for "
                      "a second INVITE and Asterisk's answer to it.")
            self.return_caller(failed=True)
            return
        self.legs["leg2"] = channel_of(ev)["id"]
        self.log("E2: a call in arrived while leg1 was held")
        self.subscribe("leg2")
        self.step = "E2 answer leg2"
        ok, _ = self.ari("E2: answer leg2", "POST", f"/channels/{self.legs['leg2']}/answer")
        if not ok:
            self.fail("E2: could not answer the call in")
            self.hangup("E2 hang up leg2", "leg2")
            self.return_caller(failed=True)
            return
        self.required = {"leg1", "leg2"}
        self.pause("E2 answer")
        self.stop_moh()
        self.step = "E2 unhold leg1"
        self.must("E2: unhold leg1 (re-INVITE a=sendrecv)", "DELETE",
                  f"/channels/{self.legs['leg1']}/hold")
        self.pause("E2 unhold leg1")
        self.join("E2")

    # ---- Test D4 --in2: a second call in, in call-waiting order ----------------------------

    def is_call_in(self, ev):
        """A call in that the dialplan sent here while TESTD_WAIT=1 and that has no label."""
        return (ev.get("type") == "StasisStart" and first_arg(ev) == "incoming"
                and not self.label_of(channel_of(ev).get("id")))

    def refuse_other(self, ev, why):
        """Refuse a call in from another number with busy (cause 17; chan_pjsip should send
        486)."""
        number = re.sub(r"\D", "", str((channel_of(ev).get("caller") or {}).get("number") or ""))
        self.in2["others"] += 1
        self.log(f"D4-in2: the call in from a number ending in {number[-4:] or '(none)'} {why}: "
                 "refusing it (busy)")
        self.ari("D4-in2: refuse that call (busy)", "DELETE",
                 f"/channels/{channel_of(ev).get('id')}", reason="busy")

    def call_in_conf(self, forward):
        """D4 --in2, in the order Ganpath will use. leg1 is an active call and stays so. The
        script waits for a call in from the forward number (the dialplan sends a test number or
        the forward number here while TESTD_WAIT=1, as for E2) and does not answer it: it
        asks Asterisk to send it ringing and lets decide() ask leg1. A call in from another
        test number is refused with busy and the wait goes on. TESTD_WAIT goes back to 0 once
        leg2 rings, so after that no other call in reaches the script."""
        s = self.in2 = {"keys": [], "others": 0, "arrived": None, "ring_at": None, "ring": "",
                        "key": None, "key_at": None, "other_keys": [], "leg2_keys": [],
                        "answered": False}
        self.step = "D4-in2 wait for a call in"
        if not self.set_flag("1", "D4-in2: set TESTD_WAIT=1 so the next call in comes here"):
            self.fail("D4-in2: ARI would not set TESTD_WAIT")
            self.play("D4-in2: play beeperr to leg1", "leg1", "beeperr")
            return
        # Not waited for: a call in that came during the beep would be sent back.
        self.play("D4-in2: beep to leg1: the forward number may call now", "leg1", "beep", wait=0)
        want = re.sub(r"\D", "", forward)[-10:]
        self.log(f"D4-in2: stay on the call on the test phone. The forward-number owner calls the "
                 f"landline now. Waiting {WAIT_IN} s.")
        end, ev = time.monotonic() + WAIT_IN, None
        while ev is None:
            left = end - time.monotonic()
            if left <= 0:
                break
            ev = self.wait_for(left, self.is_call_in)
            if ev is None:
                break
            number = re.sub(r"\D", "", str((channel_of(ev).get("caller") or {}).get("number")
                                           or ""))
            if number.endswith(want):
                break
            self.refuse_other(ev, "is not the forward number")
            ev = None
        if ev is None:
            self.set_flag("0", "D4-in2: clear TESTD_WAIT")
            refused = s["others"]
            self.fail(f"D4-in2: no call in from the forward number reached the script in "
                      f"{WAIT_IN} s"
                      + (f" ({refused} call(s) in from another number were refused)" if refused
                         else "")
                      + ". Check the trace for a second INVITE and Asterisk's answer to it.")
            self.play("D4-in2: play beeperr to leg1", "leg1", "beeperr")
            return
        s["arrived"] = time.monotonic()
        self.legs["leg2"] = channel_of(ev)["id"]
        self.step = "D4-in2 ring leg2"
        ok, _ = self.ari("D4-in2: ring leg2 (Asterisk sends Airtel 180 or 183); it is not "
                         "answered", "POST",
                         f"/channels/{self.legs['leg2']}/ring")
        s["ring_at"] = time.monotonic()
        if not ok:
            s["ring"] = self.last_error
            self.log("D4-in2: Asterisk refused the ring, so it has sent Airtel no 180 or 183 for "
                     "leg2; the script waits for a key all the same")
        self.log(f"D4-in2: the forward number called in while leg1 was on an active call; leg2 "
                 + ("is waiting unanswered, with no ring sent," if s["ring"] else
                    "was sent ringing")
                 + f" from {time.strftime('%H:%M:%S')}")
        self.subscribe("leg2")
        self.set_flag("0", "D4-in2: clear TESTD_WAIT")
        self.decide(forward)

    def decide(self, forward):
        """--in2: leg2 rings. Ask leg1 with three beeps and wait up to --in2-wait s for 1 (add
        the caller) or 2 (refuse). Only leg1's keys count; leg2's are logged and ignored.
        TESTD_WAIT is 0 by now, so the dialplan sends no other call here; one that got in
        before it was cleared is sent back to the stage-1 dialplan by wait_for().
        Key times are taken when the script reads the event, so a key pressed during an ARI
        call (the ring, the subscribe, the beeps) is stamped up to that call's length late."""
        s, secs = self.in2, self.opt.in2_wait
        self.step = "D4-in2 ask leg1"
        self.play("D4-in2: three beeps to leg1: a call is waiting", "leg1", IN2_CUE, wait=0)
        self.log(f"D4-in2: a call is waiting. Press 1 on the test phone to add the caller, 2 to "
                 f"refuse. Waiting {secs} s.")
        self.step = "D4-in2 wait for a key from leg1"
        end, seen = s["ring_at"] + secs, 0
        while s["key"] is None:
            while seen < len(s["keys"]) and s["key"] is None:
                label, digit, at = s["keys"][seen]
                seen += 1
                after = at - s["ring_at"]
                if after < 0:
                    self.log(f"D4-in2: key {digit} from {label} came before the call was waiting: "
                             "not counted")
                elif label != "leg1":
                    s["leg2_keys"].append(digit)
                    self.log(f"D4-in2: key {digit} from {label} {after:.1f} s after the ring: "
                             "ignored, only leg1 decides")
                elif digit in ("1", "2"):
                    s["key"], s["key_at"] = digit, after
                    self.log(f"D4-in2: key {digit} from leg1 {after:.1f} s after the ring: "
                             + ("adding the caller" if digit == "1" else "refusing the caller"))
                else:
                    s["other_keys"].append(digit)
                    self.log(f"D4-in2: key {digit} from leg1 {after:.1f} s after the ring: not 1 "
                             "or 2, waiting on")
            if s["key"] is not None or "leg2" in self.ended:
                break
            left = end - time.monotonic()
            if left <= 0:
                self.log(f"D4-in2: no 1 or 2 from leg1 in {secs} s")
                break
            self.wait_for(left, lambda e: "leg2" in self.ended or len(s["keys"]) > seen)
        if "leg2" in self.ended:
            self.rang_out(held=False)
        elif s["key"] == "1":
            self.add_caller(forward)
        else:
            self.refuse_caller()

    def add_caller(self, forward):
        """--in2, key 1: hold leg1, answer leg2, then D4 from the hello-world to leg2."""
        s = self.in2
        self.required = {"leg1"}
        self.step = "D4-in2 hold leg1"
        self.hold_leg1("D4-in2", after="D4-in2 hold leg1")
        if "leg2" in self.ended:
            self.rang_out(held=True)
            return
        self.step = "D4-in2 answer leg2"
        ok, _ = self.ari("D4-in2: answer leg2", "POST", f"/channels/{self.legs['leg2']}/answer")
        if not ok:
            if "leg2" not in self.ended:
                self.wait_for(1.0, lambda e: "leg2" in self.ended)
            if "leg2" in self.ended:
                self.rang_out(held=True)
                return
            self.fail(f"D4-in2: could not answer the call in ({self.last_error})")
            self.hangup("D4-in2: refuse leg2 with busy (cause 17)", "leg2", reason="busy")
            self.return_caller(failed=True)
            return
        s["answered"] = True
        self.answered_at = time.monotonic()
        self.log(f"D4-in2: leg2 answered {self.answered_at - s['ring_at']:.1f} s after the ring")
        self.required = {"leg1", "leg2"}
        self.pause("D4-in2 answer")
        self.conference(forward)

    def keys_text(self):
        """--in2: which keys came after the ring, for the RESULT."""
        s = self.in2
        parts = []
        if s["other_keys"]:
            parts.append(f"other keys from leg1: {' '.join(s['other_keys'])}")
        if s["leg2_keys"]:
            parts.append(f"keys from leg2, ignored: {' '.join(s['leg2_keys'])}")
        if s["others"]:
            parts.append(f"{s['others']} call(s) in from another number refused with busy")
        if s["ring"]:
            parts.append(f"Asterisk refused the ring ({s['ring']}), so it sent Airtel no 180 "
                         "or 183")
        return "; ".join(parts)

    def refuse_caller(self):
        """--in2, key 2 or no key in time: refuse leg2 with busy, tell leg1, ask what the
        forward phone heard."""
        s, secs = self.in2, self.opt.in2_wait
        self.required = set()               # from here leg1 may hang up
        self.step = "D4-in2 refuse leg2"
        if s["key"] == "2":
            how = (f"key 2 from leg1 {s['key_at']:.1f} s after the ring (keys reach the script "
                   "on an active call)")
        else:
            how = (f"no 1 or 2 from leg1 in {secs} s"
                   + (" (if 1 or 2 was pressed, keys do not reach the script on an active call)"
                      if not s["other_keys"] else ""))
        at = time.monotonic() - s["ring_at"]
        refused = self.hangup("D4-in2: refuse leg2 with busy (cause 17)", "leg2", reason="busy")
        if not refused and "leg2" in self.ended:
            self.rang_out(held=False)       # it ended on its own just before the refusal
            return
        if not refused and "leg2" not in self.ended:
            self.fail(f"D4-in2: {how}, but Asterisk would not refuse leg2 ({self.last_error})",
                      verdict="STOPPED")
            return
        if "leg1" not in self.ended:
            self.play("D4-in2: two beeps to leg1: the waiting call was refused", "leg1",
                      IN2_REFUSED)
        self.step = "D4-in2 question"
        heard = self.ask("What did the forward phone hear, while it rang and after the refusal? A "
                         "few words, for example: ringing, then a busy tone. Or press Enter to "
                         "skip.")
        extra = self.keys_text()
        end = (self.end_str("leg2") if "leg2" in self.ended
               else "ARI showed no end of leg2 within 5 s, see the trace")
        self.passed(f"{how}: the script asked Asterisk to refuse the waiting call with busy "
                    f"(cause 17; chan_pjsip should send 486, confirm in the trace) {at:.1f} s "
                    f"after the ring; {end}. The forward phone heard: "
                    f"{heard or 'not given'}." + (f" {extra[:1].upper()}{extra[1:]}." if extra
                                                  else ""), verdict="REFUSED")

    def rang_out(self, held):
        """--in2: leg2 ended while it rang, before the script answered or refused it. Give leg1
        back (held: it was already on hold for the merge) and ask who hung up."""
        s = self.in2
        if "leg2" in self.subscribed and self.end_how.get("leg2") != "destroyed":
            # Its ChannelDestroyed, which carries the cause text, follows the StasisEnd.
            self.wait_for(1.0, lambda e: self.end_how.get("leg2") == "destroyed")
        secs = self.ended_at["leg2"] - s["arrived"]
        cause, text = self.ended["leg2"]
        why = (cause_str(cause, text) if cause is not None
               else END_HOW.get(self.end_how.get("leg2"), "ARI gave no cause"))
        self.log(f"D4-in2: leg2 ended while it rang, {secs:.1f} s after it reached the script "
                 f"({why})")
        self.required = set()
        if held:
            self.return_caller(failed=True)
        elif "leg1" not in self.ended:
            self.play("D4-in2: play beeperr to leg1", "leg1", "beeperr")
        self.step = "D4-in2 question"
        hung = self.ask_yn("Did the forward-number owner hang up while the call rang? y/n")
        base = (f"D4-in2: leg2 ended while it rang, {secs:.1f} s after it reached the script and "
                + ("after key 1 from leg1, " if held else "")
                + f"before the script answered it ({why}), so nothing was merged")
        extra = self.keys_text()
        base += f"; {extra}" if extra else ""
        if hung == "y":
            self.fail(f"{base}. The forward-number owner hung up.", verdict="STOPPED")
        elif hung == "n":
            self.passed(f"{base}. The forward-number owner did not hang up, so something else "
                        f"ended the waiting call after {secs:.1f} s: a CANCEL from the carrier "
                        "side or the caller's network, or Asterisk itself. The trace shows "
                        "which, with the CANCEL's Reason header.", verdict="UNCLEAR")
        else:
            self.passed(f"{base}. Nobody said at the terminal whether the forward-number owner "
                        "hung up: the trace shows who sent the CANCEL.", verdict="UNCLEAR")

    def in2_text(self):
        """--in2 once key 1 has had leg2 answered: the call-waiting sentence for the RESULT."""
        s = self.in2
        extra = self.keys_text()
        how = ("waited unanswered, with no ring sent," if s["ring"] else "was sent ringing")
        return (f"Call waiting: leg2 {how} until key 1 from leg1 {s['key_at']:.1f} s after the "
                "ring (keys reach the script on an active call)"
                + (f"; {extra}" if extra else "") + ".")

    def join(self, tag):
        self.step = f"{tag} join"
        ok, body = self.ari(f"{tag}: create a mixing bridge", "POST", "/bridges",
                            type="mixing,dtmf_events", name="testd")
        if not ok or not body.get("id"):
            self.fail(f"{tag}: Asterisk would not create the bridge")
            return
        self.bridge = body["id"]
        ok, _ = self.ari(f"{tag}: add leg1 and leg2", "POST", f"/bridges/{self.bridge}/addChannel",
                         channel=f"{self.legs['leg1']},{self.legs['leg2']}",
                         absorbDTMF="true", inhibitConnectedLineUpdates="true")
        if not ok:
            self.fail(f"{tag}: Asterisk would not add both legs to the bridge")
            return
        start = time.monotonic()
        self.step = f"{tag} 3-way"
        try:
            self.play_bridge(f"{tag}: play hello-world to both", "hello-world")
            self.log(f"{tag}: 3-way up. Talk on both phones. * plays a beep, # ends the test.")
            while True:
                left = TALK_LIMIT - (time.monotonic() - start)
                ev = self.wait_for(left, lambda e: e.get("type") == "ChannelDtmfReceived"
                                   and self.label_of(channel_of(e).get("id")) in ("leg1", "leg2"))
                if ev is None:
                    ended_by, clean = f"the {TALK_LIMIT} s limit", True
                    break
                label, digit = self.label_of(channel_of(ev)["id"]), ev.get("digit")
                if digit in ("*", "#"):
                    self.dtmf_from.add(label)
                if digit == "*":
                    self.play_bridge(f"{tag}: * from {label}, beep to both", "beep", wait=0)
                elif digit == "#":
                    ended_by, clean = f"# from {label}", True
                    break
        except CallEnded as e:
            ended_by = f"{e.leg} hanging up ({cause_str(e.cause, e.text)})"
            clean = time.monotonic() - start >= 10
        except KeyboardInterrupt:
            ended_by, clean = "Ctrl-C", True
        talk = int(time.monotonic() - start)
        keys = ", ".join(sorted(self.dtmf_from)) or "neither leg"
        held = [label for label in ("leg1", "leg2") if label in self.net_held]
        if held:
            self.fail(f"the carrier kept {' and '.join(held)} on hold from its side, so only one "
                      f"call had two-way audio. The 3-way ran {talk} s, ended by {ended_by}. "
                      f"Keys arrived from {keys}. Check the trace: the carrier's SDP for that "
                      "leg carries a=sendonly or a=inactive.")
            return
        detail = (f"both legs stayed up through the re-INVITEs and the 3-way ran {talk} s, "
                  f"ended by {ended_by}. Keys arrived from {keys}. "
                  "Note whether each phone heard the other and hello-world.")
        if clean:
            self.passed(detail)
        else:
            self.passed(detail + " A leg ended within 10 s of the join: check the trace "
                        "for who sent the BYE.", verdict="UNCLEAR")

    # ---- Test D4: setup ------------------------------------------------------------------

    def sip_domain(self):
        """The SIP_DOMAIN global from extensions.conf (HOME_PHONE_SIP_DOMAIN in ../.env)."""
        status, reason, body = self.rest.call("GET", "/asterisk/variable", http_timeout=5,
                                              variable="SIP_DOMAIN")
        value = str(body.get("value") or "").strip() if status == 200 else ""
        if not value:
            got = "it is empty" if status == 200 else f"HTTP {status or 'no answer'} {reason}"
            raise SetupError(f"could not read the SIP_DOMAIN global ({got}). It comes from "
                             "HOME_PHONE_SIP_DOMAIN in ../.env.")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", value):
            raise SetupError("the SIP_DOMAIN global is not a plain domain name")
        self.log(f"setup: SIP domain {value}")
        return value

    def conf_uris(self):
        """The conference factory addresses for this run, placeholders filled in and checked.
        Nothing is dialled when one of them is malformed."""
        o = self.opt
        env_uris = os.environ.get("HOME_PHONE_TEST_CONF_URIS", "").split()
        if o.conf_uri:
            templates, source = list(o.conf_uri), "--conf-uri"
        elif o.conf and o.flash:            # factory addresses in the shell do not apply
            templates, source = [FLASH_URI], "--flash"
        elif env_uris:
            templates, source = env_uris, "HOME_PHONE_TEST_CONF_URIS"
        else:
            templates, source = list(DEFAULT_CONF_URIS), "the default list"
        if o.probe and o.wide:
            templates += list(WIDE_CONF_URIS)
            source += " and --wide"
        values = {"domain": self.domain}
        labels = self.domain.split(".")
        if len(labels) >= 3:
            values["parent"] = ".".join(labels[1:])
        plmn = o.plmn or os.environ.get("HOME_PHONE_TEST_PLMN", "").strip()
        if plmn:
            m = PLMN_OK.fullmatch(plmn)
            if not m:
                raise SetupError("HOME_PHONE_TEST_PLMN must be MCC-MNC: three digits, a dash, "
                                 "then two or three digits")
            values["mcc"], values["mnc"] = m.group(1), m.group(2).zfill(3)
        uris = []
        for template in templates:
            names = re.findall(r"\{(\w+)\}", template)
            unknown = [n for n in names if n not in PLACEHOLDERS]
            if unknown:
                raise SetupError(f"{template}: unknown placeholder {{{unknown[0]}}}. The script "
                                 "knows {domain}, {parent}, {mcc} and {mnc}.")
            missing = [n for n in names if n not in values]
            if missing:
                why = ("it needs --plmn or HOME_PHONE_TEST_PLMN" if missing[0] in ("mcc", "mnc")
                       else "the SIP domain has fewer than 3 labels, so there is no {parent}")
                self.log(f"setup: skipping {template}: {why}")
                continue
            uri = re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], template)
            if not URI_OK.fullmatch(uri):
                raise SetupError(f"{uri!r} is not a SIP URI the script can dial (sip: or sips:, "
                                 "no spaces, quotes, brackets, commas, braces or backslashes). "
                                 "Nothing was dialled.")
            if uri not in uris:
                uris.append(uri)
        if not uris:
            raise SetupError("no conference factory address is left to try (see the skipped "
                             "lines above)")
        kind = "switching-order" if o.conf and o.flash else "conference factory"
        self.log(f"setup: {len(uris)} {kind} address(es) from {source}")
        return uris

    # ---- Test D4: reading a channel --------------------------------------------------------

    def chan_var(self, step, label, expr, quiet=False):
        """A channel variable or dialplan function, read through ARI (a name ending in ')' is
        evaluated with ast_func_read2). The value, or None when Asterisk has none."""
        status, reason, body = self.rest.call("GET", f"/channels/{self.legs[label]}/variable",
                                              http_timeout=5, variable=expr)
        value = str(body.get("value", "")) if status == 200 else None
        if not quiet or value is None:
            shown = (repr(value) if value is not None
                     else f"none (HTTP {status or 'no answer'} {reason})")
            self.log(f"{step}: {expr} on {label} = {shown}")
        return value

    def read_focus(self, label, step):
        """The 200 OK to the factory INVITE: its Contact (;isfocus marks a conference focus,
        and the URI inside <> is the conference URI) and a few headers for the record.
        PJSIP_RESPONSE_HEADER keeps only the initial INVITE's 200 OK."""
        f = {"contact": self.chan_var(step, label, "PJSIP_RESPONSE_HEADER(read,Contact)"),
             "target": self.chan_var(step, label, "CHANNEL(pjsip,target_uri)")}
        for name in ("Allow", "Supported", "Server", "Record-Route"):
            f[name] = self.chan_var(step, label, f"PJSIP_RESPONSE_HEADER(read,{name})")
        contact = f["contact"] or ""
        if not contact:
            f["isfocus"], f["conf"] = "unreadable", f["target"] or ""
        else:
            f["isfocus"] = "yes" if ISFOCUS.search(contact) else "no"
            m = re.search(r"<([^<>]*)>", contact)
            f["conf"] = (m.group(1) if m else contact.split(";", 1)[0]).strip()
        return f

    def read_dialog(self, label):
        """Call-ID, tags, remote URI and remote target of a leg, for Replaces."""
        d = {}
        for key, item in (("call_id", "call-id"), ("local_tag", "local_tag"),
                          ("remote_tag", "remote_tag"), ("remote_uri", "remote_uri"),
                          ("target", "target_uri")):
            d[key] = self.chan_var(f"D4 {label} dialog", label, f"CHANNEL(pjsip,{item})",
                                   quiet=True) or ""
        if not (d["call_id"] and d["local_tag"] and d["remote_tag"]):
            raise Refused(f"Asterisk gave no Call-ID or tag for {label}, so no Replaces could be "
                          "built; nothing was sent to the carrier for it")
        self.log(f"D4 {label} dialog: Call-ID {d['call_id']}, ours {d['local_tag']}, theirs "
                 f"{d['remote_tag']}, remote {d['remote_uri']}, contact {d['target']}")
        return d

    def party_uri(self, d):
        """(the party's SIP URI, its user part) from the dialog's remote URI: the URI inside <>
        without ?headers; a tel URI becomes sip:<number>@<domain>;user=phone."""
        raw = d["remote_uri"]
        m = re.search(r"<([^<>]*)>", raw)
        uri = (m.group(1) if m else raw).strip().split("?", 1)[0]
        if uri.lower().startswith("tel:"):
            number = uri[4:].split(";", 1)[0]
            return f"sip:{number}@{self.domain};user=phone", number
        m = re.match(r"(?i)sips?:([^@;]*)", uri)
        return uri, (m.group(1) if m else "")

    @staticmethod
    def replaces(d):
        """The Replaces value for one of our dialogs: Call-ID;to-tag=theirs;from-tag=ours.

        RFC 3891 section 3 defines the tags relative to the dialog as its recipient holds it,
        not by the To and From headers of the first INVITE: the recipient matches them "as if
        they were tags present in an incoming request", so to-tag is compared with its local
        tag and from-tag with its remote tag. The recipient of the focus's INVITE with
        Replaces is Airtel's side of our call, whose local tag is our remote tag. So to-tag is
        always Airtel's tag and from-tag always ours, whoever placed the call:
        - leg2 dialled out (D4): ours is the From tag of our INVITE, Airtel's the To tag of its
          200 OK;
        - leg1, or leg2 with --in2 (calls in): ours is the To tag of our 200 OK, Airtel's the
          From tag of its INVITE.
        PJSIP's local_tag and remote_tag (CHANNEL(pjsip,...)) are the dialog's own tags in
        both directions, so one formula is right for both."""
        return urllib.parse.quote(f"{d['call_id']};to-tag={d['remote_tag']};"
                                  f"from-tag={d['local_tag']}", safe="")

    @staticmethod
    def refer_ok(refer_to):
        return bool(REFER_TO_OK.fullmatch(refer_to)
                    and URI_OK.fullmatch(refer_to[1:-1].split("?", 1)[0]))

    # ---- Test D4: calls --------------------------------------------------------------------

    def originate_uri(self, label, uri, step):
        """Call a SIP URI through the airtel endpoint (Request-URI and To are the URI; Route,
        From and auth come from the endpoint). ('answered', None), ('failed', (cause, text)),
        ('refused', the ARI error) or ('timeout', None)."""
        cid = "testd-" + uuid.uuid4().hex[:12]
        self.legs[label] = cid      # known before the originate, so its events get the label
        ok, _ = self.ari(step, "POST", "/channels", body=ICSI_BODY if self.opt.icsi else None,
                         endpoint="PJSIP/airtel/" + uri, app=APP, appArgs="focus",
                         timeout=FOCUS_RING, channelId=cid)
        if not ok:
            error = self.last_error
            del self.legs[label]
            return "refused", error
        ev = self.wait_for(FOCUS_RING + 10, lambda e: label in self.ended or (
            e.get("type") == "StasisStart" and self.label_of(channel_of(e).get("id")) == label))
        if ev is None:
            self.hangup(f"{step}: give up on {label}", label)
            return "timeout", None
        if label in self.ended:
            return "failed", self.ended[label]
        return "answered", None

    def refer(self, on, refer_to, moved, step):
        """Send one REFER inside `on`'s own dialog through [testd-refer] and wait for the result.

        `moved` is the leg whose end is expected (None when the focus calls the party afresh).
        Returns a dict with status SUCCESS or FAILURE (from Transfer()), ENDED (`on` itself
        ended with no result: its dialog was replaced), GONE (leg3 ended), TIMEOUT or NOT SENT;
        code (TRANSFERSTATUSPROTOCOL); secs from the REFER to the result; at, when it came.
        """
        cid = self.legs[on]
        out = {"status": "NOT SENT", "code": None, "secs": 0.0, "at": time.monotonic(),
               "error": ""}
        self.log(f"{step}: Refer-To {refer_to}")
        ok, _ = self.ari(f"{step}: set D4_REFER_TO on {on}", "POST", f"/channels/{cid}/variable",
                         variable="D4_REFER_TO", value=refer_to)
        if not ok:
            return out
        start = time.monotonic()
        # Its StasisEnd on the way to [testd-refer] is not an end, and while it runs the REFER
        # its end is a result (ENDED or GONE) for this step, so it is not required meanwhile.
        self.outside.add(on)
        required = on in self.required
        self.required.discard(on)
        try:
            # By label: ARI checks a label exists (404 if not), and does not check a priority.
            # An image built before [testd-refer] then stops here with nothing sent.
            ok, _ = self.ari(f"{step}: send the REFER inside {on}'s dialog ({on} runs "
                             "testd-refer)", "POST", f"/channels/{cid}/continue",
                             context="testd-refer", extension="s", label="refer")
            if not ok:
                if self.last_status:            # refused, so it never left the app
                    self.outside.discard(on)
                out["error"] = self.last_error
                return out
            end = start + REFER_WAIT

            def back(e):
                return (e.get("type") == "StasisStart" and channel_of(e).get("id") == cid
                        and first_arg(e) == "referred")
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    out.update(status="TIMEOUT", secs=float(REFER_WAIT), at=time.monotonic())
                    self.log(f"{step}: no REFER result in {REFER_WAIT} s")
                    return out
                ev = self.wait_for(min(1.0, left), lambda e: back(e) or on in self.ended)
                now = time.monotonic()
                if ev is not None and back(ev):
                    args = [str(a) for a in (ev.get("args") or [])] + ["", "", ""]
                    status = args[1] or "?"
                    code = int(args[2]) if re.fullmatch(r"-?\d+", args[2]) else None
                    out.update(status=status, code=code, secs=now - start, at=now)
                    note = ""
                    if status == "SUCCESS" and moved and now - start > 4:
                        note = ": the focus probably rang the phone afresh"
                    elif status == "FAILURE":
                        note = f" ({REFER_HINT.get(code, 'see the NOTIFY sipfrag in the trace')})"
                    self.log(f"{step}: {on} is back with {status} {code} after "
                             f"{now - start:.1f} s{note}")
                    return out
                self.poll_outside()
                if on in self.ended:
                    out.update(status="ENDED" if on == moved else "GONE", secs=now - start,
                               at=now)
                    return out
        finally:
            if required and on not in self.ended:
                self.required.add(on)

    def refer_targets(self, party, d, dropped, focus, forward):
        """([(variant, Refer-To), ...], the leg the REFER goes in, the leg expected to end)."""
        if party in dropped:
            if party == "leg2":
                number = forward
            else:
                user = self.party_uri(d)[1]
                number = user if NUMBER_OK.fullmatch(user) else ""
            if not number:
                self.log(f"D4: {party}'s remote URI has no number, so the focus cannot be asked "
                         "to call it")
                return [], "leg3", None
            return [("dial-out", f"<sip:{number}@{self.domain}>")], "leg3", None
        r = self.replaces(d)
        if self.opt.refer_in == "legs":
            conf = focus["conf"]
            return ([("bridge+replaces", f"<{conf}?Replaces={r}>"), ("bridge", f"<{conf}>")],
                    party, party)
        uri, user = self.party_uri(d)
        targets = []
        if NUMBER_OK.fullmatch(user):
            targets.append(("aor", f"<{uri};method=INVITE?Replaces={r}&Require=replaces>"))
        else:
            self.log(f"D4: {party}'s remote URI has no number as its user part, so the aor "
                     "variant is skipped")
        targets.append(("contact", f"<{d['target']};method=INVITE?Replaces={r}"
                                   "&Require=replaces>"))
        return targets, "leg3", party

    def move_parties(self, dialogs, dropped, focus, forward):
        """REFER leg2, then leg1, into the conference. A line per party for the result, or
        None after a failure (already recorded)."""
        moves, lead = [], None
        for party in ("leg2", "leg1"):
            if party == "leg1":
                # Let leg2's REFER subscription close first (Asterisk may still be sending its
                # SUBSCRIBE Expires: 0), so a NOTIFY without ;id cannot be matched to it.
                self.pause("D4 before leg1's REFER")
            self.required.discard(party)        # its end is now expected
            if party == "leg1":
                self.stop_moh()
            targets, on, moved = self.refer_targets(party, dialogs[party], dropped, focus,
                                                    forward)
            if lead:
                targets.sort(key=lambda t: t[0] != lead)   # what worked for leg2 goes first
            result, failures = None, []
            for name, refer_to in targets:
                step = f"D4 REFER for {party} ({name})"
                if not self.refer_ok(refer_to):
                    self.log(f"{step}: skipped: the Refer-To is not a SIP URI that Transfer() "
                             "can carry")
                    continue
                self.step = step
                result = self.refer(on, refer_to, moved, step)
                result["variant"] = name
                if result["status"] in ("SUCCESS", "ENDED"):
                    lead = name
                    break
                if result["status"] == "FAILURE":
                    failures.append(f"{name} {result['code']}")
                    if result["code"] not in STOP_FORMS:
                        continue                # the next variant
                break
            if result is None:
                self.fail(f"D4: no Refer-To could be built for {party}", verdict="STOPPED")
                return None
            if result["status"] not in ("SUCCESS", "ENDED"):
                detail, verdict = self.refer_failure(party, result, failures, on, moved)
                self.fail(detail, verdict=verdict)
                return None
            moves.append(self.after_refer(party, result, moved))
        return moves

    def refer_failure(self, party, result, failures, on, moved):
        status, code, name = result["status"], result["code"], result["variant"]
        if status == "NOT SENT":
            text = (f"D4: Asterisk refused to start the REFER for {party} (see the ARI line "
                    "above); nothing reached the carrier for it.")
            if "label" in result.get("error", "").lower():
                text += (" The running image has no [testd-refer]: rebuild it (docker compose "
                         "--env-file ../.env up -d --build) and check dialplan show testd-refer.")
            return text, "STOPPED"
        if status == "GONE":
            cause = self.ended[on][0]
            return (f"D4: leg3 has gone during the REFER for {party} ({name}): the focus dropped "
                    "us" + (f", {cause_str(*self.ended[on])}" if cause is not None else ""),
                    "FAIL")
        if status == "TIMEOUT":
            text = (f"D4: no REFER result for {party} ({name}) in {REFER_WAIT} s. The focus took "
                    "the REFER but sent no final NOTIFY, or it follows RFC 4488 without "
                    "Refer-Sub: false.")
            if moved and moved != on and moved in self.ended:
                text += (f" {moved} did end {self.ended_at[moved] - result['at'] + REFER_WAIT:.1f}"
                         " s after the REFER, so the move may have worked: see the trace.")
            self.hangup(f"D4: give up on the REFER, hang up {on}", on)
            return text, "FAIL"
        if code in REFUSED_REFER:
            text = (f"D4: the REFER for {party} ({name}) was refused, {code}: "
                    f"{REFER_HINT.get(code, 'see the trace')}. Its Allow and Server headers in "
                    "the trace show whether the focus or the P-CSCF refused it.")
            if len(failures) > 1:
                text += f" Forms tried: {', '.join(failures)}."
            if self.opt.refer_in == "focus" and name != "dial-out":
                text += " Next run: --refer-in legs."
            return text, "FAIL"
        text = f"D4: every Refer-To for {party} failed ({', '.join(failures)})."
        codes = {f.rsplit(" ", 1)[1] for f in failures}
        if codes == {"481"} and self.opt.refer_in == "focus":
            text += (" Replaces did not match: the P-CSCF probably rewrites dialog IDs. "
                     "Next run: --dial-out both.")
        return text, "FAIL"

    def after_refer(self, party, result, moved):
        """After a REFER that worked: wait for Airtel to end the old call, or release it."""
        name, secs = result["variant"], result["secs"]
        if moved is None:
            return f"{party} ({name}): SUCCESS after {secs:.1f} s, the focus calls it afresh"
        if result["status"] == "ENDED":
            self.log(f"D4: {party} ended after its REFER (no NOTIFY; see the 202 in the trace)")
            return f"{party} ({name}): {party} ended {secs:.1f} s after its REFER"
        if moved not in self.ended:
            # StasisEnd or ChannelDestroyed, whichever ARI sends (note() marks either).
            self.wait_for(BYE_WAIT, lambda e: moved in self.ended)
        if moved in self.ended:
            gap = self.ended_at[moved] - result["at"]
            when = (f"{gap:.1f} s after the result" if gap >= 0
                    else f"{-gap:.1f} s before the result")
            self.log(f"D4: {self.end_str(moved)}: Airtel replaced {moved} {when}")
            return f"{party} ({name}): SUCCESS after {secs:.1f} s, Airtel ended {party} {when}"
        self.log(f"D4: Airtel did not end {moved} within {BYE_WAIT} s of the result")
        if self.hangup(f"D4: release {moved} after the NOTIFY 200 (TS 24.147 5.3.1.3.3)", moved):
            return (f"{party} ({name}): SUCCESS after {secs:.1f} s; Airtel did not end {party}, "
                    "so the script released it")
        if self.end_how.get(moved) == "404":
            return (f"{party} ({name}): SUCCESS after {secs:.1f} s; ARI showed no end of {party} "
                    f"within {BYE_WAIT} s, and it had gone when the script went to release it "
                    "(see the trace for its BYE)")
        return (f"{party} ({name}): SUCCESS after {secs:.1f} s; Airtel did not end {party}, and "
                f"the script could not release it ({self.last_error})")

    def recover(self):
        """After a D4 failure: leave the conference, drop leg2, and give the caller back."""
        self.step = "D4 recover"
        for label in ("leg3", "leg2"):
            if label in self.legs and label not in self.ended:
                self.hangup(f"D4 recover: hang up {label}", label)
        if "leg1" not in self.legs or "leg1" in self.ended:
            return
        if "leg1" in self.outside:
            self.hangup("D4 recover: hang up leg1 (it is still inside its REFER)", "leg1")
            return
        self.return_caller(failed=True)

    # ---- Test D4: the terminal -------------------------------------------------------------

    def typed_line(self):
        """The next line typed at the terminal (stripped), or None if there is none yet.
        Sets stdin_eof at the end of input."""
        while "\n" not in self.typed and not self.stdin_eof:
            try:
                ready, _, _ = select.select([0], [], [], 0)
                if not ready:
                    return None
                data = os.read(0, 1024)
            except (OSError, ValueError):
                data = b""
            if data:
                self.typed += data.decode("utf-8", "replace")
            else:
                self.stdin_eof = True
        if "\n" in self.typed:
            line, self.typed = self.typed.split("\n", 1)
            return line.strip()
        if self.typed:                          # the last line had no newline
            line, self.typed = self.typed, ""
            return line.strip()
        return None

    def ask(self, question, limit=ASK_WAIT, talk=False, until=None):
        """One line from the terminal (lower case), while ARI events keep being handled. None at
        the end of input, after `limit` s, or once until() is true. In the talk, # on leg3 ends
        it (returns '#') and * plays a beep into the conference."""
        if question:
            self.log(question)
        end = time.monotonic() + limit
        while True:
            if until is not None and until():
                return None
            line = self.typed_line()
            if line is not None:
                self.log(f"typed: {line!r}" if line else "typed: Enter")
                return line.lower()
            if self.stdin_eof:
                self.log("no answer: the terminal input has ended")
                return None
            left = end - time.monotonic()
            if left <= 0:
                self.log(f"the {limit} s talk limit has passed" if talk
                         else f"no answer in {limit} s")
                return None
            ev = self.wait_for(min(0.25, left), lambda e: e.get("type") == "ChannelDtmfReceived"
                               and self.label_of(channel_of(e).get("id")) == "leg3")
            if ev is None:
                continue
            digit = str(ev.get("digit"))
            if digit == "*" and "leg3" not in self.ended:
                self.play("D4: * on leg3, beep into the conference", "leg3", "beep", wait=0)
            elif digit == "#" and talk:
                self.log("D4: # on leg3 ends the talk")
                return "#"

    def ask_yn(self, question):
        prompt = question
        while True:
            answer = self.ask(prompt)
            if answer is None:
                return None
            if answer[:1] in ("y", "n"):
                return answer[:1]
            prompt = "Please type y or n. " + question

    # ---- Test D4a: the probe ---------------------------------------------------------------

    def probe(self):
        uris, n = self.uris, len(self.uris)
        self.log(f"D4a: {n} address(es) to try, one call at a time, {PROBE_GAP} s apart. "
                 "No phone rings.")
        found, unclear, failed = [], [], []
        for i, uri in enumerate(uris, 1):
            label = f"try{i}"
            self.step = f"D4a {i}/{n} INVITE"
            self.log(f"D4a {i}/{n}: INVITE {uri}")
            kind, info = self.originate_uri(label, uri, f"D4a {i}: originate")
            if kind == "refused":
                self.log(f"D4a {i}: Asterisk refused the originate ({info}): nothing was sent. "
                         "The URI is malformed for pjsip.")
                failed.append(f"{uri} -> originate refused")
            elif kind == "timeout":
                self.log(f"D4a {i}: neither answered nor ended in {FOCUS_RING + 10} s")
                failed.append(f"{uri} -> no final answer")
            elif kind == "failed":
                cause, text = info
                if label in self.progress:
                    self.log(f"D4a {i}: Airtel played early media (an announcement) before the "
                             "answer: see the 183 in the trace")
                self.log(f"D4a {i}: no conference here: {cause_str(cause, text)}: "
                         f"{FOCUS_HINT.get(cause, 'see the trace for the code')}")
                failed.append(f"{uri} -> cause {cause}")
            else:
                self.step = f"D4a {i}/{n} read the 200 OK"
                f = self.read_focus(label, f"D4a {i}")
                self.log(f"D4a {i}: answered; Contact isfocus={f['isfocus']}; conference URI "
                         f"{f['conf'] or 'unknown'}; Allow {f['Allow'] or '-'}; Server "
                         f"{f['Server'] or '-'}")
                self.hangup(f"D4a {i}: hang up", label)
                answered = f"{uri} (isfocus={f['isfocus']})"
                (found if f["isfocus"] == "yes" else unclear).append(answered)
                if f["isfocus"] == "yes" and not self.opt.all:
                    if i < n:
                        self.log(f"D4a: stopping at the first conference focus; --all tries the "
                                 f"other {n - i}")
                    break
            if i < n:
                self.wait_for(PROBE_GAP)
        if found:
            first = found[0].split(" ", 1)[0]
            self.passed(f"a conference focus answered: {', '.join(found)}. Run D4 with --conf "
                        f"--conf-uri '{first}'."
                        + (" This probe used --icsi; leave it out of D4 if a probe without "
                           "--icsi found the same focus." if self.opt.icsi else "")
                        + (f" Also answered without ;isfocus: {', '.join(unclear)}." if unclear
                           else ""))
        elif unclear:
            self.passed(f"something answered without ;isfocus: {', '.join(unclear)}. Read that "
                        "200 OK in the trace (Contact, Allow, Server, Record-Route) before D4."
                        + (f" No answer from: {'; '.join(failed)}." if failed else ""),
                        verdict="UNCLEAR")
        else:
            self.fail("no conference factory answered: " + "; ".join(failed))

    # ---- Test D4: the conference -----------------------------------------------------------

    def conference(self, forward):
        """D4, after D1b: leg1 is held and leg2 has answered."""
        o = self.opt
        self.step = "D4 hello to leg2"
        self.play("D4: play hello-world to leg2", "leg2", "hello-world")
        if o.flash:
            self.flash()
            return
        self.step = "D4 step 1, hold leg2"
        self.must("D4 step 1: hold leg2 (re-INVITE a=sendonly)", "POST",
                  f"/channels/{self.legs['leg2']}/hold")
        self.pause("D4 step 1")
        self.step = "D4 read the dialogs"
        dialogs = {label: self.read_dialog(label) for label in ("leg1", "leg2")}
        dropped = []
        if o.dial_out:
            self.step = "D4 --dial-out"
            self.hangup("D4 --dial-out: hang up leg2, so the focus calls the forward number "
                        "itself", "leg2")
            dropped.append("leg2")
            if o.dial_out == "both":
                self.moh_on = False
                self.hangup("D4 --dial-out both: hang up leg1, so the focus calls the caller "
                            "back", "leg1")
                dropped.append("leg1")
            self.pause("D4 after the hang-up")
        self.step = "D4 factory INVITE"
        kind, info = self.originate_uri("leg3", self.uris[0],
                                        "D4 step 2: INVITE the conference factory as leg3")
        if kind != "answered":
            self.fail(self.focus_failure(kind, info))
            self.recover()
            return
        self.required.add("leg3")
        focus = self.read_focus("leg3", "D4 focus")
        self.log(f"D4: leg3 answered; Contact isfocus={focus['isfocus']}; conference URI "
                 f"{focus['conf'] or 'unknown'}")
        try:
            moves = self.move_parties(dialogs, dropped, focus, forward)
            if moves is None:
                self.recover()
                return
            self.merge_at = time.monotonic()
            self.talk(focus, moves)
        except CallEnded as e:
            if e.leg != "leg3":
                raise
            self.fail(f"D4: the focus ended leg3 during {self.step}, {cause_str(e.cause, e.text)}.")
            self.recover()

    def flash(self):
        """D4 --flash, after D1b: leg1 is held and leg2 is active. TS 183 043 C.14.3 (option 2,
        tight coupling): a new dialog with an INVITE to a switching-order address, after which
        the AS re-INVITEs all three parties onto its conference bridge (MRFC/MRFP). No REFER."""
        self.stop_moh()
        self.required = set()       # the AS may end leg1 and leg2 when it joins the calls
        self.step = "D4 flash INVITE"
        start = time.monotonic()
        kind, info = self.originate_uri("leg3", self.uris[0], "D4 flash: INVITE the "
                                        "switching-order address as leg3 (leg1 held, leg2 active)")
        if kind != "answered":
            self.fail(self.flash_failure(kind, info))
            self.recover()
            return
        secs = time.monotonic() - start
        self.required = {"leg3"}
        focus = self.read_focus("leg3", "D4 flash")
        self.log(f"D4 flash: leg3 answered after {secs:.1f} s; Contact isfocus={focus['isfocus']}; "
                 f"contact URI {focus['conf'] or 'unknown'}. Watching {FLASH_WATCH} s for "
                 "Airtel's re-INVITEs (ChannelHold or ChannelUnhold lines, or a leg that ends).")
        try:
            self.step = "D4 flash watch"
            self.wait_for(FLASH_WATCH)
            held = ", ".join(sorted(self.net_held)) or "no leg"
            state = (f"flash INVITE answered after {secs:.1f} s; {FLASH_WATCH} s later: "
                     f"{self.states()}; Airtel holds {held} from its side")
            self.log(f"D4 flash: {state}")
            self.talk(focus, [state])
        except CallEnded as e:
            if e.leg != "leg3":
                raise
            self.fail(f"D4: Airtel ended leg3 during {self.step}, {cause_str(e.cause, e.text)}.")
            self.recover()

    def flash_failure(self, kind, info):
        uri = self.uris[0]
        if kind == "refused":
            return f"D4: Asterisk refused the flash originate ({info}), so nothing went to {uri}."
        if kind == "timeout":
            return (f"D4: the flash INVITE to {uri} neither answered nor ended in "
                    f"{FOCUS_RING + 10} s.")
        cause, text = info
        hint = {
            1: "the IMS core does not know this address. Try another user part with --conf-uri",
            28: "the AS read the user part as incomplete dialled digits. The address may need "
                "a provisioned prefix and a command (on many lines 3 starts a 3-party call): "
                "try another with --conf-uri",
            58: "the line refused a third call, so Airtel did not take the INVITE as a "
                "switching order",
        }.get(cause, FOCUS_HINT.get(cause, "see the trace for the code"))
        out = f"D4: the flash INVITE to {uri} failed, {cause_str(cause, text)}: {hint}."
        if "leg3" in self.progress:
            out += " Airtel played early media (an announcement) first."
        return out

    def focus_failure(self, kind, info):
        uri = self.uris[0]
        if kind == "refused":
            return (f"D4: Asterisk refused the factory originate ({info}), so nothing went to "
                    f"{uri}.")
        if kind == "timeout":
            return (f"D4: the factory INVITE to {uri} neither answered nor ended in "
                    f"{FOCUS_RING + 10} s.")
        cause, text = info
        out = (f"D4: the factory INVITE to {uri} failed, {cause_str(cause, text)}: "
               f"{FOCUS_HINT.get(cause, 'see the trace for the code')}.")
        if "leg3" in self.progress:
            out += " Airtel played early media (an announcement) first."
        if not self.opt.dial_out and cause in (58, 17, 34):
            out += (" With two calls held this points to a cap on calls per line: run again "
                    "with --dial-out leg2.")
        elif not self.opt.dial_out and cause in (1, 21):
            out += (" If D4a got a 200 from this address, it depends on the call state: note it "
                    "and try --dial-out leg2.")
        return out

    def talk(self, focus, moves):
        flash = self.opt.flash
        self.step = "D4 conference up"
        if "leg3" in self.ended:
            self.fail("D4: leg3 has gone, so there is no conference left to hear. "
                      + "; ".join(moves))
            self.recover()
            return
        # With --flash a hold on leg3 is only recorded: the testers' answers decide.
        if "leg3" in self.net_held and not flash:
            self.fail("D4: the focus holds leg3 from its side (its SDP carried a=sendonly or "
                      "a=inactive), so the conference does not carry our audio. "
                      + "; ".join(moves))
            self.recover()
            return
        played = self.play("D4: play hello-world to leg3", "leg3", "hello-world")
        self.talk_started = True
        try:
            self.key_check_start()
            keys = ("Then the key check, with both phones quiet while keys are pressed: press 1 "
                    "three times on the test phone, about a second apart, then say \"now\", and "
                    "the forward-number owner presses 2 three times the same way. Then press * "
                    "and then # on the test phone. * beeps into the conference and # ends the "
                    "talk, only if keys reach the script as keypad events. Press Enter here when "
                    "done, or type n if either could not hear the other.")
            kc = self.kc
            if kc and kc["requested"] and not kc["error"]:
                if self.opt.watch_end is not None:
                    keys += (" The conference audio is recorded for the "
                             + ("end watch" if self.opt.no_key_check
                                else "key check and the end watch")
                             + " until the end watch is over, and the script then deletes it.")
                else:
                    keys += (" The conference audio is recorded for the key check until the "
                             "talk ends, and the script then deletes it.")
            if flash:
                self.log("D4 flash: leg3 is up. If Airtel joined the three calls, both phones "
                         "heard hello-world and can hear each other. Talk on both phones. " + keys)
            else:
                self.log("D4: conference up. Talk on both phones. " + keys)
            self.step = "D4 talk"
            start = time.monotonic()
            said = self.ask(None, limit=TALK_LIMIT, talk=True)
        except CallEnded:
            self.key_check_end()
            raise
        talk = int(time.monotonic() - start)
        self.required.discard("leg3")           # from here its end is reported, not a failure
        watch = self.opt.watch_end is not None
        if not watch:                           # --watch-end: the recording runs on
            self.key_check_end()
        if said is not None and said.startswith("n"):
            heard = "n"
        else:
            self.step = "D4 question"
            heard = self.ask_yn("Did both phones hear each other and hello-world? y/n")
        drop = "not tested (--watch-end)" if watch else "not tested (--no-drop)"
        if not self.opt.no_drop:
            self.step = "D4 drop"
            if "leg3" in self.ended:
                drop = "not tested, leg3 had already ended"
            else:
                # The script leaves the call entirely: leg3, and with --flash leg2 and leg1 too
                # (after REFERs they have already ended).
                for label in ("leg3", "leg2", "leg1"):
                    if label in self.legs and label not in self.ended:
                        self.hangup(f"D4 drop: hang up {label}", label)
                drop = self.ask_yn("Are the two phones still connected? y/n") or "no answer"
            self.log(f"D4 drop: still connected: {drop}")
        keys = (f"keys on leg3: {' '.join(self.keys)}" if self.keys
                else "no keys reached leg3")
        sig = (f"{'Flash' if flash else 'Factory'} 200 OK with isfocus={focus['isfocus']}; "
               + "; ".join(moves)
               + f"; talk {talk} s; {keys}. Still connected after leg3 left: {drop}.")
        if heard == "n":
            self.fail(f"signalling joined both calls, but the testers could not hear each other. "
                      f"{sig}")
        elif heard is None:
            self.passed(f"signalling passed, but nobody answered at the terminal. {sig}",
                        verdict="UNCLEAR")
        elif not played:
            self.passed(f"the testers heard each other, but Asterisk would not play hello-world "
                        f"to leg3. {sig}", verdict="UNCLEAR")
        elif flash:
            self.passed(f"after the flash INVITE the testers heard each other: Airtel's AS "
                        f"joined the calls. {sig}")
        elif focus["isfocus"] == "no":
            self.passed(f"the testers heard each other, but the factory's 200 OK had no ;isfocus: "
                        f"read its Contact in the trace before relying on it. {sig}",
                        verdict="UNCLEAR")
        else:
            note = (" ;isfocus could not be read through ARI: check it in the trace."
                    if focus["isfocus"] == "unreadable" else "")
            self.passed(f"both calls moved into Airtel's conference and the testers heard each "
                        f"other. {sig}{note}")
        if watch:
            self.end_watch()
        elif self.opt.no_drop:
            self.stay()

    def stay(self):
        """--no-drop: stay on the conference until leg3 ends, Ctrl-C or TALK_LIMIT. The result
        is already set, so a Ctrl-C here only ends the stay."""
        if "leg3" not in self.legs or "leg3" in self.ended:
            return
        if self.opt.hold_leg3:
            try:
                reported = self.hold_check(self.opt.hold_leg3)
            except KeyboardInterrupt:
                self.log("D4 leg3 hold check: stopped with Ctrl-C, leaving the conference")
                self.add_sentence("The leg3 hold check was stopped with Ctrl-C.")
                return
            if "leg3" in self.ended:
                if not reported:
                    self.add_sentence(f"During the leg3 hold check: {self.end_str('leg3')}.")
                return
        self.step = "D4 stay on the conference"
        self.log(f"D4 --no-drop: staying on the conference for up to {TALK_LIMIT} s. Hang up "
                 "both phones, or press Ctrl-C here to leave it.")
        start = time.monotonic()
        try:
            ev = self.wait_for(TALK_LIMIT, lambda e: "leg3" in self.ended)
            how = ("Airtel ended leg3, " + cause_str(*self.ended["leg3"]) if ev is not None
                   else f"the {TALK_LIMIT} s limit")
        except KeyboardInterrupt:
            how = "Ctrl-C"
        secs = int(time.monotonic() - start)
        self.log(f"D4 --no-drop: left the conference after {secs} s ({how})")
        self.detail += f" With --no-drop the script stayed on the conference {secs} s, until {how}."

    def hold_check(self, secs):
        """--hold-leg3: on Enter, hold leg3 for `secs` s, take it off hold, play a beep into
        the conference and ask what the testers heard. Ganpath would hold leg3 like this to
        take a second call during a merged call, so this shows whether the parties hear
        Airtel's hold message and whether a held leg3 rejoins. The result is already set; this
        adds a sentence only. True if it has already reported that leg3 ended."""
        gone = lambda: "leg3" in self.ended         # noqa: E731
        self.step = "D4 leg3 hold check"
        answer = self.ask(
            "D4 leg3 hold check. Keep both phones on the call. For the line check with leg3 "
            "still up, call the landline now from the third phone, note what it hears, and hang "
            f"it up. Then press Enter here to put leg3 on hold for {secs} s, or type s to skip. "
            "While leg3 is held, both testers note what they hear.",
            limit=TALK_LIMIT, until=gone)
        if gone():
            return False                        # stay() reports how leg3 ended
        if answer is None or answer.startswith("s"):
            self.log("D4 leg3 hold check: skipped")
            self.add_sentence("Leg3 hold check: skipped.")
            return False
        ok, _ = self.ari(f"D4 leg3 hold check: hold leg3 for {secs} s (re-INVITE a=sendonly)",
                         "POST", f"/channels/{self.legs['leg3']}/hold")
        if not ok:
            self.add_sentence(f"Leg3 hold check: Asterisk refused the hold ({self.last_error}), "
                              "so nothing reached the focus.")
            return False
        self.wait_for(secs, lambda e: gone())
        if gone():
            self.add_sentence(f"Leg3 hold check: {self.end_str('leg3')} while it was on hold.")
            return True
        focus_held = "leg3" in self.net_held
        ok, _ = self.ari("D4 leg3 hold check: take leg3 off hold (re-INVITE a=sendrecv)",
                         "DELETE", f"/channels/{self.legs['leg3']}/hold")
        unhold = "sent" if ok else f"refused by Asterisk ({self.last_error})"
        self.wait_for(self.opt.pause, lambda e: gone())
        if gone():
            self.add_sentence(f"Leg3 hold check: {self.end_str('leg3')} after the unhold.")
            return True
        still_held = "leg3" in self.net_held
        beep = self.play("D4 leg3 hold check: play a beep into the conference", "leg3", "beep")
        heard = self.ask("What did each phone hear while leg3 was held? A few words, for "
                         "example: Airtel's hold message on both, music, silence, each other. "
                         "Or press Enter to skip.", until=gone)
        talking = beep_heard = None
        if not gone():
            talking = self.ask_yn("Can the two phones still hear each other? y/n")
        if not gone():
            beep_heard = self.ask_yn("Did both phones hear a beep after the unhold? y/n")
        self.log(f"D4 leg3 hold check: done (still talking: {talking or 'no answer'}, "
                 f"beep heard: {beep_heard or 'no answer'})")
        self.add_sentence(
            f"Leg3 hold check ({secs} s): "
            + ("the focus's answer held leg3 from its side during the hold"
               if focus_held else "the focus raised no hold of its own")
            + f"; unhold {unhold}; afterwards leg3 was "
            + ("still held by the focus" if still_held else "not held")
            + ("" if beep else "; Asterisk would not play the beep")
            + f"; heard while held: {heard or 'not given'}"
            + f"; still talking after: {talking or 'no answer'}"
            + f"; beep heard: {beep_heard or 'no answer'}.")
        return False

    # ---- Test D4: the end of a merged call (--watch-end) ------------------------------------

    def end_watch(self):
        """--watch-end, after the heard question: both testers hang up and press Enter, and
        leg3 stays up for S s while the script watches for Airtel ending it. Then the key
        check's recording, running since just after the merge, is stopped, fetched, summarised
        as per-second levels (and searched for key tones) and deleted, and leg3 is hung up.
        The verdict is already set: this adds the End watch sentence only (watch_text), and a
        Ctrl-C here ends the watch early."""
        secs = self.opt.watch_end
        if self.merge_at is None:               # not reached without a merge; kept safe
            self.merge_at = time.monotonic()
        w = self.watch = {"secs": secs, "prompt_at": time.monotonic(), "enter_at": None,
                          "enter_how": "", "end_at": None, "stopped": False, "bye_at": None,
                          "before": False, "audio": None, "late_at": None, "cut": False}
        gone = lambda: "leg3" in self.ended         # noqa: E731
        self.step = "D4 end watch"
        try:
            if gone():
                w["before"] = True
                self.log("D4 end watch: leg3 had already ended, so there is nothing to watch")
            else:
                answer = self.ask(
                    "D4 end watch. Talk a few seconds more, then both of you hang up. Press Enter "
                    f"here when both phones have hung up. The script then keeps leg3 on the call "
                    f"for {secs} s and logs whether Airtel ends it.", limit=WATCH_HANGUP_WAIT)
                w["enter_at"] = time.monotonic()
                w["enter_how"] = ("Enter" if answer is not None
                                  else "the terminal input had ended" if self.stdin_eof
                                  else f"no Enter came in {WATCH_HANGUP_WAIT} s")
                if answer is None:
                    self.log(f"D4 end watch: {w['enter_how']}, so the watch counts from now")
                if not gone():
                    self.log(f"D4 end watch: watching leg3 for {secs} s")
                    end = w["enter_at"] + secs
                    while not gone():
                        left = end - time.monotonic()
                        if left <= 0:
                            break
                        self.wait_for(min(WATCH_NOTE, left), lambda e: gone())
                        if not gone() and end - time.monotonic() > 0.5:
                            self.log(f"D4 end watch: {time.monotonic() - w['enter_at']:.0f} s "
                                     "after Enter, leg3 is still up")
        except KeyboardInterrupt:
            w["stopped"] = True
            self.log("D4 end watch: stopped with Ctrl-C")
        w["end_at"] = time.monotonic()
        if gone():
            w["bye_at"] = self.ended_at["leg3"]
            where = (f"{w['bye_at'] - w['enter_at']:.1f} s after Enter, " if w["enter_at"]
                     and w["bye_at"] >= w["enter_at"] else
                     f"{w['enter_at'] - w['bye_at']:.1f} s before Enter, " if w["enter_at"]
                     else "")
            self.log(f"D4 end watch: leg3 ended {where}"
                     f"{w['bye_at'] - self.merge_at:.1f} s after the merge: "
                     f"{self.end_str('leg3')}")
        elif not w["stopped"]:
            self.log(f"D4 end watch: Airtel did not end leg3 within {secs} s of Enter")
        try:
            self.key_check_end()                # watch_levels() runs inside it
            if not gone():
                # Events that came while the recording was read are still queued: read them,
                # so a leg3 that ended in that time is not taken for one the script hangs up.
                self.wait_for(0.2, lambda e: gone())
            hung = False if gone() else self.hangup("D4 end watch: hang up leg3", "leg3")
        except KeyboardInterrupt:
            # A second Ctrl-C while the recording is read: the verdict stands, and cleanup()
            # deletes the recording and hangs up leg3.
            w["cut"] = True
            self.log("D4 end watch: stopped with Ctrl-C while the recording was read; the "
                     "cleanup deletes it and hangs up leg3")
            return
        if w["bye_at"] is None and gone() and not hung:
            # leg3 ended after the watch, while the recording was read (or its DELETE found it
            # gone): the script did not hang it up.
            w["late_at"] = self.ended_at["leg3"]
            self.log(f"D4 end watch: leg3 ended {w['late_at'] - w['end_at']:.1f} s after the "
                     f"watch ended, while the script read the recording: {self.end_str('leg3')}")

    def watch_levels(self, samples):
        """--watch-end: log the per-second level timeline of the whole recording and keep a
        summary of the part after Enter (or after the end-watch prompt, without an Enter) for
        the RESULT. Nothing but numbers leaves this function."""
        w, kc = self.watch, self.kc
        levels = second_levels(samples)
        file_secs = len(samples) / DTMF_RATE
        t0 = kc.get("t0") or w["prompt_at"]
        ref = w["enter_at"] or w["prompt_at"]
        ref_name = "Enter" if w["enter_at"] else "the end-watch prompt"
        k = min(len(levels), max(0, int(ref - t0 + 0.5)))
        self.log(f"D4 end watch: levels in dBFS, one a second from the start of the recording "
                 f"({len(levels)} s; {ref_name} at {k} s, the merge "
                 f"{max(0.0, t0 - self.merge_at):.0f} s before the start):")
        for i in range(0, len(levels), 20):
            self.log(f"  {i:>4} s: " + " ".join(f"{v:.0f}" for v in levels[i:i + 20]))
        notes = []
        t_end = kc.get("t_end") or self.ended_at.get("snoop") or time.monotonic()
        gap = (t_end - t0) - file_secs
        head = f"Audio after {ref_name}"
        if gap > WATCH_GAP:
            # Enter is placed in the file by the clock, but a stretch without RTP writes
            # nothing, and the file does not show where it fell. Any of it before Enter moves
            # Enter earlier in the file than k, so the summary below may miss that much.
            head = (f"Audio from {k} s into the file, where {ref_name} falls only if no audio "
                    f"was missing before it ({ref_name} may be up to {min(gap, k):.0f} s "
                    "earlier in the file, so that much audio after it may be left out)")
            notes.append(f"the file holds {gap:.0f} s less audio than the {t_end - t0:.0f} s it "
                         "ran: for that long no audio frames came from Airtel (no RTP), and the "
                         "file does not show when, so the seconds after such a stretch count "
                         "early")
        if w["end_at"] and t_end < w["end_at"] - WATCH_GAP and not gap > WATCH_GAP:
            notes.append(f"the recording stopped {w['end_at'] - t_end:.0f} s before the watch "
                         "ended")
        # The last switch to digital silence, over the whole file: the phones' audio stops
        # reaching the conference (12 Nov: about 20 s before Enter, when both had hung up).
        switch = last_zero_switch(samples)
        if switch is None:
            zero = (f"no switch from audio to all-zero samples ({WATCH_ZERO_RUN:g} s or longer) "
                    "in the recording")
        else:
            start, length, to_end = switch
            rel = start - (ref - t0)
            if gap > WATCH_GAP:
                # Enter is placed by the clock; missing audio before it moves it earlier in the
                # file, so the switch may be up to that much later relative to it.
                rel_text = (f"{abs(rel):.1f} s {'after' if rel >= 0 else 'before'} {ref_name} "
                            f"by the clock (it may be up to {min(gap, max(0.0, ref - t0)):.0f} s later "
                            "relative to it, see the missing-audio note)")
            else:
                rel_text = f"{abs(rel):.1f} s {'after' if rel >= 0 else 'before'} {ref_name}"
            zero = (f"the last switch from audio to all-zero samples came {start:.1f} s into "
                    f"the recording, {rel_text}, and the zeros "
                    + ("ran to the end of the file" if to_end else f"lasted {length:.1f} s"))
        self.log(f"D4 end watch: {zero}")
        after = levels[k:]
        if not after:
            text = (f"none: the file ends before {k} s" if gap > WATCH_GAP
                    else f"none: the recording ended before {ref_name}")
        else:
            runs = level_runs(after)
            count = {kind: sum(b - a + 1 for kk, a, b in runs if kk == kind)
                     for kind in ("silent", "low", "loud")}
            digital = sum(1 for v in after if v < WATCH_DIGITAL_DBFS)
            longest = max([b - a + 1 for kk, a, b in runs if kk == "silent"], default=0)
            shape = ", ".join(f"{kind} {a}-{b} s" if b > a else f"{kind} {a} s"
                              for kind, a, b in runs[:WATCH_RUNS])
            if len(runs) > WATCH_RUNS:
                shape += f", {len(runs) - WATCH_RUNS} more runs"
            text = (f"{len(after)} s recorded: silent {count['silent']} s (below "
                    f"{WATCH_SILENT_DBFS:g} dBFS; {digital} s of them digital silence, all-zero "
                    f"or below {WATCH_DIGITAL_DBFS:g} dBFS), low {count['low']} s, tone or speech "
                    f"level {count['loud']} s (at or above {WATCH_LOUD_DBFS:g} dBFS; loudest "
                    f"{max(after):.0f} dBFS), longest silent run {longest} s; in order: {shape}")
        text += f"; {zero}"
        if notes:
            text += "; " + "; ".join(notes)
        w["audio"] = f"{head}: {text}."
        self.log(f"D4 end watch: {w['audio']}")

    def watch_text(self):
        """The End watch sentence for the RESULT line ('' when it was never due)."""
        w = self.watch
        if w is None:
            return ("End watch: not reached, the run stopped first." if self.talk_started
                    else "")
        secs, merge = w["secs"], self.merge_at or w["prompt_at"]
        cause = ""
        if "leg3" in self.ended:
            # The script had not hung leg3 up, so the end came from the far side, most likely
            # Airtel's BYE. Without a hang-up cause ARI does not show even that much.
            code, code_text = self.ended["leg3"]
            cause = (cause_str(code, code_text) if code is not None
                     else END_HOW.get(self.end_how.get("leg3"), "ARI gave no cause")
                     + "; no BYE is confirmed, so check the trace")
        if w["bye_at"] is not None:
            after_merge = f"{w['bye_at'] - merge:.0f} s after the merge"
            if w["before"]:
                text = f"leg3 ended before the end watch began, {after_merge} ({cause})"
            elif w["enter_at"] is None:
                text = f"leg3 ended {after_merge}, before Enter ({cause})"
            elif w["bye_at"] >= w["enter_at"]:
                text = (f"leg3 ended {w['bye_at'] - w['enter_at']:.1f} s after Enter, "
                        f"{after_merge} ({cause})")
            else:
                text = (f"leg3 ended {w['enter_at'] - w['bye_at']:.1f} s before Enter, "
                        f"{after_merge} ({cause})")
        else:
            if w["stopped"]:
                when = (f"{w['end_at'] - w['enter_at']:.0f} s after Enter" if w["enter_at"]
                        else "before Enter")
                text = f"stopped with Ctrl-C {when}; leg3 had not ended by then"
            else:
                text = f"Airtel did not end leg3 within {secs} s of Enter"
            if w["late_at"] is not None:
                text += (f", but leg3 ended {w['late_at'] - w['end_at']:.1f} s after the watch "
                         f"ended, while the script read the recording ({cause}); the script did "
                         "not hang it up")
            elif w["cut"]:
                text += ("; a second Ctrl-C stopped the reading of the recording, and the "
                         "cleanup hung leg3 up")
            else:
                text += ", so the script hung it up"
        if w["enter_at"] and w["enter_how"] != "Enter":
            text += f". The watch counted from when {w['enter_how']}"
        kc = self.kc
        if w["audio"]:
            audio = w["audio"]
        elif kc is not None and kc["error"]:
            audio = (f"No audio summary: {kc['error']}." if self.opt.no_key_check
                     else "No audio summary: the recording failed (see Key check).")
        else:
            audio = "No audio summary: the recording was not analysed."
        return f"End watch ({secs} s): {text}. {audio}"

    # ---- Test D4: the key check -----------------------------------------------------------

    def key_check_start(self):
        """Once the conference is up: a snoop channel on leg3 with spy=in (what Airtel sends
        us) and a recording of it. ARI's record runs as a command on the channel's control and
        holds it until the recording ends (res_stasis_recording record_file), so recording
        leg3 itself would hold back the beep for *; the snoop channel takes that wait. A failed
        step is logged and leaves kc['error']; the test goes on without the check."""
        watch = self.opt.watch_end
        if self.opt.no_key_check and watch is None:
            self.log("D4 key check: off (--no-key-check)")
            return
        if self.opt.no_key_check:
            self.log("D4 key check: off (--no-key-check); the end watch still records leg3, "
                     "for its levels only")
        max_secs = KEY_MAX_SECS
        if watch is not None:
            # The talk, two questions, the wait for Enter, the watch and a margin.
            want = TALK_LIMIT + 2 * ASK_WAIT + WATCH_HANGUP_WAIT + watch + 30
            max_secs = min(WATCH_REC_SECS, want)
            if want > WATCH_REC_SECS:
                self.log(f"D4 end watch: the recording stops by itself after {WATCH_REC_SECS} s "
                         "(the 16 MB tmpfs), so after a long talk the end of the watch is not "
                         "recorded")
        # The dash keeps 10 digits from running together, which the log's redaction would mask.
        tag = uuid.uuid4().hex[:6] + "-" + uuid.uuid4().hex[:6]
        # requested: a record request went out, so a file may exist; started: ARI accepted it;
        # gone: no file is left in the container.
        kc = self.kc = {"name": f"testd-keys-{tag}", "error": "", "state": "", "fail_cause": "",
                        "requested": False, "started": False, "gone": False, "ended": False,
                        "presses": None, "secs": 0.0}
        step = self.step
        self.step = "D4 key check setup"
        try:
            # Known before the request, so its events get the label and send_back() skips it.
            self.legs["snoop"] = f"testd-snoop-{tag}"
            ok, _ = self.ari("D4 key check: snoop on leg3 (spy=in: the audio Airtel sends us)",
                             "POST", f"/channels/{self.legs['leg3']}/snoop", spy="in",
                             whisper="none", app=APP, appArgs="keycheck",
                             snoopId=self.legs["snoop"])
            if not ok:
                if self.last_status:            # refused, so no snoop channel exists
                    del self.legs["snoop"]
                kc["error"] = f"Asterisk would not create the snoop channel ({self.last_error})"
                self.log(f"D4 key check did not start: {kc['error']}")
                return
            ev = self.wait_for(KEY_WAIT, lambda e: "snoop" in self.ended or (
                e.get("type") == "StasisStart"
                and self.label_of(channel_of(e).get("id")) == "snoop"))
            if ev is None or "snoop" in self.ended:
                kc["error"] = f"the snoop channel did not enter the app within {KEY_WAIT} s"
                self.log(f"D4 key check did not start: {kc['error']}")
                # Outside the app it sends no events, so the DELETE's answer is the only sign.
                if (self.hangup("D4 key check: hang up the snoop channel", "snoop", wait=1.0)
                        and "snoop" not in self.ended):
                    self.mark_ended("snoop", None, None, "hung-up")
                return
            kc["requested"] = True              # from here a file may exist: cleanup deletes it
            ok, _ = self.ari("D4 key check: record the snoop channel", "POST",
                             f"/channels/{self.legs['snoop']}/record", name=kc["name"],
                             format="wav", maxDurationSeconds=max_secs, ifExists="overwrite",
                             beep="false", terminateOn="none")
            if not ok:
                kc["error"] = f"Asterisk refused the recording ({self.last_error})"
                if self.last_status == 500:
                    kc["error"] += (f". A missing {KEY_FOLDER} gives this: recreate the "
                                    "container from docker-compose.yml, which mounts it (see "
                                    "Set up D4 in the README)")
                self.log(f"D4 key check did not start: {kc['error']}")
                self.hangup("D4 key check: hang up the snoop channel", "snoop")
                return
            kc["started"] = True
            kc["t0"] = time.monotonic()         # RecordingStarted, when it comes, sets it again
            # Wait for RecordingStarted: a stop while the recording is still queued is ignored
            # in 20.6 (recording_disregard), and the recording would then run on.
            self.wait_for(KEY_WAIT, lambda e: kc["state"] not in ("", "queued")
                          or "snoop" in self.ended)
            if kc["state"] == "failed":
                kc["error"] = f"Asterisk could not record ({kc['fail_cause']})"
                self.log(f"D4 key check did not start: {kc['error']}")
            elif kc["state"] != "recording":
                self.log(f"D4 key check: no RecordingStarted within {KEY_WAIT} s; carrying on")
            else:
                self.log(f"D4 key check: recording {kc['name']} in the container")
        finally:
            self.step = step

    def key_check_end(self):
        """When the talk ends: stop the recording, hang up the snoop channel, download the file
        into memory, delete it in Asterisk, then look for key tones. Failures are logged and
        leave kc['error']; the test goes on."""
        kc = self.kc
        if kc is None or kc["ended"]:
            return
        kc["ended"] = True
        step = self.step
        self.step = "D4 key check"
        try:
            name = kc["name"]
            over = ("done", "failed", "canceled")
            if kc["started"] and kc["state"] not in over:
                # 404: it had already finished, as when leg3 ended and took the snoop with it.
                self.ari("D4 key check: stop the recording", "POST",
                         f"/recordings/live/{name}/stop")
                if kc["state"] not in over and "snoop" not in self.ended:
                    self.wait_for(KEY_WAIT, lambda e: kc["state"] in over
                                  or "snoop" in self.ended)
            if "snoop" in self.legs:
                self.hangup("D4 key check: hang up the snoop channel", "snoop")
            if not kc["requested"]:
                return
            self.key_check_settle(lambda s: self.wait_for(s))
            kc.setdefault("t_end", self.ended_at.get("snoop") or time.monotonic())
            data = None
            if kc["state"] == "failed":
                kc["error"] = kc["error"] or f"Asterisk could not record ({kc['fail_cause']})"
                self.log(f"D4 key check: {kc['error']}")
            elif not kc["error"]:
                data = self.key_check_fetch()
            self.key_check_delete("D4 key check")
            if data is not None:
                self.key_check_analyse(data)
        finally:
            self.step = step

    def key_check_settle(self, sleep):
        """Wait (up to KEY_WAIT s) until ARI no longer lists the live recording, so the file is
        closed before it is read or deleted. Polls ARI, so it works without events too."""
        end = time.monotonic() + KEY_WAIT
        while time.monotonic() < end:
            status, _, _ = self.rest.call("GET", f"/recordings/live/{self.kc['name']}",
                                          http_timeout=3)
            if status != 200:
                return
            sleep(0.2)
        self.log(f"D4 key check: the recording was still live after {KEY_WAIT} s")

    def key_check_fetch(self):
        """The recording's bytes (kept in memory only), or None with kc['error'] set."""
        kc = self.kc
        path = f"/recordings/stored/{kc['name']}/file"
        status, reason, data, info = self.rest.raw("GET", path)
        got = (f"{len(data)} bytes, {info or 'no Content-Type'}" if status == 200
               else (info or reason))
        self.log(f"D4 key check: download: GET {path} -> {status or 'no answer'} {reason} ({got})")
        if status == 200 and data:
            return data
        if status == 404:
            kc["error"] = "Asterisk stored no recording file (HTTP 404)"
        elif status == 200:
            kc["error"] = "the recording file was empty"
        else:
            kc["error"] = (f"the recording could not be downloaded (HTTP {status or 'no answer'} "
                           f"{reason}{': ' + info if info else ''})")
        self.log(f"D4 key check: {kc['error']}")
        return None

    def key_check_delete(self, prefix):
        """Delete the stored recording through ARI and log the result."""
        kc = self.kc
        name = kc["name"]
        ok, _ = self.ari(f"{prefix}: delete the recording", "DELETE", f"/recordings/stored/{name}",
                         http_timeout=5)
        if ok:
            kc["gone"] = True
            self.log(f"{prefix}: deleted the recording {name} in Asterisk")
        elif self.last_status == 404:
            kc["gone"] = True
            self.log(f"{prefix}: Asterisk holds no recording {name}, so there is nothing to delete")
        else:
            self.log(f"{prefix}: could not delete the recording {name} ({self.last_error}). "
                     f"Remove it by hand: {KEY_RM}")

    def key_check_analyse(self, data):
        """Read the WAV in memory, summarise its levels for --watch-end, and look for DTMF
        tones in it (not with --no-key-check)."""
        kc = self.kc
        try:
            samples, unclosed = read_wav(data)
            del data
            secs = len(samples) / DTMF_RATE
            if unclosed:
                self.log(f"D4 key check: the WAV header has no sizes (Asterisk had not closed the "
                         f"file), so the {secs:.0f} s after it are read as the audio")
            if self.watch is not None:
                try:
                    self.watch_levels(samples)
                except Exception as e:          # a bug here must not stop the test
                    self.watch["audio"] = f"No audio summary: the level count failed ({e!r})."
                    self.log(f"D4 end watch: {self.watch['audio']}")
            if self.opt.no_key_check:
                return
            loudest = max(map(abs, samples)) if samples else 0
            if secs < 2:
                raise ValueError(f"the recording holds only {secs:.1f} s of audio")
            if loudest == 0:
                raise ValueError(f"the {secs:.0f} s recording is digital silence: no audio "
                                 "reached leg3 from the conference")
            self.log(f"D4 key check: looking for key tones in {secs:.0f} s of conference audio")
            began = time.monotonic()
            presses = find_key_tones(samples)
        except ValueError as e:
            kc["error"] = str(e)
            self.log(f"D4 key check: {e}")
            return
        except Exception as e:                  # a bug here must not stop the test
            kc["error"] = f"the tone search failed ({e!r})"
            self.log(f"D4 key check: {kc['error']}")
            return
        kc["secs"], kc["presses"] = secs, presses
        for digit, at, dur, low, high in presses:
            self.log(f"D4 key check: tone {digit} at {at:.2f} s, {dur * 1000:.0f} ms, low tone "
                     f"{low:.0f} dBm0, high tone {high:.0f} dBm0")
        found = (f"{len(presses)} key tone(s): {tone_summary(presses)}" if presses
                 else "no key tones")
        self.log(f"D4 key check: {found} in {secs:.0f} s of conference audio (searched in "
                 f"{time.monotonic() - began:.1f} s)")

    def key_check_cleanup(self):
        """cleanup(): if the run stopped before key_check_end() finished, stop the recording,
        hang up the snoop channel and delete the file. REST only, so it works after the event
        connection has gone."""
        kc = self.kc
        if kc is None or not kc["requested"] or kc["gone"]:
            return
        if kc["started"] and kc["state"] not in ("done", "failed", "canceled"):
            self.ari("cleanup: stop the key-check recording", "POST",
                     f"/recordings/live/{kc['name']}/stop", http_timeout=3)
        if "snoop" in self.legs and "snoop" not in self.ended:
            self.ari("cleanup: hang up the snoop channel", "DELETE",
                     f"/channels/{self.legs['snoop']}", reason="normal", http_timeout=3)
            self.hung_up.add("snoop")
        self.key_check_settle(time.sleep)
        self.key_check_delete("cleanup")

    def key_check_text(self):
        """The key check's sentence for the RESULT line ('' when it was never due)."""
        kc = self.kc
        events = (f"keypad events on leg3: {' '.join(self.keys)}" if self.keys
                  else "no keypad events on leg3")
        if self.opt.no_key_check:
            # With --watch-end a recording ran all the same, for the levels only.
            text = ("Key check: off (--no-key-check); " + events + "."
                    if self.talk_started else "")
        elif kc is None:
            return ""
        elif kc["error"]:
            text = f"Key check did not run: {kc['error']}; {events}."
        elif kc["presses"] is None:
            why = ("a second Ctrl-C stopped the reading" if self.watch and self.watch["cut"]
                   else "the run stopped first")
            text = f"Key check: not analysed, {why}; {events}."
        elif kc["presses"]:
            text = (f"Key check: {events}; in-band tones in the conference audio: "
                    f"{tone_summary(kc['presses'])}.")
        elif self.keys:
            text = f"Key check: {events}; no key tones in {kc['secs']:.0f} s of conference audio."
        else:
            text = (f"Key check: no keypad events and no key tones in {kc['secs']:.0f} s of "
                    "conference audio: key presses stop at Airtel's conference.")
        if kc is not None and kc["requested"] and not kc["gone"]:
            text += (f" The recording {kc['name']} may still be in the container: {KEY_RM}")
        return text.strip()

    # ---- start and finish ----------------------------------------------------------------

    def cleanup(self):
        """Clear TESTD_WAIT, destroy the bridge, hang up every leg (newest first; a leg inside
        [testd-refer] hangs up too, since ARI hangup needs no Stasis), close the websocket."""
        if self.flag_set:
            self.set_flag("0", "cleanup: clear TESTD_WAIT")
        try:
            self.key_check_cleanup()
        except Exception as e:      # the calls below must still be hung up
            self.log(f"cleanup error in the key check: {e!r}. If a recording is left: {KEY_RM}")
        if self.bridge:
            self.ari("cleanup: destroy the bridge", "DELETE", f"/bridges/{self.bridge}",
                     http_timeout=3)
        for label in reversed(list(self.legs)):
            cid = self.legs.get(label)
            if cid and label not in self.ended and label not in self.hung_up:
                # An --in2 leg2 that was never answered is still ringing: refuse it with busy.
                ringing = label == "leg2" and self.in2 is not None and not self.in2["answered"]
                self.ari(f"cleanup: hang up {label}" + (" with busy, it was still ringing"
                                                        if ringing else ""),
                         "DELETE", f"/channels/{cid}", reason="busy" if ringing else "normal",
                         http_timeout=3)
        if self.ws:
            self.ws.close()

    def main(self):
        self.log(f"Test {self.mode}; this log is also in {self.log.path}")
        code = 1
        try:
            self.run()
        except SetupError as e:
            self.fail(str(e), verdict="NOT RUN")
            code = 2
        except CallEnded as e:
            self.fail(f"{e} during {self.step}")
        except Refused as e:
            self.fail(str(e), verdict="STOPPED")
        except WsClosed as e:
            self.fail(f"{e} during {self.step}", verdict="STOPPED")
        except KeyboardInterrupt:
            self.fail(f"stopped by hand during {self.step}", verdict="STOPPED")
            code = 130
        except Exception as e:  # keep going to the cleanup whatever happened
            for line in traceback.format_exc().splitlines():
                self.log(line)
            self.fail(f"script error during {self.step}: {e!r}", verdict="STOPPED")
        finally:
            # Let the cleanup finish: a second Ctrl-C, or a TERM/HUP (which this script turns
            # into KeyboardInterrupt), must not cut it short or lose the RESULT line.
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, signal.SIG_IGN)
            try:
                self.cleanup()
            except Exception as e:
                self.log(f"cleanup error: {e!r}")
            if self.verdict is None:
                self.verdict, self.detail = "STOPPED", f"no result reached ({self.step})"
            merging = self.in2 is None or self.in2["answered"]   # --in2: leg2 was answered
            if self.in2 is not None and self.in2["answered"]:
                self.add_sentence(self.in2_text())
            if self.opt.conf and self.opt.watch_end is not None:
                text = self.watch_text()
                if text:
                    self.add_sentence(text)
            if self.verdict in ("PASS", "FAIL", "UNCLEAR"):
                if self.opt.conf and merging:
                    self.add_sentence("The script does not judge voices: the audio verdict is "
                                      "the testers'.")
                elif self.opt.probe and self.verdict != "FAIL":
                    self.add_sentence("A 200 OK shows signalling only: D4 tests whether a "
                                      "conference carries audio.")
            if self.opt.conf:
                # The key check never changes the verdict; it only adds its own sentence.
                text = self.key_check_text()
                if text:
                    self.add_sentence(text)
            self.log(f"RESULT: {self.mode} {self.verdict}: {self.detail}")
        if self.verdict in ("PASS", "REFUSED"):     # REFUSED: --in2 refused the call, as asked
            code = 0
        return code


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    ap = argparse.ArgumentParser(
        description="Test D: hold the caller, then dial (or wait for) a second call.",
        epilog="Default: D1. Run from Home-Phone/ on den-ubuntu with HOME_PHONE_ARI_PASSWORD "
               "exported, then call the landline from a test number and press 4.")
    ap.add_argument("--moh", action="store_true",
                    help="D2: play our music on hold to the held caller")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--mix", action="store_true",
                       help="D3: hold leg2, unhold leg1, unhold leg2, then join both")
    group.add_argument("--mix-direct", action="store_true",
                       help="D3b: unhold leg1 while leg2 stays active, then join both")
    group.add_argument("--wait-in", action="store_true",
                       help="E2: hold the caller and wait 60 s for a call in, then join both")
    group.add_argument("--probe", action="store_true",
                       help="D4a: INVITE each likely conference factory address once and log "
                            "the answer; no caller, no phone rings")
    group.add_argument("--conf", action="store_true",
                       help="D4: hold the caller, dial the forward number, then move both calls "
                            "into Airtel's network conference with REFER")
    ap.add_argument("--pause", type=float, default=1.5, metavar="S",
                    help="seconds between re-INVITE steps (default 1.5)")
    d4 = ap.add_argument_group("Test D4 options")
    d4.add_argument("--conf-uri", action="append", metavar="URI",
                    help="conference factory address, repeatable (--probe tries each in order, "
                         "--conf uses the first). Placeholders: {domain}, {parent}, {mcc}, "
                         "{mnc}. Default: HOME_PHONE_TEST_CONF_URIS from the shell, else the "
                         "3GPP default forms")
    d4.add_argument("--plmn", metavar="MCC-MNC",
                    help="fills {mcc} and {mnc} (default: HOME_PHONE_TEST_PLMN from the shell); "
                         "without it, an address that needs them is skipped")
    d4.add_argument("--icsi", action="store_true",
                    help="add the MMTEL Accept-Contact and P-Preferred-Service headers to the "
                         "factory or flash INVITE")
    d4.add_argument("--wide", action="store_true",
                    help="--probe: also try the guessed factory names and flash@")
    d4.add_argument("--all", action="store_true",
                    help="--probe: do not stop at the first conference focus")
    d4.add_argument("--refer-in", choices=("focus", "legs"),
                    help="--conf: send the REFERs inside the focus dialog (default), or REFER "
                         "each party to the conference URI inside its own dialog (RFC 4579 / "
                         "TS 24.147)")
    d4.add_argument("--dial-out", choices=("leg2", "both"),
                    help="--conf: hang up leg2 (or both legs) before the factory INVITE and "
                         "REFER the focus to call that number itself")
    d4.add_argument("--flash", action="store_true",
                    help="--conf: TS 183 043 C.14.3 tight coupling, no REFER: with leg1 held and "
                         "leg2 active, INVITE a switching-order address as leg3 (default "
                         "sip:flash@{domain}, or the first --conf-uri)")
    d4.add_argument("--no-drop", action="store_true",
                    help="--conf: skip the drop test and stay on the conference until it ends, "
                         f"Ctrl-C or {TALK_LIMIT} s")
    d4.add_argument("--hold-leg3", type=int, nargs="?", const=10, metavar="S",
                    help="--conf --no-drop: before the stay, on Enter, hold leg3 for S s "
                         "(default 10), take it off hold, play a beep into the conference and "
                         "ask what the phones heard")
    d4.add_argument("--watch-end", type=int, nargs="?", const=WATCH_DEFAULT, metavar="S",
                    help="--conf: the end-of-merge check. No drop test: after the heard question "
                         "both testers hang up and press Enter, and the script keeps leg3 up for "
                         f"S s (default {WATCH_DEFAULT}, {WATCH_RANGE[0]} to {WATCH_RANGE[1]}) "
                         "and logs whether Airtel ends it. The key check's recording runs from "
                         "the merge to the end of the watch and is summarised as per-second "
                         "levels. Not with --flash or --hold-leg3")
    d4.add_argument("--in2", action="store_true",
                    help="--conf: leg2 is a call in from the forward number, in call-waiting "
                         f"order: leg1 stays active, the script waits {WAIT_IN} s for the call, "
                         "keeps it ringing and asks leg1 with three beeps; 1 adds it (hold leg1, "
                         "answer leg2, then as D4), 2 or no key refuses it with busy. Reported "
                         "as D4-in2. Not with --flash or --dial-out")
    d4.add_argument("--in2-wait", type=int, metavar="S",
                    help="--conf --in2: how long leg2 rings while the script waits for 1 or 2 "
                         f"from leg1 (default {IN2_WAIT}, {IN2_WAIT_RANGE[0]} to "
                         f"{IN2_WAIT_RANGE[1]})")
    d4.add_argument("--no-key-check", action="store_true",
                    help="--conf: do not record the conference audio during the talk, so no "
                         "search for key tones (keypad events on leg3 are still logged). With "
                         "--watch-end the recording still runs, for the levels only")
    opt = ap.parse_args()
    d4_mode = opt.probe or opt.conf
    if opt.moh and opt.probe:
        ap.error("--moh needs a caller on hold, so it does not go with --probe")
    for flag, given, allowed, where in (
            ("--conf-uri", opt.conf_uri, d4_mode, "--probe or --conf"),
            ("--plmn", opt.plmn, d4_mode, "--probe or --conf"),
            ("--icsi", opt.icsi, d4_mode, "--probe or --conf"),
            ("--wide", opt.wide, opt.probe, "--probe"),
            ("--all", opt.all, opt.probe, "--probe"),
            ("--refer-in", opt.refer_in, opt.conf, "--conf"),
            ("--dial-out", opt.dial_out, opt.conf, "--conf"),
            ("--flash", opt.flash, opt.conf, "--conf"),
            ("--no-drop", opt.no_drop, opt.conf, "--conf"),
            ("--watch-end", opt.watch_end is not None, opt.conf, "--conf"),
            ("--in2", opt.in2, opt.conf, "--conf"),
            ("--in2-wait", opt.in2_wait is not None, opt.conf and opt.in2, "--conf --in2"),
            ("--no-key-check", opt.no_key_check, opt.conf, "--conf")):
        if given and not allowed:
            ap.error(f"{flag} works only with {where}")
    if opt.dial_out and opt.refer_in == "legs":
        ap.error("--dial-out and --refer-in legs do not go together: with --dial-out the focus "
                 "calls the party, so there is no party dialog to send the REFER in")
    if opt.flash and (opt.dial_out or opt.refer_in):
        ap.error("--flash sends no REFER, so --dial-out and --refer-in do not go with it")
    if opt.in2 and opt.flash:
        ap.error("--in2 does not go with --flash: --flash tests a different way to join the "
                 "calls, and its leg2 is always dialled out")
    if opt.in2 and opt.dial_out:
        ap.error("--in2 does not go with --dial-out: --dial-out hangs up leg2, so no incoming "
                 "dialog would be merged")
    if opt.in2_wait is None:
        opt.in2_wait = IN2_WAIT
    elif not IN2_WAIT_RANGE[0] <= opt.in2_wait <= IN2_WAIT_RANGE[1]:
        ap.error(f"--in2-wait takes {IN2_WAIT_RANGE[0]} to {IN2_WAIT_RANGE[1]} seconds")
    if opt.watch_end is not None:
        if opt.flash:
            ap.error("--watch-end does not go with --flash: there leg3 is the flash call, not a "
                     "leg in Airtel's conference")
        if opt.hold_leg3 is not None:
            ap.error("--watch-end does not go with --hold-leg3: run them as separate calls")
        if not WATCH_RANGE[0] <= opt.watch_end <= WATCH_RANGE[1]:
            ap.error(f"--watch-end takes {WATCH_RANGE[0]} to {WATCH_RANGE[1]} seconds")
        opt.no_drop = True                  # the watch stays on the conference
    if opt.hold_leg3 is not None:
        if not opt.no_drop:
            ap.error("--hold-leg3 works only with --conf --no-drop")
        if opt.flash:
            ap.error("--hold-leg3 does not go with --flash: there leg3 is the flash call, not "
                     "a leg in Airtel's conference")
        if not 1 <= opt.hold_leg3 <= 120:
            ap.error("--hold-leg3 takes 1 to 120 seconds")
    if opt.plmn and not PLMN_OK.fullmatch(opt.plmn):
        ap.error("--plmn takes MCC-MNC: three digits, a dash, then two or three digits")
    opt.refer_in = opt.refer_in or "focus"

    password = os.environ.get("HOME_PHONE_ARI_PASSWORD", "")
    if not password:
        sys.exit("HOME_PHONE_ARI_PASSWORD is not set. From Home-Phone/ run:\n"
                 "  export HOME_PHONE_ARI_PASSWORD=\"$(grep '^HOME_PHONE_ARI_PASSWORD=' ../.env"
                 " | cut -d= -f2-)\"")
    auth = "Basic " + base64.b64encode(f"{USER}:{password}".encode()).decode()
    here = os.path.dirname(os.path.abspath(__file__))
    log = Log(os.path.join(here, time.strftime("testd-%Y%m%d-%H%M%S.log")))
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _interrupt)
    return TestD(opt, Rest(auth), log).main()


if __name__ == "__main__":
    sys.exit(main())
