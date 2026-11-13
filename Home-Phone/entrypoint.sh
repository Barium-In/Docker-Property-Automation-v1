#!/bin/sh
# Render the Asterisk config from the environment (root .env), then run Asterisk
# in the foreground so `docker logs` shows its console.
set -eu

: "${HOME_PHONE_SIP_DOMAIN:?set HOME_PHONE_SIP_DOMAIN in the root .env}"
: "${HOME_PHONE_SIP_PROXY:?set HOME_PHONE_SIP_PROXY in the root .env}"
: "${HOME_PHONE_SIP_NUMBER:?set HOME_PHONE_SIP_NUMBER in the root .env}"
: "${HOME_PHONE_SIP_PASSWORD:?set HOME_PHONE_SIP_PASSWORD in the root .env}"
: "${HOME_PHONE_BIND:=0.0.0.0}"
: "${HOME_PHONE_TEST_CALLERS:=}"
: "${HOME_PHONE_TEST_FORWARD:=}"
: "${HOME_PHONE_TEST_MODE:=menu}"
: "${HOME_PHONE_ARI_PASSWORD:=}"
# Ganpath (README, "Ganpath (stage 2, phase 1)"). Used only with HOME_PHONE_TEST_MODE=ganpath
# or ganpath_all, apart from the ARI password. The defaults match the Ganpath service's own settings
# (`ganpath phone dialplan-vars`); a changed setting there must be changed here too.
: "${HOME_PHONE_GANPATH_ARI_PASSWORD:=}"
: "${HOME_PHONE_HA_FALLBACK_WEBHOOK:=}"
: "${HOME_PHONE_GANPATH_DEADMAN_S:=120}"
: "${HOME_PHONE_VM_MAX_S:=120}"
: "${HOME_PHONE_VM_SILENCE_S:=5}"
: "${HOME_PHONE_VM_RETENTION_H:=48}"
: "${HOME_PHONE_VM_SWEEP_S:=300}"

# An empty value in the root .env (NAME=) counts as unset: use the default.
[ -n "$HOME_PHONE_GANPATH_DEADMAN_S" ] || HOME_PHONE_GANPATH_DEADMAN_S=120
[ -n "$HOME_PHONE_VM_MAX_S" ] || HOME_PHONE_VM_MAX_S=120
[ -n "$HOME_PHONE_VM_SILENCE_S" ] || HOME_PHONE_VM_SILENCE_S=5
[ -n "$HOME_PHONE_VM_RETENTION_H" ] || HOME_PHONE_VM_RETENTION_H=48
[ -n "$HOME_PHONE_VM_SWEEP_S" ] || HOME_PHONE_VM_SWEEP_S=300

# no_lead0 VALUE: a whole number without its leading zeros (08 gives 8, 000 gives 0). The
# shell reads 08 as a bad octal number in $((...)) and 010 as 8. Any other value comes back
# unchanged, for the checks below.
no_lead0() {
    case "$1" in
        '' | *[!0-9]*) printf '%s' "$1" ;;
        *) v=$(printf '%s' "$1" | sed 's/^0*//'); printf '%s' "${v:-0}" ;;
    esac
}
HOME_PHONE_GANPATH_DEADMAN_S=$(no_lead0 "$HOME_PHONE_GANPATH_DEADMAN_S")
HOME_PHONE_VM_MAX_S=$(no_lead0 "$HOME_PHONE_VM_MAX_S")
HOME_PHONE_VM_SILENCE_S=$(no_lead0 "$HOME_PHONE_VM_SILENCE_S")
HOME_PHONE_VM_SWEEP_S=$(no_lead0 "$HOME_PHONE_VM_SWEEP_S")

# ARI is on when either user has a password. Test D's homephone user also turns on hold
# passthrough. Without any password, the container runs as in stage 1: no web server, no
# ARI, and menu option 4 gives an error beep.
if [ -n "$HOME_PHONE_ARI_PASSWORD" ] || [ -n "$HOME_PHONE_GANPATH_ARI_PASSWORD" ]; then
    HOME_PHONE_ARI_ENABLED=yes
else
    HOME_PHONE_ARI_ENABLED=no
fi
if [ -n "$HOME_PHONE_ARI_PASSWORD" ]; then
    HOME_PHONE_MOH_PASSTHROUGH=yes
else
    HOME_PHONE_MOH_PASSTHROUGH=no
fi

# whole NAME VALUE: stop the container unless VALUE is a whole number of at least 1.
whole() {
    case "$2" in
        '' | *[!0-9]*) echo "home-phone: $1 must be a whole number of seconds, at least 1" >&2; exit 1 ;;
    esac
    if [ "$2" -lt 1 ]; then
        echo "home-phone: $1 must be at least 1" >&2
        exit 1
    fi
}

