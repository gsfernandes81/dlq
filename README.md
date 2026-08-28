# dlq — the overnight download queue

The phone's data plan grants free data daily and wipes whatever is unused at
00:00 UTC. In the hour before that reset, a nightly job spends the leftover on
downloads queued here — always leaving a floor for the morning and never
touching the paid reserve. `docs/download-queue.md` is the user guide;
`queue/README.md` is the item contract.

One command: **`dlqd`**. Bare on a terminal it opens the queue's screen; off
one it prints status. `dlqd dlq <url>` queues a plain file URL (until
2026-08-28 that was a command of its own). Videos are queued by
[`ytq`](../ytq), which lives in its own repo and writes into this queue.

## The three checkouts

This repo split out of a monorepo on 2026-08-28, alongside two siblings:

- **`~/dlq`** — this repo: the queue, its runner (`expire_runner.py`), the
  shared downloader (`expire_dl.py`), and `dlqd` (`expire_sched.py`,
  `expire_ui.py`, `dlq.py`).
- **`~/ytq`** — the YouTube front end, plus `ytdl_item.py`, which the items it
  writes download through.
- **`~/zwana-quota`** — the portal client and quota widget; the runner reads
  every guard figure through it.

They find each other as checkouts, never as installed packages: each module
looks at `$EXPIRE_HOME` / `$YTQ_HOME` / `$ZWANA_HOME` first, then for a clone
beside its own repo, then under `~`. The phone keeps all three under `~`.

## Checks

`make test` (pytest) or `make check` — the same module self-tests either way;
`.githooks/checks.sh` is the one copy of what runs, and the pre-push hook
(`git config core.hooksPath .githooks`, once per clone) refuses a push that
fails them. They need the sibling checkouts present. They are offline: no
network, no scheduler, no portal.

## Migrating from or3 (one-time, on the phone)

The queue used to live at `~/or3/termux/expire`. After cloning the three
repos:

```
mv ~/or3/termux/expire/queue/*.py ~/dlq/queue/        # pending items
mv ~/or3/termux/expire/{work,out,done,failed,logs} ~/dlq/  # partials + history
mv ~/or3/termux/expire/{state.json,config.json} ~/dlq/     # 2>/dev/null ok
sed -i 's|/home/or3/termux/expire|/home/dlq|' ~/dlq/queue/*.py  # old item paths
mv ~/or3/.env ~/zwana-quota/.env                      # portal credentials
uv tool uninstall or3-expire-queue
uv tool install --offline --editable ~/dlq
uv tool install --offline --editable ~/ytq
ln -sf ~/dlq/completions/dlqd.fish ~/.config/fish/completions/
rm -f ~/.config/fish/completions/dlq.fish             # dlq is a dlqd verb now
dlqd arm                                              # re-register the nightly job
dlqd status                                           # says what it found
```

The `sed` matters: items written before the split carry an absolute
`sys.path.insert` to the old checkout, and an item that cannot import its
downloader fails every night without spending a byte.
