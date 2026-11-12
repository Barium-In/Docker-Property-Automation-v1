#!/bin/sh
# Ganpath mode only (README, "Ganpath (stage 2, phase 1)"). A copy of the Ganpath service's
# own sweep script: keep the logic of the two the same. The entrypoint starts it with
# GANPATH_VM_RETENTION_H from HOME_PHONE_VM_RETENTION_H and GANPATH_VM_SWEEP_S from
# HOME_PHONE_VM_SWEEP_S in the root .env.
#
# Deletes voicemails that are older than retention.voicemail_h (plan rule 91, decision 53).
# It runs inside the home-phone container beside Asterisk and does not need the Ganpath
# service: a voicemail is deleted on time whether or not the service is running.
#
# Usage:
#   ganpath-vm-sweep.sh --once    one pass, then exit (a cron job or a test can call this)
#   ganpath-vm-sweep.sh           one pass every GANPATH_VM_SWEEP_S seconds, until stopped
#                                 (the entrypoint starts this in the background)
#
# Environment (the first two come from `ganpath phone dialplan-vars`):
#   GANPATH_VM_RETENTION_H   hours a voicemail is kept, counted from the file's last write,
#                            which is the end of the recording. May be fractional (0.5).
#                            Required. 0 deletes a file once it has not been written to for
#                            a minute, so a recording in progress is not cut.
#   GANPATH_VM_SWEEP_S       seconds between passes in loop mode. Required in loop mode.
#   GANPATH_VM_DIR           the voicemail folder. Default:
#                            /var/spool/asterisk/recording/voicemail
#
# Only regular files named ganpath-vm-* directly inside the folder are touched. The script
# prints counts and file names that carry no number (the names hold the last 4 digits at most).
# Exit status: 0 on success, 2 on a missing or bad setting.

set -u

DIR="${GANPATH_VM_DIR:-/var/spool/asterisk/recording/voicemail}"

die() {
    echo "ganpath-vm-sweep: $1" >&2
    exit 2
}

# minutes_for HOURS: whole minutes, rounded up, at least 1. Prints nothing for a bad value.
minutes_for() {
    awk -v h="$1" 'BEGIN {
        if (h !~ /^[0-9]+([.][0-9]+)?$/) exit 1
        m = h * 60
        n = int(m)
        if (m > n) n++
        if (n < 1) n = 1
        print n
    }'
}

sweep_once() {
    [ -n "${GANPATH_VM_RETENTION_H:-}" ] || die "GANPATH_VM_RETENTION_H is not set"
    case "$DIR" in /*) ;; *) die "GANPATH_VM_DIR must be an absolute path" ;; esac
    minutes=$(minutes_for "$GANPATH_VM_RETENTION_H") || die "GANPATH_VM_RETENTION_H is not a number"
    [ -d "$DIR" ] || return 0
    deleted=0
    failed=0
    # -mmin +N: last written more than N minutes ago. The names are listed first so a
    # file that Asterisk creates during the pass is not in the list.
    list=$(find "$DIR" -maxdepth 1 -type f -name 'ganpath-vm-*' -mmin "+$minutes" 2>/dev/null)
    old_ifs=$IFS
    IFS='
'
    for f in $list; do
        if rm -f -- "$f"; then
            deleted=$((deleted + 1))
        else
            failed=$((failed + 1))
        fi
    done
    IFS=$old_ifs
    if [ "$deleted" -gt 0 ] || [ "$failed" -gt 0 ]; then
        echo "ganpath-vm-sweep: deleted $deleted voicemail(s), $failed failed"
    fi
    [ "$failed" -eq 0 ]
}

if [ "${1:-}" = "--once" ]; then
    sweep_once
    exit $?
fi

[ -n "${GANPATH_VM_SWEEP_S:-}" ] || die "GANPATH_VM_SWEEP_S is not set"
case "$GANPATH_VM_SWEEP_S" in
    '' | *[!0-9]*) die "GANPATH_VM_SWEEP_S must be a whole number of seconds" ;;
esac
[ "$GANPATH_VM_SWEEP_S" -ge 1 ] || die "GANPATH_VM_SWEEP_S must be at least 1"
# Check the retention value once at start, so a bad value stops the loop at once.
minutes_for "$GANPATH_VM_RETENTION_H" >/dev/null || die "GANPATH_VM_RETENTION_H is not a number"
while :; do
    sweep_once || true
    sleep "$GANPATH_VM_SWEEP_S"
done
