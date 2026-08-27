#!/data/data/com.termux/files/usr/bin/bash
# The tunnel that makes or3ecr reachable from the or3-dev container on zero.
#
#   or3-tunnel up        bring the link up (detached, survives closing Termux)
#   or3-tunnel down      take it down
#   or3-tunnel status    is it up, and is ssh actually connected right now
#   or3-tunnel log       the last reconnects
#   or3-tunnel watch     attach the screen session (Ctrl-A d to leave)
#   or3-tunnel run       the loop, in the foreground — what `up` runs under screen
#
# `or3-tunnel` is ~/.local/bin/or3-tunnel, a symlink to this file.
#
# Topology, and why it is this shape:
#
#   phone --cloudflared--> or3-dev's own connector --> or3-dev sshd
#     |                                                     |
#     '-- -R 2236:10.1.0.236:22 -----------------------> 127.0.0.1:2236
#
# The phone is the only machine that can see BOTH or3-dev (over the internet) and
# or3ecr (10.1.0.236, vessel LAN), so the link has to originate here. The forward
# is remote (-R), and it terminates INSIDE the container rather than on zero's own
# sshd — that keeps the vessel LAN off zero's docker bridge, and needs no
# GatewayPorts change to a boot-path sshd config on the box that runs Immich.
#
# CHANGED 2026-08-24. It used to be `phone --cloudflared--> zero --nc 127.0.0.1:2224-->
# or3-dev sshd`: one hop through zero's own sshd, because zero's tunnel carried the
# container's public name. or3-dev runs its own cloudflared now, so the connector is in
# the container and zero is not in the path at all. Nothing here had to change for that
# — it is one ssh alias, and the alias is what moved.
#
# Consequence worth knowing: or3ecr is reachable from the container only while
# this is running. When claude in there reports "connection refused on 2236", the
# tunnel is down; it is not or3ecr being off.
set -u

# ── which alias, and why it is the -sh one ───────────────────────────────────
# `or3-dev-sh`, NOT `or3-dev`. Both reach the same container over the same tunnel and
# meet the same host key; the difference is that `or3-dev` carries
#
#     RequestTTY yes
#     RemoteCommand in-workspace abduco -A claude claude
#
# so that `ssh or3-dev` on its own IS the claude session. A RemoteCommand cannot be
# combined with `-N`, which is the whole of what this script's session is: no command,
# one forward. The `-sh` alias exists for exactly this — it, scp, and any
# `ssh or3-dev-sh <command>`.
#
# Both aliases are written by infra's ansible/playbooks/dev-client.yml. Their
# ProxyCommand sources a mode-600 token file and execs `cloudflared access ssh`; the
# Access service token is never on the command line, because a secret on a command line
# is a secret in `ps` output and in shell history.
#
# `or3-dev-lan` is the third alias and the break-glass one: through zero's own sshd to
# the published loopback port, touching no Cloudflare. It uses `ProxyCommand ssh zero nc
# %h %p` and NOT ProxyJump, because zero sets AllowTcpForwarding no, which refuses the
# direct-tcpip channel ProxyJump opens; a session channel running nc is not covered by
# it. Set OR3_TUNNEL_HOST=or3-dev-lan to build this tunnel that way when Cloudflare is
# the thing that is broken.
HOST="${OR3_TUNNEL_HOST:-or3-dev-sh}"
RPORT="${OR3_TUNNEL_RPORT:-2236}"         # port inside the container
TARGET="${OR3_TUNNEL_TARGET:-10.1.0.236:22}"

SESSION="${OR3_TUNNEL_SESSION:-or3-tunnel}"
LOG="${OR3_TUNNEL_LOG:-$HOME/.cache/or3-tunnel.log}"
LOG_MAX=262144                        # 256 KiB, then start it over

MIN_BACKOFF=5
MAX_BACKOFF=300                       # 5 min: the WiFi here swings 5ms..200ms and drops

stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
say()   { printf '%s  %s\n' "$(stamp)" "$*"; }

