# Home Phone

The fibre landline that comes with the broadband connection, run from this host through Asterisk. Later stages put a voice assistant on the line. This first stage only proves the line works from here:

1. Asterisk registers the line.
2. An incoming call from a test number gets two-way audio (echo test).
3. Asterisk can ask the carrier to move a call to a mobile number (transfer test).
4. Asterisk can place an outgoing call.

## Why it is built this way

- **Host networking.** Call audio (RTP, UDP 10000–10100) has to reach the VM's LAN IP directly. Docker's bridge NAT breaks it.
- **One registration.** The carrier expects one device on the line. Switch the account off in any softphone (Zoiper: **Unregister**) before starting this, or the two will steal calls from each other.
- **Secrets in the root `.env`.** The number, password and test numbers are rendered into the Asterisk config when the container starts. Nothing personal is in git; this repo is public.
- **Small surface.** Only PJSIP on UDP 5060 listens. There is no web interface, no AMI and no ARI. Calls are accepted only from the carrier's proxy address; there is no anonymous endpoint, so SIP from anywhere else is refused.

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

**Echo test.** Call the landline from a test number. You hear "hello world" and a beep. Press 1, or wait. Then speak: you should hear yourself back. That proves audio flows both ways.

**Transfer test (REFER).** Call again and press 2 after the beep. Asterisk asks the carrier to connect you to the `HOME_PHONE_TEST_FORWARD` number and drop out.
- Works: that mobile rings, and you talk to it while Asterisk is no longer on the call.
- Fails: you hear an error beep and the call ends.

Either way, the log has a line with the result and the SIP code:
```bash
docker logs home-phone 2>&1 | grep "TRANSFER RESULT"
```

**Deflect test (302).** Set `HOME_PHONE_TEST_MODE=deflect`, restart with `docker compose --env-file ../.env up -d`, and call. Asterisk asks the carrier to redirect the call before answering. Read the result the same way. Set it back to `menu` afterwards.

**Outgoing call.** Ring your own mobile. Replace the number in this command; keep the +91 form. When you answer, you hear "hello world" and the echo test:
```bash
docker exec home-phone asterisk -rx "channel originate PJSIP/+91XXXXXXXXXX@airtel extension s@outbound-test"
```
If the carrier rejects the `+91` form, try `0XXXXXXXXXX`.

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
