#!/bin/sh
# Checks the running home-phone container after the switch to Asterisk 22.12 (stage 2,
# phase 7). Run it on den-ubuntu from the Home-Phone folder:
#   sh tests/check-ast22.sh
# One line per check: PASS, FAIL, or SKIP for a Test D check while Test D is off
# (HOME_PHONE_ARI_PASSWORD empty). It prints only its own labels, so no number, address or
# password from a command's output reaches the terminal. Exit status 1 if any check failed.
set -u

C=home-phone
LIST="$(dirname "$0")/../ast22/modules.txt"
fails=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }
skip() { printf 'SKIP  %s\n' "$1"; }

# cli COMMAND: run an Asterisk CLI command in the container.
cli() { docker exec "$C" asterisk -rx "$1" 2>/dev/null; }

# has LABEL COMMAND PATTERN: PASS when the command's output matches the extended regex.
has() {
    if cli "$2" | grep -Eq -- "$3"; then pass "$1"; else fail "$1"; fi
}

if ! docker exec "$C" true 2>/dev/null; then
    fail "container $C is running"
    echo "See README, Checks after the switch."
    exit 1
fi

# The image and the version.
img=$(docker inspect -f '{{.Config.Image}}' "$C" 2>/dev/null)
if [ "$img" = home-phone-asterisk:22.12.0 ]; then
    pass "container runs image home-phone-asterisk:22.12.0"
else
    fail "container runs image home-phone-asterisk:22.12.0 (it runs $img)"
fi
if docker image inspect home-phone-asterisk:local >/dev/null 2>&1; then
    pass "rollback image home-phone-asterisk:local is still there"
else
    fail "rollback image home-phone-asterisk:local is still there"
fi
has "core show version: Asterisk 22.12.0" "core show version" '^Asterisk 22\.12\.0 '
has "data directory is /usr/share/asterisk" "core show settings" \
    'Data directory:[[:space:]]+/usr/share/asterisk$'

# Every module in ast22/modules.txt is loaded and running.
if [ -r "$LIST" ]; then
    mods=$(cli "module show")
    for m in $(sed -e 's/#.*//' -e 's/[[:space:]]//g' -e '/^$/d' "$LIST"); do
        if printf '%s\n' "$mods" | grep "^$m\.so " | grep -qv "Not Running"; then
            pass "module $m"
        else
            fail "module $m"
        fi
    done
else
    fail "module list $LIST is readable"
fi

# Timer, prompts, music on hold, and the gsm to G.711 path the prompts take.
has "timer: timing test uses timerfd" "timing test" "Using the 'timerfd' timing module"
for f in hello-world beep beeperr demo-echotest; do
    if docker exec "$C" test -r "/usr/share/asterisk/sounds/en/$f.gsm"; then
        pass "prompt sounds/en/$f.gsm"
    else
        fail "prompt sounds/en/$f.gsm"
    fi
done
has "music on hold: class default has files" "moh show files" \
    'File: /usr/share/asterisk/moh/'
if cli "core show translation paths gsm" | grep 'To alaw:8000' | grep -q -- '->'; then
    pass "translation path gsm to alaw"
else
    fail "translation path gsm to alaw"
fi
# ARI's record (the D4 key check) answers HTTP 500 without this folder.
if docker exec "$C" sh -c 'test -d /var/spool/asterisk/recording && [ "$(stat -c %U /var/spool/asterisk/recording)" = asterisk ]' 2>/dev/null; then
    pass "recording folder, owned by asterisk"
else
    fail "recording folder, owned by asterisk"
fi

# The line, the dialplan and the functions Test D4 uses. The line registers a few seconds
# after the container starts, so this waits up to 30 s for it.
reg=no
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if cli "pjsip show registrations" | grep -Eq '[[:space:]]Registered([[:space:]]|$)'; then
        reg=yes
        break
    fi
    sleep 2
done
if [ "$reg" = yes ]; then
    pass "line registration: Registered"
else
    fail "line registration: Registered (waited 30 s)"
fi
has "dialplan [inbound] loaded" "dialplan show inbound" "Context 'inbound' created by 'pbx_config'"
has "dialplan [testd-refer]: 3 priorities" "dialplan show testd-refer" \
    '-= 1 extension \(3 priorities\) in 1 context\. =-'
has "function PJSIP_RESPONSE_HEADER" "core show function PJSIP_RESPONSE_HEADER" \
    "Info about Function 'PJSIP_RESPONSE_HEADER'"
has "function PJSIP_HEADER" "core show function PJSIP_HEADER" \
    "Info about Function 'PJSIP_HEADER'"

# Test D: the web server, the ARI user and hold passthrough. Only while Test D is on.
http=$(cli "http show status")
if printf '%s\n' "$http" | grep -q 'Server Disabled'; then
    for t in "web server on 127.0.0.1:8088" "ARI user homephone, not read-only" \
        "airtel endpoint moh_passthrough=true" "port 8088 listens on loopback only"; do
        skip "$t (Test D is off)"
    done
else
    if printf '%s\n' "$http" | grep -q 'Server Enabled and Bound to 127\.0\.0\.1:8088$'; then
        pass "web server on 127.0.0.1:8088"
    else
        fail "web server on 127.0.0.1:8088"
    fi
    if cli "ari show users" | awk '$NF == "homephone" && $1 == "No" { found = 1 } END { exit !found }'; then
        pass "ARI user homephone, not read-only"
    else
        fail "ARI user homephone, not read-only"
    fi
    has "airtel endpoint moh_passthrough=true" "pjsip show endpoint airtel" \
        'moh_passthrough[[:space:]]*:[[:space:]]*true'
    # Host networking: the container's listener shows up on the host.
    listen=$(ss -ltnH 'sport = :8088' 2>/dev/null | awk '{ print $4 }')
    if [ -n "$listen" ] && ! printf '%s\n' "$listen" | grep -qvx '127\.0\.0\.1:8088'; then
        pass "port 8088 listens on loopback only"
    else
        fail "port 8088 listens on loopback only"
    fi
fi

if [ "$fails" -eq 0 ]; then
    echo "All checks passed."
else
    echo "$fails check(s) failed. See README, Checks after the switch."
    exit 1
fi
