# Home Phone

The fibre landline that comes with the broadband connection, run from this host through Asterisk. Later stages put a voice assistant on the line. This first stage only proves the line works from here:

1. Asterisk registers the line.
2. An incoming call from a test number gets two-way audio (echo test).
3. Asterisk can ask the carrier to move a call to a mobile number (transfer test).
4. Asterisk can place an outgoing call.

[Test D](#test-d-hold-then-dial) is the first step of stage 2. It checks whether the line can hold one call and place or take a second one, and stays off unless you switch it on.

## Why it is built this way

- **Host networking.** Call audio (RTP, UDP 10000–10100) has to reach the VM's LAN IP directly. Docker's bridge NAT breaks it.
- **One registration.** The carrier expects one device on the line. Switch the account off in any softphone (Zoiper: **Unregister**) before starting this, or the two will steal calls from each other.
- **Secrets in the root `.env`.** The number, password and test numbers are rendered into the Asterisk config when the container starts. Nothing personal is in git; this repo is public.
- **Small surface.** PJSIP on UDP 5060 is the only port open to the network. There is no AMI. ARI listens on loopback (127.0.0.1:8088) only while `HOME_PHONE_ARI_PASSWORD` is set for Test D; without it there is no ARI and no web server. Calls are accepted only from the carrier's proxy address; there is no anonymous endpoint, so SIP from anywhere else is refused.
- **Logs.** Docker keeps at most 30 MB of console log (3 files of 10 MB). NOTICE lines show only the last 4 digits of a number. The verbose lines and SIP traces still carry full numbers, and so does `logs/messages.log`, which nothing rotates: empty it after a test (see [Clear the logs](#clear-the-logs)).

## Setup

1. In the root `.env`, fill in the `HOME_PHONE_*` block (see `.env.example`). Type the password yourself.
2. Check that the SIP port is free on the VM. This should print nothing:
   ```bash
   sudo ss -lunp | grep -E ':5060\b'
   ```
3. Start it from this folder:
   ```bash
   docker compose --env-file ../.env up -d --build
   docker logs -f home-phone
   ```
4. Check the registration. `Registered` is the goal:
   ```bash
   docker exec home-phone asterisk -rx "pjsip show registrations"
   ```

## Tests

Put your own mobile number in `HOME_PHONE_TEST_CALLERS` and a second family mobile in `HOME_PHONE_TEST_FORWARD`. Callers who aren't on the test list just hear ringing, so the line behaves as unanswered for everyone else while this stage runs.

Menu option 4 belongs to [Test D](#test-d-hold-then-dial). While Test D is off, pressing 4 gives an error beep.

**Echo test.** Call the landline from a test number. You hear "hello world" and a beep. Press 1, or wait. Then speak: you should hear yourself back. That proves audio flows both ways.

**Transfer test (REFER).** Call again and press 2 after the beep. Asterisk asks the carrier to connect you to the `HOME_PHONE_TEST_FORWARD` number and drop out.
- Works: that mobile rings, and you talk to it while Asterisk is no longer on the call.
- Fails: you hear an error beep and the call ends.

Either way, the log has a line with the result and the SIP code (on this line, Airtel answers REFER with 405: transfer isn't allowed):
```bash
docker logs home-phone 2>&1 | grep "TRANSFER RESULT"
```

**Bridge test (second call).** Call again and press 3 after the beep. Asterisk keeps your call and places a second call from the landline to the `HOME_PHONE_TEST_FORWARD` number, then joins the two. This only works if the line carries two calls at once.
- Works: you hear that mobile ring, and once it answers you can talk to each other through Asterisk.
- Fails: you hear an error beep. The log says why:
```bash
docker logs home-phone 2>&1 | grep "BRIDGE RESULT"
```

**Deflect test (302).** Set `HOME_PHONE_TEST_MODE=deflect`, restart with `docker compose --env-file ../.env up -d`, and call. Asterisk asks the carrier to redirect the call before answering. Read the result the same way. Set it back to `menu` afterwards.

**Outgoing call.** Ring your own mobile. Replace the number in this command; keep the +91 form. When you answer, you hear "hello world" and the echo test:
```bash
docker exec home-phone asterisk -rx "channel originate PJSIP/+91XXXXXXXXXX@airtel extension s@outbound-test"
```
If the carrier rejects the `+91` form, try `0XXXXXXXXXX`.

## Test D: hold, then dial

The line refuses a second call while one is active (488) and refuses transfers (405). Test D checks whether it carries a second call once the first is on SIP hold, the way a phone does call waiting and 3-way calls. The answer decides how Ganpath can reach a family member's mobile.

What it adds, only while `HOME_PHONE_ARI_PASSWORD` is set:
- ARI, Asterisk's call-control interface, on 127.0.0.1:8088. With host networking, only processes on den-ubuntu can reach it.
- `moh_passthrough=yes` on the `airtel` endpoint. A hold then goes to Airtel as a re-INVITE with `a=sendonly`, in place of local music.
- `tests/testd.py`, which runs on den-ubuntu with Python 3 and nothing else installed. It gets the forward number from the dialplan and the password from your shell, so neither is in the file.

With the password empty or unset, the container starts as in stage 1: no web server, no ARI, no passthrough, and option 4 gives an error beep.

### Switch it on

1. Check that nothing else uses port 8088. With host networking, another program on that port would receive the script's password. This should print nothing:
   ```bash
   sudo ss -ltnp | grep ':8088'
   ```
2. Make a password and type it into the root `.env` yourself, without quotes. A quoted value keeps its quotes in the `export` under [Run](#run), and the script then gets a 401.
   ```bash
   openssl rand -hex 24
   ```
   ```
   HOME_PHONE_ARI_PASSWORD=<the output>
   ```
3. Rebuild from this folder:
   ```bash
   docker compose --env-file ../.env up -d --build
   docker logs home-phone 2>&1 | grep "Test D"      # Test D on: ARI on 127.0.0.1:8088, hold passthrough on
   ```

### Checks

```bash
docker exec home-phone asterisk -rx "http show status"      # Server Enabled and Bound to 127.0.0.1:8088
docker exec home-phone asterisk -rx "ari show users"        # No    homephone   (No = not read-only)
docker exec home-phone asterisk -rx "pjsip show endpoint airtel" | grep moh_passthrough   # true
docker exec home-phone asterisk -rx "moh show classes"      # Class: default
sudo ss -ltnp | grep 8088                                   # 127.0.0.1:8088 only
```
While the script runs, `docker exec home-phone asterisk -rx "ari show apps"` lists `home-phone-testd`. The script also reads `moh_passthrough` from the running endpoint when it starts and prints `setup: airtel endpoint moh_passthrough=true`. If it reads `false`, the script stops before any call, because a hold would then play local music and send nothing to Airtel.

### Before each run

- **Tell the owner of the forward number first** that a test call is coming from the landline, and what to do: answer, listen for "hello world", and in D1 and D2 let the landline end the call (hanging up first makes the result UNCLEAR). In D3, D3b and E2, talk and press `*`.
- **D3, D3b and E2 join two phones.** Put the testers in separate rooms, or use wired earphones on both, or the phones will howl.
- Run each mode as its own call. The script handles one test and exits. Its last line is `TESTD RESULT: ...`.
- `Ctrl-C` stops the script at any point. It then clears `TESTD_WAIT`, hangs up the calls it holds and closes its connection.

### Run

On den-ubuntu, from this folder:
```bash
export HOME_PHONE_ARI_PASSWORD="$(grep '^HOME_PHONE_ARI_PASSWORD=' ../.env | cut -d= -f2-)"
docker exec home-phone asterisk -rx "pjsip set logger on"
```

Then, for each mode: start the script, call the landline from a test number, and press 4 after the beep.

| Mode | Command | What happens | What to note |
|---|---|---|---|
| D1 | `python3 tests/testd.py` | You hear a beep and go on hold. The landline rings the forward number. When it answers it hears "hello world" and is hung up. You come off hold and hear "hello world". | Did the mobile ring? Did you hear "hello world" at the end? |
| D2 | `python3 tests/testd.py --moh` | As D1, and our hold music plays to you while you are held. | What you heard on hold: silence, an Airtel message, or our music. |
| D3 | `python3 tests/testd.py --mix` | When the mobile answers, the script uses a phone's order: mobile on hold, you off hold, mobile off hold. Then both calls are joined and both hear "hello world". `*` on either phone plays a beep; `#` ends the test. | Can you hear each other? Did `*` beep from both phones? |
| D3b | `python3 tests/testd.py --mix-direct` | As D3 with a shorter order: you come off hold while the mobile stays active, then both are joined. Run it after D3. | As D3. |
| E2 | `python3 tests/testd.py --wait-in` | You go on hold and the script waits 60 s. The forward-number owner rings the landline. The script answers, takes you off hold and joins both. | Did the second call ring through, or did the mobile hear busy or a message? |

`--moh` can be added to D3, D3b and E2 as well. `--pause S` changes the 1.5 s wait between re-INVITE steps.

When the session is done:
```bash
docker exec home-phone asterisk -rx "pjsip set logger off"
unset HOME_PHONE_ARI_PASSWORD
```
The `unset` matters: Docker Compose takes a variable from the shell before the one in `--env-file`, so a later `docker compose up` from this shell would keep using the exported password.

Two more checks fit in the same session:
- **D0.** In the first incoming INVITE, does the SDP offer `telephone-event/8000` (keypad presses as RFC 4733 events)? Which form does the caller's number take in `From` and `P-Asserted-Identity`: +91, 0 or 10 digits? The redacted trace below keeps the form.
- **E.** Run it before E2. During an echo test (press 1), ring the landline from another phone. Watch whether a second INVITE arrives and what the other phone hears. It shows whether Airtel delivers a second incoming call at all, with no hold involved, so it explains an E2 fail.

### Read and paste the trace

The trace holds family numbers and the digest `response` of each authenticated request. Pass it through `sed` before pasting it anywhere. The expression keeps each number's form (+91, 91, 0 or 10 digits) and its last 4 digits, and runs twice so that two numbers one character apart are both caught:
```bash
R='s/(^|[^0-9+])(\+91|91|0)?[0-9]{6}([0-9]{4})([^0-9]|$)/\1\2XXXXXX\3\4/g'
docker logs --since 30m home-phone 2>&1 \
  | grep -E "Transmitting SIP|Received SIP|INVITE sip:|BYE sip:|SIP/2.0 [1-6][0-9][0-9]|Call-ID:|CSeq:|a=(sendonly|recvonly|sendrecv|inactive)|telephone-event|P-Asserted-Identity:|From:|Reason:|Warning:|TESTD" \
  | sed -E -e "$R" -e "$R" -e 's/(response=")[^"]*/\1hidden/g'
```
The script's own lines already show numbers this way. A copy of them is in `tests/testd-<date>-<time>.log`.

### Reading the results

| Step | Pass | Fail | What a fail means |
|---|---|---|---|
| D1a hold | A transmitted INVITE with the incoming call's Call-ID, CSeq one higher and `a=sendonly`. Airtel answers `200 OK` with `a=recvonly` or `a=inactive`. The script logs `D1a, 1.5 s later: leg1 Up`. | 403, 488, 491, 501 or similar, or the script logs `leg1 ended` | Airtel refuses SIP hold. Stop here: mobiles get the notification and call-back route. |
| D1b second call | An INVITE to the forward number with a new Call-ID, then 100, then 180 or 183, then `200 OK`. The script logs `D1b PASS`. | `488 Not Acceptable Here` again; the script shows `cause 58`. Others: 486 → 17, 403/603 → 21, 503 → 34, 480 → 19. | Holding does not unlock a second call. Mobiles get the notification and call-back route. |
| D1c return | An INVITE on the first Call-ID with `a=sendrecv` gets `200 OK`, the caller hears "hello world", and the script ends with `RESULT: D1 PASS`. | A 4xx, a BYE, or a `200 OK` that carries `a=sendonly` or `a=inactive` (the script logs `the carrier now holds leg1` and ends with FAIL) | Even consult-and-return breaks the caller's call. |
| D2 hold audio | Record what the held caller heard: silence, an Airtel message (watch for a network re-INVITE that moves the media), or our music. | n/a | Decides whether Ganpath plays its own music while it consults. |
| D3 3-way | Leg 2 `sendonly` gets 200, leg 1 `sendrecv` gets 200, leg 2 `sendrecv` gets 200. Both phones hear each other and "hello world", and `*` beeps. `RESULT: D3 PASS`. | A 4xx on a `sendrecv` re-INVITE, a network BYE on either call (`leg1 ended` or `leg2 ended`), or a `200 OK` to a `sendrecv` re-INVITE that carries `a=sendonly` or `a=inactive`: the carrier keeps that call on hold itself. The script logs `the carrier now holds leg2` and ends with FAIL. This is what Airtel did on 10 Nov. | Only one active call is allowed. Mobiles get consult and return, without conference. |
| D3b 3-way, short order | Leg 1 `sendrecv` gets 200 while leg 2 stays active, then as D3. `RESULT: D3b PASS`. | As D3 | Ganpath must use D3's longer order to join a mobile. |
| E2 call in | A second incoming INVITE arrives while leg 1 is held (script: `event StasisStart new call ... args=['incoming']`). Asterisk answers it, the unhold gets 200, and both phones hear each other. `RESULT: E2 PASS`. | No second INVITE in the trace (the mobile hears busy or a message), or a 4xx or BYE after the answer, or the carrier holds one of the calls from its side as in D3. The script ends with `RESULT: E2 FAIL`. | A family member who rings back cannot be joined to a held caller. |

ARI reports only the Q.850 cause and its text, so the SIP code comes from the trace. The script's hint after each cause comes from `ast_sip_hangup_sip2cause` in [res_pjsip.c](https://github.com/asterisk/asterisk/blob/20.6.0/res/res_pjsip.c).

Other results:
- In D3, D3b and E2 the script cannot hear the calls. Before 10 Nov it printed PASS whenever both calls stayed up, even when Airtel had put one back on hold. It now fails the run when a call is still held from Airtel's side, but what each phone heard is still the deciding check.
- `RESULT: ... UNCLEAR`: in D3, D3b or E2, a call ended within 10 s of being joined. In D1 or D2, the mobile's call ended before the script hung it up; the script still takes you off hold, so D1c is tested. Check the trace for who sent the BYE.
- `RESULT: ... STOPPED: Asterisk refused ...`: Asterisk itself rejected a hold or unhold, so no re-INVITE went to Airtel for that step and the run says nothing about the line. Check `docker logs home-phone` for the reason and run it again.
- `RESULT: ... STOPPED: stopped by hand`: Ctrl-C. Run that mode again.

**What each result means for the plan.**

| D1a | D1b | D3 or D3b | E2 | Route to a mobile number |
|---|---|---|---|---|
| fail | n/a | n/a | n/a | Notification and call-back |
| pass | fail | n/a | fail | Notification and call-back |
| pass | fail | n/a | pass | Call-in: the family member rings the landline back and Ganpath joins them to the caller |
| pass | pass | fail | any | Consult and return. "Join" becomes a call-back, or a call-in if E2 passed |
| pass | pass | pass | any | Consult, then conference, using the order that passed |

A family member's SIP app gets a conference in every case, because it does not use the Airtel line.

### Clear the logs

Once the results are written down, remove the full numbers that the test left behind:
```bash
docker exec home-phone truncate -s 0 /var/log/asterisk/messages.log
rm -f tests/testd-*.log
```
Docker's own copy rotates away after 30 MB. To drop it now, recreate the container: `docker compose --env-file ../.env up -d --force-recreate`.

### Switch it off (rollback)

1. Empty `HOME_PHONE_ARI_PASSWORD` in the root `.env`, or delete the line.
2. `unset HOME_PHONE_ARI_PASSWORD; docker compose --env-file ../.env up -d` (without the `unset`, a password still exported in this shell keeps Test D on)
3. Check: `docker logs home-phone 2>&1 | grep "Test D"` says `Test D off`, `http show status` says `Server Disabled`, and `moh_passthrough` reads `false`.

The container then runs as in stage 1, and option 4 gives an error beep. Options 1, 2 and 3, deflect mode and the outgoing test are unchanged throughout. The Test D files can stay in place, since none of them runs without the password; to remove them from the repo, revert the Test D commit.

## Troubleshooting

Turn on the SIP message log, make the call, then turn it off. The log shows SIP headers, which include the line number but not the password:
```bash
docker exec home-phone asterisk -rx "pjsip set logger on"
docker exec home-phone asterisk -rx "pjsip set logger off"
```

- **Registration never succeeds:** check that the VM can reach the proxy (`ping` the `HOME_PHONE_SIP_PROXY` address), and that no softphone is still registered.
- **401/403 on register:** the password, or the auth username form (`<number>@<domain>`).
- **Call connects but there is no audio one way:** NAT. Look for a SIP ALG option on the router or gateway and turn it off.
- **Outgoing call rejected with 404/484:** try the `0XXXXXXXXXX` number form.
- **Option 4 gives an error beep at once:** the script isn't running, or Test D is off. `docker logs home-phone 2>&1 | grep -E "Test D|home-phone-testd"` shows which. `Stasis app 'home-phone-testd' not registered` (no run since Asterisk started) or `... not active` (an earlier run has ended) means the script isn't connected.
- **The SSH session closed mid-test:** the script still clears up (hangs up both calls, resets `TESTD_WAIT`) and writes its `RESULT` line to `tests/testd-<date>-<time>.log`.
- **The script was killed with `kill -9` mid-test:** the caller may sit in silence until they hang up, and the next call in goes through the stage-1 menu as usual. `docker exec home-phone asterisk -rx "channel request hangup all"` ends any call left over.
- **The script stops with `401`:** the exported `HOME_PHONE_ARI_PASSWORD` differs from the one the container started with. Export it again, or recreate the container after changing `.env`.
