# Queue contract

Scripts here are run by `expire_runner.py` in the window before 00:00 UTC (an
hour by default; `dlq settings window` changes it), to spend free data that
would otherwise expire. Drop an item in and it runs on the next night there
is expiring allowance for it.

## Required header

```python
#!/data/data/com.termux/files/usr/bin/python3
# EXPIRE: v1
# EXPECT_BYTES: 4200000000
# PARTIAL: yes
# SLICE_MIN_BYTES: 33554432
# DESC: Ubuntu 24.04 desktop ISO
```

| Field | Required | Meaning |
|---|---|---|
| `EXPIRE: v1` | yes | marks the file as an item at all |
| `EXPECT_BYTES` | yes | honest total in bytes, rounded up, including overhead |
| `PARTIAL` | no | `yes` = can take a slice smaller than the total and resume |
| `SLICE_MIN_BYTES` | no | smallest slice worth starting (default 32 MiB) |
| `DEST` | no | where the finished file is put — see below |
| `SOURCE` | no | what this is a download *of*, for spotting duplicates |
| `DESC` | no | shown by `dlq status` |

`DEST` is `video`, `file`, or an absolute path. The two words are the
destinations `dlq dest` configures — `video` is where `ytq` puts things,
`file` where `dlq` does — and they are **resolved when the file is delivered**,
not when the item was queued, so changing one moves what is already waiting in
the queue too. An absolute path overrides both.

`SOURCE` is an opaque identity, not a URL to fetch: `ytq` writes
`youtube:<video id>` and `dlq` writes `url:<the url>`. Nothing downloads from
it and the runner ignores it entirely — it exists so that queueing the same
video twice can be noticed *before* it is paid for twice, whichever URL it is
pasted as. An item without one can still be recognised by its name, which is
the same title, which is usually but not always the same thing; both front ends
say which of the two they matched on. Hand-written items need not carry it.

**No `DEST` means the file stays in `out/<item>/`**, which is what every item
did before this existed. The item always downloads into `out/<item>/` either
way; the runner moves it afterwards, so an unreachable destination leaves the
file safe and paid-for where it landed rather than half-written somewhere else.

The header is parsed **statically** — the script is never executed to ask it
anything, because an `--estimate` mode would mean running unvetted code outside
the guarded window, exactly where a bug could spend data before any guard
exists. A file without a valid header is skipped, logged and notified.

Anything named like an item is parsed, whether or not it is one — `ITEM_RE` is
just `NN-`, so a photo saved here as `20-holiday.png` counts as a claim to be an
item and is reported as a rejected one. That is deliberate: silently ignoring it
is how a genuinely broken item goes unnoticed. Move stray files out of `queue/`
rather than leaving them to be reported every night.

`EXPECT_BYTES` is also the **cap enforced against the item**, so declare the
most you are willing to let it cost. "Unknown" is not a valid answer.

> **Shebang:** must be `#!/data/data/com.termux/files/usr/bin/python3`.
> `/usr/bin/env` does not exist on Termux and the runner rejects an item whose
> interpreter is not on disk — deliberately, because otherwise exec fails with a
> bare "No such file or directory" naming the *script*, which reads as though it
> vanished.

## Naming and order

`NN-slug.py` — items run in strict lexicographic order, so `NN` is your
priority. Ordering is completion-first and never interleaved: the earliest item
takes what it can and the remainder flows down the queue. A very large item at
the head will legitimately occupy whole nights; that is a naming decision.

Stage new items in `.staging/` and `mv` them into `queue/`, so the runner never
sees a half-written file.

## Environment the item receives

| Variable | Meaning |
|---|---|
| `EXPIRE_SLICE_BYTES` | payload bytes **this slice** may take, already derated for wire overhead |
| `EXPIRE_BUDGET_BYTES` | same value; kept for items written against the older contract |
| `EXPIRE_TOTAL_BYTES` | the item's own `EXPECT_BYTES`, echoed back |
| `EXPIRE_STOP_EPOCH` | UTC epoch second the item will be killed at |
| `EXPIRE_RUN_ID` | nonce to stamp into the status file |
| `EXPIRE_WORK` | scratch, **persists between nights** — download here |
| `EXPIRE_OUT` | finished artifacts, and only when complete |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | finished; archived to `done/`, never runs again |
| `75` | not finished — took a slice, or could do nothing. Stays queued, no strike |
| anything else | failed attempt; after 3, moves to `failed/` |

