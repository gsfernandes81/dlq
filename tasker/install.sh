#!/data/data/com.termux/files/usr/bin/bash
# Put zwana-tile where the Termux:Tasker plugin will run it. Runs ON THE PHONE.
#
#   termux/tasker/install.sh           link it in and check the preconditions
#   termux/tasker/install.sh --stub    write a one-line stub instead of a link
#
# A symlink, not a copy, for the reason the fish completions are symlinked: a
# `git pull` should be the whole update. If the plugin's file picker refuses to
# show the link, --stub writes a tiny script that execs the checkout copy, which
# updates the same way and is an ordinary file to anything looking at it.
set -eu

SRC="$(dirname "$(readlink -f "$0")")/zwana-tile"
DEST_DIR="$HOME/.termux/tasker"
DEST="$DEST_DIR/zwana-tile"
PROPS="$HOME/.termux/termux.properties"

stub=false
[ "${1:-}" = "--stub" ] && stub=true

if [ "$SRC" = "$DEST" ]; then
    printf 'refusing to install %s onto itself\n' "$SRC" >&2
    exit 1
fi

mkdir -p "$DEST_DIR"
# Remove the old entry rather than writing over it. A previous run leaves a
# symlink here, and `> "$DEST"` follows that link: the stub would land in the
# checkout, replacing the real script with a stub that execs itself forever.
# Observed, not theorised.
rm -f "$DEST"
if [ "$stub" = true ]; then
    printf '#!%s\nexec %s "$@"\n' "$(command -v bash)" "$SRC" >"$DEST"
else
    ln -sfn "$SRC" "$DEST"
fi
chmod +x "$DEST" "$SRC"
printf 'installed %s -> %s\n' "$DEST" "$SRC"

# The plugin refuses to run anything unless Termux is willing to be driven from
# outside, and it refuses *silently enough* that the tile just never updates.
if ! grep -qs '^allow-external-apps[[:space:]]*=[[:space:]]*true' "$PROPS"; then
    cat <<EOF

Termux:Tasker will not run this until Termux allows external apps. Add

    allow-external-apps = true

to $PROPS, then run: termux-reload-settings
EOF
fi

printf '\nchecking it runs:\n'
"$DEST" cached || printf 'zwana-tile exited %s\n' "$?"
printf '\nNow wire the Tasker side: docs/quota-tile.md\n'
