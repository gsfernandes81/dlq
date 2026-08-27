#!/data/data/com.termux/files/usr/bin/bash
# Move code from GitHub to or3ecr, through the phone. Runs ON THE PHONE.
#
#   or3-deploy            pull from GitHub, push to the PC, check it landed
#   or3-deploy status     what the phone has, what the PC has, is the PC clean
#   or3-deploy setup      add the `pc` remote, once
#   or3-deploy push       push only -- no pull, and so no metered bytes
#
# Topology, and why it is this shape:
#
#   GitHub  --metered, over the radio-->  phone ~/or3  --free, ship LAN-->  or3ecr
#
# The phone starts both connections. That is the whole design: nothing needs a
# fixed address for the phone, nothing listens on the phone, and no tunnel is
# involved. or3ecr is 10.1.0.236 and does not move, and the phone already holds
# the key its sshd accepts.
#
# Why the PC end takes a push at all: its checkout has a branch checked out, and
# git refuses to update that by default. Stage 4 of the pen drive sets
# `receive.denyCurrentBranch=updateInstead` there, which makes git update the
# working tree with the push. The phone does NOT need that setting -- nothing
# pushes into the phone.
#
# What lands is what runs: the PC's packages are installed editable from that
# checkout, so there is no wheel to build and no install step afterwards.
set -u

REPO="${OR3_REPO:-$HOME/or3}"
PC_HOST="${OR3_PC_HOST:-or3ecr}"
PC_PATH="${OR3_PC_PATH:-C:/Users/OR3-ECR/src/or3}"
BRANCH="${OR3_BRANCH:-main}"
REMOTE="${OR3_PC_REMOTE:-pc}"

# The expiring-quota runner reads this checkout between these times, and a pull
# rewrites files under it. Pushing to the PC is unaffected -- only the pull is
# refused.
WINDOW_FROM="${OR3_WINDOW_FROM:-2230}"
WINDOW_TO="${OR3_WINDOW_TO:-2400}"

say()  { printf '%s\n' "$*"; }
die()  { printf '%s\n' "$*" >&2; exit 1; }

[ -d "$REPO/.git" ] || die "no git checkout at $REPO (set OR3_REPO)"

pc_ssh() { ssh -o ConnectTimeout=15 "$PC_HOST" "$@" 2>/dev/null | tr -d '\r'; }
pc_git() { pc_ssh git -C "$PC_PATH" "$@"; }

in_window() {
    local now; now="$(date +%H%M)"
    [ "$now" -ge "$WINDOW_FROM" ] && [ "$now" -lt "$WINDOW_TO" ]
}

cmd_setup() {
    if git -C "$REPO" remote get-url "$REMOTE" > /dev/null 2>&1; then
        say "remote '$REMOTE' is already $(git -C "$REPO" remote get-url "$REMOTE")"
    else
        git -C "$REPO" remote add "$REMOTE" "$PC_HOST:$PC_PATH" ||
            die "could not add the remote"
        say "added remote '$REMOTE' -> $PC_HOST:$PC_PATH"
    fi
    say ""
    say "The PC side is set up by stage 4 of the pen drive. If this checkout was"
    say "made another way, it needs this once, on the PC:"
    say "    git -C $PC_PATH config receive.denyCurrentBranch updateInstead"
}

cmd_status() {
    local here there dirty
    here="$(git -C "$REPO" rev-parse --short "$BRANCH" 2>/dev/null || echo '?')"
    say "phone   $REPO"
    say "        $BRANCH at $here"
    case "$(git -C "$REPO" status --porcelain | head -1)" in
        "") say "        tree clean" ;;
        *)  say "        tree has local changes" ;;
    esac

    there="$(pc_git rev-parse --short "$BRANCH")"
    if [ -z "$there" ]; then
        say "pc      unreachable ($PC_HOST) -- ship WiFi, or git not on its PATH"
        return 1
    fi
    say "pc      $PC_HOST:$PC_PATH"
    say "        $BRANCH at $there"
    dirty="$(pc_git status --porcelain | head -1)"
    if [ -n "$dirty" ]; then
        say "        tree has local changes -- a push will be refused until they go"
    else
        say "        tree clean"
    fi
    [ "$here" = "$there" ] && say "" && say "in step." || { say ""; say "the PC is behind."; }
}

cmd_pull() {
    if in_window; then
        die "It is $(date +%H:%M). The expiring-quota runner is using $REPO between
${WINDOW_FROM:0:2}:${WINDOW_FROM:2} and midnight, and a pull rewrites files under it.
Wait, or run: or3-deploy push   (no pull, no metered bytes)"
    fi
    say "pulling from GitHub (this crosses the radio)"
    git -C "$REPO" pull --ff-only 2>&1 | sed 's/^/  /' || die "pull failed"
}

cmd_push() {
    local before after
    before="$(pc_git rev-parse "$BRANCH")"
    say "pushing to $PC_HOST (ship LAN, no quota)"
    git -C "$REPO" push "$REMOTE" "$BRANCH" 2>&1 | sed 's/^/  /'

    # Verified, not assumed: git reports success for the ref update, and the
    # working tree update is a separate thing that updateInstead does. If that
    # part were skipped the PC would hold new commits and run old files.
    after="$(pc_git rev-parse "$BRANCH")"
    local mine; mine="$(git -C "$REPO" rev-parse "$BRANCH")"
    if [ -z "$after" ]; then
        say "could not read the PC's HEAD back; check with: or3-deploy status"
        return 1
    fi
    if [ "$after" != "$mine" ]; then
        say "the PC is at ${after:0:12}, this phone is at ${mine:0:12} -- the push did not land"
        return 1
    fi
    [ "$before" = "$after" ] && say "already up to date" || say "landed: ${after:0:12}"
    say "The checkout is installed editable there, so that is now what runs."
}

case "${1:-deploy}" in
    setup)  cmd_setup ;;
    status) cmd_status ;;
    push)   cmd_push ;;
    deploy) cmd_pull && cmd_push ;;
    *)      die "usage: or3-deploy [deploy|push|status|setup]" ;;
esac