`75` covers both "made progress" and "did nothing" because that distinction is a
*quantity*, not a state — it belongs in the status file. An item that returns
`75` while moving under 1 MiB despite being given room and time accrues a stall
strike, and three of those count as one failed attempt, so nothing can sit in
the queue forever.

## Use the shared downloader

`expire_dl` handles slicing, resume, validators and reporting. For a direct
file URL, `python3 ../expire_sched.py <url>` writes this whole item for you, sized from
the server's own headers. By hand, a complete item:

```python
#!/data/data/com.termux/files/usr/bin/python3
# EXPIRE: v1
# EXPECT_BYTES: 4200000000
# PARTIAL: yes
# DESC: Ubuntu 24.04 desktop ISO
import sys
sys.path.insert(0, "/data/data/com.termux/files/home/dlq")
import expire_dl

sys.exit(expire_dl.run(
    "https://releases.ubuntu.com/24.04/ubuntu-24.04-desktop-amd64.iso",
    "ubuntu-24.04-desktop-amd64.iso",
    expect_sha256="....",           # optional; verified before delivery
))
```

It requests `Range: bytes=off-(off+slice-1)`, so the **server** ends the
transfer at the slice boundary rather than the item racing to abort locally. The
resume offset is always `stat(.part).st_size`, and writes are append-only, so
whatever is on disk is a valid prefix of the remote file however the process
died. A server that ignores `Range` is detected and the partial discarded rather
than appended to; if the file also cannot fit one slice, the item fails fast
instead of re-downloading a doomed slice every night.

## Videos: use `ytq.py`

`python3 ../ytq.py <url>` writes the item for you. It runs a metadata-only
extraction, lists every format with the size yt-dlp states for it, and puts that
figure in `EXPECT_BYTES` with a stated margin — so the cap is a measurement
rather than a guess, which is the whole reason "unknown" is not allowed there.
It stages and renames, as above. `--list` prints the same table without writing
anything; `--from-json FILE` re-uses a saved `yt-dlp -J` dump so changing your
mind costs no data.

The item it writes calls `ytdl_item.run(...)`, which invokes **yt-dlp on every
firing** rather than resolving a media URL once and handing it to `expire_dl`.
That is deliberate: media URLs are signed and expire in about six hours, so a
video spanning two nights would resume against a dead link. yt-dlp re-extracts
each firing and resumes its own `.part`.

The trade against `expire_dl`, which is worth knowing before choosing:

| | `expire_dl` | `ytdl_item` |
|---|---|---|
| slice edge | the server stops at the byte boundary — exact | watches the file grow and stops the child; lands a second or two short |
| resume across nights | dies when a signed URL expires | re-extracts every firing |
| per-firing overhead | none | one extraction, ~0.1-0.5 MB |

So a plain file URL belongs in `expire_dl` — queue it with `dlq.py`. Use
`ytq.py` when the URL is a page rather than a file.

## What the runner guarantees whatever an item does

- **The configured reserve left afterwards** — 100 MB by default, changed with
  `dlq settings reserve` and waived (only) when `reserve-when-paid` is `no`
  and the portal reports paid data left. Checked against the portal's
  measured remainder every 60s during a transfer, and projected down between
  polls using interface counters, which over-count and so err safe.
- **Nothing runs past 00:00 UTC** — a `timeout` wrapper decided at spawn holds
  even if the runner dies, plus a reaper on the next firing.
- **The paid reserve is never spent** — the budget is capped by the free
  allowance, discounted because that figure can only over-state.

The item's self-imposed stop is deliberately earlier than every one of those, so
a well-behaved item is never killed and always leaves a resumable file.

All three are read off the portal, so all three are gone when it cannot be
reached — the phone on mobile data, or the portal down — and a run in that state
has to be asked for by name (`dlq run-now --blind`, which says what it will
spend and waits for a yes). **Nothing about the contract changes**, and no item
needs to know it is happening: it is still handed `EXPIRE_SLICE_BYTES`, still
killed if it crosses its cap on the wire, and still expected to leave a
resumable file. What holds a blind run in is `EXPECT_BYTES` — enforced against
the item exactly as it always is, and now the only ceiling there is.

The one field that reads differently is `EXPIRE_STOP_EPOCH`, and only because a
blind run has no deadline to put there: it arrives as **`0`**, which the
contract already defines as "no stop" and which `dlq now` has always passed.
An item that honours it needs no change; one that assumed a non-zero epoch was
already wrong for `dlq now`.
