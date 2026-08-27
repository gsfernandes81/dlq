# Completions for dlq, which queues a direct file URL for the download queue.
#
#   ln -s ~/or3/termux/expire/completions/dlq.fish ~/.config/fish/completions/

complete -c dlq -f
complete -c dlq -l name -r -d "saved file name (default: taken from the URL)"
complete -c dlq -l number -r -d "queue priority prefix (default: after the last)"
complete -c dlq -l sha256 -r -d "verify the finished file before delivery"
complete -c dlq -l expect-bytes -r -d "spending cap when the server states no size"
complete -c dlq -l probe -d "print the size and resume support, write nothing"
complete -c dlq -l dry-run -d "print the item instead of writing it"
complete -c dlq -l self-test -d "check the sizing and item logic, no network"
complete -c dlq -s h -l help -d "show the usage"
