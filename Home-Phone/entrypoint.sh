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

# A ';' starts a comment in Asterisk config files, so escape it in the password.
HOME_PHONE_SIP_PASSWORD_CONF=$(printf '%s' "$HOME_PHONE_SIP_PASSWORD" | sed 's/;/\\;/g')

export HOME_PHONE_SIP_DOMAIN HOME_PHONE_SIP_PROXY HOME_PHONE_SIP_NUMBER \
    HOME_PHONE_SIP_PASSWORD_CONF HOME_PHONE_BIND HOME_PHONE_TEST_CALLERS \
    HOME_PHONE_TEST_FORWARD HOME_PHONE_TEST_MODE

# Only these variables are substituted; the dialplan's own ${...} stay as written.
VARS='${HOME_PHONE_SIP_DOMAIN} ${HOME_PHONE_SIP_PROXY} ${HOME_PHONE_SIP_NUMBER} ${HOME_PHONE_SIP_PASSWORD_CONF} ${HOME_PHONE_BIND} ${HOME_PHONE_TEST_CALLERS} ${HOME_PHONE_TEST_FORWARD} ${HOME_PHONE_TEST_MODE}'
for f in /opt/home-phone/config/*.conf; do
    envsubst "$VARS" < "$f" > "/etc/asterisk/$(basename "$f")"
done

mkdir -p /var/run/asterisk /var/log/asterisk
chown -R asterisk:asterisk /etc/asterisk /var/run/asterisk /var/log/asterisk \
    /var/lib/asterisk /var/spool/asterisk
chmod 600 /etc/asterisk/pjsip.conf

exec asterisk -f -U asterisk -G asterisk -vvv
