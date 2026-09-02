# The overnight download queue

The data plan grants ~763 MiB of free data a day, and whatever is unused is
wiped at 00:00 UTC. In the window before that reset — an hour by default —
a scheduled job spends the leftover on downloads you queued — always leaving
a floor for the morning (100 MB by default) and never touching the paid
reserve. `dlq settings` changes the window, the floor, what the job says, or
turns it off outright — see [Settings](#settings).

The queue lives in `~/dlq/` — this checkout — and its YouTube front end in a
sibling checkout, `~/ytq/`. Two commands are installable (see
[Installing](#installing-the-commands)) and then work from any directory:
`ytq` queues videos, `dlq` is the queue itself — typed on its own it opens
[the screen](#changing-what-is-queued), which is where the queue is worked on,
and `dlq <url>` queues a plain file URL. Without installing, every line
in these docs also works as `python3 ~/ytq/ytq.py …` — and `dlq` as
`python3 ~/dlq/expire_sched.py …`.

The checkouts find each other as siblings: each looks beside itself first,
then under `~` (`~/ytq`, `~/dlq`, `~/zwana-quota` — the runner reads the
portal through the quota widget in that third one). `YTQ_HOME`, `EXPIRE_HOME`
and `ZWANA_HOME` override, in that order of likelihood to be needed.

If you need something before the window rather than during it, see
[Downloading something now](#downloading-something-now) — that spends data
nothing is holding back, and says so, and says whether anything is counting it,
before it does. If the crew portal cannot be reached
at all, which is what being on mobile data looks like from here, see [When the
portal cannot be reached](#when-the-portal-cannot-be-reached).

## Queue something

| You have | Run |
|---|---|
| a video page (YouTube etc.) | `ytq <url>` — see [ytq.md](ytq.md) |
| no link, just a title | `ytq` and search for it |
| a direct file URL | `dlq <url>` |
| something more exotic | write an item by hand — `queue/README.md` is the contract |

`dlq` sizes the file from its response headers (headers only — costs no
data), writes the queue item, and prints where the file will land:

```
dlq <url>                    # probe the size, queue it
dlq <url> --name x.iso       # choose the saved file name
dlq <url> --sha256 <hex>     # verify the file before delivery
dlq <url> --probe            # just print size + resume support
dlq <url> --expect-bytes N   # server won't state a size: set the
                                  # most you are willing to let it cost
dlq <url> --again            # queue it even though it is already
                                  # queued or already downloaded
```

A URL that is already in the queue, already downloaded or already given up on
is refused rather than queued a second time, naming what it already is; the
same check catches a video `ytq` has seen before, on the id rather than the URL
— see [ytq.md](ytq.md#it-will-not-queue-the-same-video-twice).

Nothing downloads when you queue — the item just waits for the nightly window.

## Installing the commands

Optional — the scripts run fine as `python3 ~/dlq/expire_sched.py`. Installing
just puts them on PATH as `dlq` and `ytq` — one install per checkout, since
they are separate repos. Neither pyproject declares a dependency (stdlib only;
yt-dlp is a separate binary on PATH), so nothing but the build backend is ever
fetched.

**Editable** — the installed commands run the files in the checkouts as they
are, so edits take effect immediately and pulling a repo needs no reinstall:

```
uv tool install --editable ~/dlq
uv tool install --editable ~/ytq
```

**Non-editable** — copies the modules into the tool's own environment, so the
commands are unaffected by edits to the checkouts, at the cost of needing
`--force` to pick any change up:

```
uv tool install ~/dlq
uv tool install --force ~/dlq   # after editing
```

An installed copy still works out of `~/dlq/`, not next to itself
— the queue has to be the one the nightly runner reads, and `dlq` has to arm
the runner that lives in the checkout. Both commands anchor there deliberately
(`ytq._root()` finds it, and `dlq` takes the same answer rather than its own
location) because the alternative fails without saying so: a non-editable copy
would queue into its own installed directory, where the runner never looks, and
`dlq arm` would register the copy of the runner sitting beside it — which has
no queue next to it and cannot reach `quota_widget` at all. The symptom would be
a job firing faithfully every night onto an empty queue.

Set `EXPIRE_HOME` if the queue checkout is somewhere other than `~/dlq`:

```
EXPIRE_HOME=~/src/dlq ytq <url>
```

Either way `uv` builds an isolated environment under `~/.local/share/uv/tools/`
and links the commands into `~/.local/bin` — nothing is written inside the
checkouts and no other project's environment is touched. If they are not found
afterwards, `~/.local/bin` is not on PATH; `uv tool update-shell` fixes that.

The install fetches at most the **≈1 MB** hatchling build backend from PyPI,
once — after that it is served from `uv`'s cache. (Add `--offline` if you want
the install to refuse rather than fetch on a metered connection.)

Both packages need hatchling rather than `uv_build` (which is compiled into
the uv binary and fetches nothing): `uv_build` resolves one module name to one
directory, and these are flat sibling modules on purpose — the queue items
written into `queue/` `import expire_dl` and `import ytdl_item` by bare name.
So this fetch is the standing cost of that layout, and it is paid once: the
build backend is shared from uv's cache across both installs.

To remove: `uv tool uninstall dlq` (and `uv tool uninstall ytq`). Nothing else
in the queue depends on the install — the nightly runner is registered by
absolute path and is unaffected by it, and queue items already written keep
working.

### Fish completions

```
ln -s ~/dlq/completions/dlq.fish ~/.config/fish/completions/
ln -s ~/ytq/completions/ytq.fish ~/.config/fish/completions/
```

Symlinks rather than copies, so pulling the repo updates them. New shells pick
them up; `exec fish` in the current one. They complete the subcommands and
options, and — for `dlq path` — the download names themselves, each shown
with its state:

```
$ dlq path <tab>
40-ubuntu-24-04.py  (queued, 1 GiB here)  60-some-talk.py  (queued)
```

That list comes from `dlq names`, which walks the queue directories and parses
nothing, because it runs on every press of the tab key.

## One-time setup

```
dlq arm
```

Registers the job with Android's scheduler (needs the **Termux:API app**, not
just the package). It survives reboots and Termux being killed; `dlq cancel`
unregisters it. Both are also `a` and `c` on [the queue
screen](#the-queue-itself), under the line that says which it currently is. `arm` registers the runner **in the checkout** whether or not
`dlq` itself is installed, so re-arm after moving `~/dlq`.

## Watching one download

Opening an item that is downloading says `downloading · 45%  20 MiB/44 MiB`
at the top and draws a bar at the bottom. The two are one reading — the
download's own progress file, asked once per redraw — so they cannot
disagree. An item that is merely queued says `queued` and draws no bar; if
some *other* item is downloading, the foot names that one instead.

## When something fails

`dlq dump` prints one paste of everything a bug report needs: the
environment, how each sibling checkout resolved, what the gate thinks, the
state rows, the head of each failing item (its `sys.path` lines are usually
the evidence) and the tails of the newest logs. `dlq dump NAME` narrows it
to one download. Every section is guarded, so it finishes on broken trees —
which is when it is needed.

Items queued **before the split** import `ytdl_item` off the queue root;
the module moved to the ytq checkout, and a shim at the queue root answers
that import with the real module, so pre-split items keep downloading
without being edited.

## Checking on it

```
dlq status      # what happens next, and what it turns on
dlq list        # every download, and how much of it is here
dlq ui          # the same queue, on a screen that can change it — or just: dlq
dlq path NAME   # where a finished download landed
dlq logs        # last 40 lines of the runner log
dlq run-now     # run the whole queue now, without waiting for the window
dlq run-now --blind   # ...with no portal reading, on mobile data
```

All of these except `path` are on the screen too, under `s` — see [The queue
itself](#the-queue-itself). The command line is what is left for the things a
screen cannot do: `path` prints a path and nothing else, so `cd (dlq path
ubuntu)` works; and a `dlq` with no terminal to draw on — in a pipe, or a
script — prints the status screen rather than opening anything.

`dlq status` answers "is it going to download tonight, and what". It leads
with that answer and then shows the working:

```
DOWNLOAD QUEUE                    22:41Z
  waiting for tonight
  opens in 19m (23:00Z)

data
   1.12 GiB left today
    763 MiB of it expires at 00:00Z
     100 MB is always kept back
    480 MiB the queue may spend tonight
  800 KiB/s measured download speed

queue (2)
  40-ubuntu-24-04-desktop-amd64
  18%  1.02 GiB/5.61 GiB  try 2/3

  60-some-talk
    -  0 B/≤505 MiB

  3 done, 1 failed - dlq list

job       armed, fires every 15m
last run
  22:30Z (11m ago) not yet: window opens
  23:00Z
root      ~/dlq
```

The first line under the heading is the whole answer, and there are only ten
of them: **automatic downloads are off** (the `auto` switch — see
[Settings](#settings), asked before anything else), **waiting for tonight**,
**window open, downloading**, **downloading now** (something is being
downloaded at this second, by the nightly job or by the screen's `n`), **done
for tonight**, **nothing queued**, **no data to spend tonight**, **PAID: no
portal, downloading** (a `--blind` run, on mobile data — see [When the portal
cannot be reached](#when-the-portal-cannot-be-reached)), and the two blocked
ones — the portal not answering, or its reading being too old to spend
against. They come from the same function the runner itself gates on, so the
screen cannot say one thing while the runner does another.

The figures under `data` are the arithmetic the runner does, in the order it
does it: what is left, how much of that dies at the reset, the reserve that
is never spent (100 MB unless `dlq settings` says otherwise), and therefore
what tonight may use. The speed under them is what the last download actually
managed, which is what the runner sizes each slice against. `try 2/3` on an
item means one night already failed and it has two left before it is set
aside for you.

`dlq status` is read-only and takes no lock, so it answers while a firing is
in progress. It exits non-zero when it prints a `BROKEN` line.

`dlq list` is the "where is everything" answer — queued, failed and done in
that order, with what is on the disk against what it is going to be:

```
queued (2)
  40-ubuntu-24-04           16%  1 GiB of 5.61 GiB   Ubuntu 24.04 desktop ISO
  60-some-talk                -  0 B of ≤506 MiB     Some talk [1080p mp4]
done (1)
  50-other-talk         complete  201 MiB            other-talk.mp4
```

The figure is measured from the files on disk, not read out of what the item
last claimed, so it survives a kill. `≤` means no server has stated a size yet
and the number shown is the item's declared cap, which is deliberately larger
than the file. Amber is started, grey is waiting, green is here, red is failed.

A finished download whose file you have since deleted says **file gone**, and
one whose folder cannot be reached at all — card out, storage permission not
granted — says **folder away**. Those are different facts and only the first is
about the file. Neither is a download waiting to happen: an item leaves the
queue when it finishes and the runner only ever looks in `queue/`, so nothing
can start it again. To download it a second time, queue it again.

It lays itself out to the terminal it is in. On a phone in portrait the
description goes and, if the names are long, each download takes two lines
rather than having its name clipped — the name is what you type back at
`dlq path`, so it is the last thing to lose room. Both lines of a download sit
at the same indent and a blank line separates one download from the next,
since a phone loses two columns to every level of indent and the eye reads the
gap anyway:

```
queued (2)
  40-ubuntu-24-04-desktop-amd64
  18%  1.02 GiB/5.61 GiB

  60-some-talk
    -  0 B/≤505 MiB
```

`NAME` is name-ish: any unambiguous part of the name (`ubuntu`, `some-talk`)
or the priority number (`40`). Anything matching two downloads is refused with
both listed.

`dlq path` prints only the path, so `cd (dlq path ubuntu)` works, and exits
non-zero if the download has not finished — it still prints where it is going
to land. For one that finished and whose file has since gone, it prints where
it *was* delivered and exits non-zero, because that is the only record of where
it went and it is still the answer to the question.

`dlq logs` does the same thing to the runner's log: on a phone the date is
lifted off the lines and printed once above them — but only when every line on
the screen really is that one day, since the queue runs across midnight — and a
long line is wrapped with its continuations indented rather than left to break
wherever the screen ends.

Finished files land in the configured download directory — see [Where downloads
go](#where-downloads-go); on the phone that is Android's Downloads. The item
script itself moves to `done/` when finished, or to `failed/` after three failed
nights (you get a notification either way something needs you).

## Changing what is queued

```
dlq ui     # or just: dlq
```

`dlq` with nothing after it opens this screen. In a pipe or a script — no
terminal to draw on — it prints `dlq status` instead, which is what a bare
`dlq` has always done.

Everything that changes the queue is on this one screen: reorder, rename,
remove, put a failed download back, give it its nights back, download one now,
open a finished one, read its log. There are no commands for those any more —
each of them is easier to do to a download you are looking at than to a name
you have to type correctly first. `s` goes one level up, to the queue as a
whole: the status, the nightly job, the destinations, the settings, and
running the lot now.

The listing picks and the item screen acts. `↑↓` moves, `⏎` opens whatever the
cursor is on, and every key that changes something is on that second screen,
under a bar naming the one download it will act on — so the download being
removed is the download on the screen. `q` goes back, and again to leave; what
was changed is printed on the terminal on the way out.

| On an item | |
|---|---|
| `n` | download it now — asks once, and says what it will cost |
| `m` | move it in the queue |
| `u` | put a failed download back, with its three nights again |
| `t` | clear the failed nights counted against it |
| `r` | rename it |
| `o` | open the finished file |
| `l` | that item's own log, from its last night |
| `d` | remove it from the list |

**Order is a place in a list, not a number.** `m` picks a download up, `↑↓`
move it, `⏎` drops it — nothing is renamed until then, so a move thought better
of costs nothing. The screen shows slugs and positions ("3rd of 7"); the `NN-`
prefix that actually carries the order is bookkeeping, and nothing asks you to
type it any more. Underneath, dropping one usually renames a single file, and
when two neighbours have no room between them the whole queue is quietly dealt
fresh numbers instead.

**Removing keeps what was paid for.** `d` takes the download off the list and
leaves every byte where it is — the partial in `work/`, anything finished in
`out/`. The confirm screen offers a second key that also deletes the partial,
with its size on it, and it is never the default. A file already delivered to
Downloads is not touched either way: it is yours, not the queue's. Both keys are
always live — a download that has fetched nothing has nothing to keep, so there
they do the same thing and the screen says so rather than dropping one of them.

**A rename takes the download with it**, and so does a move. An item is four
things: the file in `queue/`, the part-downloaded file in `work/<item>/`,
anything waiting in `out/<item>/`, and its record in `state.json` — the
attempts, and where its files were delivered. All four move together, so
putting a half-downloaded 5 GB ISO at the front of the queue costs nothing and
it carries on from where it stopped. The same rename by hand with `mv` on the
queue file alone starts that download again from zero, on the next night,
without a word anywhere.

**Nothing can be changed while the queue is busy.** A firing or a download in
progress is writing into `work/<item>/` and will archive the item the moment it
finishes, so every action refuses and says so rather than moving the ground
under a download.

Every change is written into `logs/runner.log`, beside the runner's own
reasoning — that file is where to look when a download is not where you left
it. Per-item logs are the exception: `logs/<date>-<item>.log` is the record of
one particular night and keeps the name the item had then.

### Downloading one now

`n` on the item screen. It asks once, and what it says depends on where the
phone is — the portal at `ic.zwana.io` is on the vessel's own network, so
whether it answers is the difference between data that is counted and data that
is not:

```
 download now
   download some-talk now?
   spends 505 MiB of the vessel's allowance
   the free half is the nightly window; this is outside it
   it runs in the background — x stops it
   y do it   any other key: no
```

```
 download now — NOT COUNTED
   download some-talk now?
   zwana does not answer, so nothing is counting this
   505 MiB on whatever the phone is actually using
   on mobile data that is metered and charged to you
   on vessel wifi it means the portal is down — worth checking first
   it runs in the background — x stops it
   y do it   any other key: no
```

One yes or no either way, and the number is on both. The check is a two-second
connection to the portal, costs nothing, and errs towards the loud version: if
it does not answer in two seconds it has not answered.

The download itself runs detached, so the screen stays where it is and the
progress shows along the bottom; `x` stops it. Everything else is as it always
was — it downloads into the same place the nightly job would, so stopping it is
safe, what is downloaded stays, and the nightly window carries on from there. A
download started this way never counts as one of an item's three nightly
attempts, and it takes the runner's lock, so it cannot collide with a firing.
`ytq --now URL` is the same thing with the queueing folded in.

### Downloads that are no longer there

A finished download whose file you have since deleted is dropped from the list
by itself, the next time the screen reads the queue, and says so. The record it
keeps — where the file went — answers a question nobody can ask once the file
is gone, and the row would otherwise sit there looking like work outstanding.

That only happens on the evidence of having looked: the folder has to be
readable. A card that is out or a storage permission that was never granted
reads as **folder away**, changes nothing, and waits. It is the difference
between a file that was deleted and a file that cannot be seen, and only the
first one is a fact.

### The queue itself

`s` on the listing opens the queue's own screen: the whole of `dlq status`,
scrolling, with the keys that act on the queue as a whole underneath it.

| On the queue | |
|---|---|
| `n` | run the whole queue now — says what it will spend, and asks once |
| `a` | arm the nightly job |
| `c` | unregister it — asks, because what follows is silence |
| `w` | where finished downloads go, and change either of them |
| `s` | the window, the reserve and automatic downloads — see [Settings](#settings) |
| `l` | the runner's own log |

A bare `dlq` on an empty queue comes here, because "nothing is queued" is only
half the answer — whether the job is armed and what tonight would have spent is
the other half.

The figures are a reading, taken when the screen was opened, and the top line
says when. They are not re-read on a timer: the portal is a network call, and
asking it once a second is not watching, it is hammering. What *is* live is the
download at the bottom, which is read off a local file, and the figures are
read again the moment a run of yours ends.

**`n` here is `dlq run-now`, and it means now.** The nightly window is a
schedule, not a permission: pressing it ignores the window exactly as the item
screen's `n` does, and so does `auto` being off — everything else stays, the
reserve floor, the per-item caps, and the portal reading the whole budget is
derived from. With the portal unreachable it is [a blind
run](#when-the-portal-cannot-be-reached) instead, which is said on the same
screen and asked in the same single question. The figure it names is the
runner's own; there is no second sum worked out here. It runs detached, so
the screen stays usable, and `x` stops it — what is downloaded is kept and
the nightly window carries on from there.

**`s` here goes one level further, to the settings** — see
[Settings](#settings) — and its wording says whether `auto` is off, since that
is the one setting that changes what the rest of the screen means.

## Where downloads go

```
dlq dest                        # show all three, and where they came from
dlq dest video ~/storage/movies # where ytq puts finished videos
dlq dest audio ~/storage/music  # where ytq puts audio-only picks
dlq dest file  ~/storage/downloads
dlq dest video default          # put the built-in default back
```

Or `w` on [the queue screen](#the-queue-itself), which shows all three, says
which of them is a default and whether it can be written to, and takes a new
one in a field — `default` typed into that field puts the built-in one back.
`v`, `a` and `f` pick which.

Three destinations, because a film, a song and an installer do not belong in
the same folder on a phone. **Which one an item uses is decided by the row you
picked**, not by the file extension: an audio-only row on `ytq`'s format list
goes to `audio`, and everything else `ytq` queues goes to `video`. On a phone
that matters more than it sounds — the music player and the video player look
in different folders, and a song delivered among the films is one nothing
offers to play.

**On Termux all three default to Android's own Downloads**
(`/storage/emulated/0/Download`) — the folder the Downloads app lists and the
media scanner indexes, and one that survives Termux being uninstalled. That
needs `termux-setup-storage` to have been run once; until it has, `dlq dest`
says so rather than letting you find out after the data is spent. Anywhere
else — the container on zero — the default stays the queue's own `out/`.

A setting is read **when the file is delivered**, not when it was queued, so
changing one moves everything already waiting in the queue as well — including
the audio ones, which is the point of them being a kind rather than a folder
chosen once. For a one-off, `ytq --dest DIR` and `dlq --dest DIR` write an
absolute path into that item instead, and that wins over the kind.

Finished files land directly in the folder, named as they were downloaded. A
name already taken gets Android's own suffix — `report (2).pdf` — and nothing
is ever overwritten. `dlq path NAME` tells you where one went; the queue keeps
a record, because once a file is in a folder shared with every other app on the
phone nothing can work out which one was yours by looking.

If the destination cannot be reached when the moment comes — permission never
granted, card unmounted, disk full — **the file stays in `out/<item>/`**,
complete and already paid for, with a warning in the log and a notification.
It is never half-written into a place it cannot go.

## Settings

```
dlq settings                        # show them all, and what they are set to
dlq settings window 45              # start downloads 45 min before the reset
dlq settings window 2h              # ...or say it in hours
dlq settings reserve 150            # keep 150 MB back instead of 100
dlq settings reserve-when-paid no   # waive the reserve when paid data is there
dlq settings paid-min 150           # ...but only with 150 MB of it left
dlq settings auto off               # the nightly job fires and does nothing
dlq settings notify-blocked off     # a blocked firing goes to the log only
dlq settings window default         # put the built-in default back
```

Or `s` on [the queue screen](#the-queue-itself) (itself reached with `s` from
the item list), which shows the same six and takes the same changes: the
switches toggle where they sit, and the numbers open a field, prefilled
with what they are now — type `default` to put the built-in one back, same as
on the command line. Each setting's key is beside its name (`w r p m a n`); on
a short phone the screen keeps every name, value and complaint and drops the
grey line saying what each one means.

| setting              | default | what it does                                     |
|----------------------|---------|---------------------------------------------------|
| `window`             | 60 min  | how long before the 00:00Z reset downloads may start — a multiple of 15 minutes, 15 to 1440 |
| `reserve`            | 100 MB  | data kept back and never spent                     |
| `reserve-when-paid`  | yes     | keep the reserve even when paid data is on the account; `no` waives it |
| `paid-min`           | 0 MB    | how much paid data `reserve-when-paid no` wants before it waives; 0 is any paid data at all |
| `auto`               | on      | let the nightly job actually download              |
| `notify-blocked`     | on      | a firing stopped by a fault says so on the phone   |

A value typed as `45`, `45m` or `2h` sets the window; `150` or `150MB` sets
the reserve and `paid-min`; `on`, `off`, `yes`, `no`, `true`, `false`, `1` and
`0` all work for the switches, case-insensitively. A value hand-edited into
`config.json` that does not fit the rule — a window that is not a multiple of
15, a negative reserve — is never applied: the setting reads as its default
instead, and `dlq settings` and `dlq dump` both say which stored value they
declined and why, rather than letting a stray character in a file stop a
night's downloads.

A `config.json` that will not parse **at all** — a trailing comma, a missing
brace — is the same for the runner: every setting reads as its default and the
night goes ahead. It is not the same for changing one. `dlq settings NAME
VALUE` and `dlq dest KIND PATH` refuse and write nothing, saying which file
will not parse, and the two screens say the same line in red, because saving
on top of a file nothing can read means saving the one new key and losing the
destinations and the other settings with it.

**`reserve-when-paid no` waives the reserve; it does not lower it or remove
the guarantee behind it.** With it set to `no`, the runner stops holding the
configured reserve back on any reading where the portal says paid data is
left — the free allowance can be spent down to the runner's own margin, since
nothing is being held back for the morning. That margin is not a second
reserve to be waived in turn: it is the headroom the projection between portal
polls needs, and the haircut the free figure is discounted by because the
portal states it as an upper bound. Neither is money kept for you, and both
stand with the reserve waived. The waiver answers to the reading in hand, not
to the night as a whole: paid data bought at 23:50 waives the reserve from the
next poll on, and losing it partway through a run brings the reserve straight
back for whatever is left to download. Left at the default, `yes`, the reserve
is kept regardless of what is paid for, which is the point of having one for
most people.

**`paid-min` is how much paid data that waiver wants to see**, and it does
nothing at all while `reserve-when-paid` is `yes` — it qualifies that setting
rather than standing on its own. Left at `0`, the default, the waiver means
what it has always meant: any paid data at all, down to the last byte the
portal reports. Set it to `150` and the reserve is only waived on a reading
with at least 150 MB of paid data behind it — for the account whose last few
MB of paid data are worth less than the reserve they would stand down. It is
measured against `paid.left_bytes`, which can only understate what is actually
paid for, so asking for "at least this much" errs towards keeping the reserve
rather than towards spending it. The threshold is checked on each reading,
like the waiver itself: paid data falling below it partway through a night
brings the reserve back for whatever is left to download.

**`auto off` stops the nightly job from downloading; it does not stop the job
from running.** The job stays armed and keeps firing every ~15 minutes; each
firing does nothing, and `dlq status` leads with **automatic downloads are
off** instead of one of the usual verdicts. `dlq run-now` and the screen's `n`
still download with the switch off, the same way `--force` still overrides
the clock: a person asking for a download now is not the schedule, and the
switch is only ever a schedule. Nothing about money moves with it — the
reserve, the per-item caps and the portal reading go on deciding exactly what
they decided with the switch on.

**`notify-blocked off` silences one notification and no others.** With it on,
a firing stopped by a fault — the portal not answering, or its reading being
too old to spend against — posts **Download queue blocked** to the phone. That
is the one that repeats: a firing lands every ~15 minutes, so a phone away
from the vessel's wifi says it all evening, which is the fastest way to teach
anyone to ignore the notification that matters. Turned off, the runner still
logs why it stopped and `dlq status` still leads with it; only the phone goes
quiet. An item that has run out of nights, a malformed item and a download
folder that cannot be reached go on notifying either way — each happens once,
and nothing else is ever going to mention them.

## Downloading something now

The whole point of the queue is to spend data that is about to be wiped, so
everything above waits for the window before the reset (an hour by default —
`dlq settings window` changes it). When you need a file before then, that is
`n` on the item screen — see [Downloading one
now](#downloading-one-now). `ytq --now URL` is the same thing for something
not queued yet: pick the format as usual, press `n`, and instead of waiting
it writes the item and starts it detached, so the screen stays where it was
and the download reports itself along the bottom. `x` stops it in either.

Whatever starts it, the download is outside the expiring window by
definition, so none of the runner's three guarantees are in play: no reserve
floor, no midnight stop, no cap against the free allowance. What is on the
screen before it starts is the number it may spend, and whether anything is
counting it.

Everything else is kept. It downloads into the same place the nightly job
would, so stopping it is safe: what is downloaded stays, the item stays queued,
and the nightly window carries on from there. It takes the runner's lock, so it
cannot collide with a firing that is already going; and if it finishes, the
item is archived to `done/` exactly as a nightly one would be. A manual run
never counts as one of an item's three nightly attempts.

## When the portal cannot be reached

Every figure the runner decides on comes from the crew portal at
`ic.zwana.io` — how much is left, how much of it expires tonight, how much is
already paid for. The portal is on the vessel's own network, so **the phone on
mobile data cannot see it at all**, and neither can anyone while it is down.
With no reading there is nothing to spend against, so the nightly job stops and
says so:

```
data
  no reading, so nothing can be spent
  no credentials: set zwana_username and zwana_password in ~/zwana-quota/.env
  dlq run-now --blind spends mobile data instead
```

That last line is the way through, and it is the same bargain the screen's
`n` strikes, taken for the queue rather than one item. It is also `n` on [the
queue screen](#the-queue-itself), which asks the same single question with the
same figure in it; on the command line:

```
dlq run-now --blind        # run the queue with no portal reading
dlq run-now --blind --yes  # without being asked to confirm
```

```
the queue    : 2 items, no portal reading
will spend   : up to 5.59 GiB of mobile data
run the queue now? [y/N] y
ctrl-c stops it; what is downloaded is kept and resumes
```

**That figure is what the items declared, and it is ordinary mobile data.**
There is no floor, no free-allowance cap and no measurement of what is left,
because all three are portal figures; what is left is each item's own
`EXPECT_BYTES`, which is enforced against it as usual, so the queue cannot take
more than the sum shown — only less, when an item finishes early.

Two things change with the clock, both for the same reason: there is no
expiring grant to land inside. It does not wait for the window, and **nothing
interrupts it for the time** — no nine-minute slice, no midnight stop, no
`timeout` around the download. A run cut short would only have to buy the same
bytes over again, so it works the queue until the queue is done, however long
that takes and across as many midnights as it needs.

**Ctrl-c is what stops it**, and it leaves exactly what a deadline used to: the
download stopped cleanly, the file resumable, the item still queued. Everything
else is untouched too — the per-item caps, the lock, `done/`, and the three
nightly attempts a manual run still never spends.

The flag only does anything when the portal really is unreachable. If it
answers, the run is an ordinary one with the floor and the free-allowance cap
back in force — `--blind` is what to do without a reading, never what to do
instead of one.

## What to expect

- Nothing runs before the window opens — an hour before the reset unless
  `dlq settings window` says otherwise. Firings come every ~15 minutes and
  each works for at most ~9 minutes — Android's limit, not a bug.
- Big files span nights. Both `dlq` and `ytq` items resume from where they
  stopped; a multi-GB file finishing over several nights is normal.
- Items run in name order: the `NN-` prefix is the priority, lower first. The
  head of the queue takes what it can and the remainder flows down.
- Guarantees, whatever an item does: the configured reserve is left
  afterwards (100 MB unless changed, waived only if `reserve-when-paid` is
  `no` and paid data is there — see [Settings](#settings)), nothing runs past
  00:00 UTC, and the paid reserve is never spent. All three are measured
  against the portal, so all three are off for the two things that
  deliberately spend paid data and say so first — `dlq ui`'s `n` and
  `dlq run-now --blind`.
- `dlq settings auto off` leaves all of the above scheduled but idle: the job
  still fires, it just downloads nothing until a person asks for it by name.

## When something looks wrong

- `dlq status` — start here. Its first line is what the next firing would do
  and why, and the rest is what that turned on: the budget, the window, every
  item's progress and attempts, why any item was rejected, and whether the job
  is still registered. (`python3 expire_runner.py --status` draws the same
  screen, minus the job registration.) The `last run` line at the bottom is the
  heartbeat, which is also readable on its own with `cat heartbeat`.
- `logs/runner.log` is the runner's reasoning; `logs/<date>-<item>.log` is one
  item's own output for that night.
- An item in `failed/` kept its logs and its attempt history in `state.json` —
  diagnose before re-queueing. `dlq ui` reads that item's log with `l` and puts
  it back with `u`, which also clears the three spent nights; by hand it is a
  move back into `queue/`, and the attempts have to be cleared too or the first
  firing to touch it gives up on it again.
