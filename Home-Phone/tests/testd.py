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
In a 3-way, * on either phone plays a beep and # ends the test.
If the carrier holds a leg from its side (a ChannelHold event: its answer to an unhold, or its
own re-INVITE, carried a=sendonly or a=inactive) and the leg is still held when the test
checks, the result is FAIL: that leg never had two-way audio.

  export HOME_PHONE_ARI_PASSWORD="$(grep '^HOME_PHONE_ARI_PASSWORD=' ../.env | cut -d= -f2-)"
  python3 tests/testd.py [--moh] [--mix | --mix-direct | --wait-in]

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

    def call(self, method, path, http_timeout=10, **params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"http://{HOST}:{PORT}/ari{path}" + (f"?{query}" if query else "")
        data = None if method == "GET" else b""
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": self.auth})
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
        self.mode = ("D3" if opt.mix else "D3b" if opt.mix_direct else "E2" if opt.wait_in
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

    def ari(self, step, method, path, http_timeout=10, **params):
        status, reason, body = self.rest.call(method, path, http_timeout=http_timeout, **params)
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
        for label in ("leg1", "leg2"):
            cid = self.legs.get(label)
            if not cid:
                continue
            if label in self.ended:
                parts.append(f"{label} ended, {cause_str(*self.ended[label])}")
                continue
            status, _, body = self.rest.call("GET", f"/channels/{cid}", http_timeout=5)
            parts.append(f"{label} {body.get('state', '?')}" if status == 200
                         else f"{label} not found (HTTP {status})")
        return "; ".join(parts)

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
        self.wait_for(5, lambda e: e.get("type") == "ChannelDestroyed"
                      and self.label_of(channel_of(e).get("id")) == label)
        return ok

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
        self.log(f"ready for {self.mode}. Call the landline from a test number and press 4 "
                 "after the beep. Ctrl-C stops.")
        ev = self.wait_for(None, lambda e: e.get("type") == "StasisStart"
                           and first_arg(e) not in ("second", "incoming"))
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

    # ---- start and finish ----------------------------------------------------------------

    def cleanup(self):
        """Clear TESTD_WAIT, destroy the bridge, hang up both legs, close the websocket."""
        if self.flag_set:
            self.set_flag("0", "cleanup: clear TESTD_WAIT")
        if self.bridge:
            self.ari("cleanup: destroy the bridge", "DELETE", f"/bridges/{self.bridge}",
                     http_timeout=3)
        for label in ("leg2", "leg1"):
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
    ap.add_argument("--pause", type=float, default=1.5, metavar="S",
                    help="seconds between re-INVITE steps (default 1.5)")
    opt = ap.parse_args()

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
