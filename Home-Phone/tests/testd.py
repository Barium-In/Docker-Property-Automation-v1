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
In a 3-way, * on either phone plays a beep and # ends the test.
If the carrier holds a leg from its side (a ChannelHold event: its answer to an unhold, or its
own re-INVITE, carried a=sendonly or a=inactive) and the leg is still held when the test
checks, the result is FAIL: that leg never had two-way audio.
D4 sends each REFER through the [testd-refer] context in extensions.conf, which runs
Transfer() on the channel and returns it to this script with the result.

  export HOME_PHONE_ARI_PASSWORD="$(grep '^HOME_PHONE_ARI_PASSWORD=' ../.env | cut -d= -f2-)"
  python3 tests/testd.py [--moh] [--mix | --mix-direct | --wait-in | --conf [--flash]]
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
import json
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

HOST, PORT = "127.0.0.1", 8088
APP, USER = "home-phone-testd", "homephone"
RING_TIMEOUT = 30   # s the forward number rings before Asterisk gives up
WAIT_IN = 60        # s E2 waits for a call in
TALK_LIMIT = 300    # s a 3-way may run before the script ends it

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

# A phone number: optional +91, 91 or 0, then 10 digits. Shown as the prefix, XXXXXX and the
# last 4 digits. Digits next to a '.digit' (Asterisk channel ids) are left alone.
_NUMBER = re.compile(r"(?<!\d)(?<!\d\.)(\+91|91|0)?\d{6}(\d{4})(?!\d|\.\d)")


def redact(text):
    return _NUMBER.sub(lambda m: (m.group(1) or "") + "XXXXXX" + m.group(2), text)