# Ganpath mode (ganpath: test callers only; ganpath_all: every caller reaches the service,
# which decides whom it answers). ganpath_on is yes in both.
case "$HOME_PHONE_TEST_MODE" in
    ganpath | ganpath_all) ganpath_on=yes ;;
    menu | deflect) ganpath_on=no ;;
    *)
        ganpath_on=no
        echo "home-phone: WARNING: HOME_PHONE_TEST_MODE is not menu, deflect, ganpath or ganpath_all; calls are handled as in menu mode" >&2 ;;
esac

# Ganpath mode: check the values before anything is rendered, so a bad one stops the
# container with a clear message instead of a broken dialplan.
if [ "$ganpath_on" = yes ]; then
    whole HOME_PHONE_GANPATH_DEADMAN_S "$HOME_PHONE_GANPATH_DEADMAN_S"
    whole HOME_PHONE_VM_MAX_S "$HOME_PHONE_VM_MAX_S"
    whole HOME_PHONE_VM_SILENCE_S "$HOME_PHONE_VM_SILENCE_S"
    whole HOME_PHONE_VM_SWEEP_S "$HOME_PHONE_VM_SWEEP_S"
    if ! printf '%s\n' "$HOME_PHONE_VM_RETENTION_H" | grep -Eqx '[0-9]+([.][0-9]+)?'; then
        echo "home-phone: HOME_PHONE_VM_RETENTION_H must be a number of hours, such as 48 or 0.5" >&2
        exit 1
    fi
    # The URL goes unquoted into CURL(url,body) in the dialplan. A comma would start the body
    # early, and $ " ( ) \ or a space would change how Asterisk reads the line, so the alert
    # would silently fail. Home Assistant webhook URLs never need these characters.
    case "$HOME_PHONE_HA_FALLBACK_WEBHOOK" in
        *[,\$\"\(\)\\\ ]*)
            echo "home-phone: HOME_PHONE_HA_FALLBACK_WEBHOOK must not contain a comma, \$, \", (, ), \\ or a space" >&2
            exit 1 ;;
    esac
fi
# The fallback's own timer: the longest voicemail plus 60 s, so the message and the
# recording fit inside it. A value that is not a number gives 180 here; in Ganpath mode the
# check above has already stopped the container.
case "$HOME_PHONE_VM_MAX_S" in
    '' | *[!0-9]*) HOME_PHONE_GANPATH_FALLBACK_S=180 ;;
    *) HOME_PHONE_GANPATH_FALLBACK_S=$((HOME_PHONE_VM_MAX_S + 60)) ;;
esac
# The fallback calls the HA webhook only when one is set (1), never with an empty URL (0).
if [ -n "$HOME_PHONE_HA_FALLBACK_WEBHOOK" ]; then
    HOME_PHONE_GANPATH_HA_ALERT=1
else
    HOME_PHONE_GANPATH_HA_ALERT=0
fi

# A ';' starts a comment in Asterisk config files, so escape it in the passwords and the URL.
HOME_PHONE_SIP_PASSWORD_CONF=$(printf '%s' "$HOME_PHONE_SIP_PASSWORD" | sed 's/;/\\;/g')
HOME_PHONE_ARI_PASSWORD_CONF=$(printf '%s' "$HOME_PHONE_ARI_PASSWORD" | sed 's/;/\\;/g')
HOME_PHONE_GANPATH_ARI_PASSWORD_CONF=$(printf '%s' "$HOME_PHONE_GANPATH_ARI_PASSWORD" | sed 's/;/\\;/g')
HOME_PHONE_HA_FALLBACK_WEBHOOK_CONF=$(printf '%s' "$HOME_PHONE_HA_FALLBACK_WEBHOOK" | sed 's/;/\\;/g')

export HOME_PHONE_SIP_DOMAIN HOME_PHONE_SIP_PROXY HOME_PHONE_SIP_NUMBER \
    HOME_PHONE_SIP_PASSWORD_CONF HOME_PHONE_BIND HOME_PHONE_TEST_CALLERS \
    HOME_PHONE_TEST_FORWARD HOME_PHONE_TEST_MODE HOME_PHONE_ARI_ENABLED \
    HOME_PHONE_ARI_PASSWORD_CONF HOME_PHONE_MOH_PASSTHROUGH \
    HOME_PHONE_GANPATH_ARI_PASSWORD_CONF HOME_PHONE_HA_FALLBACK_WEBHOOK_CONF \
    HOME_PHONE_GANPATH_HA_ALERT HOME_PHONE_GANPATH_DEADMAN_S HOME_PHONE_GANPATH_FALLBACK_S \
    HOME_PHONE_VM_MAX_S HOME_PHONE_VM_SILENCE_S

