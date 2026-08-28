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

`make dev` (`uv sync`, once, networked) puts the locked pytest into `.venv`;
then `make test` (`uv run pytest`) or `make check` — the same
module self-tests either way;
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
uv tool install --editable ~/dlq
uv tool install --editable ~/ytq
ln -sf ~/dlq/completions/dlqd.fish ~/.config/fish/completions/
rm -f ~/.config/fish/completions/dlq.fish             # dlq is a dlqd verb now
dlqd arm                                              # re-register the nightly job
dlqd status                                           # says what it found
```

The `sed` matters: items written before the split carry an absolute
`sys.path.insert` to the old checkout, and an item that cannot import its
downloader fails every night without spending a byte.

## Scheduling on Android — findings the nightly job stands on

Measured on this phone in 2026-08, for the retired installer-download job,
and still what `dlqd arm` relies on:

- **JobScheduler, not runit/cron/nohup.** A userspace poll loop lives only
  while Termux does; JobScheduler is the system's own scheduler — it survives
  Termux being killed and restores after a reboot. `termux-job-scheduler`
  needs the Termux:API **app**, not just the CLI package (without the app it
  hangs).
- **15 minutes is Android's floor** for a periodic job (clamped since N).
  The real uncertainty is **Doze**: screen off, unplugged and stationary,
  firings defer into maintenance windows; charging suppresses Doze entirely.
  The pre-midnight window is a *bias*, not a guarantee — an accepted trade,
  since a missed night just means the job tries again the next evening. A
  night that matters: put the phone on charge over midnight.
- **A job body must never take Termux's wake lock.** JobScheduler holds one
  across the job's execution, and Termux's own lock is a single global flag
  with no reference counting — `termux-wake-unlock` releases it regardless of
  who acquired it, so a job that unlocked on its way out would silently drop
  a lock taken for something else.