def cause_str(cause, text=None):
    hint = SIP_HINT.get(cause)
    out = f"cause {cause}"
    if text:
        out += f" {text}"
    return out + (f" ({hint})" if hint else "")


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
        self.leg, self.cause, self.text = leg, cause, text


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
                     else "D4" if opt.conf else "D3" if opt.mix
                     else "D3b" if opt.mix_direct else "E2" if opt.wait_in
                     else "D2" if opt.moh else "D1")
        if opt.moh and self.mode != "D2":
            self.mode += "+moh"
        self.ws = None
        self.legs = {}         # "leg1" / "leg2" -> channel id
        self.ended = {}        # label -> (cause, cause text)
        self.ended_at = {}     # label -> time.monotonic() when it ended
        self.answered_at = None
        self.last_error = ""
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
            return f"{label} ended (found by polling: ARI sends no events while it runs a REFER)"
        return f"{label} ended, {cause_str(cause, text)}"

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
        if label is None and kind.startswith("Playback"):
            target = ev.get("playback", {}).get("target_uri", "")
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
        if "cause" in ev:
            bits.append(cause_str(ev.get("cause"), ev.get("cause_txt")))
        self.log(" ".join(str(b) for b in bits))
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
            self.outside.discard(label)      # back from [testd-refer]: events flow again
        elif kind == "Dial" and ev.get("dialstatus") == "PROGRESS" and label in self.legs:
            self.progress.add(label)
        if kind == "ChannelDestroyed" and label in self.legs:
            self.ended[label] = (ev.get("cause"), ev.get("cause_txt"))
            self.ended_at[label] = time.monotonic()
            if label in self.required:
                self.required.discard(label)
                raise CallEnded(label, ev.get("cause"), ev.get("cause_txt"))

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
        ok, body = self.ari(step, "POST", f"/channels/{self.legs[label]}/play",
                            media=f"sound:{sound}")
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

    def hangup(self, step, label):
        """Hang up a leg. True if this hang-up ended it, False if it had already gone."""
        cid = self.legs.get(label)
        if not cid or label in self.ended:
            return False
        self.required.discard(label)
        ok, _ = self.ari(step, "DELETE", f"/channels/{cid}", reason="normal")
        if label in self.outside:
            # A leg inside [testd-refer] sends no events: look until it has gone.
            end = time.monotonic() + 5
            while label not in self.ended and time.monotonic() < end:
                self.wait_for(0.5)
                self.poll_outside()
            return ok
        self.wait_for(5, lambda e: e.get("type") == "ChannelDestroyed"
                      and self.label_of(channel_of(e).get("id")) == label)
        return ok

    def poll_outside(self):
        """Find legs that ended while they ran a REFER. In 20.6 the app gets no events for a
        channel after its StasisEnd (res_stasis unsubscribes it), so ask for each one."""
        for label in list(self.outside):
            if label in self.ended:
                self.outside.discard(label)
                continue
            status, _, _ = self.rest.call("GET", f"/channels/{self.legs[label]}", http_timeout=5)
            if status == 404:
                self.outside.discard(label)
                self.required.discard(label)
                self.ended[label] = (None, None)
                self.ended_at[label] = time.monotonic()
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
        ev = self.wait_for(None, lambda e: e.get("type") == "StasisStart"
                           and first_arg(e) not in ("second", "incoming", "focus", "referred"))
        self.legs["leg1"] = channel_of(ev)["id"]
        self.required = {"leg1"}
        forward = first_arg(ev)
        self.step = "D1a hold leg1"
        if not self.opt.wait_in and not re.fullmatch(r"\+?\d{6,15}", forward):
            self.fail("no usable forward number came from the dialplan. "
                      "Set HOME_PHONE_TEST_FORWARD in ../.env.", verdict="NOT RUN")
            self.play("tell leg1", "leg1", "beeperr")
            return
        self.play("start: beep to leg1", "leg1", "beep", wait=3)
        self.must("D1a hold leg1 (re-INVITE a=sendonly)", "POST",
                  f"/channels/{self.legs['leg1']}/hold")
        self.pause("D1a")
        if self.opt.moh:
            ok, _ = self.ari("D2 start our music on hold to leg1", "POST",
                             f"/channels/{self.legs['leg1']}/moh", mohClass="default")
            self.moh_on = ok
        if self.opt.wait_in:
            self.call_in()
        else:
            self.dial_out(forward)

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
        ev = self.wait_for(RING_TIMEOUT + 15, lambda e: e.get("type") in (
            "StasisStart", "ChannelDestroyed") and self.label_of(channel_of(e).get("id")) == "leg2")
        if ev is None:
            self.fail(f"D1b: the second call neither answered nor ended in {RING_TIMEOUT + 15} s")
            self.hangup("D1b give up on leg2", "leg2")
            self.return_caller(failed=True)
            return
        if ev["type"] == "ChannelDestroyed":
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
        """RFC 3891 section 3: the recipient compares to-tag with its local tag, so to-tag is
        Airtel's tag on our dialog and from-tag is ours, for incoming and outgoing legs."""
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
        ev = self.wait_for(FOCUS_RING + 10, lambda e: e.get("type") in (
            "StasisStart", "ChannelDestroyed") and self.label_of(channel_of(e).get("id")) == label)
        if ev is None:
            self.hangup(f"{step}: give up on {label}", label)
            return "timeout", None
        if ev["type"] == "ChannelDestroyed":
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
        # By label: ARI checks a label exists (404 if not), and does not check a priority. An
        # image built before [testd-refer] then stops here with nothing sent.
        ok, _ = self.ari(f"{step}: send the REFER inside {on}'s dialog ({on} runs testd-refer)",
                         "POST", f"/channels/{cid}/continue", context="testd-refer",
                         extension="s", label="refer")
        if not ok:
            out["error"] = self.last_error
            return out
        self.outside.add(on)
        end = start + REFER_WAIT
        while True:
            left = end - time.monotonic()
            if left <= 0:
                out.update(status="TIMEOUT", secs=float(REFER_WAIT), at=time.monotonic())
                self.log(f"{step}: no REFER result in {REFER_WAIT} s")
                return out
            ev = self.wait_for(min(1.0, left), lambda e: e.get("type") == "StasisStart"
                               and channel_of(e).get("id") == cid and first_arg(e) == "referred")
            now = time.monotonic()
            if ev is not None:
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
                out.update(status="ENDED" if on == moved else "GONE", secs=now - start, at=now)
                return out

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
            return (f"D4: leg3 has gone during the REFER for {party} ({name}): the focus dropped "
                    "us", "FAIL")
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
            self.wait_for(BYE_WAIT, lambda e: e.get("type") == "ChannelDestroyed"
                          and self.label_of(channel_of(e).get("id")) == moved)
        if moved in self.ended:
            gap = self.ended_at[moved] - result["at"]
            when = (f"{gap:.1f} s after the result" if gap >= 0
                    else f"{-gap:.1f} s before the result")
            self.log(f"D4: {self.end_str(moved)}: Airtel replaced {moved} {when}")
            return f"{party} ({name}): SUCCESS after {secs:.1f} s, Airtel ended {party} {when}"
        self.log(f"D4: Airtel did not end {moved} within {BYE_WAIT} s of the result")
        self.hangup(f"D4: release {moved} after the NOTIFY 200 (TS 24.147 5.3.1.3.3)", moved)
        return (f"{party} ({name}): SUCCESS after {secs:.1f} s; Airtel did not end {party}, so "
                "the script released it")

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

    def ask(self, question, limit=ASK_WAIT, talk=False):
        """One line from the terminal (lower case), while ARI events keep being handled. None at
        the end of input or after `limit` s. In the talk, # on leg3 ends it (returns '#') and
        * plays a beep into the conference."""
        if question:
            self.log(question)
        end = time.monotonic() + limit
        while True:
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
            self.keys.append(digit)
            self.dtmf_from.add("leg3")
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
            run = f"--conf --conf-uri '{first}'" + (" --icsi" if self.opt.icsi else "")
            self.passed(f"a conference focus answered: {', '.join(found)}. Run D4 with {run}."
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
        if flash:
            self.log("D4 flash: leg3 is up. If Airtel joined the three calls, both phones heard "
                     "hello-world and can hear each other. Talk on both phones. * beeps and # "
                     "ends, only if keys reach leg3. Press Enter here when done, or type n if "
                     "either could not hear the other. The script cannot hear the call.")
        else:
            self.log("D4: conference up. Talk on both phones. * beeps into the conference and "
                     "# ends, only if keys pass through Airtel's conference. Press Enter here "
                     "when done, or type n if either could not hear the other. The script "
                     "cannot hear the call.")
        self.step = "D4 talk"
        start = time.monotonic()
        said = self.ask(None, limit=TALK_LIMIT, talk=True)
        talk = int(time.monotonic() - start)
        self.required.discard("leg3")           # from here its end is reported, not a failure
        if said is not None and said.startswith("n"):
            heard = "n"
        else:
            self.step = "D4 question"
            heard = self.ask_yn("Did both phones hear each other and hello-world? y/n")
        drop = "not tested (--no-drop)"
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
        if self.opt.no_drop:
            self.stay()

    def stay(self):
        """--no-drop: stay on the conference until leg3 ends, Ctrl-C or TALK_LIMIT. The result
        is already set, so a Ctrl-C here only ends the stay."""
        if "leg3" not in self.legs or "leg3" in self.ended:
            return
        self.step = "D4 stay on the conference"
        self.log(f"D4 --no-drop: staying on the conference for up to {TALK_LIMIT} s. Hang up "
                 "both phones, or press Ctrl-C here to leave it.")
        start = time.monotonic()
        try:
            ev = self.wait_for(TALK_LIMIT, lambda e: e.get("type") == "ChannelDestroyed"
                               and self.label_of(channel_of(e).get("id")) == "leg3")
            how = ("Airtel ended leg3, " + cause_str(*self.ended["leg3"]) if ev is not None
                   else f"the {TALK_LIMIT} s limit")
        except KeyboardInterrupt:
            how = "Ctrl-C"
        secs = int(time.monotonic() - start)
        self.log(f"D4 --no-drop: left the conference after {secs} s ({how})")
        self.detail += f" With --no-drop the script stayed on the conference {secs} s, until {how}."

    # ---- start and finish ----------------------------------------------------------------

    def cleanup(self):
        """Clear TESTD_WAIT, destroy the bridge, hang up every leg (newest first; a leg inside
        [testd-refer] hangs up too, since ARI hangup needs no Stasis), close the websocket."""
        if self.flag_set:
            self.set_flag("0", "cleanup: clear TESTD_WAIT")
        if self.bridge:
            self.ari("cleanup: destroy the bridge", "DELETE", f"/bridges/{self.bridge}",
                     http_timeout=3)
        for label in reversed(list(self.legs)):
            cid = self.legs.get(label)
            if cid and label not in self.ended:
                self.ari(f"cleanup: hang up {label}", "DELETE", f"/channels/{cid}",
                         reason="normal", http_timeout=3)
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
            if self.verdict in ("PASS", "FAIL", "UNCLEAR"):
                if self.opt.conf:
                    self.detail += (" The script cannot hear the call: the audio verdict is "
                                    "the testers'.")
                elif self.opt.probe and self.verdict != "FAIL":
                    self.detail += (" A 200 OK shows signalling only: D4 tests whether a "
                                    "conference carries audio.")
            self.log(f"RESULT: {self.mode} {self.verdict}: {self.detail}")
        if self.verdict == "PASS":
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
            ("--no-drop", opt.no_drop, opt.conf, "--conf")):
        if given and not allowed:
            ap.error(f"{flag} works only with {where}")
    if opt.dial_out and opt.refer_in == "legs":
        ap.error("--dial-out and --refer-in legs do not go together: with --dial-out the focus "
                 "calls the party, so there is no party dialog to send the REFER in")
    if opt.flash and (opt.dial_out or opt.refer_in):
        ap.error("--flash sends no REFER, so --dial-out and --refer-in do not go with it")
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