# Only these variables are substituted; the dialplan's own ${...} stay as written.
VARS='${HOME_PHONE_SIP_DOMAIN} ${HOME_PHONE_SIP_PROXY} ${HOME_PHONE_SIP_NUMBER} ${HOME_PHONE_SIP_PASSWORD_CONF} ${HOME_PHONE_BIND} ${HOME_PHONE_TEST_CALLERS} ${HOME_PHONE_TEST_FORWARD} ${HOME_PHONE_TEST_MODE} ${HOME_PHONE_ARI_ENABLED} ${HOME_PHONE_ARI_PASSWORD_CONF} ${HOME_PHONE_MOH_PASSTHROUGH} ${HOME_PHONE_GANPATH_ARI_PASSWORD_CONF} ${HOME_PHONE_HA_FALLBACK_WEBHOOK_CONF} ${HOME_PHONE_GANPATH_HA_ALERT} ${HOME_PHONE_GANPATH_DEADMAN_S} ${HOME_PHONE_GANPATH_FALLBACK_S} ${HOME_PHONE_VM_MAX_S} ${HOME_PHONE_VM_SILENCE_S}'
for f in /opt/home-phone/config/*.conf; do
    envsubst "$VARS" < "$f" > "/etc/asterisk/$(basename "$f")"
done

# drop_section NAME FILE: delete [NAME] from its header to the next [header] or the end.
drop_section() {
    awk -v h="[$1]" '/^\[/ { skip = ($0 == h) } !skip' "$2" > "$2.tmp" && mv "$2.tmp" "$2"
}

# Each ARI user exists only while its password is set.
[ -n "$HOME_PHONE_ARI_PASSWORD" ] || drop_section homephone /etc/asterisk/ari.conf
[ -n "$HOME_PHONE_GANPATH_ARI_PASSWORD" ] || drop_section ganpath /etc/asterisk/ari.conf

if [ -n "$HOME_PHONE_ARI_PASSWORD" ]; then
    echo "home-phone: Test D on: ARI on 127.0.0.1:8088, hold passthrough on"
elif [ "$HOME_PHONE_ARI_ENABLED" = yes ]; then
    echo "home-phone: Test D off: no ARI user homephone (HOME_PHONE_ARI_PASSWORD is not set)"
else
    echo "home-phone: Test D off: no ARI (HOME_PHONE_ARI_PASSWORD is not set)"
fi
if [ -n "$HOME_PHONE_GANPATH_ARI_PASSWORD" ]; then
    echo "home-phone: Ganpath ARI user on: ganpath, ARI on 127.0.0.1:8088"
fi

mkdir -p /var/run/asterisk /var/log/asterisk
if [ "$ganpath_on" = yes ]; then
    # The voicemail folder in the recording tmpfs. Record() would make it too; made here so
    # ARI and the sweep find it before the first voicemail.
    mkdir -p /var/spool/asterisk/recording/voicemail
fi
chown -R asterisk:asterisk /etc/asterisk /var/run/asterisk /var/log/asterisk \
    /var/lib/asterisk /var/spool/asterisk
# extensions.conf can hold the HA fallback webhook URL, which carries a webhook ID.
chmod 600 /etc/asterisk/pjsip.conf /etc/asterisk/ari.conf /etc/asterisk/extensions.conf

if [ "$ganpath_on" = yes ]; then
    if [ "$HOME_PHONE_TEST_MODE" = ganpath_all ]; then
        who="everyone"
    else
        who="testers"
    fi
    if [ "$HOME_PHONE_GANPATH_HA_ALERT" = 1 ]; then alert=on; else alert="off (HOME_PHONE_HA_FALLBACK_WEBHOOK is not set)"; fi
    echo "home-phone: Ganpath mode on: $who; callers sent to Stasis(ganpath); dead-man ${HOME_PHONE_GANPATH_DEADMAN_S} s; voicemail up to ${HOME_PHONE_VM_MAX_S} s, ends after ${HOME_PHONE_VM_SILENCE_S} s of silence; HA fallback alert $alert"
    if [ -z "$HOME_PHONE_GANPATH_ARI_PASSWORD" ]; then
        echo "home-phone: WARNING: HOME_PHONE_GANPATH_ARI_PASSWORD is not set, so the Ganpath service cannot connect and every test call goes to the fallback"
    fi
    if [ ! -r /usr/share/asterisk/sounds/ganpath/ganpath-unavailable.wav ]; then
        echo "home-phone: WARNING: no ganpath-unavailable.wav in the Ganpath prompts folder; the fallback plays only the beep"
    fi
    # The voicemail sweep: deletes each voicemail HOME_PHONE_VM_RETENTION_H hours after it
    # was written, with or without the Ganpath service. It runs as root beside Asterisk.
    GANPATH_VM_RETENTION_H=$HOME_PHONE_VM_RETENTION_H
    GANPATH_VM_SWEEP_S=$HOME_PHONE_VM_SWEEP_S
    export GANPATH_VM_RETENTION_H GANPATH_VM_SWEEP_S
    /usr/local/bin/home-phone-vm-sweep &
    echo "home-phone: voicemail sweep on: every ${HOME_PHONE_VM_SWEEP_S} s, keeps ${HOME_PHONE_VM_RETENTION_H} h"
fi

exec asterisk -f -U asterisk -G asterisk -vvv
