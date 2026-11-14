# Home Phone

The fibre landline that comes with the broadband connection, run from this host through Asterisk. Later stages put a voice assistant on the line. This first stage only proves the line works from here:

1. Asterisk registers the line.
2. An incoming call from a test number gets two-way audio (echo test).
3. Asterisk can ask the carrier to move a call to a mobile number (transfer test).
4. Asterisk can place an outgoing call.

[Test D](#test-d-hold-then-dial) is the first step of stage 2. It checks whether the line can hold one call and place or take a second one, and stays off unless you switch it on. Its last part, [Test D4](#test-d4-network-conference), checks whether Airtel's network will run a 3-way call itself.

[Asterisk 22.12](#asterisk-2212-stage-2-phase-7) is an opt-in image for the rest of stage 2; the default build stays on Ubuntu's Asterisk 20.6.

[Ganpath mode](#ganpath-stage-2-phase-1) sends test callers (or, in `ganpath_all`, every caller) to the Ganpath assistant service in place of the test menu. It stays off unless you switch it on.

## Why it is built this way

- **Host networking.** Call audio (RTP, UDP 10000–10100) has to reach the VM's LAN IP directly. Docker's bridge NAT breaks it.
- **One registration.** The carrier expects one device on the line. Switch the account off in any softphone (Zoiper: **Unregister**) before starting this, or the two will steal calls from each other.
- **Secrets in the root `.env`.** The number, password and test numbers are rendered into the Asterisk config when the container starts. Nothing personal is in git; this repo is public.
- **Small surface.** PJSIP on UDP 5060 is the only port open to the network. There is no AMI. ARI listens on loopback (127.0.0.1:8088) only while `HOME_PHONE_ARI_PASSWORD` (Test D) or `HOME_PHONE_GANPATH_ARI_PASSWORD` (Ganpath) is set; without either there is no ARI and no web server. Calls are accepted only from the carrier's proxy address; there is no anonymous endpoint, so SIP from anywhere else is refused.
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

**Owner call.** Ring the number in `HOME_PHONE_TEST_FORWARD` and play Ganpath's recorded line `ganpath-owner-call` twice (asking you to come back to the computer). It falls back to "hello world" while that prompt is not rendered. The line carries one call at a time, so check that nothing is up first:
```bash
docker exec home-phone asterisk -rx "core show channels count" | grep -q '^0 active call' \
  && docker exec home-phone asterisk -rx "channel originate Local/s@owner-call-dial/n extension s@owner-call-say"
```
The log shows `Owner call to the number ending in ...` and `OWNER CALL RESULT`.

## Test D: hold, then dial

The line refuses a second call while one is active (488) and refuses transfers (405). Test D checks whether it carries a second call once the first is on SIP hold, the way a phone does call waiting and 3-way calls. The answer decides how Ganpath can reach a family member's mobile.

What it adds, only while `HOME_PHONE_ARI_PASSWORD` is set:
- ARI, Asterisk's call-control interface, on 127.0.0.1:8088. With host networking, only processes on den-ubuntu can reach it.
- `moh_passthrough=yes` on the `airtel` endpoint. A hold then goes to Airtel as a re-INVITE with `a=sendonly`, in place of local music.
- `tests/testd.py`, which runs on den-ubuntu with Python 3 and nothing else installed. It gets the forward number from the dialplan and the password from your shell, so neither is in the file.

With the password empty or unset, the container starts as in stage 1: no web server, no ARI, no passthrough, and option 4 gives an error beep. (If `HOME_PHONE_GANPATH_ARI_PASSWORD` is set, the web server and the ARI user `ganpath` stay on; see [Ganpath](#ganpath-stage-2-phase-1).)

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
- `Ctrl-C` stops the script at any point. It then clears `TESTD_WAIT`, deletes the D4 key-check recording if one exists, hangs up the calls it holds and closes its connection.

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
| D4 | `python3 tests/testd.py --conf --conf-uri '<the URI D4a found>'` | As D1 until the mobile answers. Then the script asks Airtel for a conference and moves both calls into it, runs the key check during the talk, and asks you questions at the terminal. See [Test D4](#test-d4-network-conference). | Your answers to the questions; whether the mobile rang a second time (call waiting); what each phone heard after Airtel's hold message; whether `*` beeped; the `D4 key check` lines; the drop answer. Paste the output and the trace after each run. |
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
The D4 key check deletes its recording itself and logs `deleted the recording testd-keys-... in Asterisk`. If the script was killed hard (`kill -9`) during the talk, a recording may be left in the container's memory. List and remove it:
```bash
docker exec home-phone ls -l /var/spool/asterisk/recording
docker exec home-phone sh -c 'rm -f /var/spool/asterisk/recording/testd-keys-*'
```
The `sh -c` lets the container's own shell expand the `*`. The folder is a tmpfs, so restarting the container also clears it.
Docker's own copy rotates away after 30 MB. To drop it now, recreate the container: `docker compose --env-file ../.env up -d --force-recreate`.

### Keypad PINs (Ganpath phase 5)

Family members type a PIN on the keypad during a call. Key events are printed by these debug
commands: `rtp set debug`, `core set debug`, `ari set debug` and `pjsip set logger`. Run them only when
no PIN entry is expected. Afterwards truncate the log:

    docker exec home-phone sh -c ': > /var/log/asterisk/messages.log'

`config/logger.conf` keeps no `debug` and no `dtmf` level on any channel, so a PIN digit never reaches
`messages.log` in normal running.

### Switch it off (rollback)

1. Empty `HOME_PHONE_ARI_PASSWORD` in the root `.env`, or delete the line.
2. `unset HOME_PHONE_ARI_PASSWORD; docker compose --env-file ../.env up -d` (without the `unset`, a password still exported in this shell keeps Test D on)
3. Check: `docker logs home-phone 2>&1 | grep "Test D"` says `Test D off`, `http show status` says `Server Disabled` (or `Server Enabled` while `HOME_PHONE_GANPATH_ARI_PASSWORD` is set), and `moh_passthrough` reads `false`.

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

D4 also runs a **key check** during the talk. In the Ganpath design a family member presses `*` to bring Ganpath back into a call. In a merged network conference Ganpath has only its own call to the conference (leg3), which carries the mixed audio. The key check shows whether key presses from either phone reach leg3 at all:
- as keypad events (RFC 4733), which the script logs as `event ChannelDtmfReceived leg3`;
- or as DTMF tones inside the mixed audio. Asterisk does not detect those, because `telephone-event` was negotiated on leg3, so they would pass as plain sound.

For the second, the script puts an ARI snoop channel on leg3 that hears what the conference sends (`spy=in`) and records it as an 8 kHz WAV, `/var/spool/asterisk/recording/testd-keys-<random>.wav` in the container. `docker-compose.yml` mounts that folder as a 64 MB tmpfs, so the recording is held in memory and no file of it is written to den-ubuntu's disk. When the talk ends the script stops the recording, downloads the file into its own memory, deletes it in Asterisk and searches it for tones with a detector modelled on Asterisk's own (`main/dsp.c`). The two phones press different digits, so a tone shows whose key got through. `--no-key-check` skips the recording. With `--watch-end` the same recording runs on until the end of the watch (see [End of a merged call](#end-of-a-merged-call)), and `--no-key-check` then skips only the tone search.

### Set up D4

The `config/` files are copied into the image when it is built, so the first D4 run needs the new files on den-ubuntu and a rebuild. The key check needs the container recreated from the current `docker-compose.yml`, which mounts `/var/spool/asterisk/recording` as a tmpfs. Ubuntu's Asterisk package ships no such folder, so without the mount ARI's record answers HTTP 500 and the key check does not run. The `up` below does both. From this folder:
```bash
git pull
docker compose --env-file ../.env up -d --build
docker exec home-phone asterisk -rx "dialplan show testd-refer"                  # 3 priorities under 's', the first labelled refer
docker exec home-phone asterisk -rx "core show function PJSIP_RESPONSE_HEADER"    # present
docker exec home-phone asterisk -rx "pjsip show registrations"                    # Registered
docker logs home-phone 2>&1 | grep "Test D"                                       # Test D on
docker exec home-phone df -h /var/spool/asterisk/recording                        # key check: tmpfs, 64M
docker exec home-phone ls -ld /var/spool/asterisk/recording                       # key check: drwx------ asterisk asterisk
docker exec home-phone asterisk -rx "module show like snoop"                      # key check: res_stasis_snoop.so Running
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
- The caller presses 1 three times and then says "now". Then press 2 three times, about a second apart, so the script can tell whose key presses got through. Stay quiet while either of you presses keys: speech over a tone can hide it.
- The script records the conference audio during the talk, only to look for key tones. The recording stays in memory inside the container on den-ubuntu, and the script deletes it as soon as the talk ends.
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
4. **D4.** Brief the forward-number owner first. Then run the command that D4a's RESULT line gives. When the probe ran with `--icsi`, the line says so: add `--icsi` to D4 only if no probe without it found the same focus.
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
6. It plays hello-world into the conference.
7. It starts the key check: a snoop channel on leg3 (`D4 key check: snoop on leg3`) and its recording (`D4 key check: recording testd-keys-... in the container`). Then it prints `D4: conference up` (with `--flash`, `D4 flash: leg3 is up`).

During the talk:
- Talk on both phones until you hear each other.
- Then the key check, once the terminal shows `D4: conference up` (with `--flash`, `D4 flash: leg3 is up`). That line comes after the recording has started. Both of you stay quiet while keys are pressed. Press 1 three times on your phone, about a second apart, then say "now". The forward-number owner presses 2 three times the same way. Then press `*` and then `#` on your phone.
- A beep after `*` and the end of the talk after `#` show that keys reach the script as keypad events. The recording shows whether they reach the conference audio as tones.
- Press Enter at the terminal when you are done, or type `n` if either of you could not hear the other.
- The script stops the recording, downloads it into memory, deletes it in Asterisk (`deleted the recording testd-keys-... in Asterisk`) and searches it for tones. This takes a few seconds, then the next question comes.
- Answer `Did both phones hear each other and hello-world? y/n`.
- The script then hangs up leg3 (with `--flash`, all three of its calls) and asks `Are the two phones still connected? y/n`. The testers answer, then hang up. With `--no-drop` the script skips this and stays on the conference (see the table below).

The recording runs only during the talk. If leg3 ends during the talk, the snoop channel and its recording end with it, and the script still downloads, deletes and searches what was recorded. On `Ctrl-C`, or when Asterisk closes the event connection, the script deletes the recording without searching it.

Each fallback run is its own call, chosen by the result before it:

| Flag | When to use it | What changes |
|---|---|---|
| `--dial-out leg2` | The factory INVITE got cause 58, 17 or 34 while two calls were held. | The script hangs up the mobile's call after the consult, so at most two calls exist, and asks the conference to dial the forward number itself. |
| `--refer-in legs` | The REFER inside the conference's dialog got 405, 501 or 489, or 403 on every form. | Each REFER goes inside that party's own call, with `Refer-To` the conference URI (RFC 4579 / TS 24.147). |
| `--dial-out both` | Both `Refer-To` forms got `NOTIFY` with `481`. | The script hangs up both calls and the conference calls the caller and the mobile back. |
| `--flash` | In every D4 session (step 5 above). | TS 183 043 C.14.3: no hold of the mobile and no REFER. With you held and the mobile active, the script INVITEs `sip:flash@{domain}` (or the first `--conf-uri`) as leg3 and waits 5 s for Airtel to join the calls. The script keeps your call and the mobile's until the drop test. |
| `--icsi` | Repeat the best run with it. | Adds the MMTEL `Accept-Contact` and `P-Preferred-Service` headers to the factory or flash INVITE. |
| `--no-drop` | You want the conference to stay up at the end. | No drop test. The script stays on the conference until it ends (for example when both testers hang up), Ctrl-C, or 300 s, then leaves. The key check still ends with the talk. |
| `--hold-leg3 [S]` (with `--no-drop`) | Run 2 of the line check (below). | Before the stay, on Enter, the script holds leg3 for S s (default 10), takes it off hold, plays a beep into the conference and asks what you heard. Not with `--flash`. |
| `--watch-end [S]` | [End of a merged call](#end-of-a-merged-call). | No drop test. After the heard question both testers hang up and press Enter. The script keeps leg3 up for S s (default 180, 10 to 900) and logs whether Airtel ends it. The recording runs until the watch ends and is summarised as levels. Not with `--flash` or `--hold-leg3`. |
| `--in2 [--in2-wait S]` | [Incoming second call](#incoming-second-call). | leg2 is a call in from the forward number, in call-waiting order: you stay on an active call, the script keeps the second call ringing and you press 1 to add it or 2 to refuse it (S s to decide, default 20, 5 to 120). The RESULT says `D4-in2`. Not with `--flash` or `--dial-out`. |
| `--no-key-check` | Someone on the call does not want it recorded, or the key question is already answered. | No snoop channel and no recording. Keypad events on leg3 are still logged, and the RESULT says `Key check: off`. With `--watch-end` the recording still runs, for the levels only. |
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
| `D4 leg1 dialog` and `D4 leg2 dialog` | leg1's Call-ID is the incoming INVITE's. Its `ours` is the To-tag in our `200 OK` and `theirs` is the From-tag. leg2's Call-ID is our INVITE's, and its `theirs` is the To-tag in Airtel's `200 OK`. With `--in2` leg2 came in, so it reads like leg1. They go into `Replaces` as `to-tag=theirs;from-tag=ours` in every case: RFC 3891 defines the tags from the side that receives the Replaces, not by the first INVITE's To and From. |
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
| Keys | `event ChannelDtmfReceived leg3 digit=*` means Airtel's conference passes keypad events through. The digit shows who pressed: 1 is the test phone and 2 the forward number's phone. No key events means the keys stop at the conference server as events, which the standards allow; the key check then says whether they come through as tones. |
| `D4 key check: tone 1 at 4.21 s, 96 ms, low tone -9 dBm0, high tone -7 dBm0`, one line per tone, then `7 key tone(s): 1 x3, 2 x3, * x1 in 34 s of conference audio` | The conference plays key presses into the mixed audio as DTMF tones. The digits show whose presses got through. Times count from the start of the recording. The levels show where a tone came from: one the network plays sits near -10 dBm0 per tone, a few dB lower after the mixer, and one far below that (under about -25 dBm0) is more likely a phone's own keypad sound picked up by its microphone. RESULT: `Key check: ...; in-band tones in the conference audio: 1 x3, 2 x3, * x1`. For Ganpath: it can listen for `*` in the mix it hears on its own call. |
| RESULT `Key check: keypad events on leg3: 1 1 1 2 2 2 * #` | Key presses arrive as events, as on a normal call. For Ganpath: `*` works as a keypad event. |
| RESULT `Key check: no keypad events and no key tones in 34 s of conference audio: key presses stop at Airtel's conference` | Neither form reached leg3 while the testers pressed keys. For Ganpath: a family member has to say "Ganpath" or use the app to call Ganpath in. Check the testers did press the keys during the talk, and stayed quiet while they pressed: speech over a tone can hide it. |
| RESULT `Key check did not run: <reason>` | The snoop channel or the recording was refused, the download failed, or the file was not 8 kHz 16-bit mono WAV. The reason is the ARI answer. `Asterisk refused the recording (HTTP 500 ...)` usually means the container runs without the tmpfs for the recording folder: recreate it as under [Set up D4](#set-up-d4). The test's own result still stands. |
| `could not delete the recording testd-keys-...` | ARI refused the delete. The file is still in the container: remove it with the command under [Clear the logs](#clear-the-logs). The RESULT line repeats this. |
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
- Each D4 RESULT line says that the audio verdict is the testers': the script judges no voices. The key check's sentence comes last, and it never changes PASS, FAIL or UNCLEAR.
- `Ctrl-C` at any point deletes the key-check recording, hangs up leg3 (even during a REFER), then the mobile's call and yours, and clears `TESTD_WAIT`.
- If the conference survives the drop test, the two testers have to hang up themselves.

### Line check during a merged call

This answers one question for a later session: while Airtel's conference holds the merged call, does the line take a new call? It needs a third phone that is not one of the two in the call, and not a SIP app such as Zoiper, which would take the line over. Do it twice: once after leg3 has left the merged call, and once with leg3 still on it.

**Run 1, after leg3 has left.**
1. Run D4 as usual (`--conf --conf-uri '<the URI D4a found>'`).
2. Answer the drop question. If the answer is `y`, keep both phones talking. If it is `n`, the merged call has ended and there is nothing to check: skip the steps below.
3. Call the landline from the third phone.
4. Run the log check below.

**Run 2, with leg3 still up, and the leg3 hold check.** Ganpath would put leg3 on hold to take a second call during a merged call, so this run also holds leg3 for 10 s and asks what you heard.
1. Run D4 with `--no-drop --hold-leg3` (and `--no-key-check` if run 1 already did the key check):
   ```bash
   python3 tests/testd.py --conf --conf-uri '<the URI D4a found>' --no-drop --hold-leg3 --no-key-check
   ```
2. Talk, press Enter and answer the heard question as usual. The script then stays on the conference and prints `D4 leg3 hold check. Keep both phones on the call.`
3. Call the landline from the third phone, note what it hears, hang it up, and run the log check below in a second terminal.
4. Press Enter in the script's terminal. It holds leg3 for 10 s (`--hold-leg3 S` sets another length, 1 to 120 s). Both testers note what they hear while leg3 is held: Airtel's hold message, music, silence, or each other.
5. The script takes leg3 off hold, plays a beep into the conference, and asks three questions: what each phone heard while leg3 was held, whether the two phones still hear each other, and whether both heard the beep.
6. Hang up both phones, or press Ctrl-C, to end the stay.

The RESULT line gets a `Leg3 hold check` sentence: whether the conference's answer held leg3 from its side, whether leg3 was still held after the unhold, and your three answers. Like the key check, it never changes PASS, FAIL or UNCLEAR. Type `s` instead of Enter at step 4 to skip the hold.

**The log check.** On den-ubuntu:
```bash
docker logs --since 5m home-phone 2>&1 | grep -E "Incoming call|Not a test caller"
```

| What you see | Meaning |
|---|---|
| A new `Incoming call from a number ending in ...` line from the third phone | Airtel offered the new call to Asterisk while the merged call was still going. A caller who is not a test number hears ringing for 40 s; a test number hears hello-world. |
| No new line, and the third phone hears busy or a message | The merged call still occupies the line. |

### End of a merged call

On 11 Nov Airtel merged the calls, and both phones dropped when leg3 hung up. So Ganpath has to stay on leg3 until the merged call ends. This run shows what that end looks like from leg3. Does Airtel send a `BYE` to leg3 once both phones have hung up? And what does leg3 hear in the meantime?

Run it as D4, with `--watch-end`:
```bash
python3 tests/testd.py --conf --conf-uri '<the URI D4a found>' --watch-end
```
`--watch-end S` sets how long the script waits after Enter: 10 to 900 s, 180 by default. There is no drop test, so the script stays on the conference. It does not go with `--flash` or `--hold-leg3`. It works with `--in2`.

Tell the forward-number owner that the recording runs until the end of the watch this time, not just the talk.

Steps:
1. Call in, press 4 and talk as in D4. Press Enter at the terminal and answer the heard question.
2. The terminal shows `D4 end watch. Talk a few seconds more, then both of you hang up.` Talk a few seconds more. Then both testers hang up.
3. When both phones have hung up, press Enter. The script keeps leg3 on the call for S s and logs `leg3 is still up` every 30 s.
4. The watch ends when leg3 ends, or after S s. The script stops the recording, downloads it into memory, deletes it in Asterisk and hangs up leg3 if it is still up.

Each tester hears the same as in D4 until they hang up.

The recording is the key check's: one file of what leg3 receives (`spy=in`), from just after the merge to the end of the watch. It sits in the same tmpfs and is deleted the same way. It stops by itself after the talk, the questions and the watch, at 1000 s at most, about 16 MB, which the 64 MB tmpfs holds. The script logs one level a second for the whole file (`D4 end watch: levels in dBFS`), and the RESULT summarises the part after Enter. No audio is kept or written anywhere else. With `--no-key-check` the script still records, but does not search for key tones.

`Ctrl-C` during the watch ends it early. The script still reads and deletes the recording, hangs up leg3 and prints the RESULT. A second `Ctrl-C` while it reads the recording stops the reading; the cleanup then deletes the recording and hangs up leg3, and the verdict stands. `Ctrl-C` before the watch, during the talk or a question, stops the run as in D4 and deletes the recording unread.

The RESULT gets an `End watch (S s)` sentence. Like the key check, it never changes PASS, FAIL or UNCLEAR.

| RESULT | Meaning |
|---|---|
| `leg3 ended N s after Enter, M s after the merge (cause 16 ...)` | leg3 ended N s after both phones had hung up, and the script had not hung it up. Most likely Airtel sent a `BYE`, and Ganpath can end its call on it. Check the trace for a received `BYE` on leg3's Call-ID before relying on it. |
| `... (found by polling ... ; no BYE is confirmed, so check the trace)` | leg3 ended, but ARI gave no hang-up cause, so the script cannot tell how. Only the trace shows whether a `BYE` came. |
| `leg3 ended N s before Enter` | leg3 ended while the testers were still hanging up, probably when the first or the second phone hung up. Compare its time in the trace with when each tester hung up. |
| `leg3 ended before the end watch began, M s after the merge` | leg3 ended during the heard question, before the script asked both testers to hang up. There was nothing to watch. |
| `Airtel did not end leg3 within S s of Enter, so the script hung it up` | Airtel keeps the conference with only leg3 in it. Ganpath then has to decide by itself that the call is over, for example from the audio below. |
| `Airtel did not end leg3 within S s of Enter, but leg3 ended N s after the watch ended, while the script read the recording` | leg3 ended in the few seconds after the watch, before the script went to hang it up, so the script did not hang it up. The end came that much after Enter plus S s. The cause follows in brackets, as above. |
| `stopped with Ctrl-C ...` | The watch was ended by hand. The audio summary covers the time until then. A second `Ctrl-C` while the recording is read adds `a second Ctrl-C stopped the reading of the recording`: then there is no audio summary, and the cleanup deletes the recording and hangs up leg3. |
| `The watch counted from when ...` | No Enter came (no answer in 120 s, or no terminal), so times count from then. |

The audio after Enter, in the same sentence:

| Part | Meaning |
|---|---|
| `silent N s (below -55 dBFS; D s of them digital silence, all-zero or below -90 dBFS)` | Seconds of nothing but line noise. D of them carry no noise at all: every sample, or nearly every one, is 0. Line noise and comfort noise never go that low, so digital silence means no audio is being mixed into leg3 any more. |
| `low N s` | Seconds between -55 and -40 dBFS: hiss, comfort noise or a faint sound. |
| `tone or speech level N s (at or above -40 dBFS; loudest L dBFS)` | Seconds with a tone, a message or a voice. A network tone sits near -16 dBFS. |
| `longest silent run N s` | The longest stretch of silent seconds after Enter. |
| `in order: loud 0-5 s, silent 6-179 s` | Each run in seconds after Enter. This gives the shape: a message and then silence, silence throughout, or sound that goes on. |
| `the last switch from audio to all-zero samples came T s into the recording, N s before Enter, and the zeros ran to the end of the file` | Where in the whole recording the audio last changed to all-zero samples (1 s or longer; shorter stretches of zeros happen in quiet line noise). On 12 Nov this came about 20 s before Enter, when both phones had hung up, and the zeros ran to the end. `the zeros lasted L s` means audio came back after them. `no switch from audio to all-zero samples` means the recording never went fully silent. When the file holds less audio than the time it ran, `N s before Enter` gains `by the clock (it may be up to G s later relative to it, see the missing-audio note)`: T is exact, but Enter's place in the file is not. |
| `the file holds N s less audio than the T s it ran` | For that long no audio packets came from Airtel. Asterisk writes nothing when no RTP arrives, so the file is shorter than the watch, and the seconds after such a stretch count early. The file does not show when the stretch was. After Enter it means the conference stopped sending audio. |
| `Audio from K s into the file, where Enter falls only if no audio was missing before it ...` | Shown in place of `Audio after Enter` when the file holds less audio than the time it ran. The script places Enter in the file by the clock, so any missing audio before Enter puts Enter earlier in the file than K s. The summary may then leave out up to that much audio just after Enter, and the counts then cover less than the time after Enter. Use the per-second levels in the log to judge it. |
| `none: the recording ended before Enter` | leg3 ended before Enter, and the recording with it. With missing audio it reads `none: the file ends before K s`. |
| `No audio summary: ...` | The recording did not run or could not be read. The leg3 timing still stands. |

The levels are fixed in `tests/testd.py` (`WATCH_SILENT_DBFS`, `WATCH_LOUD_DBFS`, `WATCH_DIGITAL_DBFS`, and `WATCH_ZERO_RUN` for the shortest stretch of zeros that counts). The service will have its own settings.

### Incoming second call

Every D4 so far dialled leg2 out. Ganpath also has to merge a call that comes in while a family member is on a normal call with it. `--in2` tests that, in the order Ganpath will use. The first call stays active, with no hold. The second call comes in from the forward number, and Asterisk keeps it ringing (call waiting). The script asks the person on the call whether to add it. On yes it holds leg1, answers leg2, holds leg2 and merges both through the conference factory, as D4 does. On no, or no answer in time, the second call gets busy.

```bash
python3 tests/testd.py --conf --conf-uri '<the URI D4a found>' --in2
```
`--in2-wait S` sets how long the second call rings while the script waits for a key: 5 to 120 s, 20 by default. `--in2` works with `--no-drop`, `--watch-end`, `--refer-in legs`, `--moh` and `--no-key-check`, and not with `--flash` or `--dial-out`. The RESULT line says `D4-in2`.

Steps:
1. Call the landline from a test number and press 4 after the beep. Stay on the call. The script does not hold you.
2. You hear one beep, and the terminal shows `D4-in2: stay on the call on the test phone. The forward-number owner calls the landline now. Waiting 60 s.` The forward-number owner calls the landline.
3. The script does not answer that call. It asks Asterisk to send it ringing (180 or 183, depending on the endpoint's `inband_progress`; the trace shows which) and logs the time. You hear three beeps, and the terminal shows `Press 1 on the test phone to add the caller, 2 to refuse.`
4. Press 1 or 2 on the test phone within 20 s (or the `--in2-wait` time).
   - 1: the script holds you, answers the second call, plays it hello-world and holds it. Then it calls the conference factory and sends the REFERs, leg2 first and then leg1, as in D4. Talk, the key check and the questions go as in D4.
   - 2, or no key in time: the script asks Asterisk to refuse the second call with busy (cause 17, which chan_pjsip should send as `486 Busy Here`; the trace confirms it). You hear two beeps. The terminal then asks what the forward phone heard. Type a few words and press Enter, or just press Enter. The run ends there.
5. If the second call stops ringing before the script answers or refuses it, the script logs after how many seconds. You hear an error beep, and the terminal asks whether the forward-number owner hung up. Answer y or n.

The script logs every key with its leg. Only keys from the test phone (leg1) count. Keys from the forward phone (leg2) are logged and ignored, and so are keys pressed before the second call arrived. A key other than 1 or 2 is logged and the wait goes on. So the run also shows whether keys reach the script during a normal active call. Key times are taken when the script reads the event, not when Asterisk sends it. A key pressed while the script waits on an ARI call (the ring, the subscribe, the beeps) is stamped late by up to that call's length, usually milliseconds. A key pressed in the moment between the call arriving and the ring request can therefore count as after the ring.

What each tester hears:
- You: the beep after pressing 4, silence (the script does not talk), one beep when the forward-number owner should call, three beeps when the call is waiting. After 1: Airtel's hold message, then hello-world and the other voice. After 2 or no key: two beeps. The script hangs up your call when the run ends.
- The forward-number owner: ringing (or whatever Airtel plays to a caller who is waiting). After 1: hello-world, then Airtel's hold message, then hello-world again and your voice. After 2 or no key: a busy tone or an Airtel message. Note what it was for the terminal question.

Only the forward number counts as the second call. While the script waits, the dialplan sends it a call in from a test number or the forward number, as in E2; any other number just rings. The script refuses another test number with busy, logs its last 4 digits and waits on. Once the second call rings, the script sets `TESTD_WAIT` back to 0, so a further call in gets the normal stage-1 handling and never reaches the script. `Ctrl-C` while the second call rings stops the run, and the cleanup refuses that call with busy.

| RESULT | Meaning |
|---|---|
| `D4-in2 PASS: ... Call waiting: leg2 was sent ringing until key 1 from leg1 N s after the ring` | Airtel merged a call that had been kept ringing while leg1 was active. `waited unanswered, with no ring sent,` in place of `was sent ringing` means Asterisk refused the ring request. The `D4 leg2 dialog` line shows its tags: `ours` is the To-tag of our `200 OK`, `theirs` the From-tag of Airtel's INVITE. |
| `D4-in2 REFUSED: key 2 from leg1 N s after the ring ...: the script asked Asterisk to refuse the waiting call with busy (cause 17; chan_pjsip should send 486, confirm in the trace) ... The forward phone heard: ...` | The refusal path worked as far as ARI shows. The trace shows the SIP answer Airtel got, and the forward-number owner's words show what a refused caller hears from Airtel. |
| `D4-in2 REFUSED: no 1 or 2 from leg1 in S s` | No key came in time, so the script refused the call. `(if 1 or 2 was pressed, keys do not reach the script on an active call)` is added when no key came from leg1 at all. If you did press 1 or 2, keys do not reach the script during an active call: check the trace for RFC 4733 events on leg1. |
| `Other keys from leg1: ...`, `keys from leg2, ignored: ...` | Keys that came in the wait and were not used. |
| `leg2 ended while it rang, N s after it reached the script ... The forward-number owner did not hang up, so something else ended the waiting call after N s` (UNCLEAR) | A measurement, not a failure. The trace shows who ended it: a CANCEL from the carrier side (with its Reason header) or the caller's network, or Asterisk. If it was a CANCEL from the carrier side, a waiting call rings no longer than N s, so the time Ganpath takes to ask has to fit inside it. |
| `... The forward-number owner hung up.` (STOPPED) | The caller gave up. Nothing was merged. Run it again. |
| `... Nobody said at the terminal ...` (UNCLEAR) | The question got no answer. The trace shows who sent the CANCEL. |
| `... after key 1 from leg1, before the script answered it` | The second call ended between the key and the answer. The script took leg1 off hold again. |
| `Asterisk refused the ring ..., so it sent Airtel no 180 or 183` | ARI refused the ring request. The run went on without it. |
| `no call in from the forward number reached the script in 60 s` | The second call never reached the script. Check the trace for a second INVITE and Asterisk's answer. `(N call(s) in from another number were refused)` means a call came from another phone. |
| `FAILURE 481` on leg2 | `Replaces` did not match for the incoming call. The `Refer-To` carries `to-tag=theirs;from-tag=ours`, as for leg1. If leg1 moved in an earlier D4 run, the tag order is right and the proxy rewrote something for this call. |
| Anything else | As under [Reading the D4 results](#reading-the-d4-results). |

The exit code is 0 for PASS and REFUSED.

## Asterisk 22.12 (stage 2, phase 7)

Stage 2 moves to Asterisk 22.12.0 LTS, built from the upstream release. Ubuntu's 20.6 can hang up an AudioSocket call when a frame arrives in pieces, and Ganpath's audio goes over AudioSocket. It has also had no security update since April 2024, so it lacks the June 2026 ARI and PJSIP fixes. The private stage-2 plan (section 4.6) has the sources.

The 22.12 image is opt-in. `Dockerfile` (Ubuntu's 20.6 package, image `home-phone-asterisk:local`) stays the default and is the rollback. Both images are built from the same `config/` and `entrypoint.sh` and read the same root `.env`. Each image holds the copies from its own last build, so a change to either file reaches an image only when that image is rebuilt.

| File | What it does |
|---|---|
| `Dockerfile.ast22` | Two stages. The build stage downloads the 22.12.0 release and the pjproject 2.17 tarball it bundles, checks both against pinned SHA-256 sums, and compiles. The runtime stage holds the installed files, the libraries they link, Ubuntu's prompts and music on hold, and the same config and entrypoint. |
| `ast22/modules.txt` | The 95 modules built, with the reason for each group. The build stops if one is missing or an unlisted one appears. |
| `ast22/asterisk.conf` | The directories, with the paths the 20.6 image uses. It sits outside `config/`, so the 20.6 image never gets it. |
| `docker-compose.ast22.yml` | Compose override: builds from `Dockerfile.ast22` and tags the image `home-phone-asterisk:22.12.0`. |
| `tests/check-ast22.sh` | Checks the running container and prints PASS, FAIL or SKIP per line. It prints no command output, so no number or address. |

What differs on 22.12:

| Item | 20.6 image | 22.12 image |
|---|---|---|
| Modules | all of Ubuntu's (346) | the 95 in `ast22/modules.txt`. Left out on purpose: `func_env`, `func_shell` and `app_system` (file and shell access), CDR and CEL, other channel drivers, SIP MESSAGE, MWI, presence, T.38. |
| pjproject | 2.13.1 | 2.17 |
| Our SIP headers | `Allow` lists MESSAGE; `Allow-Events` lists presence, dialog and message-summary | no MESSAGE, and `Allow-Events` without those three |
| `ari show users` | `No    homephone` | `No    No    homephone` (a new ACL column) |
| Option 4 with no script run since Asterisk started | ERROR `Stasis app 'home-phone-testd' not registered` | WARNING `Stasis app 'home-phone-testd' doesn't exist`, then the same error beep. The grep under [Troubleshooting](#troubleshooting) still finds it. |
| A ring that times out (D1b, the D4 factory call) | no cause set | cause 19, which the script labels `SIP 480/483`. The trace shows whether a 480 came back. |
| Opus | Debian's `codec_opus_open_source` | none: `core show translation paths opus` is empty. The line uses G.711 only. |
| RTP SSRC | kept when a call joins or leaves a bridge | a new SSRC each time a call joins or leaves a bridge: option 3, D3, D3b, E2 and every later Ganpath bridge. Upstream added it in 22.6.0. |
| `dtmf_mode=auto` | after one SDP without `telephone-event`, the call stays on in-band keys | each SDP decides again: with `telephone-event`, RFC 4733 events; without it, in-band tones |

The last two rows change only the media, so the SIP traces look the same. A difference in audio around a bridge, or in keys after a hold, points at them first.

The rest of this README applies to both images. While 22.12 runs, every `docker compose` command in it needs `-f docker-compose.yml -f docker-compose.ast22.yml` before its other options. Without them, `up -d` (Test D on or off, [Clear the logs](#clear-the-logs)) switches back to 20.6. `export COMPOSE_FILE=docker-compose.yml:docker-compose.ast22.yml` in the shell adds them to every command until `unset COMPOSE_FILE`. A change in `config/` or `entrypoint.sh` needs the override's `up -d --build`. While the build cache is there, only the last steps run.

### Build on den-ubuntu

Pick a quiet time: no call and no Test D session. The 20.6 container keeps the line up until step 4, so steps 1 to 3 can run before the D4 key check. Do step 4 after the key check has run on 20.6, so the 22.12 run has a result to compare with.

1. Get the files, from this folder: `git pull`.
2. Check memory, disk and the rollback image:
   ```bash
   free -m                                # "available" about 1700 MB
   df -h /var/lib/docker                  # a few GB free for the build
   docker image ls home-phone-asterisk    # the "local" tag is listed: it is the rollback
   ```
   Note the 20.6 figures under [Memory](#memory) now, while 20.6 still runs.
3. Build, with a log in your home folder:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.ast22.yml --env-file ../.env build 2>&1 | tee ~/ast22-build.log
   ```
   Watch memory from a second SSH session with `free -m -s 10` (`Ctrl-C` ends it); `docker stats` does not show build containers. Nothing has been measured. A guess: 15 to 40 minutes with two jobs, most of it in `main/` and pjproject, and a few hundred MB for each compiler process. The log ends the build stage with `All 95 modules in ast22/modules.txt were built.`
   - If "available" drops below about 200 MB, press `Ctrl-C` in the build session. If the log shows `Killed` or `signal 9`, the build has already stopped. Either way, build again with one job. The download and configure steps are cached, so it starts again at the compile:
     ```bash
     MAKE_JOBS=1 docker compose -f docker-compose.yml -f docker-compose.ast22.yml --env-file ../.env build 2>&1 | tee ~/ast22-build.log
     ```
   - If the one-job build also runs short of memory, stop there and send the end of `~/ast22-build.log`. 20.6 is still running, so nothing needs rolling back.
   - `Module set differs from ast22/modules.txt` names the modules that did not build. Their errors are earlier in the log.
   - The build cache takes a few GB. `docker builder prune` frees it once 22.12 is accepted; the next 22.12 build then compiles from the start.
4. Switch, wait about 10 seconds for Asterisk to start and register, then read its first lines:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.ast22.yml --env-file ../.env up -d
   docker logs home-phone 2>&1 | grep -E 'Asterisk Ready|Test D (on|off)'   # the Test D line, then Asterisk Ready.
   ```
   From here calls go through 22.12. `docker compose -f docker-compose.yml -f docker-compose.ast22.yml --env-file ../.env up -d --build` does steps 3 and 4 in one command.

### Checks after the switch

```bash
docker exec home-phone asterisk -rx "core show version"   # Asterisk 22.12.0 built by ...
sh tests/check-ast22.sh                                    # PASS on every line; SKIP on the two Test D lines while Test D is off, and on the twelve Ganpath lines unless Ganpath mode is on
```
The script waits up to 30 s for the registration. If the container is not running or the registration FAILs, the line is down. For that or any other FAIL, save the log and roll back at once, then send the FAIL lines:
```bash
docker logs home-phone > ~/ast22-start.log 2>&1      # holds numbers and the carrier's addresses: delete it once read
docker compose --env-file ../.env up -d --build      # back to 20.6, as under Roll back to 20.6
```

The script's main checks, by hand:
```bash
for m in $(sed -e 's/#.*//' -e 's/[[:space:]]//g' -e '/^$/d' ast22/modules.txt); do
  docker exec home-phone asterisk -rx "module show like $m.so" | grep "^$m\.so " | grep -qv "Not Running" || echo "MISSING $m"
done                                                                              # prints nothing
docker exec home-phone asterisk -rx "pjsip show registrations"                    # Registered
docker exec home-phone asterisk -rx "dialplan show testd-refer"                   # -= 1 extension (3 priorities) in 1 context. =-
docker exec home-phone asterisk -rx "core show function PJSIP_RESPONSE_HEADER"    # -= Info about Function 'PJSIP_RESPONSE_HEADER' =-
docker exec home-phone asterisk -rx "http show status"    # Test D on: Server Enabled and Bound to 127.0.0.1:8088. Off: Server Disabled
docker exec home-phone asterisk -rx "ari show users"      # Test D on: No    No    homephone
docker exec home-phone asterisk -rx "timing test"         # Using the 'timerfd' timing module for this test.
docker exec home-phone asterisk -rx "moh show files"      # Class: default, then File: /usr/share/asterisk/moh/...
docker exec home-phone ls -ld /var/spool/asterisk/recording   # a folder owned by asterisk (D4 key check)
```

### Calls to repeat on 22.12

Run each with `pjsip set logger on` and keep its redacted trace ([Read and paste the trace](#read-and-paste-the-trace)) to compare with the 20.6 runs.

| Call | Steps | Same as 20.6 when |
|---|---|---|
| Option 1, echo | [Tests](#tests) | you hear yourself back |
| Option 2, transfer | [Tests](#tests) | `TRANSFER RESULT` shows SIP 405 |
| Option 3, bridge | [Tests](#tests) | `BRIDGE RESULT` shows cause 58 (Airtel's 488) |
| Deflect (302) | [Tests](#tests), with the override in the `up -d` command | `TRANSFER RESULT` shows the status and SIP code of the 20.6 run |
| Outgoing call | [Tests](#tests) | hello world, then the echo test |
| D1 | [Run](#run): `python3 tests/testd.py` | `RESULT: D1 PASS` |
| E | [Run](#run), the E check under the table | a second INVITE arrives during the echo test |
| D4a | [Run D4](#run-d4), step 1: `python3 tests/testd.py --probe` | `RESULT: D4a PASS` with the same focus address |
| D4 | [Run D4](#run-d4), with the URI D4a found (private notes) | `RESULT: D4 PASS`, the same drop-test answer, and the same `Key check:` result as the 20.6 run. If the keys differ, see the `dtmf_mode=auto` row above. |

Then turn the logger off and [clear the logs](#clear-the-logs), with the override in the `--force-recreate` command.

D4 covers part of two other migration checks in the plan. It sets a channel variable over ARI (`D4_REFER_TO`), and its key check records through a snoop on leg3, which is in no bridge. In the 22.12 source, a variable set over ARI may still write `TIMEOUT()` and `PJSIP_MOH_PASSTHROUGH()`; only functions marked dangerous are refused, such as `FILE()`, `DB()` and a `CURL()` write. A snoop on a bridged call and `tech_cause` need the phase-1 tools and run with them.

### Memory

Run both on 20.6 before the switch and on 22.12 after it, registered and idle, and note the figures:
```bash
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' home-phone
ps -C asterisk -o rss=,args=      # resident memory of the Asterisk process, in KB
```

### Roll back to 20.6

```bash
docker compose --env-file ../.env up -d --build
docker exec home-phone asterisk -rx "core show version"   # Asterisk 20.6.0~dfsg...
```
If `COMPOSE_FILE` is exported, `unset COMPOSE_FILE` first. `--build` copies the checkout's `config/` and `entrypoint.sh` into the 20.6 image. Without it, `up -d` starts the 20.6 image with the files of its last build, which can be older. With Ubuntu's package in the build cache, the rebuild takes seconds. After `docker builder prune` it installs the package again, which takes longer and needs the internet. Going back to 22.12 is the override's `up -d --build`, which after a prune compiles everything again, as in step 3.

## Ganpath (stage 2, phase 1)

Ganpath is the assistant service that answers calls. It runs in its own container on this host and controls calls over ARI on 127.0.0.1:8088. With `HOME_PHONE_TEST_MODE=ganpath` only test callers reach it, and everyone else hears ringing, as today. With `ganpath_all` every caller reaches it and the service decides whom it answers (see [Everyone mode](#everyone-mode-ganpath_all)).

Ganpath mode is off unless `HOME_PHONE_TEST_MODE` is `ganpath` or `ganpath_all`. With any other value (`menu`, `deflect`, unset), calls are handled exactly as before. A value that is none of the four is treated as `menu`, and the container logs a WARNING.

### What it does

With `HOME_PHONE_TEST_MODE=ganpath`:

- A call from a number in `HOME_PHONE_TEST_CALLERS` goes to the Ganpath service (Stasis app `ganpath`, argument `inbound`) in place of the test menu. Every other caller rings, as today.
- Before the hand-over, Asterisk starts a dead-man timer on the call (`TIMEOUT(absolute)`, `HOME_PHONE_GANPATH_DEADMAN_S`) and adds a hangup handler. The service renews the timer while it runs.
- The call goes to the fallback (`[ganpath-fallback]`) when the service is not connected, when it stops renewing the timer, or when it sends the call there itself. The fallback:
  1. alerts Home Assistant through `HOME_PHONE_HA_FALLBACK_WEBHOOK`, if it is set (2 s to connect, 3 s in all);
  2. plays the recorded message `ganpath/ganpath-unavailable`;
  3. records a voicemail after the beep. It ends after `HOME_PHONE_VM_SILENCE_S` of silence, at `HOME_PHONE_VM_MAX_S`, on `#`, or when the caller hangs up. The fallback's own timer is `HOME_PHONE_VM_MAX_S` plus 60 s.
- A caller the service does not find on its own testers list goes back to `[inbound]`, label `ring`, and rings like any other caller. The service first cancels the dead-man timer and sets `GANPATH_DONE=1`, so this caller gets no fallback and no HA alert. Keep the service's testers list and `HOME_PHONE_TEST_CALLERS` the same people all the same.
- The hangup handler alerts Home Assistant about a call that neither the service nor the fallback reported.
- A sweep job in this container deletes each voicemail `HOME_PHONE_VM_RETENTION_H` hours after it was written, whether or not the service runs. It looks every `HOME_PHONE_VM_SWEEP_S` seconds.

What Asterisk and the service share:

| Item | Value |
|---|---|
| ARI user | `ganpath`, not read-only. It exists only while `HOME_PHONE_GANPATH_ARI_PASSWORD` is set. |
| Stasis app | `ganpath`, argument `inbound` |
| Channel variables | `GANPATH_DONE=1` (the service set it: the call is in its call log, or it sent the caller back to `ring`; the down-service step sets it before it sends a non-test caller to `ring`), `GANPATH_FALLBACK=1` (the fallback set it), `GP_TESTER` (the dialplan set it: 1 for a test caller, 0 for anyone else; read only by the down-service step) |
| Global variable | `GANPATH_EVERYONE`. The service sets it over ARI (`POST /ari/asterisk/variable?variable=GANPATH_EVERYONE&value=1`) when it starts and whenever its setting `answer.callers` changes: `1` for `everyone`, `0` for `testers`. Asterisk starts it at 0. Only `ganpath_all` mode reads it. |
| Where the service sends calls | `ganpath-fallback,s,1` for a caller it answers; a non-tester to `inbound`, `s`, label `ring`; a caller it lets go of before it answers them (a failed start, a call while it stops, its restart clean-up) to `ganpath-down`, label `down`, which makes the same choice as a down service. The `ring` label cancels the dead-man timer, so a continue the service could not send leaves the timer to ring the caller. |
| Prompts | the host folder `/srv/ganpath/prompts` (`HOME_PHONE_GANPATH_PROMPTS_DIR` moves it), mounted read-only at `/usr/share/asterisk/sounds/ganpath`. Asterisk plays `ganpath/<name>`. Files are WAV, 8 kHz, 16-bit, mono. The fallback needs `ganpath-unavailable.wav`. |
| Voicemail | `/var/spool/asterisk/recording/voicemail/ganpath-vm-<epoch>-<id>-<tail>.wav`. `<epoch>` is the start time, `<id>` the channel's unique id with dots as hyphens (the HA alert sends it as `vm=`), `<tail>` the last 4 digits of the caller's number or `none`. ARI names it `voicemail/ganpath-vm-...`. |
| Recording folder | `/var/spool/asterisk/recording`, a 64 MB tmpfs in RAM, shared with Test D4's key check. A 120 s voicemail is about 1.9 MB. A container restart clears it. |

### Everyone mode (`ganpath_all`)

With `HOME_PHONE_TEST_MODE=ganpath_all`:

- Every caller goes to `Stasis(ganpath)`. A test caller goes there first in the dialplan. Any other caller, a withheld or short number too, goes there from the `not_test` step. A call in while `tests/testd.py --wait-in` waits still rings for a non-test caller.
- Who the service answers is its own setting, `answer.callers`: `testers` or `everyone`. While it is `testers`, the service sends a non-tester back to `[inbound]`, label `ring`, within about 100 ms. The caller rings as today, and the service writes nothing about the call.
- What a caller gets when the service is down, or stops renewing the dead-man timer, depends on `GANPATH_EVERYONE`:

| Caller | `GANPATH_EVERYONE` 0 (service says `testers`, or has not said) | `GANPATH_EVERYONE` 1 (service says `everyone`) |
|---|---|---|
| Test caller | fallback message and voicemail | fallback message and voicemail |
| Anyone else | rings, never hears the fallback, no HA alert | fallback message and voicemail |

  So the first step of the turn-on order below changes nothing a real caller can notice, even if the service dies. Only after the service has set `GANPATH_EVERYONE=1` does a down service mean the fallback for everyone.
- `GANPATH_EVERYONE` lives in Asterisk's memory. An Asterisk restart or `dialplan reload` puts it back to 0. The service sets it again when it reconnects, and every few seconds while it runs (its setting `jobs.admin_poll_s`). In that gap a real caller rings, which is what they get today.
- `ganpath` mode is unchanged: test callers only, and anyone else never enters Stasis.

Turn-on order:

1. Set `HOME_PHONE_TEST_MODE=ganpath_all` in the root `.env` and recreate the container (about 10 s of dropped line). The service's `answer.callers` is still `testers`. Real callers enter Stasis and are sent back to ring. This soaks the hop.
2. Check `docker exec home-phone asterisk -rx "dialplan show globals" | grep GANPATH_EVERYONE` says `0`, and `sh tests/check-ast22.sh` passes.
3. Run `ganpath settings set answer.callers everyone` (a family heads-up first: from now on Ganpath answers). The global turns to `1`.

Turn-off: `ganpath settings set answer.callers testers` (instant; the global turns to `0`), or `HOME_PHONE_TEST_MODE=ganpath` or `menu` and recreate.

The `HOME_PHONE_TEST_CALLERS` list is still the dialplan's test callers. In `ganpath_all` it decides only what a down service does for a caller while `GANPATH_EVERYONE` is 0. Keep it the same people as the service's testers list. A demo number that is not a test caller reaches the service in `ganpath_all` but not in `ganpath`.

### Root .env names

Type each value yourself. Empty or unset numbers take the default.

| Name | Default | What it is |
|---|---|---|
| `HOME_PHONE_TEST_MODE` | `menu` | `ganpath` switches Ganpath mode on for test callers. `ganpath_all` sends every caller to the service (see [Everyone mode](#everyone-mode-ganpath_all)). |
| `HOME_PHONE_GANPATH_ARI_PASSWORD` | empty | The ARI password of the user `ganpath`. A secret. The Ganpath service reads the same name from the root `.env`. Without it there is no `ganpath` user, and in Ganpath mode every test call goes to the fallback. |
| `HOME_PHONE_HA_FALLBACK_WEBHOOK` | empty | The full Home Assistant webhook URL for fallback alerts. A secret: it holds a webhook ID. Empty: no alert is sent. |
| `HOME_PHONE_GANPATH_DEADMAN_S` | 120 | The dead-man timer, in seconds. |
| `HOME_PHONE_VM_MAX_S` | 120 | The longest voicemail, in seconds. |
| `HOME_PHONE_VM_SILENCE_S` | 5 | Seconds of silence that end a voicemail. |
| `HOME_PHONE_VM_RETENTION_H` | 48 | Hours a voicemail is kept. A fraction such as `0.5` works. |
| `HOME_PHONE_VM_SWEEP_S` | 300 | Seconds between sweeps. |
| `HOME_PHONE_GANPATH_PROMPTS_DIR` | `/srv/ganpath/prompts` | The host folder of Ganpath's prompts. It must be the folder the Ganpath service writes them to. Docker mounts it in every mode and creates it, empty and owned by root, when it is missing. |

In Ganpath mode a value that is not a number stops the container, and `docker logs home-phone` names it. Leading zeros are dropped (`08` is 8). A webhook URL with a comma, `$`, `"`, `(`, `)`, `\` or a space also stops it: the URL goes unquoted into `CURL()`, where those characters would break the alert without an error.

**The numbers must match the Ganpath service's settings.** The service keeps its own copy and does not read these. Print its values with `ganpath phone dialplan-vars` (in Ganpath's admin container) and compare:

| `dialplan-vars` line | Root `.env` name |
|---|---|
| `GANPATH_DEADMAN_TIMEOUT` | `HOME_PHONE_GANPATH_DEADMAN_S` |
| `GANPATH_VM_MAX` | `HOME_PHONE_VM_MAX_S` |
| `GANPATH_VM_SILENCE` | `HOME_PHONE_VM_SILENCE_S` |
| `GANPATH_VM_RETENTION_H` | `HOME_PHONE_VM_RETENTION_H` |
| `GANPATH_VM_SWEEP_S` | `HOME_PHONE_VM_SWEEP_S` |
| `GANPATH_FALLBACK_TIMEOUT` | none: the entrypoint uses `HOME_PHONE_VM_MAX_S` + 60 |

After a change on either side, set the same value in the root `.env` and recreate the container (`up -d`).

### Switch it on

1. Make the ARI password and type it into the root `.env` yourself, without quotes:
   ```bash
   openssl rand -hex 24
   ```
   ```
   HOME_PHONE_GANPATH_ARI_PASSWORD=<the output>
   ```
2. Put the approved prompt `ganpath-unavailable.wav` in `/srv/ganpath/prompts`. Asterisk runs as the user `asterisk` in the container, so give the folder, its subfolders (the greetings are in `greetings/`) and all their files that group, after the container is on the 22.12 image. The setgid bit (the `2` in 2750) makes files written later take the group too:
   ```bash
   G=$(docker exec home-phone id -g asterisk)
   sudo chgrp -R "$G" /srv/ganpath/prompts
   sudo find /srv/ganpath/prompts -type d -exec chmod 2750 {} +
   ```
   The group id comes from the image. After a build from scratch, run these lines again.
   If the folder does not exist, Docker creates it empty, owned by root, and the container still starts. The fallback then plays only the beep.
3. In the root `.env`, set `HOME_PHONE_TEST_MODE=ganpath` (or `ganpath_all`, see [Everyone mode](#everyone-mode-ganpath_all) for the order), and `HOME_PHONE_HA_FALLBACK_WEBHOOK` if you have the HA automation for it. Check the numbers against `ganpath phone dialplan-vars`.
4. Rebuild and recreate from this folder. The line drops for about 10 s:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.ast22.yml --env-file ../.env up -d --build
   docker logs home-phone 2>&1 | grep -E 'Ganpath|sweep|WARNING'
   ```
   Good: `Ganpath ARI user on`, `Ganpath mode on: ...`, `voicemail sweep on: ...`, and no `WARNING`.
5. Start or restart the Ganpath service, so it logs in as `ganpath`.

### Checks

```bash
sh tests/check-ast22.sh                                                      # the eleven Ganpath lines PASS
docker exec home-phone asterisk -rx "ari show users"                         # No    No    ganpath
docker exec home-phone asterisk -rx "dialplan show ganpath@inbound"          # Stasis(ganpath,inbound)
docker exec home-phone asterisk -rx "dialplan show ganpath-fallback" | tail -1   # -= 3 extensions (17 priorities) in 1 context. =-
docker exec home-phone asterisk -rx "module show like app_record.so"         # Running
docker exec home-phone df -h /var/spool/asterisk/recording | tail -1         # 64M
docker exec -u asterisk home-phone head -c 4 /usr/share/asterisk/sounds/ganpath/ganpath-unavailable.wav; echo   # RIFF
docker exec home-phone asterisk -rx "ari show apps"                          # ganpath, once the service is connected
```

Keep `| tail -1` on `dialplan show ganpath-fallback`, and do not run `dialplan show ganpath-hangup`, once a webhook is set: the full output prints the webhook URL to anyone at the console. The start lines and `check-ast22.sh` never print it.

The voicemail line in the log comes from the fallback's `h` extension, so it appears however the call ends: `Voicemail from a number ending in 1234: HANGUP` (or `SILENCE`, `DTMF`, `TIMEOUT`). An empty status means the caller hung up before the beep.

The sweep prints a line only when it deletes something: `docker logs home-phone 2>&1 | grep vm-sweep`. It runs in the background beside Asterisk and nothing restarts it. It stops only on a bad setting, which the entrypoint checks first, or if it is killed. `check-ast22.sh` has the line `voicemail sweep running`; if that line fails, recreate the container (`up -d`) and voicemails are deleted on time again. To list voicemails waiting in memory: `docker exec home-phone ls -l /var/spool/asterisk/recording/voicemail`. The names hold only the last 4 digits.

### Switch it off (rollback)

1. Set `HOME_PHONE_TEST_MODE=menu` in the root `.env`.
2. `docker compose -f docker-compose.yml -f docker-compose.ast22.yml --env-file ../.env up -d`
3. Check: `docker logs home-phone 2>&1 | grep 'Ganpath mode on'` prints nothing for the new start, and a test call reaches the menu.

The recreate empties the recording tmpfs, so a voicemail the service has not fetched yet is lost. To remove the ARI user too, empty `HOME_PHONE_GANPATH_ARI_PASSWORD` as well. With the password, the web server and the `ganpath` user stay on in menu mode; no call reaches Ganpath.

### Merged calls (`[ganpath-refer]`, phase 4)

`[ganpath-refer]` in `config/extensions.conf` sends the REFER for leg 3 of a network-conference merge. The service sets `GANPATH_REFER_TO` on the channel, continues it to this context by the label `refer`, and gets the result back from `Stasis(ganpath,referred,...)`. Only ARI reaches the context. It needs no new module and no change to `pjsip.conf`, the entrypoint or `ast22/modules.txt`. An image built without it answers the continue with a 404, and the service then sends no REFER.

A merged call survives a Ganpath restart but not an Asterisk restart, so `hp up -d` waits until no merged call runs:

    docker exec ganpath-phone ganpath health | grep -A3 '"line"'

### With Test D

- Test D needs the menu. Option 4 is reached only in menu mode, so for line tests set `HOME_PHONE_TEST_MODE=menu`, recreate, run Test D, then set `ganpath` back.
- The two ARI users can exist at once. Each has its own password: Test D's script logs in as `homephone`, Ganpath as `ganpath`. Hold passthrough (`moh_passthrough`) follows `HOME_PHONE_ARI_PASSWORD` only.
- `tests/testd.py --wait-in` (E2) takes test callers before Ganpath does while it waits. It stays menu-only: do not run it in `ganpath` or `ganpath_all` mode.
- Test D4's key check and the voicemails share the 64 MB recording tmpfs. The key check uses up to about 16 MB.

### Not checked on a running Asterisk yet

The dialplan was checked against the Asterisk 22.12.0 source and docs, not run. Still to see on the box: the `T` extension taking a caller from `Stasis()` to the fallback when the timer runs out, and (in `ganpath_all` with `GANPATH_EVERYONE=0`) a non-test caller to `ring` instead; `Set` of the global over ARI (`POST /asterisk/variable`) and its effect on the next call; `Record()` keeping the file on hang-up with option `k`; `REPLACE()`, `FILTER()` and `${EPOCH}` in the file name; `CURL()` with the timeouts; ARI listing and fetching a recording in the `voicemail/` folder; and the sweep's `find` and `awk` in the image.

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
- **The script was killed with `kill -9` mid-test:** the caller may sit in silence until they hang up, and the next call in goes through the stage-1 menu as usual. `docker exec home-phone asterisk -rx "channel request hangup all"` ends any call left over. During a D4 talk it may also leave a key-check recording: remove it as under [Clear the logs](#clear-the-logs).
- **The script stops with `401`:** the exported `HOME_PHONE_ARI_PASSWORD` differs from the one the container started with. Export it again, or recreate the container after changing `.env`.