# The screen session is only half the answer: it stays alive while the loop sits
# in its backoff sleep with nothing forwarded. The ssh child is the real state,
# so both are reported separately and neither is inferred from the other.
session_alive() { screen -S "$SESSION" -Q select . >/dev/null 2>&1; }
ssh_pid()       { pgrep -f "ssh -N -T .*-R ${RPORT}:${TARGET} ${HOST}\$" | head -n1; }

cmd_run() {
    local backoff=$MIN_BACKOFF start up rc
    trap 'say "stopping"; exit 0' INT TERM
    say "tunnel: $HOST  -R $RPORT -> $TARGET"

    while :; do
        start=$(date +%s)

        # -N no command, -T no tty: this session carries a forward and nothing else.
        # ExitOnForwardFailure is the important one — without it ssh connects happily
        # when the remote port is already bound, and you get a session that looks
        # healthy while forwarding nothing. Fail instead, and let the loop retry.
        ssh -N -T \
            -o ExitOnForwardFailure=yes \
            -o ServerAliveInterval=30 \
            -o ServerAliveCountMax=3 \
            -o ConnectTimeout=30 \
            -R "${RPORT}:${TARGET}" \
            "$HOST"
        rc=$?

        up=$(( $(date +%s) - start ))

        # A session that lasted a while was working; reset the backoff so a long-lived
        # tunnel that finally drops reconnects promptly instead of inheriting the
        # 5-minute delay from whatever happened when it was first started.
        if [ "$up" -ge 60 ]; then
            say "tunnel was up ${up}s, exited rc=$rc — reconnecting"
            backoff=$MIN_BACKOFF
        else
            say "tunnel failed after ${up}s, rc=$rc — retrying in ${backoff}s"
        fi

        sleep "$backoff"
        backoff=$(( backoff * 2 ))
        [ "$backoff" -gt "$MAX_BACKOFF" ] && backoff=$MAX_BACKOFF
    done
}

cmd_up() {
    if session_alive; then
        echo "already up — screen session '$SESSION'"
        cmd_status
        return 0
    fi

    mkdir -p "$(dirname "$LOG")"
    # Truncate rather than rotate: this log is a breadcrumb trail of reconnects,
    # not a record anyone goes back through, and an unbounded file on a phone is
    # a slow leak nobody would notice.
    if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt "$LOG_MAX" ]; then
        : > "$LOG"
    fi

    screen -dmS "$SESSION" -L -Logfile "$LOG" "$0" run
    # screen forks and returns immediately; give the first ssh a moment to get
    # far enough that status says something true rather than "connecting".
    sleep 3
    cmd_status
}

cmd_down() {
    if ! session_alive; then
        echo "already down"
        return 0
    fi
    # Quit the session, not just the ssh: killing ssh alone leaves the loop to
    # reconnect it five seconds later, which reads as "down didn't work".
    screen -S "$SESSION" -X quit
    sleep 1
    if session_alive; then
        echo "screen session '$SESSION' would not quit"
        return 1
    fi
    echo "down"
}

cmd_status() {
    local pid rc=0
    if ! session_alive; then
        echo "link:    DOWN — no screen session '$SESSION'   (or3-tunnel up)"
        rc=1
    else
        pid=$(ssh_pid)
        if [ -n "$pid" ]; then
            echo "link:    UP — ssh pid $pid, $HOST -R $RPORT -> $TARGET"
        else
            # Session alive, no ssh: the loop is between attempts. or3ecr is not
            # reachable from the container right now either way.
            echo "link:    RETRYING — session up, ssh not connected"
            rc=1
        fi
    fi
    [ -s "$LOG" ] && { echo "log:"; tail -n 3 "$LOG" | sed 's/^/  /'; }
    return $rc
}

case "${1:-status}" in
    up)             cmd_up ;;
    down|stop)      cmd_down ;;
    status)         cmd_status ;;
    restart)        cmd_down; cmd_up ;;
    log)            tail -n "${2:-20}" "$LOG" ;;
    watch|attach)   screen -r "$SESSION" ;;
    run)            cmd_run ;;
    -h|--help|help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' ;;
    *)              echo "usage: or3-tunnel {up|down|status|restart|log|watch}" >&2; exit 2 ;;
esac
