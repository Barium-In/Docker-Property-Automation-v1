# Home Phone

The fibre landline that comes with the broadband connection, run from this host through Asterisk. Later stages put a voice assistant on the line. This first stage only proves the line works from here:

1. Asterisk registers the line.
2. An incoming call from a test number gets two-way audio (echo test).
3. Asterisk can ask the carrier to move a call to a mobile number (transfer test).
4. Asterisk can place an outgoing call.

[Test D](#test-d-hold-then-dial) is the first step of stage 2. It checks whether the line can hold one call and place or take a second one, and stays off unless you switch it on. Its last part, [Test D4](#test-d4-network-conference), checks whether Airtel's network will run a 3-way call itself.

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
docker exec home-phone asterisk -rx "dialplan show testd-refer"   # D4: 3 priorities under 's', the first labelled refer
docker exec home-phone asterisk -rx "core show function PJSIP_RESPONSE_HEADER"   # D4: present
```
While the script runs, `docker exec home-phone asterisk -rx "ari show apps"` lists `home-phone-testd`. The script also reads `moh_passthrough` from the running endpoint when it starts and prints `setup: airtel endpoint moh_passthrough=true`. If it reads `false`, the script stops before any call, because a hold would then play local music and send nothing to Airtel.

### Before each run

- **Tell the owner of the forward number first** that a test call is coming from the landline, and what to do: answer, listen for "hello world", and in D1 and D2 let the landline end the call (hanging up first makes the result UNCLEAR). In D3, D3b and E2, talk and press `*`.
- **D3, D3b, E2 and D4 join two phones.** Put the testers in separate rooms, or use wired earphones on both, or the phones will howl.
- **D4 has its own briefing** for the forward-number owner: see [Before a D4 run](#before-a-d4-run).
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
| D4a | `python3 tests/testd.py --probe` | No call in, and no phone rings. The script dials each likely conference factory address once and hangs up. See [Test D4](#test-d4-network-conference). | The `D4a N:` lines and the RESULT. Paste the output and the trace after each run (see [Run D4](#run-d4)). |
| D4 | `python3 tests/testd.py --conf --conf-uri '<the URI D4a found>'` | As D1 until the mobile answers. Then the script asks Airtel for a conference and moves both calls into it, and asks you questions at the terminal. See [Test D4](#test-d4-network-conference). | Your answers to the questions; whether the mobile rang a second time (call waiting); what each phone heard after Airtel's hold message; whether `*` beeped; the drop answer. Paste the output and the trace after each run. |
| D4 flash | `python3 tests/testd.py --conf --flash` | As D1 until the mobile answers. Then the script sends Airtel the 3-party request from the line's own spec (TS 183 043) and asks you the same questions. See [Test D4](#test-d4-network-conference). | As D4, except that the mobile hears no hold message. |

`--moh` can be added to D3, D3b, E2 and D4 as well. `--pause S` changes the 1.5 s wait between re-INVITE steps.

When the session is done:
```bash
docker exec home-phone asterisk -rx "pjsip set logger off"
unset HOME_PHONE_ARI_PASSWORD HOME_PHONE_TEST_PLMN HOME_PHONE_TEST_CONF_URIS
```
The `unset` matters for the password: Docker Compose takes a variable from the shell before the one in `--env-file`, so a later `docker compose up` from this shell would keep using the exported password.

Two more checks fit in the same session:
- **D0.** In the first incoming INVITE, does the SDP offer `telephone-event/8000` (keypad presses as RFC 4733 events)? Which form does the caller's number take in `From` and `P-Asserted-Identity`: +91, 0 or 10 digits? The redacted trace below keeps the form.
- **E.** Run it before E2. During an echo test (press 1), ring the landline from another phone. Watch whether a second INVITE arrives and what the other phone hears. It shows whether Airtel delivers a second incoming call at all, with no hold involved, so it explains an E2 fail.

### Read and paste the trace

The trace holds family numbers and the digest `response` of each authenticated request. Pass it through `sed` before pasting it anywhere. The expression keeps each number's form (+91, 91, 0 or 10 digits) and its last 4 digits, and runs twice so that two numbers one character apart are both caught:
```bash
R='s/(^|[^0-9+])(\+91|91|0)?[0-9]{6}([0-9]{4})([^0-9]|$)/\1\2XXXXXX\3\4/g'
docker logs --since 30m home-phone 2>&1 \
  | grep -E "Transmitting SIP|Received SIP|INVITE sip:|BYE sip:|REFER sip:|NOTIFY sip:|SUBSCRIBE sip:|SIP/2.0 [1-6][0-9][0-9]|Call-ID:|CSeq:|a=(sendonly|recvonly|sendrecv|inactive)|telephone-event|P-Asserted-Identity:|From:|Contact:|isfocus|Refer-To:|Referred-By:|Replaces|Event:|Subscription-State:|Refer-Sub:|Allow:|Supported:|Require:|Unsupported:|Server:|Record-Route:|Reason:|Warning:|TESTD" \
  | sed -E -e "$R" -e "$R" -e 's/(response=")[^"]*/\1hidden/g'
```
The script's own lines already show numbers this way. A copy of them is in `tests/testd-<date>-<time>.log`. For D4, the `SIP/2.0 [1-6]..` pattern also catches the `message/sipfrag` body of each NOTIFY, and the `sed` also masks the number inside each `Refer-To`.

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

After D3 failed, [Test D4](#test-d4-network-conference) decides between these rows:

| D4 | Drop test | Route to a mobile number |
|---|---|---|
| fail | n/a | Consult and return, as in the table above |
| pass | the phones lost each other | Network conference with mixed audio. Ganpath stays on the conference for the whole call. |
| pass | still connected | Transfer by conference: Ganpath consults, joins both into the conference, then leaves |

A pass of D4 with `--flash` counts as a D4 pass in this table. Ganpath would then start each conference with the flash INVITE in place of the REFERs.

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

## Test D4: network conference

D3 showed that the line gives two-way audio to one call at a time, so Asterisk cannot mix two Airtel calls itself. D4 checks whether Airtel's network will run the 3-way call. A VoLTE phone merges calls this way:
1. It calls a conference factory address.
2. Airtel answers with a conference room. The `200 OK` Contact carries `;isfocus`.
3. The phone sends the room one REFER per existing call. Each REFER names that call with `Replaces`.
4. Airtel moves each person into the room and ends the phone's old calls.

The phone is left with one call to the room, and that call carries everyone's voices mixed.

The line's own spec gives a second way. The NOTIFY Airtel sends after registration uses the PSTN emulation schema of ETSI TS 183 043 and provisions its three-party service. In TS 183 043 C.14.3 (option 2, tight coupling), the line's gateway, here Asterisk, invokes the 3-way call while one call is held and one is active. It opens a new dialog with an INVITE to a switching-order address such as `flash@<domain>`. Airtel's application server then re-INVITEs all three calls onto its conference bridge. No REFER is involved.

No public source gives the factory address for Airtel's fixed-line core, so D4 comes in these runs:
- **D4a (`--probe`)** needs no people. It dials each likely address once, logs Airtel's answer and whether a conference room answered, and hangs up within seconds.
- **D4 (`--conf`)** uses the address D4a found. You call in and press 4. The script holds you, rings the forward number, holds that call too and opens the conference. It moves both calls in and plays hello-world into the room. At the end it asks you at the terminal whether you both heard each other, then drops its own call to see whether you two stay connected.
- **D4 with `--flash` (`--conf --flash`)** tests the TS 183 043 way. It runs as D4 until the mobile answers. Then, with you held and the mobile still active, it sends the flash INVITE, waits 5 s for Airtel to join the calls, and asks the same questions. It needs no address from D4a.

D4 adds a three-line dialplan context, `[testd-refer]`, at the end of `config/extensions.conf`. Only ARI can send a call there. It runs `Transfer()`, which sends a REFER inside that call's own SIP dialog, and hands the result (`TRANSFERSTATUS` and the SIP code) back to the script. No endpoint uses the context, so no call from outside reaches it. Nothing is installed in the container and nothing new registers. D1 to E2 run as before.

### Set up D4

The `config/` files are copied into the image when it is built, so the first D4 run needs the new files on den-ubuntu and a rebuild. From this folder:
```bash
git pull
docker compose --env-file ../.env up -d --build
docker exec home-phone asterisk -rx "dialplan show testd-refer"                  # 3 priorities under 's', the first labelled refer
docker exec home-phone asterisk -rx "core show function PJSIP_RESPONSE_HEADER"    # present
docker exec home-phone asterisk -rx "pjsip show registrations"                    # Registered
docker logs home-phone 2>&1 | grep "Test D"                                       # Test D on
```

### Conference factory addresses

The script fills the addresses in at run time, so no value is in git:

| Placeholder | Comes from |
|---|---|
| `{domain}` | the `SIP_DOMAIN` global, which is `HOME_PHONE_SIP_DOMAIN` from the root `.env` |
| `{parent}` | the domain without its first label (only if it has 3 or more labels) |
| `{mcc}`, `{mnc}` | `HOME_PHONE_TEST_PLMN=MCC-MNC` exported in the shell, or `--plmn MCC-MNC`. The MNC is padded to 3 digits. Without either, an address that needs them is skipped. |

The default list, tried in this order:
1. `sip:mmtel@conf-factory.{domain}`: the 3GPP default form (TS 23.003 13.10), built from this line's home domain.
2. `sip:mmtel@conf-factory.ims.mnc{mnc}.mcc{mcc}.3gppnetwork.org`: the form Airtel's mobile network uses.
3. `sip:mmtel@conf-factory.{parent}`.

`--wide` adds `conference-factory@`, `conf-factory@`, `conference@`, `conf@`, `n-way_voice@` and `flash@`, each at `{domain}`. `--conf-uri URI` (repeatable) or `HOME_PHONE_TEST_CONF_URIS` (space-separated, exported in the shell) replaces the default list. With `--flash`, the address is `sip:flash@{domain}` unless `--conf-uri` gives another, and `HOME_PHONE_TEST_CONF_URIS` is not used.

`{mcc}` and `{mnc}` are the MCC and MNC of Airtel's mobile network in this circle, as a phone with an Airtel SIM from the same circle reports them (a network-info app on Android shows both). Write the pair into the private home-phone notes once. Keep it, and the line's domain, out of this repo: export them in the shell.

A URI with a space, quote, bracket, comma, brace or backslash stops the script before it dials anything (`NOT RUN`).

### Before a D4 run

Tell the forward-number owner what to expect:
- Answer the landline's call and you hear hello-world. Then you hear Airtel's hold message for a few seconds, then hello-world again and the caller's voice. Talk to each other.
- If your phone shows a second call from the landline (call waiting), answer it, and tell the person at the terminal.
- At the end the landline drops out. Say whether you can still hear the caller, then hang up.
- With `--flash` your call is not put on hold, so there is no hold message: after the first hello-world, wait for the second one and the caller's voice.

Put the two testers in separate rooms, or on wired earphones.

Run the script in a real terminal. It asks up to three questions there, and with its input redirected (for example from `/dev/null`) the result is UNCLEAR.

### Run D4

Start as under [Run](#run) (export the password, `pjsip set logger on`). Then export the circle's MCC-MNC from the private notes, so the mobile-form address is tried:
```bash
export HOME_PHONE_TEST_PLMN=<MCC-MNC>
```
Without it the script skips that address and says so.

1. **D4a, no phones.** Expect it to take under a minute:
   ```bash
   python3 tests/testd.py --probe
   ```
   It dials the default addresses in turn (two of them without `HOME_PHONE_TEST_PLMN`). It waits up to 20 s for each, logs the answer, hangs up and waits 3 s before the next. It stops at the first conference focus. The last line is `TESTD RESULT: D4a PASS`, `UNCLEAR` or `FAIL`.
2. **If D4a FAILs**, widen it, then run the same again with `--icsi`:
   ```bash
   python3 tests/testd.py --probe --wide --all
   python3 tests/testd.py --probe --wide --all --icsi
   ```
   If nothing answers as a focus, D4 cannot run. The remaining sources are an exported config from a provisioned Airtel router (its `ConferenceCallDomainURI`, or `X_5067F0_ConferenceURI` and `MixerMode`) or Airtel itself, and stage 2 stays on consult and return.
3. **If D4a is UNCLEAR** (a `200 OK` without `;isfocus`), read that `200 OK` in the trace: its Contact, Allow, Server and Record-Route. If it looks like a conference server (Allow lists REFER, the Contact is a conference-style URI), D4 can still try it.
4. **D4.** Brief the forward-number owner first. Then run the command that D4a's RESULT line gives. It carries `--icsi` when D4a found the focus only with it:
   ```bash
   python3 tests/testd.py --conf --conf-uri '<the URI D4a found>'
   ```
   Call the landline from a test number and press 4 after the beep.
5. **D4 with `--flash`.** Run it in the same session whatever D4a found, since it is the way the line's own spec prescribes. Brief the forward-number owner as for D4, including the `--flash` line:
   ```bash
   python3 tests/testd.py --conf --flash
   ```
   Call in and press 4. If the flash INVITE fails with `cause 28` or `cause 1`, this line uses another switching-order address. Its user part is a provisioned prefix and a command, so a later run can try one with `--conf-uri`. A first guess is `--conf --flash --conf-uri 'sip:3@{domain}'`, since 3 starts a 3-party call after a recall on many lines.

After each D4a or D4 run, before the next one:
1. Copy the script's output, from the terminal or the newest `tests/testd-<date>-<time>.log`.
2. Run the grep under [Read and paste the trace](#read-and-paste-the-trace), with `--since 15m` in place of `--since 30m` (longer if the run took longer).
3. Paste both. For D4, also note whether the mobile rang a second time (call waiting), what each phone heard after Airtel's hold message, whether `*` beeped, and your answer to the drop question.

What the script does in D4:
1. A beep to you, then it holds you (as D1a) and dials the forward number (as D1b).
2. It plays hello-world to the mobile, then holds that call too.
3. It reads both calls' Call-ID, tags, remote URI and contact.
4. It calls the conference factory (leg3) and reads the Contact of its `200 OK`.
5. It sends a REFER inside leg3's dialog for the mobile's call, with `Refer-To <sip:+91XXXXXX1234@<domain>;method=INVITE?Replaces=...&Require=replaces>`, and waits for the result and for Airtel to end the old call. It waits 1.5 s, then does the same for your call.
6. It plays hello-world into the conference and prints `D4: conference up`.

During the talk:
- Talk on both phones. Press `*` and then `#` on your phone. A beep and the end of the talk show that keys pass through Airtel's conference; otherwise nothing happens.
- Press Enter at the terminal when you are done, or type `n` if either of you could not hear the other.
- Answer `Did both phones hear each other and hello-world? y/n`.
- The script then hangs up leg3 (with `--flash`, all three of its calls) and asks `Are the two phones still connected? y/n`. The testers answer, then hang up. With `--no-drop` the script skips this and stays on the conference (see the table below).

Each fallback run is its own call, chosen by the result before it:

| Flag | When to use it | What changes |
|---|---|---|
| `--dial-out leg2` | The factory INVITE got cause 58, 17 or 34 while two calls were held. | The script hangs up the mobile's call after the consult, so at most two calls exist, and asks the conference to dial the forward number itself. |
| `--refer-in legs` | The REFER inside the conference's dialog got 405, 501 or 489, or 403 on every form. | Each REFER goes inside that party's own call, with `Refer-To` the conference URI (RFC 4579 / TS 24.147). |
| `--dial-out both` | Both `Refer-To` forms got `NOTIFY` with `481`. | The script hangs up both calls and the conference calls the caller and the mobile back. |
| `--flash` | In every D4 session (step 5 above). | TS 183 043 C.14.3: no hold of the mobile and no REFER. With you held and the mobile active, the script INVITEs `sip:flash@{domain}` (or the first `--conf-uri`) as leg3 and waits 5 s for Airtel to join the calls. The script keeps your call and the mobile's until the drop test. |
| `--icsi` | Repeat the best run with it. | Adds the MMTEL `Accept-Contact` and `P-Preferred-Service` headers to the factory or flash INVITE. |
| `--no-drop` | You want the conference to stay up at the end. | No drop test. The script stays on the conference until it ends (for example when both testers hang up), Ctrl-C, or 300 s, then leaves. |
| `--moh` | As in D2. | Our music plays to you until your call's REFER (with `--flash`, until the flash INVITE). |

When the session is done, follow [Read and paste the trace](#read-and-paste-the-trace) for the last run, then [When the session is done](#run) and [Clear the logs](#clear-the-logs), in that order. The verbose `Executing [s@testd-refer:1] Transfer(...)` lines carry the full number, like other verbose lines.

### Reading the D4 results

Each script line starts with `HH:MM:SS TESTD`. ARI reports a Q.850 cause, which `ast_sip_hangup_sip2cause` maps from the SIP code. The exact SIP code, and any Warning or Reason header, is in the trace.

**D4a**, one `D4a N:` line per address. In the trace each one is an `INVITE sip:<address>` with a new Call-ID, then Airtel's final answer.

| Script line | Meaning |
|---|---|
| `answered; Contact isfocus=yes; conference URI <u>` | A `200 OK` whose Contact carries `;isfocus`: a conference focus. `RESULT: D4a PASS`. Use that address for D4. |
| `answered; Contact isfocus=no` | Something answered without the tag: an announcement server, an application server that took it as a call, or a focus whose `;isfocus` the proxy removed. Read the `200 OK`'s Contact, Allow (is REFER listed?), Server and Record-Route. `RESULT: D4a UNCLEAR`. |
| `answered; Contact isfocus=unreadable` | ARI could not read the Contact. Check `;isfocus` in the trace. |
| `cause 1` (404, 485, 604) | The core does not know the address. |
| `cause 21` (403, 603, or a failed 401/407) | The address is known but refused for this line, or the proxy refuses targets that are not numbers. Read the Warning and Reason headers. |
| `cause 28` (484) | The line's digit analysis took the user part as dialled digits. For `flash@` with no call up this fits TS 183 043 tight coupling, which needs one call held and one active: `--conf --flash` tests it. |
| `cause 58` (488, 606) | With no other call up, SDP or policy. |
| `cause 17` (486, 600) or `cause 34` (503, other 5xx) | Busy or unavailable. A conference server may exist and refuse. |
| `cause 3` (420), `cause 29` (501), `cause 38` (500) | An option tag is needed (see Unsupported), not implemented, server error. |
| `cause 127` | Another 4xx, such as 405, 400 or 415. The trace has the code. |
| `cause 19`, or `neither answered nor ended` | A 480, or no final answer in 20 s. |
| `Airtel played early media (an announcement)` | A 183 with media came before the final answer, so Airtel probably played an announcement. The 183 and its SDP are in the trace. |
| `Asterisk refused the originate (HTTP 400)` | Nothing was sent: pjsip could not use the URI. |

**D4.** D1a and D1b read as in [Reading the results](#reading-the-results).

| Step | What to look for |
|---|---|
| `D4 step 1: hold leg2` | A transmitted INVITE on leg2's Call-ID with `a=sendonly`, answered `200 OK` with `a=recvonly`. The mobile hears Airtel's hold message. |
| `D4 leg1 dialog` and `D4 leg2 dialog` | leg1's Call-ID is the incoming INVITE's. Its `ours` is the To-tag in our `200 OK` and `theirs` is the From-tag. leg2's Call-ID is our INVITE's, and its `theirs` is the To-tag in Airtel's `200 OK`. They go into `Replaces` as `to-tag=theirs;from-tag=ours` (RFC 3891). |
| Factory INVITE (`D4 step 2`) | `leg3 answered` with `isfocus=yes`: the conference is up. `cause 58`, `17` or `34` for an address that answered in D4a: Airtel caps the line at two calls, so run `--dial-out leg2`. `cause 1` or `21` for an address that answered in D4a: the address depends on the call state; note it and try `--dial-out leg2`. |
| `D4 REFER for leg2 (aor): leg3 is back with SUCCESS 0` | The trace shows `REFER sip:...` on leg3's Call-ID with the `Refer-To` above, `SIP/2.0 202 Accepted`, then `NOTIFY` with `Event: refer` and a `message/sipfrag` body: `SIP/2.0 100 Trying`, then `SIP/2.0 200 OK` with `Subscription-State: terminated`. Then a received `BYE` on leg2's Call-ID, and the script logs `Airtel replaced leg2 ... after the result`. A result within about 1 s with no 180 means Airtel matched `Replaces`. A `202` with `Refer-Sub: false` gives `SUCCESS 0` within milliseconds and no NOTIFY. Each NOTIFY's `Event:` should read `refer;id=<the REFER's CSeq number>`. One without `;id` is worth reporting, since the second REFER on leg3 needs it. |
| `FAILURE 405` (or 501, 489, 401, 407) | The REFER itself was refused. No other form follows. The `405`'s Allow and Server headers show whether the conference or the proxy refused it. Next run: `--refer-in legs`. |
| `FAILURE 481` | `202`, then a NOTIFY sipfrag `SIP/2.0 481`. `Replaces` did not match where it was checked; the proxy probably rewrites dialog IDs. The script tries the `contact` form next. If both get 481, run `--dial-out both`. A 481 on leg1 only, after leg2 worked, points to the tag order on the incoming call. |
| `SUCCESS 0 after 4 s or more: the focus probably rang the phone afresh` | NOTIFY sipfrags `100`, `180`, `200` and no BYE on leg2: the conference ignored `Replaces` and rang the mobile again, which shows as call waiting. After 8 s the script logs `Airtel did not end leg2` and releases it. Whether the mobile owner answered the second call decides the audio. |
| `FAILURE 486` | The same, with the mobile busy (no call waiting). |
| `FAILURE 403`, `404`, `484`, `488`, `603` or `604` | The conference's own INVITE to the party was refused, usually because of the number form or policy. The script tries the next form. A 403 can also be the answer to the REFER itself, since Asterisk reports both with the same code: the trace shows a `403` to the REFER or a NOTIFY sipfrag `SIP/2.0 403`. If every form gets 403, the script suggests `--refer-in legs`. |
| `no REFER result in 45 s` | `202` and no final NOTIFY. The conference took the REFER, and either its INVITE never happened or it skips NOTIFY without saying `Refer-Sub: false`. The script hangs up leg3. |
| `FAILURE 1` | NOTIFYs that stop at `100 Trying`, then `Subscription-State: terminated`: the conference gave up without a final answer. Or Asterisk could not send the REFER at all: then the trace has no REFER on leg3's Call-ID. |
| `FAILURE 500` with no NOTIFY | Asterisk's REFER subscription failed or timed out. |
| `leg3 has gone` | A received `BYE` on leg3: the conference dropped us. FAIL. |
| `--refer-in legs`: `leg2 ended after its REFER` | The REFER on leg2's own Call-ID, with `Refer-To <conference URI?Replaces=...>`, got `202 Accepted`, no NOTIFY, then a `BYE` on leg2 within seconds. That is success for this method (RFC 4579 / TS 24.147). A `405` here, like the one on 9 Nov, means Airtel allows no REFER inside a party's call. |
| `D4: conference up` | RTP only, no new SIP. A received re-INVITE on leg3 with `a=sendonly` or `a=inactive` (`the carrier now holds leg3`) means the conference holds us, and the result is FAIL. |
| Keys | `event ChannelDtmfReceived leg3 digit=*` means Airtel's conference passes keypad events through. The script cannot tell who pressed. No key events means the keys stop at the conference server, which the standards allow. |
| Your answers | `y` to both-heard gives PASS. `n` gives FAIL (`signalling joined both calls, but the testers could not hear each other`). No answer gives UNCLEAR. |
| `D4 drop: still connected: y` | Airtel keeps the conference after Ganpath's calls leave, so consult, conference, then leave works as a transfer. `n` means the conference ends with Ganpath's call. Either way the trace shows our `BYE` on leg3 (with `--flash`, on all three calls) and nothing more. |
| `D4 --no-drop: left the conference after N s` | Why the script left: Airtel ended leg3 (a received `BYE` on leg3, for example once both testers hung up), Ctrl-C, or the 300 s limit. The RESULT line repeats it. |
| `Asterisk refused to start the REFER`, after `Requested label can not be found` on the `continue` line | The running image was built before `[testd-refer]` existed. The script stops (STOPPED) before any REFER is sent. Rebuild as under [Set up D4](#set-up-d4). |

**D4 with `--flash`.** D1a and D1b read as above. The mobile's call is not held.

| Step | What to look for |
|---|---|
| `D4 flash: INVITE the switching-order address as leg3` | A transmitted `INVITE sip:flash@...` with a new Call-ID while leg1 is held and leg2 is active. |
| `D4 flash: leg3 answered` | Airtel's application server took the INVITE. In the trace, look for received re-INVITEs on leg1's, leg2's and leg3's Call-IDs in the next seconds, with their `c=` address and direction: TS 183 043 has the server move all three onto its conference bridge. A `ChannelHold` or `ChannelUnhold` line, or a leg that ends, shows in the script as well. The script logs the state of each call 5 s after the answer. |
| `cause 28` (484) | The server read the user part as incomplete digits, so the address needs a prefix and a command. Try another with `--conf-uri`. |
| `cause 1` (404) or `cause 21` (403) | This line has another switching-order address, or refuses this one. |
| `cause 58` (488) | The line refused a third call, so Airtel did not take the INVITE as a switching order. |
| `Airtel ended leg3` | A received `BYE` on leg3 within 5 s of the answer. FAIL. |
| Your answers | As for D4. `y` to both-heard gives `RESULT: D4-flash PASS`, and no `;isfocus` is needed. The drop test hangs up all three of the script's calls, leg3 first, then asks whether you two are still connected. |

Other D4 results:
- `RESULT: D4 PASS` (without `--flash`) also needs `isfocus=yes`. With `isfocus=unreadable` it passes with a note to check the trace. With `isfocus=no` and every other step passing, the result is UNCLEAR: read the factory's `200 OK` before relying on it.
- Each D4 RESULT line ends with what the script cannot know. It cannot hear the call, so the audio verdict is the testers'.
- `Ctrl-C` at any point hangs up leg3 (even during a REFER), then the mobile's call and yours, and clears `TESTD_WAIT`.
- If the conference survives the drop test, the two testers have to hang up themselves.

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
