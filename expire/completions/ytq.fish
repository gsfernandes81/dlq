# Completions for ytq, which searches for a video and queues it.
#
#   ln -s ~/or3/termux/expire/completions/ytq.fish ~/.config/fish/completions/
#
# The argument is either a URL or the words to search for, and nothing local
# can complete either, so this is the options and their descriptions — and,
# for --from-json, real files.

complete -c ytq -f
complete -c ytq -l now -d "start it in the background instead of waiting for the window (spends data)"
complete -c ytq -l list -d "print the formats and caps, write nothing"
complete -c ytq -l dest -r -F -d "put this one somewhere other than the configured video directory"
complete -c ytq -l from-json -r -F -d "reuse a saved 'yt-dlp -J' dump or search; costs no data"
complete -c ytq -l self-test -d "check the format, search and item logic, no network"
complete -c ytq -s h -l help -d "show the usage"
