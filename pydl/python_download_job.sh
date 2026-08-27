#!/data/data/com.termux/files/usr/bin/bash
# Job body for the gated Python-installer download.
#
# Android's JobScheduler runs this, via Termux:API, roughly every 15 minutes.
# All it does is call fetch_python_installer.py, which decides for itself
# whether the time and the quota are right -- see that script's docstring. This
# file only supplies the things a job body has to supply: a wake lock across the
# transfer, a log that survives the process, a notification when the download
# lands, and self-cancellation once there is nothing left to do.
#
# It is armed and torn down by schedule_python_download.sh; running it by hand
# is harmless and does exactly what a scheduled firing would do.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
DOWNLOADER="$HERE/fetch_python_installer.py"
# Where the installer is delivered: the work-PC bootstrap area of this repo.
BOOTSTRAP="$HERE/../../work/bootstrap/downloads"
DEST="$BOOTSTRAP/python-3.14.3-amd64.exe"
TRIGGER_LOG="$BOOTSTRAP/trigger.log"
HEARTBEAT="$BOOTSTRAP/heartbeat"

# Must match schedule_python_download.sh.
JOB_ID=3143

# How long before 00:00 UTC the download is allowed to start.
#
# The point of the exercise is to spend allowance that would otherwise expire
# unused, so the window is deliberately late and narrow: 23:30 UTC. Two firings
# of a 15-minute job are due inside it, and 30 minutes is 60x the ~30s the
# transfer needs.
#
# Widening it would only buy insurance against Doze deferring both firings, and
# would cost the thing the window exists to protect -- quota spent at 21:00 is
# quota that was still available to use. A missed night is cheap: the installer
# is a one-off, and the job simply tries again the next evening.
LEAD_SECONDS="${PYDL_LEAD_SECONDS:-1800}"

PYTHON=/data/data/com.termux/files/usr/bin/python3

mkdir -p "$BOOTSTRAP"

note() {
    printf '%s  %s\n' "$(date -u '+%Y-%m-%d %H:%M:%SZ')" "$*" >> "$TRIGGER_LOG"
}

complete() {
    # 30_213_192 bytes: the size fetch_python_installer.py pins and verifies.
    [ -f "$DEST" ] && [ "$(stat -c %s "$DEST" 2>/dev/null)" = "30213192" ]
}

unschedule() {
    termux-job-scheduler --cancel --job-id "$JOB_ID" >/dev/null 2>&1
}

# Nothing to do, and no reason to keep waking up. Reached on the firing after a
# successful download, and by any manual run once the file is there.
if complete; then
    note "installer already present - cancelling job $JOB_ID"
    unschedule
    exit 0
fi

# No wake lock is taken here, deliberately. JobScheduler holds one for us across
# the job's execution -- that is the platform's contract, and it is why a job is
# the right mechanism rather than a sleeping shell.
#
# Taking one anyway would be actively harmful: Termux's wake lock is a single
# global flag with no reference counting, so termux-wake-unlock releases it no
# matter who acquired it. A job that unlocked on its way out would silently drop
# a wake lock the user had taken for something else entirely.
#
# Worst case the platform's guarantee fails and the device suspends mid-transfer:
# the socket dies, the .part file survives, and the next firing resumes it.
output="$("$PYTHON" "$DOWNLOADER" --lead-seconds "$LEAD_SECONDS" 2>&1)"
status=$?

if [ "$status" -eq 0 ] && printf '%s' "$output" | grep -q '^not yet:'; then
    # The overwhelmingly common case: ~96 firings a day land here, and appending
    # them all would bury the handful of lines that matter. Record liveness in a
    # file that is overwritten instead.
    printf '%s  %s\n' "$(date -u '+%Y-%m-%d %H:%M:%SZ')" \
        "$(printf '%s' "$output" | grep '^not yet:')" > "$HEARTBEAT"
    exit 0
fi

note "run (exit $status)"
printf '%s\n' "$output" | sed 's/^/    /' >> "$TRIGGER_LOG"

if complete; then
    termux-notification --id python-dl \
        --title "Python installer downloaded" \
        --content "python-3.14.3-amd64.exe ready in or3/work/bootstrap/downloads" \
        >/dev/null 2>&1
    note "download complete - cancelling job $JOB_ID"
    unschedule
elif [ "$status" -ne 0 ]; then
    # Blocked or failed. Worth telling someone about, since the window closes at
    # midnight and a human may be able to free up quota; the job stays armed and
    # tries again in ~15 minutes either way.
    termux-notification --id python-dl \
        --title "Python installer download blocked" \
        --content "$(printf '%s' "$output" | tail -1)" \
        >/dev/null 2>&1
fi

exit 0
