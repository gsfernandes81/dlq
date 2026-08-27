# Completions for dlqd, the expiring-quota download queue.
#
# Install as a symlink, so that pulling the repo updates them:
#
#   ln -s ~/or3/termux/expire/completions/dlqd.fish ~/.config/fish/completions/
#
# The symlink points into the checkout rather than copying, for the same reason
# every path in expire_sched.py does: the checkout is the one queue there is,
# and a copy of anything here goes stale without saying so.

function __dlqd_bare -d "nothing but the command typed yet"
    test (count (commandline -opc)) -le 1
end

function __dlqd_after -d "the subcommand already typed is one of these"
    set -l tokens (commandline -opc)
    test (count $tokens) -ge 2; or return 1
    contains -- $tokens[2] $argv
end

# No file completion: none of these take a path, and offering the whole of the
# home directory in place of a download name is worse than offering nothing.
complete -c dlqd -f

# Typing nothing and pressing enter opens the screen; these are the commands
# that answer without one.
complete -c dlqd -n __dlqd_bare -a status  -d "queue, budget, window times and job state"
complete -c dlqd -n __dlqd_bare -a list    -d "every download, and how much of it is here"
complete -c dlqd -n __dlqd_bare -a ui      -d "change it: reorder, rename, remove, retry, download now"
complete -c dlqd -n __dlqd_bare -a path    -d "where a finished download landed"
complete -c dlqd -n __dlqd_bare -a dest    -d "show or set where finished downloads are put"
complete -c dlqd -n __dlqd_bare -a queue   -d "just the queued item files"
complete -c dlqd -n __dlqd_bare -a logs    -d "last 40 lines of the runner log"
complete -c dlqd -n __dlqd_bare -a run-now -d "fire the whole queue once, without waiting"
complete -c dlqd -n "__dlqd_after run-now" -l blind \
    -d "no portal reachable: spend mobile data (asks first)"
complete -c dlqd -n __dlqd_bare -a arm     -d "register the nightly job"
complete -c dlqd -n __dlqd_bare -a cancel  -d "unregister it"

# `dlqd names` prints `name<TAB>state`, which is fish's own format for a
# candidate with a description, so the state shows up beside each name in the
# picker. It walks the queue directories and parses no item headers, because
# this runs on every press of the tab key.
complete -c dlqd -n "__dlqd_after path" -a "(dlqd names 2>/dev/null)"

# `dest` takes a kind and then a directory. Directories only — the destination
# is a folder, and offering files here would only ever be a mis-tap.
complete -c dlqd -n "__dlqd_after dest; and test (count (commandline -opc)) -eq 2" \
    -a video -d "where ytq puts finished videos"
complete -c dlqd -n "__dlqd_after dest; and test (count (commandline -opc)) -eq 2" \
    -a file -d "where dlq puts finished files"
complete -c dlqd -n "__dlqd_after dest; and test (count (commandline -opc)) -ge 3" \
    -a default -d "put the built-in default back"
complete -c dlqd -n "__dlqd_after dest; and test (count (commandline -opc)) -ge 3" \
    -a "(__fish_complete_directories)"

complete -c dlqd -n "__dlqd_after run-now" -l yes \
    -d "do not ask before spending data"
complete -c dlqd -n __dlqd_bare -l self-test -d "offline checks; no scheduler, no network"
