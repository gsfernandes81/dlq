#!/data/data/com.termux/files/usr/bin/bash
# Arm the gated Python-installer download so it runs in the hours before
# 00:00 UTC, when the day's unspent data allowance would otherwise be lost.
#
# Mechanism: an Android JobScheduler job, registered through Termux:API
# (termux-job-scheduler). The job body is python_download_job.sh; the decision
# about whether to actually download lives in fetch_python_installer.py.
#
# Why this rather than the alternatives:
#   * runit (what this used to use) -- worked, but only while Termux itself is
#                   alive. It is a userspace poll loop with no claim on the
#                   platform: Android may kill the app during a 17-hour wait,
#                   and nothing brings it back until Termux is next opened.
#   * cron/at    -- not installed, and no package may be fetched (metered link).
#   * nohup sleep -- same fragility as runit, minus the supervision.
#
# JobScheduler is the platform's own scheduler: the job is registered with the
# system, survives Termux being killed and (with --persisted) reboots, and is
# restored without anyone opening the app. The cost is precision -- see the
# LEAD_SECONDS note in python_download_job.sh -- which is paid for with a much
# wider window and a job that fires repeatedly inside it.
#
# Requires the Termux:API app as well as the termux-api package; without the app
# every termux-* call hangs instead of returning.
#
# Usage:  schedule_python_download.sh [arm|status|cancel|logs|run-now]

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$HERE/python_download_job.sh"
# Where the installer is delivered: the work-PC bootstrap area of this repo.
BOOTSTRAP="$HERE/../../work/bootstrap/downloads"
DEST="$BOOTSTRAP/python-3.14.3-amd64.exe"
TRIGGER_LOG="$BOOTSTRAP/trigger.log"
HEARTBEAT="$BOOTSTRAP/heartbeat"

# Must match python_download_job.sh.
JOB_ID=3143
LEAD_SECONDS="${PYDL_LEAD_SECONDS:-1800}"

# The floor Android enforces since N; asking for less is silently clamped to it,
# so this is as tight as the firing interval gets.
#
# A periodic job's cycle is anchored to when it was registered, and the system
# tends to run it early in each cycle -- so 'arm' run at ~23:30 UTC biases the
# daily firing towards the window. Only a bias: Doze can defer a firing, and a
# reboot re-anchors the cycle to boot time.
PERIOD_MS=900000

# Left behind by the previous, runit-based version of this script.
OLD_SERVICE_DIR="$PREFIX/var/service/python-dl"

mkdir -p "$BOOTSTRAP"

require_api() {
    if ! command -v termux-job-scheduler >/dev/null 2>&1; then
        echo "error: termux-job-scheduler not found - pkg install termux-api" >&2
        exit 1
    fi
    # The package alone is not enough: without the companion app the call blocks
    # forever rather than failing, so probe with a timeout instead of trusting it.
    if ! timeout 20 termux-job-scheduler --pending >/dev/null 2>&1; then
        echo "error: Termux:API did not respond - is the Termux:API app installed?" >&2
        exit 1
    fi
}

# Tear down the runit service this script used to install, so the two cannot
# both be driving the downloader.
retire_runit() {
    if [ -d "$OLD_SERVICE_DIR" ]; then
        sv down python-dl >/dev/null 2>&1
        rm -rf "$OLD_SERVICE_DIR"
        echo "  removed the old runit service ($OLD_SERVICE_DIR)"
    fi
}

arm() {
    require_api
    retire_runit
    chmod +x "$RUNNER"

    # --network any: the job is pointless without a connection, so let the
    #   platform hold it until there is one.
    # --battery-not-low false: the default would let a low battery skip the only
    #   window of the day, and the job costs ~30 seconds of radio.
    # --persisted true: survives a reboot; the download is 17 hours away.
    timeout 30 termux-job-scheduler \
        --script "$RUNNER" \
        --job-id "$JOB_ID" \
        --period-ms "$PERIOD_MS" \
        --network any \
        --battery-not-low false \
        --storage-not-low false \
        --charging false \
        --persisted true
}

cancel() {
    require_api
    timeout 30 termux-job-scheduler --cancel --job-id "$JOB_ID" 2>&1
    retire_runit
}

status() {
    local now deadline start
    now=$(date -u +%s)
    deadline=$(date -u -d 'tomorrow 00:00' +%s)
    start=$(( deadline - LEAD_SECONDS ))
    echo "now            : $(date -u '+%Y-%m-%d %H:%M:%SZ')"
    if [ "$now" -lt "$start" ]; then
        echo "window opens   : $(date -u -d "@$start" '+%Y-%m-%d %H:%M:%SZ')  (in $(( (start-now)/3600 ))h $(( ((start-now)%3600)/60 ))m)"
    else
        echo "window opens   : $(date -u -d "@$start" '+%Y-%m-%d %H:%M:%SZ')  (OPEN NOW)"
    fi
    echo "deadline       : $(date -u -d "@$deadline" '+%Y-%m-%d %H:%M:%SZ')"

    echo "job            :"
    timeout 20 termux-job-scheduler --pending 2>&1 | sed 's/^/    /'

    if [ -f "$DEST" ]; then
        echo "installer      : PRESENT ($(stat -c %s "$DEST") bytes)"
    else
        echo "installer      : not downloaded yet"
    fi
    [ -f "$HEARTBEAT" ] && echo "last firing    : $(cat "$HEARTBEAT")"
    [ -d "$OLD_SERVICE_DIR" ] && echo "WARNING        : old runit service still present at $OLD_SERVICE_DIR"
    [ -f "$BOOTSTRAP/download.log" ] && { echo "download.log   :"; tail -5 "$BOOTSTRAP/download.log" | sed 's/^/    /'; }
    return 0
}

case "${1:-arm}" in
    arm)     arm; echo; status ;;
    status)  status ;;
    cancel)  cancel ;;
    logs)    tail -30 "$TRIGGER_LOG" 2>/dev/null || echo "(no trigger log yet)" ;;
    run-now) "$RUNNER"; echo "ran the job body once; see 'logs'" ;;
    *)       echo "usage: $0 [arm|status|cancel|logs|run-now]" >&2; exit 2 ;;
esac
