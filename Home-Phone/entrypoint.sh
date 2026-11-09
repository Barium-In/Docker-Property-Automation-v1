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

# Test D is optional. With an ARI password, ARI listens on 127.0.0.1:8088 and a hold goes to
# the carrier as a SIP re-INVITE. Without one, the container runs as in stage 1: no web
# server, no ARI, and menu option 4 gives an error beep.
if [ -n "$HOME_PHONE_ARI_PASSWORD" ]; then
    HOME_PHONE_ARI_ENABLED=yes
    HOME_PHONE_MOH_PASSTHROUGH=yes
else
    HOME_PHONE_ARI_ENABLED=no
    HOME_PHONE_MOH_PASSTHROUGH=no
fi

# A ';' starts a comment in Asterisk config files, so escape it in the passwords.
HOME_PHONE_SIP_PASSWORD_CONF=$(printf '%s' "$HOME_PHONE_SIP_PASSWORD" | sed 's/;/\\;/g')
HOME_PHONE_ARI_PASSWORD_CONF=$(printf '%s' "$HOME_PHONE_ARI_PASSWORD" | sed 's/;/\\;/g')

export HOME_PHONE_SIP_DOMAIN HOME_PHONE_SIP_PROXY HOME_PHONE_SIP_NUMBER \
    HOME_PHONE_SIP_PASSWORD_CONF HOME_PHONE_BIND HOME_PHONE_TEST_CALLERS \
    HOME_PHONE_TEST_FORWARD HOME_PHONE_TEST_MODE HOME_PHONE_ARI_ENABLED \
    HOME_PHONE_ARI_PASSWORD_CONF HOME_PHONE_MOH_PASSTHROUGH

# Only these variables are substituted; the dialplan's own ${...} stay as written.
VARS='${HOME_PHONE_SIP_DOMAIN} ${HOME_PHONE_SIP_PROXY} ${HOME_PHONE_SIP_NUMBER} ${HOME_PHONE_SIP_PASSWORD_CONF} ${HOME_PHONE_BIND} ${HOME_PHONE_TEST_CALLERS} ${HOME_PHONE_TEST_FORWARD} ${HOME_PHONE_TEST_MODE} ${HOME_PHONE_ARI_ENABLED} ${HOME_PHONE_ARI_PASSWORD_CONF} ${HOME_PHONE_MOH_PASSTHROUGH}'
for f in /opt/home-phone/config/*.conf; do
    envsubst "$VARS" < "$f" > "/etc/asterisk/$(basename "$f")"
done

if [ "$HOME_PHONE_ARI_ENABLED" = yes ]; then
    echo "home-phone: Test D on: ARI on 127.0.0.1:8088, hold passthrough on"
else
    # No password, so no ARI user either: drop [homephone], the last section of ari.conf.
    sed -i '/^\[homephone\]/,$d' /etc/asterisk/ari.conf
    echo "home-phone: Test D off: no ARI (HOME_PHONE_ARI_PASSWORD is not set)"
fi

mkdir -p /var/run/asterisk /var/log/asterisk
chown -R asterisk:asterisk /etc/asterisk /var/run/asterisk /var/log/asterisk \
    /var/lib/asterisk /var/spool/asterisk
chmod 600 /etc/asterisk/pjsip.conf /etc/asterisk/ari.conf

exec asterisk -f -U asterisk -G asterisk -vvv
