FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Asterisk 20 from Ubuntu's archive, English prompts, music on hold (Test D2),
# envsubst for the config templates.
# The package's sample configs are removed (all but asterisk.conf): they enable
# things this host doesn't need, such as chan_sip on port 5060.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        asterisk asterisk-core-sounds-en-gsm asterisk-moh-opsound-gsm gettext-base tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && find /etc/asterisk -type f ! -name asterisk.conf -delete

COPY config/ /opt/home-phone/config/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# The Ganpath voicemail sweep. The entrypoint starts it only in Ganpath mode.
COPY ganpath-vm-sweep.sh /usr/local/bin/home-phone-vm-sweep
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/home-phone-vm-sweep

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
