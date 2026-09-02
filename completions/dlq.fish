# Completions for dlq, the expiring-quota download queue.
#
# Install as a symlink, so that pulling the repo updates them:
#
#   ln -s ~/dlq/completions/dlq.fish ~/.config/fish/completions/
#
# The symlink points into the checkout rather than copying, for the same reason
# every path in expire_sched.py does: the checkout is the one queue there is,
# and a copy of anything here goes stale without saying so.

function __dlq_bare -d "nothing but the command typed yet"
    test (count (commandline -opc)) -le 1
end

function __dlq_after -d "the subcommand already typed is one of these"
    set -l tokens (commandline -opc)
    test (count $tokens) -ge 2; or return 1
    contains -- $tokens[2] $argv
end

# No file completion: none of these take a path, and offering the whole of the
# home directory in place of a download name is worse than offering nothing.
complete -c dlq -f

# Typing nothing and pressing enter opens the screen; these are the commands
# that answer without one.
complete -c dlq -n __dlq_bare -a status   -d "queue, budget, window times and job state"
complete -c dlq -n __dlq_bare -a list     -d "every download, and how much of it is here"
complete -c dlq -n __dlq_bare -a ui       -d "change it: reorder, rename, remove, retry, download now"
complete -c dlq -n __dlq_bare -a path     -d "where a finished download landed"
complete -c dlq -n __dlq_bare -a dest     -d "show or set where finished downloads are put"
complete -c dlq -n __dlq_bare -a settings -d "the window, the reserve and automatic downloads"
complete -c dlq -n __dlq_bare -a queue    -d "just the queued item files"
complete -c dlq -n __dlq_bare -a logs     -d "last 40 lines of the runner log"
complete -c dlq -n __dlq_bare -a dump     -d "everything a bug report needs, in one paste"
complete -c dlq -n "__dlq_after dump" -a "(dlq names 2>/dev/null)"
complete -c dlq -n __dlq_bare -a run-now  -d "fire the whole queue once, without waiting"
complete -c dlq -n "__dlq_after run-now" -l blind \
    -d "no portal reachable: spend mobile data (asks first)"
# `dlq <url>` queues a direct file download — the flags below apply only once
# a URL is the first word, so they never crowd the verb list.
function __dlq_url -d "a URL is already typed"
    set -l tokens (commandline -opc)
    test (count $tokens) -ge 2; and string match -q "*://*" -- $tokens[2]
end

complete -c dlq -n __dlq_url -l name -r -d "saved file name (default: taken from the URL)"
complete -c dlq -n __dlq_url -l number -r -d "queue priority prefix (default: after the last)"
complete -c dlq -n __dlq_url -l sha256 -r -d "verify the finished file before delivery"
complete -c dlq -n __dlq_url -l expect-bytes -r -d "spending cap when the server states no size"
complete -c dlq -n __dlq_url -l dest -r -d "put this one somewhere else"
complete -c dlq -n __dlq_url -l probe -d "print the size and resume support, write nothing"
complete -c dlq -n __dlq_url -l dry-run -d "print the item instead of writing it"
complete -c dlq -n __dlq_url -l again -d "queue it even though it is already queued or done"
complete -c dlq -n __dlq_bare -a arm      -d "register the nightly job"
complete -c dlq -n __dlq_bare -a cancel   -d "unregister it"

# `dlq names` prints `name<TAB>state`, which is fish's own format for a
# candidate with a description, so the state shows up beside each name in the
# picker. It walks the queue directories and parses no item headers, because
# this runs on every press of the tab key.
complete -c dlq -n "__dlq_after path" -a "(dlq names 2>/dev/null)"

# `dest` takes a kind and then a directory. Directories only — the destination
# is a folder, and offering files here would only ever be a mis-tap.
complete -c dlq -n "__dlq_after dest; and test (count (commandline -opc)) -eq 2" \
    -a video -d "where ytq puts finished videos"
complete -c dlq -n "__dlq_after dest; and test (count (commandline -opc)) -eq 2" \
    -a audio -d "where ytq puts audio-only downloads"
complete -c dlq -n "__dlq_after dest; and test (count (commandline -opc)) -eq 2" \
    -a file -d "where dlq puts finished files"
complete -c dlq -n "__dlq_after dest; and test (count (commandline -opc)) -ge 3" \
    -a default -d "put the built-in default back"
complete -c dlq -n "__dlq_after dest; and test (count (commandline -opc)) -ge 3" \
    -a "(__fish_complete_directories)"

# `settings` takes a name and then a value. The names are spelled out here
# rather than read from the runner: a completion may not import anything or
# run a command that could, because this fires on every press of the tab key —
# `dlq names` is the one exception and it only walks directories. The
# self-test's own pin is on the other side of it: expire_sched checks that
# every setting the runner has is one it can say a sentence about.
function __dlq_setting -d "the setting is one of these, and its value is next"
    set -l tokens (commandline -opc)
    test (count $tokens) -eq 3; or return 1
    test $tokens[2] = settings; or return 1
    contains -- $tokens[3] $argv
end

complete -c dlq -n "__dlq_after settings; and test (count (commandline -opc)) -eq 2" \
    -a window -d "how early downloads may start (a multiple of 15 minutes)"
complete -c dlq -n "__dlq_after settings; and test (count (commandline -opc)) -eq 2" \
    -a reserve -d "data kept back, never spent (MB)"
complete -c dlq -n "__dlq_after settings; and test (count (commandline -opc)) -eq 2" \
    -a reserve-when-paid -d "keep it when paid data is there"
complete -c dlq -n "__dlq_after settings; and test (count (commandline -opc)) -eq 2" \
    -a auto -d "let the nightly job download"
complete -c dlq -n "__dlq_after settings; and test (count (commandline -opc)) -eq 3" \
    -a default -d "put the built-in default back"
complete -c dlq -n "__dlq_setting auto" -a on -d "the nightly job downloads"
complete -c dlq -n "__dlq_setting auto" -a off \
    -d "the nightly job fires and does nothing; run-now still works"
complete -c dlq -n "__dlq_setting reserve-when-paid" -a yes \
    -d "the reserve is kept even when paid data is there"
complete -c dlq -n "__dlq_setting reserve-when-paid" -a no \
    -d "paid data waives the reserve"

complete -c dlq -n "__dlq_after run-now" -l yes \
    -d "do not ask before spending data"
complete -c dlq -n __dlq_bare -l self-test -d "offline checks; no scheduler, no network"
