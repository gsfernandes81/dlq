#!/data/data/com.termux/files/usr/bin/python3
"""Spend the day's expiring data allowance on a queue of wanted downloads.

The free grant of ~763 MiB lands at 00:00 UTC and whatever is left of it is
wiped at the next 00:00 UTC. This runner works through scripts dropped in
``~/or3/termux/expire/queue`` in the hour before that deadline, so allowance that
would evaporate gets spent on something asked for instead.

Guarantees, in the order they matter
------------------------------------
1. **At least 100 MB is left afterwards.** Enforced against
   ``today.remainder_bytes``, which the portal measures exactly, and re-checked
   live while a download runs — not merely predicted before it starts.
2. **Nothing runs past 00:00 UTC.** Enforced by a ``timeout`` wrapper decided at
   spawn, so it holds even if this runner is killed, plus a reaper on the next
   firing for anything that escapes.
3. **The paid reserve is not spent.** Budget is capped by ``free.left_bytes``,
   which is an upper bound, so it is discounted before use.

All three are read off the portal at ``ic.zwana.io``, so all three are gone the
moment it cannot be reached — which is what happens whenever the phone is on
mobile data rather than the vessel's wifi. That case has one answer and it is
``--blind``: see *Running with no portal* below. Everything outside that flag
still refuses to move a byte it cannot account for.

Nothing here trusts a queued script. Scripts declare a byte count; that number
is a *cap enforced against them*, not a promise believed.

Scheduling reality
------------------
Android JobScheduler cannot fire at a wall-clock time, so this is invoked every
~15 minutes and decides for itself whether to act. Two platform limits shape the
design and are worth stating because they are not obvious:

* A job gets roughly **10 minutes of execution** before the platform stops it.
  A download that needs an hour therefore cannot be one job run. Items must be
  resumable, and each firing takes a slice.
* A firing may be late, early, doubled, or missed entirely. Every path here is
  safe to repeat and safe to skip.

Running with no portal
----------------------
``--blind`` is the way through when ``ic.zwana.io`` cannot be reached at all —
the phone is on mobile data, or the portal is down. There is then no reading to
spend against, so the three guarantees above have nothing to stand on and are
**not** quietly reimplemented as something weaker: a blind run spends ordinary
paid mobile data, and what bounds it is what the queue itself declared. Each
item is still capped at its own ``EXPECT_BYTES``, which is a cap enforced
against it rather than a promise believed, so the queue's whole exposure is the
sum of those — the figure ``dlqd run-now --blind`` says out loud and asks about
before anything starts.

Two things follow from there being no expiring grant to land inside, and both
are in :func:`deadlines`: the nightly window does not apply, so a blind run
starts whatever the clock says; and **nothing stops a blind download for the
time**. There is no window to close, no midnight to be caught by and no firing
to hand back to, so a run is not cut short and made to buy the same bytes
again — it works the queue until the queue is done. What stops it is ctrl-c,
which stops the download the way a deadline used to and leaves the same
resumable file behind.

Usage::

    python3 expire_runner.py            # what a scheduled firing does
    python3 expire_runner.py --status   # explain the current decision, do nothing
    python3 expire_runner.py --force    # ignore the clock gate (testing)
    python3 expire_runner.py --blind    # no portal: spend mobile data instead

Checking it
-----------
``python3 expire_runner.py --self-test`` runs 81 offline checks. It takes neither
the lock nor the heartbeat, so it is safe to run while a firing is live.

Two parts are worth not weakening. The **timezone sweep**: the checks pin the
clock and run either side of the date line. The vessel changes zone and never changes
the clock, so a naive datetime anywhere in the window arithmetic displaces the
whole download window by hours — silently, and only on the days it matters.

And **``parse_item`` never raising**: it runs over every file in the queue at
the top of every firing, so an exception there is not one bad item, it is the
whole night — silently, for as long as the file sits in the queue.

The third is **:func:`gate`'s order**. It is the one decision two things read —
the firing that acts on it and the status screen that reports it — and getting
its order wrong leaves both plausible: the screen would say something true of
most nights, just not the reason this one is quiet.

``ytdl_item.py --self-test`` (29 checks) covers the byte metering, which is what
decides when a slice stops. ``ytq``, ``dlq`` and ``dlqd`` have their own
(137, 23 and 231); ``dlqd``'s cover the path anchoring and the two screens,
since everything else it does is talk to the platform scheduler.

``expire_dl.py --self-test`` (20 checks) drives the downloader itself against a
server on loopback — completing, slicing, resuming, hashing, and the server that
ignores ``Range``. It had none at all until a 15 KiB wheel was found declining
every firing for ever, which is the first check in it.

Both of the front ends check that **every line they draw fits the terminal**, at
widths down to 32 columns — the same property ``quota_widget`` checks of its
tile, and for the same reason. Termux in portrait is around 40 columns, and a
line wider than that is not an error: it is a wrapped fragment with the figure
that mattered pushed onto a line of its own.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import datetime as dt
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # quota_widget lives one level up, in termux/

import quota_widget as qw  # noqa: E402
import contextlib

ROOT = HERE
QUEUE = ROOT / "queue"
STAGING = QUEUE / ".staging"
WORK = ROOT / "work"
OUT = ROOT / "out"
DONE = ROOT / "done"
FAILED = ROOT / "failed"
LOGS = ROOT / "logs"
STATE_FILE = ROOT / "state.json"
LOCK_FILE = ROOT / "runner.lock"
HEARTBEAT = ROOT / "heartbeat"

MiB = 1024**2

#: The user's hard floor: this much data must survive the night's downloads.
#: Stated in decimal MB because that is how the requirement was given.
FLOOR_BYTES = 100_000_000

#: Extra headroom on the floor. Byte accounting between portal polls is
#: projected rather than measured, so stopping exactly at the floor would
#: overshoot it. This is the projection error we are willing to absorb.
#:
#: Sized for partial slices, which deliberately fly the budget down to the floor
#: every night rather than approaching it by accident. A fresh portal reading is
#: itself stale-high by the portal's own accounting lag, and each poll
#: re-baselines the interface projection, so that lag is not caught by it: at
#: ~2 MB/s and a 10-15s lag the exposure is 20-30 MB.
FLOOR_MARGIN = 32 * MiB

#: Payload bytes cost more than payload on the wire: TLS records, TCP framing
#: and retransmits. An item is handed a slice derated by this, so its own
#: cooperative stop lands short of where the runner's guards sit.
WIRE_FACTOR = 1.08

#: Below this a slice is mostly connection setup rather than transfer, and it
#: strikes nothing off the queue. Overridable per item.
SLICE_MIN_BYTES = 32 * MiB

#: Slices that move essentially nothing, despite being given room and time.
MAX_STALLS = 3

#: How long before the deadline the queue may start being worked.
WINDOW_SECONDS = 3600

#: Everything must be dead this long before the deadline, leaving room for the
#: TERM->KILL escalation to complete before the grant expires.
STOP_MARGIN = 90

#: A single JobScheduler firing gets ~10 minutes before the platform stops it.
#: Budget nine, so an item is asked to stop rather than being cut off mid-write.
FIRING_SECONDS = 9 * 60

#: The stop time of a run that has none. Every deadline in this file exists to
#: land the spending inside an allowance that is wiped at 00:00 UTC, or to hand
#: a scheduled firing back to the platform before it is stopped; a blind run is
#: spending neither that allowance nor a firing's ten minutes, so neither
#: applies and nothing may cut a download short for the time.
#:
#: Spelled as a time rather than as ``None`` so that every *comparison* against
#: a stop time goes on reading the way it did. What has to ask about it is the
#: handful of places that do arithmetic on one, and they are the places where
#: "no deadline" genuinely means something different: the ``timeout`` wrapper
#: that is not put on, the slice that is not sized against the clock, and the
#: reaper, which has to be told by the lock what the clock cannot tell it.
NO_DEADLINE = float("inf")

#: ``free.left_bytes`` can only over-state (see quota_widget's accuracy block),
#: so discount it before spending against it.
FREE_HAIRCUT_FRACTION = 0.03
FREE_HAIRCUT_FLOOR = 8 * MiB

#: Assumed background burn while a cached reading ages, in bytes per second.
AGE_BURN_RATE = 64 * 1024

#: Throughput assumption before anything has been measured.
BOOTSTRAP_BPS = 800 * 1024

#: Nights an item may fail before it is set aside for a human.
MAX_ATTEMPTS = 3

#: Poll intervals for the two independent watchdogs.
IFACE_POLL = 15
PORTAL_POLL = 60

#: An item may not be admitted unless the disk has its cap plus this spare.
DISK_SPARE = 500 * MiB

CONFIG_FILE = ROOT / "config.json"

#: Android's own Downloads folder, which is what "downloads" means on a phone:
#: the Downloads app lists it, the media scanner indexes it, and it survives
#: Termux being uninstalled. Needs ``termux-setup-storage`` to have been run
#: once — until then the path simply is not there, which :func:`dest_problem`
#: reports rather than discovering at the moment of delivery.
ANDROID_DOWNLOADS = Path("/storage/emulated/0/Download")

#: The two destinations, and which command fills each. Separate because a video
#: and an installer do not belong in the same place on a phone, and one of them
#: is usually wanted in Movies.
DEST_KINDS = ("video", "file")


def on_termux() -> bool:
    """Whether this is the phone rather than the container on zero."""
    return Path("/data/data/com.termux/files/usr").is_dir()


def default_dests() -> dict[str, Path]:
    """Where finished downloads go before anyone says otherwise.

    Android's Downloads on the phone, because that is where a phone user looks
    and it is what was asked for. Off the phone there is no such folder and
    inventing one would be a guess, so the queue's own ``out/`` stays — which
    is also exactly what this did before there was a setting at all.
    """
    if on_termux():
        return dict.fromkeys(DEST_KINDS, ANDROID_DOWNLOADS)
    return {kind: OUT for kind in DEST_KINDS}


def load_config() -> dict:
    try:
        found = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(config: dict) -> None:
    """Atomic, like the state file: a kill must not leave it half-written."""
    ROOT.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(CONFIG_FILE)


def dests() -> dict[str, Path]:
    """The configured destination per kind, falling back to the defaults."""
    config = load_config()
    out = default_dests()
    for kind in DEST_KINDS:
        set_to = config.get(f"{kind}_dir")
        if set_to:
            out[kind] = Path(set_to).expanduser()
    return out


def dest_of(item: dict) -> Path | None:
    """Where *item*'s finished file should be put, or ``None`` to leave it.

    ``DEST`` is resolved **here, at delivery**, rather than baked in when the
    item was queued: it names a kind, so changing where videos go moves the
    ones already waiting in the queue too. That is what makes it a default
    rather than a decision taken once, months ago, by a command you have since
    reconfigured. An absolute path in the header wins over both.

    An item with no ``DEST`` at all keeps the old behaviour and stays in
    ``out/<item>/``. Hand-written items predate this and never agreed to be
    moved anywhere.
    """
    declared = (item.get("dest") or "").strip()
    if not declared:
        return None
    if declared in DEST_KINDS:
        return dests()[declared]
    return Path(declared).expanduser()


def dest_problem(where: Path) -> str | None:
    """Why *where* cannot be delivered into, or ``None`` if it can.

    Checked before it matters as well as when it does, because the usual cause
    is a permission never granted — ``termux-setup-storage`` — and finding that
    out at the moment of delivery means finding it out after the data is spent.

    Not-there-yet is not a problem in itself: the queue creates its own
    ``out/``, and a new folder inside one that exists is a reasonable thing to
    ask for. What matters is whether it *can* be made and written to.
    """
    if where.is_dir():
        return None if os.access(where, os.W_OK) else f"{where} is not writable"
    if where.parent.is_dir() and os.access(where.parent, os.W_OK):
        return None
    if where == ANDROID_DOWNLOADS or ANDROID_DOWNLOADS in where.parents:
        return (
            f"{where} is not reachable; run termux-setup-storage once and "
            f"grant the permission"
        )
    return f"{where} cannot be created"


def free_name(directory: Path, name: str) -> Path:
    """A path in *directory* called *name*, or the next free variant of it.

    A shared Downloads folder already has other people's files in it, and two
    downloads may legitimately produce the same name. Nothing here may
    overwrite: the suffix pattern is Android's own, so the result looks like
    every other duplicated download on the phone.
    """
    target = directory / name
    if not target.exists():
        return target
    stem, dot, suffix = name.partition(".")
    for number in range(2, 1000):
        candidate = directory / f"{stem} ({number}){dot}{suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem} ({os.getpid()}){dot}{suffix}"


HEADER_RE = re.compile(r"^#\s*([A-Z_]+)\s*:\s*(.+?)\s*$")

#: Only files named like queue items are considered at all. Without this the
#: contract README in the same directory parses as a live item, because the
#: example header in its code fence is a perfectly valid one — documentation
#: would schedule itself as a download.
ITEM_RE = re.compile(r"^\d{2,}-")

EX_TEMPFAIL = 75


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def now() -> float:
    """UTC epoch seconds. Every time in this file is one of these."""
    return time.time()


def stamp(epoch: float | None = None) -> str:
    """UTC timestamp for logs."""
    return dt.datetime.fromtimestamp(epoch or now(), dt.UTC).strftime(
        "%Y-%m-%d %H:%M:%SZ"
    )


def clock(epoch: float) -> str:
    """``23:00Z`` — the same instant, to the minute, for a line read by a human.

    Everything this file decides is within a day of now, and every line that
    carries one of these already says which day: the heartbeat and the log
    stamp themselves, and the status screen is drawn in one sitting. So the
    date is eleven columns of a phone screen spent restating it. The zone is
    kept, because it is the one thing about these times that is not obvious.
    """
    return dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%H:%MZ")


def human(n: float) -> str:
    """Byte count for a log line."""
    for unit in ("B", "KiB", "MiB"):
        if abs(n) < 1024:
            return f"{n:,.0f} {unit}"
        n /= 1024
    return f"{n:,.2f} GiB"


def log(message: str) -> None:
    """Append to the runner log and echo, so a manual run shows its reasoning."""
    line = f"{stamp()}  {message}"
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / "runner.log").open("a") as handle:
        handle.write(line + "\n")
    print(line)


def load_state() -> dict:
    """Runner state that has to survive between firings."""
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    """Persist state by atomic rename, so a kill cannot truncate it."""
    ROOT.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True))
    temp.replace(STATE_FILE)


def iface_bytes() -> int:
    """Total bytes across every non-loopback interface.

    Deliberately device-wide rather than per-process: it counts traffic this
    runner did not cause, which makes the watchdog over-count, which is the
    safe direction for a cap.
    """
    total = 0
    try:
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            name, _, rest = line.partition(":")
            if name.strip() == "lo":
                continue
            fields = rest.split()
            total += int(fields[0]) + int(fields[8])
    except (OSError, IndexError, ValueError):
        return 0
    return total


# --------------------------------------------------------------------------- #
# The clock
# --------------------------------------------------------------------------- #


def deadlines(doc: dict | None, blind: bool = False) -> tuple[float, float, float]:
    """Return ``(deadline, window_open, stop_by)`` as UTC epoch seconds.

    The deadline is taken as the earlier of this device's idea of the next
    00:00 UTC and the portal's own ``reset.seconds_until``. The device clock can
    drift and the vessel changes timezone; the portal is authoritative about
    when the grant actually expires, so the two disagreeing means trust the one
    that takes data away sooner.

    **The window is where ``blind`` touches the clock, and it is the only place
    it does.** The hour before the reset exists for one reason — to land the
    spending inside the allowance that is about to be wiped — and a run with no
    portal reading is not spending that allowance at all, it is spending the
    SIM. So there is nothing to be early for, and nothing to be late for
    either: the window is open now and it does not close, because a download
    stopped at midnight on a night the midnight means nothing would have been
    interrupted for no reason and would have to buy the same bytes again.

    The deadline itself is left alone; it is still when the grant resets, and
    it is still true. What is gone is only its authority over this run.
    """
    current = now()
    utc = dt.datetime.fromtimestamp(current, dt.UTC)
    local_guess = qw.next_reset(utc).timestamp()
    deadline = local_guess
    if doc:
        portal_guess = current + doc["reset"]["seconds_until"]
        deadline = min(deadline, portal_guess)
    if blind:
        return deadline, current, NO_DEADLINE
    return deadline, deadline - WINDOW_SECONDS, deadline - STOP_MARGIN


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


def portal_now() -> tuple[dict | None, str]:
    """``(reading, why not)``: a forced-live portal reading, and the reason if none.

    The portal is zero-rated, so this is free and there is never a reason to
    work from a cached figure while deciding to spend money.

    Split from :func:`read_portal` for the reason a status screen needs and a
    firing does not: the reason is worth *showing* rather than logging. "the
    portal is unreachable" is not a useful answer on its own — "no credentials"
    and "no route to the portal" want different things done about them, and
    only the exception says which.
    """
    try:
        data = qw.gather(qw.stale())
        qw.store(data)
        return qw.derive(data, 0.0, True), ""
    except qw.z.PortalError as exc:
        return None, str(exc)


#: How long to wait for the portal to answer a bare connection. Short on
#: purpose: this is asked with someone waiting at a screen, and the two answers
#: it distinguishes — the vessel's network is under us, or it is not — are both
#: instant when true. A slow answer is a "no" that has not admitted it yet.
REACH_TIMEOUT = 1.5


def portal_reachable(timeout: float = REACH_TIMEOUT) -> bool:
    """Whether ``ic.zwana.io`` answers at all. The ping test, in effect.

    This is the *place* question, not the budget one. The portal lives on the
    vessel's own network, so it answering means the phone is on vessel wifi and
    a download will be counted against the crew allowance the way everything
    else is; it not answering means either mobile data, where nothing counts it
    and the phone's own plan pays, or vessel wifi with the portal down, which
    looks identical from here and is why what is said about it says both.

    A TCP connect rather than ICMP: Termux does not install a ``ping`` binary
    by default, plenty of networks drop echo requests while passing traffic,
    and "the portal answers on 443" is the same fact the rest of this module
    turns on. It costs nothing — the portal is zero-rated — and it never
    stands in for :func:`portal_now`, which is what says how much there is.
    """
    host = qw.z.BASE_URL.split("//", 1)[-1].split("/", 1)[0]
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except OSError:
        return False


def read_portal() -> dict | None:
    """A forced-live reading, or ``None``, with the failure logged.

    What a firing wants: the log is where a night's reasoning is reconstructed
    from afterwards, and a missing reading is the first thing that reasoning
    turns on.
    """
    doc, problem = portal_now()
    if problem:
        log(f"portal unreachable: {problem}")
    return doc


def usable(doc: dict | None) -> bool:
    """Whether *doc* is a reading recent enough to spend against.

    The one question two gates used to ask separately, and the one
    :func:`fire` asks to decide whether ``--blind`` is doing anything: a
    reachable portal is always preferred to flying without one, so the flag
    only takes effect when there is genuinely nothing to fly on.
    """
    return bool(doc) and doc["reading"]["live"] and doc["reading"]["age_seconds"] <= 120


def spendable_bytes(doc: dict) -> int:
    """How many bytes tonight's queue may consume in total.

    Two independent limits, and the smaller wins:

    * ``today.remainder_bytes - floor`` protects the user's 100 MB. The
      remainder is measured by the portal, so this limit is exact and is the
      one that carries the guarantee.
    * ``free.left_bytes`` keeps the spending inside the *expiring* allowance
      rather than the paid reserve. It can only over-state, so it is
      discounted: a proportional haircut, plus whatever a stale reading could
      have burned since it was taken.
    """
    free = doc["free"]["left_bytes"]
    haircut = max(FREE_HAIRCUT_FRACTION * free, FREE_HAIRCUT_FLOOR)
    age_penalty = doc["reading"]["age_seconds"] * AGE_BURN_RATE
    from_free = free - haircut - age_penalty

    from_floor = doc["today"]["remainder_bytes"] - FLOOR_BYTES - FLOOR_MARGIN
    return int(max(0, min(from_free, from_floor)))


def blind_budget(items: list[dict], state: dict) -> int:
    """How many bytes a run with no portal reading may consume in total.

    With the portal unreachable there is no remainder, no floor and no expiring
    allowance — every figure :func:`spendable_bytes` works from is a portal
    figure. What is left is the only other number in the room: what each item
    declared it needs, less what it has already taken. That is a *cap enforced
    against the items* exactly as it is on a nightly run, never a promise
    believed, and it is deliberately the same figure ``dlqd run-now --blind``
    says out loud before asking — the front end must not compute its own, or
    the number the user agreed to and the number spent would drift apart.
    """
    records = state.get("items", {})
    total = 0
    for item in items:
        done = int(records.get(item["name"], {}).get("part_bytes") or 0)
        total += max(0, item["cap"] - done)
    return total


# --------------------------------------------------------------------------- #
# Queue items
# --------------------------------------------------------------------------- #


def parse_item(path: Path) -> dict:
    """Read an item's declaration. A returned ``error`` key means do not run it.

    Parsed statically, never by executing the script. An ``--estimate`` mode
    would mean running untrusted code outside the guarded window, which is
    exactly where a buggy script could spend bytes before any guard exists.

    **Every way of failing has to come back as an ``error``, never as a raised
    exception.** This runs over every file in the queue at the top of every
    firing, before anything else happens, so an exception escaping here is not
    one bad item — it is the whole night, silently, for as long as the file
    sits there. A photo called ``20-holiday.png`` is enough to do it: it
    matches the item naming, and it is not UTF-8.
    """
    bad = lambda why: {"path": path, "name": path.name, "error": why}  # noqa: E731

    fields: dict[str, str] = {}
    first = ""
    try:
        # Explicitly UTF-8 rather than the locale's guess: a scheduled firing
        # inherits whatever environment the platform gives it, and an item is
        # not allowed to parse differently by hand than it does at midnight.
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle):
                if number == 0:
                    first = line.rstrip("\n")
                if not line.startswith("#"):
                    if fields:
                        break
                    continue
                found = HEADER_RE.match(line)
                if found:
                    fields[found.group(1)] = found.group(2)
    except OSError as exc:
        return bad(f"unreadable ({exc.strerror})")
    except UnicodeDecodeError:
        return bad("not a text file, so not an item; move it out of the queue")

    if fields.get("EXPIRE") != "v1":
        return bad("no 'EXPIRE: v1' header")
    if "EXPECT_BYTES" not in fields:
        return bad("no 'EXPECT_BYTES' header")
    try:
        cap = int(fields["EXPECT_BYTES"])
    except ValueError:
        return bad(f"EXPECT_BYTES is not an integer: {fields['EXPECT_BYTES']!r}")
    if cap <= 0:
        return bad(f"EXPECT_BYTES must be positive, got {cap}")

    # A wrong shebang fails at exec with a bare "No such file or directory"
    # naming the *script*, which reads as though the item vanished. On Termux
    # /bin/bash in particular does not exist -- /bin is a symlink to
    # /system/bin. Catching it here turns three wasted nights into one clear
    # line in the log.
    if os.access(path, os.X_OK) and first.startswith("#!"):
        interpreter = first[2:].strip().split()[0] if first[2:].strip() else ""
        if not interpreter or not Path(interpreter).exists():
            return bad(f"shebang interpreter not found: {interpreter!r}")

    partial = fields.get("PARTIAL", "").strip().lower() in ("yes", "true", "1")
    try:
        slice_min = int(fields.get("SLICE_MIN_BYTES", SLICE_MIN_BYTES))
    except ValueError:
        return bad(f"SLICE_MIN_BYTES is not an integer: {fields['SLICE_MIN_BYTES']!r}")

    return {
        "path": path,
        "name": path.name,
        "cap": cap,
        "partial": partial,
        "slice_min": slice_min,
        "desc": fields.get("DESC", path.name),
        "dest": fields.get("DEST", ""),
    }


def queued_items() -> tuple[list[dict], list[dict]]:
    """Conforming items in run order, plus rejected ones with their reasons."""
    good: list[dict] = []
    bad: list[dict] = []
    for path in sorted(QUEUE.glob("*")):
        if path.name.startswith(".") or not path.is_file():
            continue
        if not ITEM_RE.match(path.name):
            # Not named like an item: notes, READMEs, scratch. Silently ignored
            # rather than reported as malformed, since it was never a claim to
            # be an item in the first place.
            continue
        item = parse_item(path)
        (bad if "error" in item else good).append(item)
    return good, bad


# --------------------------------------------------------------------------- #
# Running one item
# --------------------------------------------------------------------------- #


def reap(state: dict, orphaned: bool = False) -> None:
    """Kill anything a previous firing left running past its stop time.

    Closes the gap where this runner died while a child was still going: the
    ``timeout`` wrapper should have handled it, but a grandchild that made its
    own session would escape both.

    A blind run records no stop time, because it has none, so the clock cannot
    say whether its child has overrun — and "past its stop time" is the whole
    test here. The lock says it instead: *orphaned* is passed by the caller
    that **holds the lock**, where a recorded process group can only belong to
    a runner that is already gone. The caller that failed to take the lock
    passes nothing and leaves it alone, because there the runner it belongs to
    is demonstrably alive and downloading.
    """
    pgid = state.get("active_pgid")
    stop_by = state.get("active_stop_by", 0)
    if not pgid:
        return
    if stop_by is None:
        if not orphaned:
            return
    elif now() < stop_by:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
            log(f"reaped stale process group {pgid} with {sig.name}")
            time.sleep(2)
        except ProcessLookupError:
            break
        except PermissionError:
            break
    state.pop("active_pgid", None)
    state.pop("active_stop_by", None)
    save_state(state)


def read_status(work: Path, run_id: str, ceiling: int) -> dict | None:
    """The item's own byte report, or ``None`` if it cannot be trusted.

    Two ways it is untrustworthy. A mismatched ``run_id`` means the file is a
    leftover from an earlier run and tonight's item died before writing — treat
    it as absent. And the count itself is a *claim*: an item that under-reports
    would make the runner's ledger optimistic, so it is clamped to the interface
    delta, which cannot be under the truth.
    """
    try:
        report = json.loads((work / ".status.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict) or report.get("run_id") != run_id:
        return None
    claim = report.get("payload_bytes_this_slice")
    if not isinstance(claim, (int, float)):
        return None
    report["spent"] = min(max(0, int(claim)), ceiling)
    return report


def spawn_plan(stop_by: float) -> tuple[list[str], str, str]:
    """``(command prefix, EXPIRE_STOP_EPOCH, how the log says it)``.

    "No deadline" has to be said three times at every spawn — to the kernel, to
    the item, and to whoever reads the log afterwards — and this is the one
    place that decides it, because the three disagreeing is a download killed
    mid-write by a ``timeout`` the item never knew about, with nothing in the
    log to say what happened.

    ``0`` is the contract's own spelling of "no deadline" and not a special
    case invented here: an item reads it as ``+inf`` (``expire_dl.deadline``),
    and it is what ``dlqd now`` has always passed.
    """
    if stop_by == NO_DEADLINE:
        # No `timeout` at all. Wrapping it in a made-up large number instead
        # would be a deadline again — one nobody wrote down, that nobody could
        # see coming, and that would land in the middle of a long download.
        return [], "0", "no stop"
    seconds = max(30, int(stop_by - now()))
    return (
        ["timeout", "--kill-after=15", str(seconds)],
        str(int(stop_by)),
        f"{seconds}s until stop",
    )


def run_item(
    item: dict, cap: int, stop_by: float, doc: dict | None, state: dict
) -> tuple[int, int, dict | None]:
    """Run one queued item under every guard.

    Returns ``(exit code, bytes on the wire, status report or None)``. The item
    is spawned in its own session so the whole tree can be signalled, and
    wrapped in ``timeout`` so the deadline is enforced by a process that
    outlives this one.

    On a blind run there is no deadline to enforce, so there is no ``timeout``
    around it and the item is told ``EXPIRE_STOP_EPOCH=0`` — the contract's own
    spelling of "no deadline", and the one ``dlqd now`` has always used. The
    item then runs until it is done. Everything that is not the clock is
    unchanged: the session, the byte cap, and the interruption path below.
    """
    work = WORK / item["name"]
    out = OUT / item["name"]
    for directory in (work, out):
        directory.mkdir(parents=True, exist_ok=True)

    endless = stop_by == NO_DEADLINE
    wrapper, stop_epoch, until = spawn_plan(stop_by)
    run_id = f"{int(now())}-{os.getpid()}"
    env = dict(os.environ)
    env.update(
        {
            # BUDGET is kept equal to SLICE for items written against the older
            # contract: a slice is never larger than the cap they expect there.
            "EXPIRE_BUDGET_BYTES": str(cap),
            "EXPIRE_SLICE_BYTES": str(cap),
            "EXPIRE_TOTAL_BYTES": str(item["cap"]),
            "EXPIRE_RUN_ID": run_id,
            "EXPIRE_STOP_EPOCH": stop_epoch,
            "EXPIRE_WORK": str(work),
            "EXPIRE_OUT": str(out),
        }
    )

    day = dt.datetime.fromtimestamp(now(), dt.UTC).strftime("%Y-%m-%d")
    log_path = LOGS / f"{day}-{item['name']}.log"

    log(f"start {item['name']}  cap {human(cap)}  {until}")
    with log_path.open("a") as sink:
        sink.write(f"\n===== {stamp()} cap={cap} stop_in={until} =====\n")
        sink.flush()
        # Honour the shebang when the item is executable, so a .py item runs as
        # Python rather than being fed to bash; fall back to bash otherwise.
        launch = (
            [str(item["path"])]
            if os.access(item["path"], os.X_OK)
            else ["bash", str(item["path"])]
        )
        # ``start_new_session`` rather than an external ``setsid``, and the
        # difference is a race that only bites the process group being killed.
        # Python calls ``setsid()`` in the child between fork and exec, so the
        # child *is* the session leader by the time Popen returns and its pgid
        # is its pid. Spawning ``setsid`` as a program instead leaves a window
        # — Popen has returned, the program has not yet run — in which
        # ``os.getpgid(child.pid)`` answers with the group the child was forked
        # into, which is **this runner's own**. Recording that means every
        # later ``killpg`` signals the runner and whatever else shares its
        # group, and leaves the download running.
        child = subprocess.Popen(
            wrapper + launch,
            stdout=sink,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(work),
            start_new_session=True,
        )

    # Not os.getpgid(): the child leads its own session, so this is its group
    # by construction, and it is still the answer if it has already exited.
    pgid = child.pid
    state["active_pgid"] = pgid
    # Recorded as "there is no stop time" rather than as a very large one, so
    # that the reaper is made to ask the lock instead of quietly waiting for a
    # clock that will never come round.
    state["active_stop_by"] = None if endless else stop_by
    save_state(state)

    try:
        spent = watch(child, pgid, cap, stop_by, doc)
    except BaseException:
        # ctrl-c, or anything else unwinding out of the supervisor. The child
        # has its own session, so the signal the terminal sent never reached
        # it: without this, stopping a run with no deadline would leave a
        # download nothing is watching and nothing will stop, spending mobile
        # data until the phone is rebooted. What it leaves behind is what a
        # deadline would have left — a resumable file and a queued item.
        kill_tree(pgid, "runner interrupted")
        state.pop("active_pgid", None)
        state.pop("active_stop_by", None)
        save_state(state)
        raise
    report = read_status(work, run_id, spent)

    state.pop("active_pgid", None)
    state.pop("active_stop_by", None)
    save_state(state)
    code = child.returncode if child.returncode is not None else -1
    return code, spent, report


def kill_tree(pgid: int, why: str) -> None:
    """Signal the whole process group, escalating if it does not go."""
    log(f"killing process group {pgid}: {why}")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(10):
        time.sleep(1)
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def watch(
    child: subprocess.Popen, pgid: int, cap: int, stop_by: float, doc: dict | None
) -> int:
    """Supervise a running item. Returns bytes attributed to it.

    Two independent watchdogs, because either alone has a hole. Interface
    counters see every byte immediately but cannot tell whose they are; the
    portal knows the authoritative remainder but only updates periodically.

    ``doc`` is ``None`` on a blind run, and then only the first of those
    exists: there is no remainder to project the floor against and no portal to
    go dark. Note which guards do *not* depend on it and so still hold — the
    deadline, the interface byte cap, and the ``timeout`` wrapper around the
    child. What the missing half costs is stated where it is agreed to, in
    :func:`blind_budget` and in the question ``dlqd`` asks before starting.
    """
    start_iface = iface_bytes()
    last_portal = now()
    remainder = doc["today"]["remainder_bytes"] if doc else None
    remainder_iface = start_iface
    hard_floor = FLOOR_BYTES + FLOOR_MARGIN
    allowance = cap * 1.15 + 8 * MiB
    portal_dark_since: float | None = None

    while True:
        # Notice a finished child quickly, but run the costly checks only once
        # per IFACE_POLL: charging a fast item fifteen seconds of a nine-minute
        # slice would waste a meaningful share of the window.
        tick = now() + IFACE_POLL
        while now() < tick and child.poll() is None:
            time.sleep(0.5)
        if child.poll() is not None:
            break

        current = now()
        spent = max(0, iface_bytes() - start_iface)

        if current >= stop_by:
            kill_tree(pgid, "deadline reached")
            break
        if spent > allowance:
            kill_tree(pgid, f"byte cap exceeded ({human(spent)} > {human(cap)})")
            break

        # Between portal polls the remainder is projected downwards using the
        # interface delta, which over-counts — so the floor is approached
        # pessimistically rather than crossed unnoticed.
        if remainder is not None:
            projected = remainder - max(0, iface_bytes() - remainder_iface)
            if projected <= hard_floor:
                kill_tree(pgid, f"floor reached (projected {human(projected)})")
                break

        if doc is not None and current - last_portal >= PORTAL_POLL:
            last_portal = current
            fresh = read_portal()
            if fresh:
                portal_dark_since = None
                remainder = fresh["today"]["remainder_bytes"]
                remainder_iface = iface_bytes()
                if remainder <= hard_floor:
                    kill_tree(pgid, f"floor reached ({human(remainder)} left)")
                    break
            else:
                portal_dark_since = portal_dark_since or current
                if current - portal_dark_since > 300:
                    kill_tree(pgid, "portal unreachable for 5 minutes")
                    break

    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        kill_tree(pgid, "did not exit after signal")
    return max(0, iface_bytes() - start_iface)


# --------------------------------------------------------------------------- #
# Disposition
# --------------------------------------------------------------------------- #


def tidy(item: dict) -> None:
    """Drop the scratch and output directories if the item left them empty."""
    for directory in (WORK / item["name"], OUT / item["name"]):
        with contextlib.suppress(OSError):
            directory.rmdir()


def hand_over(item: dict, state: dict) -> list[Path]:
    """Move what the item delivered into its configured download directory.

    The item itself always delivers into ``out/<item>/`` and knows nothing
    about any of this. Two reasons that separation is worth the extra step:

    * ``out/`` is private to one item, so "have I already delivered this?" is
      answerable by looking. A shared Downloads folder is full of other
      people's files, and an item called ``video`` would find somebody else's
      ``video.mp4`` and archive itself without downloading anything.
    * A destination on shared storage is a **different filesystem**, so this is
      a copy rather than a rename and it can fail — no permission, card
      unmounted, no space. Failing here leaves the file sitting in ``out/``,
      already paid for and still there, instead of half-written into Downloads.

    Returns where the files ended up, which is what ``dlqd path`` reports.
    """
    where = dest_of(item)
    source = OUT / item["name"]
    try:
        files = sorted(path for path in source.iterdir() if path.is_file())
    except OSError:
        files = []
    if where is None or not files:
        return files  # No DEST: the old behaviour, and it stays in out/.

    problem = dest_problem(where)
    if problem is None:
        try:
            where.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problem = f"{where} could not be created ({exc.strerror})"
    if problem:
        log(f"WARNING {item['name']} stays in {source}: {problem}")
        notify(
            "Download queue: cannot reach the download folder",
            f"{item['name']} is in out/, not {where}: {problem}",
        )
        return files

    landed: list[Path] = []
    for path in files:
        target = free_name(where, path.name)
        try:
            # shutil, never Path.replace: the destination is usually on shared
            # storage, and a rename across filesystems fails with EXDEV.
            shutil.move(str(path), str(target))
        except (OSError, shutil.Error) as exc:
            log(f"WARNING could not move {path.name} to {where}: {exc}")
            landed.append(path)
            continue
        if target.name != path.name:
            log(f"{path.name} was taken, saved as {target.name}")
        landed.append(target)
    return landed


def archive(item: dict, state: dict) -> None:
    """A finished item leaves the queue by rename, so it cannot run twice."""
    day = dt.datetime.fromtimestamp(now(), dt.UTC).strftime("%Y-%m-%d")
    target = DONE / day
    target.mkdir(parents=True, exist_ok=True)
    landed = hand_over(item, state)
    item["path"].rename(target / item["name"])
    record = state.get("items", {}).get(item["name"])
    if record is not None:
        record["retired"] = "done"
        # Where it actually is, recorded because after the move nothing can
        # work it out by looking: the file is in a folder full of other files.
        record["delivered"] = [str(path) for path in landed]
    tidy(item)
    log(f"done {item['name']} -> {landed[0] if landed else target}")


def give_up(item: dict, state: dict) -> None:
    """Set an item aside after too many failed nights.

    The attempt history is kept rather than dropped: an item that failed three
    nights needs diagnosing, and how it failed each time is the evidence.
    """
    FAILED.mkdir(parents=True, exist_ok=True)
    item["path"].rename(FAILED / item["name"])
    record = state.get("items", {}).get(item["name"])
    if record is not None:
        record["retired"] = "failed"
    log(f"gave up on {item['name']} after {MAX_ATTEMPTS} nights")
    notify(
        "Download queue: item failed",
        f"{item['name']} failed {MAX_ATTEMPTS} nights; moved to failed/",
    )


def notify(title: str, content: str) -> None:
    """Best-effort notification; never allowed to break a run."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [
                "termux-notification",
                "--id",
                "expire-queue",
                "--title",
                title,
                "--content",
                content,
            ],
            timeout=20,
            capture_output=True,
        )


# --------------------------------------------------------------------------- #
# The firing
# --------------------------------------------------------------------------- #


#: Every answer :func:`gate` can give, in the order it checks them. ``go`` and
#: ``blind`` are the two that download; the rest are why not, and the status
#: screen says each of them in its own words.
GATE_STATES = ("empty", "early", "late", "no-portal", "stale", "blind", "spent", "go")

#: The two that are a fault rather than a schedule: nothing is wrong with a
#: queue that is merely waiting, but a runner that cannot see the portal has
#: been stopped by something a person may be able to fix. ``blind`` is not one
#: of them — it is the same missing portal, already answered for by a human.
GATE_FAULTS = ("no-portal", "stale")

#: The answers that spend. ``blind`` is ``go`` with no reading behind it: the
#: same queue and the same per-item caps, on ordinary paid mobile data instead
#: of the expiring grant.
GATE_GO = ("blind", "go")


def gate(
    items: list[dict],
    doc: dict | None,
    window_open: float,
    stop_by: float,
    current: float,
    force: bool = False,
    blind: bool = False,
) -> tuple[str, str]:
    """Whether a firing would download right now, and the line saying why not.

    One decision with two readers: :func:`fire`, which acts on it, and the
    status screen, which reports it. Spelled once because the alternative is a
    screen that says "waiting for the window" on a night the runner is actually
    refusing for some other reason — a wrong answer that looks exactly like the
    right one, and the only place the difference shows is a log nobody is
    reading at 23:40.

    The order is the order of the gates, and it is part of the answer: an empty
    queue is reported as an empty queue even when the portal is also down,
    because that is the one that would have to be fixed first.

    *force* overrides the clock gate and only that. *blind* overrides the two
    portal gates and only those — it turns "there is no reading, so nothing
    starts" into "there is no reading, so this is mobile data", which is a
    thing only a person can decide and is asked for by name. Neither of them
    reaches the empty queue or the per-item caps. Note where *blind* does
    **not** appear: past the reading it never gets to, since a portal that
    answers is always preferred to guessing, so a blind run that finds the
    portal up is an ordinary run with the ordinary floor still enforced.
    """
    if not items:
        return "empty", "queue empty"
    if not force and current < window_open:
        return "early", f"not yet: window opens {clock(window_open)}"
    if current >= stop_by:
        return "late", "past stop time for tonight"
    if not usable(doc):
        if blind:
            return "blind", "no portal reading; spending mobile data"
        if doc is None:
            return "no-portal", "no live portal reading; not starting anything"
        return "stale", "portal reading is stale; not starting anything"
    if spendable_bytes(doc) <= 0:
        return "spent", f"no spendable data ({human(doc['free']['left_bytes'])} free)"
    return "go", "window open"


def snapshot(force: bool = False, blind: bool = False) -> dict:
    """Everything the status screen says, gathered without touching the queue.

    Read-only and lock-free, so "what is it doing right now" stays answerable
    while a firing is in progress — which is exactly when it is asked. The one
    write anywhere under this is :func:`portal_now` refreshing the quota cache,
    which belongs to the widget rather than to the queue.

    Facts only: what the gate decided, the figures it decided on, and the items
    as declared. How any of it is drawn is :func:`expire_sched.compose_status`,
    which lays out the same terminal ``dlqd list`` does.
    """
    state = load_state()
    items, rejected = queued_items()
    doc, portal_problem = portal_now()
    # Asked for and actually flying blind are two different things: with the
    # portal up, --blind changes nothing and the screen must not claim it did.
    flying = blind and not usable(doc)
    deadline, window_open, stop_by = deadlines(doc, flying)
    current = now()
    verdict, detail = gate(items, doc, window_open, stop_by, current, force, blind)

    records = state.get("items", {})
    return {
        "root": ROOT,
        "now": current,
        "forced": force,
        "blind": flying,
        "verdict": verdict,
        "detail": detail,
        "deadline": deadline,
        "window_open": window_open,
        "stop_by": stop_by,
        "portal": doc,
        "portal_problem": portal_problem,
        "spendable": (
            blind_budget(items, state)
            if verdict == "blind"
            else spendable_bytes(doc)
            if doc
            else 0
        ),
        "floor_bytes": FLOOR_BYTES,
        "bps": state.get("ewma_bps", BOOTSTRAP_BPS),
        "max_attempts": MAX_ATTEMPTS,
        "items": [
            {
                "name": item["name"],
                "cap": item["cap"],
                "partial": item["partial"],
                "desc": item["desc"],
                "attempts": int(records.get(item["name"], {}).get("attempts") or 0),
            }
            for item in items
        ],
        "rejected": [
            {"name": item["name"], "error": item["error"]} for item in rejected
        ],
    }


def fire(force: bool = False, blind: bool = False) -> int:
    """One scheduled firing: decide, then act. Returns a shell exit code."""
    for directory in (QUEUE, STAGING, WORK, OUT, DONE, FAILED, LOGS):
        directory.mkdir(parents=True, exist_ok=True)

    state = load_state()
    # Killing a stale process group and rewriting state.json are both writes,
    # and the lock is what makes them safe. Holding it is also what says
    # anything still recorded here is an orphan: its runner cannot be alive and
    # not holding the lock this one just took.
    reap(state, orphaned=True)

    items, rejected = queued_items()
    doc = read_portal()
    flying = blind and not usable(doc)
    if flying:
        # Dropped rather than carried: everything downstream — the budget, the
        # floor projection in watch(), the poll that would kill the run — takes
        # a reading as authority, and a reading too old to gate on is too old
        # to be an authority for any of them either.
        doc = None
    deadline, window_open, stop_by = deadlines(doc, flying)
    current = now()

    verdict, detail = gate(items, doc, window_open, stop_by, current, force, blind)
    if verdict in GATE_FAULTS:
        log(detail)
        return 1

    budget = (
        blind_budget(items, state)
        if verdict == "blind"
        else spendable_bytes(doc)
        if doc
        else 0
    )
    if verdict in GATE_GO or verdict == "spent":
        # Logged for all of them, because "the window opened and there was
        # nothing to spend" is a different night from "the window never
        # opened", and only this line tells them apart afterwards. A blind run
        # says so here as well: the log is where a night's reasoning is
        # reconstructed from, and "this was mobile data" is the first thing
        # anyone reading it back would want to know.
        opening = (
            "no portal, spending mobile data"
            if verdict == "blind"
            else "window open"
        )
        closing = (
            "no stop"
            if stop_by == NO_DEADLINE
            else f"{int(stop_by - current)}s to stop"
        )
        log(f"{opening}, {human(budget)} spendable, {len(items)} queued, {closing}")
    if verdict not in GATE_GO:
        heartbeat(detail)
        return 0

    for item in rejected:
        log(f"skipped {item['name']}: {item['error']}")
    if rejected:
        notify(
            "Download queue: malformed item",
            f"{len(rejected)} item(s) lack a valid header and were skipped",
        )

    # A firing is stopped by the platform after ~10 minutes, so this run gets a
    # slice and the rest of the window belongs to later firings. That is a fact
    # about JobScheduler, and a blind run is not one of its firings — it is a
    # person at a terminal — so there is nothing to hand back to and it works
    # the queue until the queue is done.
    firing_stop = (
        stop_by if stop_by == NO_DEADLINE else min(stop_by, current + FIRING_SECONDS)
    )
    done_count = 0
    spent_total = 0

    for item in items:
        remaining_time = firing_stop - now()
        if remaining_time < 45:
            log("out of time for this firing; the rest waits for the next")
            break

        free_disk = shutil.disk_usage(str(ROOT)).free
        if free_disk < item["cap"] + DISK_SPARE:
            log(f"skip {item['name']}: only {human(free_disk)} disk free")
            continue

        record = state.setdefault("items", {}).setdefault(item["name"], {"attempts": 0})
        rate = max(0.5 * state.get("ewma_bps", BOOTSTRAP_BPS), 100 * 1024)

        if item["partial"]:
            # A partial item is never refused for being too big; it is given a
            # slice and asked to come back. The slice is derated for wire
            # overhead so the item's own stop lands short of the runner's kill.
            done_already = int(record.get("part_bytes") or 0)
            need = max(0, item["cap"] - done_already)
            # The wire derate belongs to a budget that is measured on the wire
            # — the portal's remainder, the interface counters. A blind budget
            # is the items' own payload declarations added up, so derating it
            # here would charge the overhead twice: every partial item would be
            # handed a slice ~7% short of the size it declared and need another
            # run to collect a tail that was never really missing.
            by_budget = budget if flying else int(budget / WIRE_FACTOR)
            # What the clock allows is one of the limits, and only where there
            # is a clock: with no deadline the item is given the whole of what
            # it still needs and takes as long as that takes.
            limits = [need, by_budget]
            if remaining_time != NO_DEADLINE:
                limits.append(int(rate * max(0, remaining_time - 45)))
            cap = max(0, min(limits))

            # The minimum exists to stop nightly churn, but must not block a
            # file that is nearly finished.
            floor_slice = min(item["slice_min"], need)
            if cap < floor_slice:
                left = (
                    "no deadline"
                    if remaining_time == NO_DEADLINE
                    else f"{remaining_time:.0f}s left"
                )
                log(
                    f"skip {item['name']}: slice {human(cap)} below the useful "
                    f"minimum {human(floor_slice)} "
                    f"(budget {human(budget)}, {left})"
                )
                continue
            log(
                f"{item['name']}: slice {human(cap)} of {human(need)} still "
                f"needed ({human(done_already)} done)"
            )
        else:
            cap = item["cap"]
            if cap > budget:
                log(
                    f"skip {item['name']}: needs {human(cap)}, "
                    f"{human(budget)} spendable"
                )
                continue
            predicted = cap / rate
            if predicted > remaining_time - 30:
                log(
                    f"skip {item['name']}: needs ~{predicted:.0f}s, "
                    f"{remaining_time:.0f}s left this firing"
                )
                continue

        item_stop = min(firing_stop, stop_by)
        began = now()
        code, spent, claimed = run_item(item, cap, item_stop, doc, state)
        elapsed = max(1.0, now() - began)
        spent_total += spent
        budget = max(0, budget - spent)

        payload = claimed["spent"] if claimed else None
        if claimed:
            record["part_bytes"] = int(claimed.get("part_bytes") or 0)
            if spent > payload + 8 * MiB:
                # Worth naming: a big gap means something else on the phone was
                # using the link, which is also what would wrongly trip the
                # interface cap kill on an innocent item.
                log(
                    f"note: {human(spent)} crossed the interface vs "
                    f"{human(payload)} claimed by the item"
                )

        # Only learn the rate from transfers big enough to be representative;
        # a 200 kB item that spent most of its life in DNS would poison it.
        if spent > 4 * MiB:
            observed = spent / elapsed
            state["ewma_bps"] = (
                0.7 * state.get("ewma_bps", BOOTSTRAP_BPS) + 0.3 * observed
            )
            log(f"throughput now ~{human(state['ewma_bps'])}/s")

        record["last"] = stamp()
        record["last_exit"] = code
        record["bytes"] = record.get("bytes", 0) + spent

        if code == 0:
            archive(item, state)
            done_count += 1
        elif code == EX_TEMPFAIL:
            # "Not tonight" is a legitimate answer, but an item that says it
            # every night while moving nothing would never retire. Only count a
            # stall when it was actually given room and time to make progress.
            moved = payload if payload is not None else spent
            if (
                item["partial"]
                and cap >= min(item["slice_min"], MiB)
                and elapsed >= 120
                and moved < MiB
            ):
                record["stalls"] = record.get("stalls", 0) + 1
                log(
                    f"{item['name']} stalled ({human(moved)} in {elapsed:.0f}s), "
                    f"stall {record['stalls']}/{MAX_STALLS}"
                )
                if record["stalls"] >= MAX_STALLS:
                    record["stalls"] = 0
                    record["attempts"] += 1
                    log(
                        f"{item['name']} counted a strike for repeated stalls, "
                        f"attempt {record['attempts']}/{MAX_ATTEMPTS}"
                    )
                    if record["attempts"] >= MAX_ATTEMPTS:
                        give_up(item, state)
            else:
                record["stalls"] = 0
                log(f"{item['name']} made progress but is not finished; left queued")
        else:
            record["attempts"] += 1
            log(
                f"{item['name']} failed (exit {code}), "
                f"attempt {record['attempts']}/{MAX_ATTEMPTS}"
            )
            if record["attempts"] >= MAX_ATTEMPTS:
                give_up(item, state)
        save_state(state)

        # Only where there was a reading to begin with. On a blind run there is
        # nothing at the other end to ask, and asking anyway would log the same
        # failure once per item and stall each gap on a connection timeout.
        if doc is not None:
            fresh = read_portal()
            if fresh:
                doc = fresh
                budget = min(budget, spendable_bytes(doc))

    if done_count or spent_total > MiB:
        left = (
            f", {human(doc['today']['remainder_bytes'])} left"
            if doc
            else " of mobile data"
        )
        notify(
            "Download queue ran",
            f"{done_count} finished, {human(spent_total)} spent{left}",
        )
    return 0


def heartbeat(message: str) -> None:
    """Record liveness without filling the log.

    Roughly 96 firings a day are no-ops; appending them all would bury the few
    lines that matter, so this file is overwritten instead.
    """
    ROOT.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(f"{stamp()}  {message}\n")


def report(force: bool = False, blind: bool = False) -> int:
    """Draw the status screen: what would happen now, and what it turns on.

    Drawn by ``expire_sched`` rather than here, because it is one screen and it
    lays out one terminal — the phone's, in portrait, at about 40 columns. The
    queue rows in particular have to agree with ``dlqd list`` down to the
    figures, and the way to be sure of that is for the same code to draw both.
    This end owns the facts (:func:`snapshot`); that end owns the layout.
    """
    facts = snapshot(force, blind)
    # Imported here rather than at the top: a firing must not pay for it, and
    # only this path ever draws anything.
    import expire_sched

    paint = qw.Paint(expire_sched.colour_ok())
    for _, painted in expire_sched.compose_status(facts, paint=paint):
        print(painted)
    return 0


def _self_test() -> int:
    """Offline checks on the clock. No network, no queue, no Windows host.

    The vessel changes timezone regularly — only the zone, never the clock — so
    the property that matters is that none of the window arithmetic can see a
    local offset. A naive datetime anywhere in the chain would still produce a
    plausible window, just displaced by hours, and the only symptom would be
    downloads starting at the wrong time months later.
    """
    passed = failed = 0

    def check(label: str, got, want) -> None:
        nonlocal passed, failed
        if got == want:
            passed += 1
        else:
            failed += 1
            print(f"FAIL {label}: got {got!r}, want {want!r}")

    # Zones spanning the date line, so a local-time leak shows up as a whole
    # day rather than only as an hour offset.
    zones = [
        "UTC",
        "Etc/GMT+2",
        "Pacific/Kiritimati",
        "Pacific/Midway",
        "Asia/Kolkata",
        "Australia/Sydney",
    ]
    saved_tz = os.environ.get("TZ")
    saved_now = globals()["now"]
    # Pinned so the sweep cannot straddle a midnight and disagree for a real
    # reason: 2026-08-03 12:00:00Z.
    globals()["now"] = lambda: 1785758400.0
    try:
        windows = []
        for zone in zones:
            os.environ["TZ"] = zone
            time.tzset()
            windows.append(deadlines(None))
        for zone, got in zip(zones[1:], windows[1:], strict=False):
            check(f"window is identical under {zone}", got, windows[0])

        deadline, window_open, stop_by = windows[0]
        check(
            "deadline is the next midnight UTC", stamp(deadline), "2026-08-04 00:00:00Z"
        )
        check(
            "window opens WINDOW_SECONDS before it",
            deadline - window_open,
            WINDOW_SECONDS,
        )
        check("stop_by leaves STOP_MARGIN", deadline - stop_by, STOP_MARGIN)
        check("the window opens before it stops", window_open < stop_by, True)

        # The blind clock. The window exists to land the spending inside an
        # allowance that is about to be wiped; with no reading there is no such
        # allowance, so there is nothing to be early for and nothing to be late
        # for either. **Nothing may cut a blind download short for the time** —
        # a run stopped at a midnight that means nothing to it would have to
        # buy the same bytes twice. The reset it no longer turns on is still
        # reported as the reset.
        blind_deadline, blind_open, blind_stop = deadlines(None, blind=True)
        check("a blind window is open now", blind_open, now())
        check("and it does not close", blind_stop, NO_DEADLINE)
        check("the reset it reports is still the real one", blind_deadline, deadline)
        check(
            "so nothing blind is ever early or late",
            {
                gate(
                    [{"name": "40-x.py", "cap": 10, "partial": True}],
                    None,
                    blind_open,
                    blind_stop,
                    at,
                    blind=True,
                )[0]
                # However long it runs. A download of a 6 GB file on a slow
                # link outlives the reset it was started before, and that is
                # the case the whole flag exists for.
                for at in (blind_open, blind_open + 86_400, blind_open + 86_400 * 30)
            },
            {"blind"},
        )

        for zone in zones:
            os.environ["TZ"] = zone
            time.tzset()
            check(f"stamp is UTC under {zone}", stamp(), "2026-08-03 12:00:00Z")
    finally:
        globals()["now"] = saved_now
        if saved_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved_tz
        time.tzset()

    # The delegated call that would break all of the above if it ever went
    # naive: .timestamp() on a naive datetime reads it as local time.
    reset = qw.next_reset(dt.datetime.fromtimestamp(now(), dt.UTC))
    check("next_reset stays timezone-aware", reset.tzinfo is not None, True)
    check("and lands on midnight", (reset.hour, reset.minute, reset.second), (0, 0, 0))

    # The gate. One decision with two readers — the firing that acts on it and
    # the status screen that reports it — so what is pinned here is the *order*
    # it answers in. Get that wrong and the screen still says something
    # plausible on every night of the year; it is just not the reason.
    item = [{"name": "40-x.py", "cap": 10, "partial": True}]
    fresh = {
        "reading": {"live": True, "age_seconds": 0, "online": True},
        "free": {"left_bytes": 700 * MiB, "grant_bytes": 763 * MiB},
        "today": {"remainder_bytes": 900 * MiB},
    }
    open_at, stop_at = 1000.0, 4000.0
    check(
        "no items is the first answer",
        gate([], None, open_at, stop_at, 0.0)[0],
        "empty",
    )
    check(
        "before the window it is early",
        gate(item, fresh, open_at, stop_at, 500.0)[0],
        "early",
    )
    check(
        "and --force is what overrides that and only that",
        gate(item, fresh, open_at, stop_at, 500.0, force=True)[0],
        "go",
    )
    check(
        "an empty queue is still empty under --force",
        gate([], fresh, open_at, stop_at, 500.0, force=True)[0],
        "empty",
    )
    check(
        "and --blind is what overrides the portal and only that",
        gate(item, None, open_at, stop_at, 2000.0, blind=True)[0],
        "blind",
    )
    check(
        "an empty queue is still empty under --blind",
        gate([], None, open_at, stop_at, 2000.0, blind=True)[0],
        "empty",
    )
    check(
        "--blind does not open the window on its own",
        gate(item, None, open_at, stop_at, 500.0, blind=True)[0],
        "early",
    )
    # The flag says what to do when there is no reading, not what to do
    # instead of one. A portal that answers still carries the floor, so a
    # blind run that finds it up is an ordinary run and can still be broke.
    check(
        "a reachable portal outranks --blind",
        gate(item, fresh, open_at, stop_at, 2000.0, blind=True)[0],
        "go",
    )
    at_floor = json.loads(json.dumps(fresh))
    at_floor["today"]["remainder_bytes"] = FLOOR_BYTES
    check(
        "including when it says there is nothing to spend",
        gate(item, at_floor, open_at, stop_at, 2000.0, blind=True)[0],
        "spent",
    )
    # A stale reading is unusable for the same reason a missing one is, so the
    # flag has to cover both — otherwise the one path it does not cover is the
    # one that fails at 23:50 with nobody watching.
    aged = json.loads(json.dumps(fresh))
    aged["reading"]["age_seconds"] = 300
    check(
        "a reading too old to spend against is blind too",
        gate(item, aged, open_at, stop_at, 2000.0, blind=True)[0],
        "blind",
    )
    check(
        "past the stop time it is late",
        gate(item, fresh, open_at, stop_at, 5000.0)[0],
        "late",
    )
    check(
        "and late outranks a missing portal",
        gate(item, None, open_at, stop_at, 5000.0)[0],
        "late",
    )
    check(
        "no reading blocks it",
        gate(item, None, open_at, stop_at, 2000.0)[0],
        "no-portal",
    )
    stale_doc = json.loads(json.dumps(fresh))
    stale_doc["reading"]["age_seconds"] = 300
    check(
        "so does one that has aged out",
        gate(item, stale_doc, open_at, stop_at, 2000.0)[0],
        "stale",
    )
    # The floor is the user's guarantee, so a remainder inside it must read as
    # "nothing to spend" and never as a small budget.
    broke = json.loads(json.dumps(fresh))
    broke["today"]["remainder_bytes"] = FLOOR_BYTES
    check(
        "the floor leaves nothing to spend",
        gate(item, broke, open_at, stop_at, 2000.0)[0],
        "spent",
    )
    check(
        "and an open window with data is go",
        gate(item, fresh, open_at, stop_at, 2000.0)[0],
        "go",
    )
    check("every answer it gives is a named one", set(GATE_STATES) >= {
        gate(*args)[0]
        for args in (
            ([], None, open_at, stop_at, 0.0),
            (item, fresh, open_at, stop_at, 500.0),
            (item, fresh, open_at, stop_at, 2000.0),
            (item, None, open_at, stop_at, 2000.0),
            (item, stale_doc, open_at, stop_at, 2000.0),
            (item, broke, open_at, stop_at, 2000.0),
            (item, fresh, open_at, stop_at, 5000.0),
        )
    }, True)
    check("the faults are gate states", set(GATE_FAULTS) <= set(GATE_STATES), True)
    check("so are the two that download", set(GATE_GO) <= set(GATE_STATES), True)
    # Flying without a reading is a decision already taken, not a fault to be
    # reported and retried: counting it as one would exit non-zero and raise
    # the "runner blocked" notification on a run that is downloading fine.
    check("and a blind run is not a fault", set(GATE_GO) & set(GATE_FAULTS), set())

    # What a blind run may spend: what the queue declared, less what it has.
    # The front end asks the user about this exact number, so it is computed in
    # one place and the check is that the arithmetic is the obvious one.
    budget_items = [
        {"name": "40-a.py", "cap": 100 * MiB},
        {"name": "50-b.py", "cap": MiB},
    ]
    check(
        "a fresh queue's blind budget is what it declared",
        blind_budget(budget_items, {}),
        101 * MiB,
    )
    check(
        "and part of an item already here is not paid for twice",
        blind_budget(budget_items, {"items": {"40-a.py": {"part_bytes": 40 * MiB}}}),
        61 * MiB,
    )
    check(
        "an item over its own declaration adds nothing, never a negative",
        blind_budget(budget_items, {"items": {"50-b.py": {"part_bytes": 9 * MiB}}}),
        100 * MiB,
    )
    check("an empty queue costs nothing", blind_budget([], {}), 0)

    # The reaper. Its whole test is "past its stop time", and a blind run has
    # no stop time to be past — so the clock cannot answer and the lock has to.
    # Both mistakes are silent and opposite: reap a live blind download and the
    # bytes are bought again; never reap an orphan and it spends mobile data,
    # unwatched, until the phone is rebooted.
    killed: list[int] = []

    def _signalled(pgid: int, sig) -> None:
        """Record the group and answer as a group that is already gone.

        Which is what reap() is usually signalling, and it is also what stops
        this check paying the two-second TERM->KILL escalation four times over.
        """
        killed.append(pgid)
        raise ProcessLookupError

    saved = {name: globals()[name] for name in ("save_state", "log")}
    saved_killpg = os.killpg
    globals()["save_state"] = lambda state: None
    globals()["log"] = lambda message: None
    os.killpg = _signalled  # type: ignore[assignment]
    try:
        live = {"active_pgid": 4242, "active_stop_by": None}
        reap(dict(live))
        check("a blind run is left alone by the clock", killed, [])
        reap(dict(live), orphaned=True)
        check("and reaped once the lock says it is an orphan", killed, [4242])

        killed.clear()
        reap({"active_pgid": 77, "active_stop_by": now() + 3600})
        check("a nightly run inside its stop time is left alone", killed, [])
        reap({"active_pgid": 77, "active_stop_by": now() - 1})
        check("and reaped once it is past it", killed, [77])

        killed.clear()
        reap({}, orphaned=True)
        reap({"active_stop_by": None}, orphaned=True)
        check("nothing recorded is nothing to reap", killed, [])
        # An older state.json predates the key entirely, and must keep reading
        # as "no blind run here" rather than as one that may never be reaped.
        reap({"active_pgid": 9}, orphaned=True)
        check("a state file without the key is still reaped", killed, [9])
    finally:
        os.killpg = saved_killpg  # type: ignore[assignment]
        globals().update(saved)

    # What a blind run's "no deadline" actually reaches the item as. Three
    # spellings of one decision, and the reason they are made together: a
    # `timeout` the item does not know about kills it mid-write, and the log
    # line is the only place that would have said so.
    pinned = 1_785_758_400.0
    saved_now = globals()["now"]
    globals()["now"] = lambda: pinned
    try:
        wrapper, stop_epoch, until = spawn_plan(NO_DEADLINE)
        check("no deadline puts no timeout around the item", wrapper, [])
        check("and tells the item it has none", stop_epoch, "0")
        check("and says so in the log", until, "no stop")

        wrapper, stop_epoch, until = spawn_plan(pinned + 600)
        check("a deadline still wraps the item in timeout", wrapper[0], "timeout")
        check("with the seconds it has", wrapper[2], "600")
        check("and hands the item the epoch itself", stop_epoch, str(int(pinned + 600)))
        # The floor exists because a `timeout` of a second or less kills the
        # item before it has opened anything: a strike, and no progress to show
        # for it.
        check(
            "a deadline already passed still gets the floor",
            spawn_plan(pinned - 99)[0][2],
            "30",
        )
        # The session is Python's, not a `setsid` program's, and nothing here
        # may put one back: a spawned setsid has not run yet when Popen
        # returns, so the pgid read at that moment is the runner's own and
        # every later killpg would signal the runner instead of the download.
        check(
            "nothing spawns setsid, which would race the pgid",
            [word for words in (spawn_plan(NO_DEADLINE)[0], wrapper) for word in words],
            ["timeout", "--kill-after=15", "600"],
        )
    finally:
        globals()["now"] = saved_now

    # parse_item runs over every file in the queue at the top of every firing.
    # It has to answer, never raise: a photo saved as 20-holiday.png matches
    # the item naming and is not UTF-8, and the exception took the entire
    # night's queue with it — no heartbeat, no log line, nothing to say why.
    import tempfile

    def _write(path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    with tempfile.TemporaryDirectory() as raw:
        junk = Path(raw) / "20-holiday.png"
        junk.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xe3\xe3\xe3\xe3")
        parsed = parse_item(junk)
        check("a binary file is refused, not raised", bool(parsed.get("error")), True)
        check(
            "and is named for what is wrong with it",
            "not a text file" in (parsed.get("error") or ""),
            True,
        )
        missing = Path(raw) / "21-not-there.py"
        check(
            "so is one that is not there", bool(parse_item(missing).get("error")), True
        )
        # A title can hold anything; the item has to parse anyway.
        wide = Path(raw) / "22-unicode.py"
        wide.write_text(
            "# EXPIRE: v1\n# EXPECT_BYTES: 10\n# DESC: café — 日本語\n",
            encoding="utf-8",
        )
        check("a non-ASCII header still parses", parse_item(wide).get("error"), None)
        check(
            "DEST is carried off the header",
            parse_item(
                _write(
                    Path(raw) / "23-dest.py",
                    "# EXPIRE: v1\n# EXPECT_BYTES: 10\n# DEST: video\n",
                )
            ).get("dest"),
            "video",
        )
        check("and is empty when absent", parse_item(wide).get("dest"), "")

    # Where a finished download is put. Resolved at delivery from a kind, so
    # that changing the setting moves what is already queued; an absolute path
    # in the header overrides both.
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        saved_config, saved_out = CONFIG_FILE, OUT
        globals()["CONFIG_FILE"] = root / "config.json"
        globals()["OUT"] = root / "out"
        try:
            check("no DEST means do not move it", dest_of({"dest": ""}), None)
            check("a missing DEST is the same", dest_of({}), None)
            check(
                "an absolute path is taken literally",
                dest_of({"dest": str(root / "elsewhere")}),
                root / "elsewhere",
            )
            # `default_dests()` branches on on_termux(), so comparing a kind
            # against OUT pinned "this machine is not a phone" rather than
            # anything about destinations. It passed in a container and failed
            # on the phone -- the machine this whole package is for, and the one
            # the push gate runs on. Both branches are pinned instead, which is
            # what the check was always meant to say.
            saved_on_termux = globals()["on_termux"]
            try:
                globals()["on_termux"] = lambda: False
                check(
                    "off the phone a kind resolves to the queue's own out/",
                    dest_of({"dest": "video"}),
                    OUT,
                )
                globals()["on_termux"] = lambda: True
                check(
                    "on the phone it resolves to Android's Downloads",
                    dest_of({"dest": "video"}),
                    ANDROID_DOWNLOADS,
                )
            finally:
                globals()["on_termux"] = saved_on_termux
            save_config({"video_dir": str(root / "films")})
            check(
                "and follows it once it is set",
                dest_of({"dest": "video"}),
                root / "films",
            )
            # The default for the kind nobody configured, whichever machine this
            # is -- the property is that the two kinds are independent, not that
            # the default is any particular folder.
            check(
                "which the other kind does not share",
                dest_of({"dest": "file"}),
                default_dests()["file"],
            )
            check(
                "resetting puts the default back", load_config().get("file_dir"), None
            )

            # A destination that cannot be written is the phone without
            # termux-setup-storage, and it has to be caught before the data is
            # spent, not at the moment of delivery.
            check("a real directory is fine", dest_problem(root), None)
            check(
                "so is one that can still be made",
                dest_problem(root / "new"),
                None,
            )
            check(
                "one whose parent is absent is not",
                bool(dest_problem(root / "no" / "such" / "place")),
                True,
            )
            check(
                "and Android's says which permission is missing",
                "termux-setup-storage" in (dest_problem(ANDROID_DOWNLOADS) or "")
                if not on_termux()
                else True,
                True,
            )

            # Downloads is shared with every other app on the phone, so a name
            # that is taken must never be overwritten.
            (root / "a.txt").write_text("first", encoding="utf-8")
            check("a free name is used as is", free_name(root, "b.txt"), root / "b.txt")
            check(
                "a taken one gets Android's suffix",
                free_name(root, "a.txt"),
                root / "a (2).txt",
            )
            (root / "a (2).txt").write_text("second", encoding="utf-8")
            check("and keeps counting", free_name(root, "a.txt"), root / "a (3).txt")
            check(
                "a name with no extension still works",
                free_name(root, "a.txt").suffix,
                ".txt",
            )
            check("nothing was overwritten", (root / "a.txt").read_text(), "first")
        finally:
            globals()["CONFIG_FILE"], globals()["OUT"] = saved_config, saved_out

    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    """Entry point, single-instance."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--force", action="store_true", help="ignore the clock gate (testing)"
    )
    parser.add_argument(
        "--blind",
        action="store_true",
        help="run with no portal reading, on paid mobile data",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="explain the current decision and do nothing",
    )
    args = parser.parse_args(argv)

    if args.status:
        # Read-only, and deliberately outside the lock: "what is it doing right
        # now" must not answer "another firing holds the lock", which is the
        # one moment the question has a real answer.
        return report(force=args.force, blind=args.blind)

    ROOT.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # A firing is already in progress. Still reap, in case that one died —
        # but by the clock only, and never as an orphan: whatever is recorded
        # belongs to the runner holding the lock, which is downloading.
        reap(load_state())
        print("another firing holds the lock")
        return 0

    try:
        code = fire(force=args.force, blind=args.blind)
        if code:
            # Worth telling someone: the window closes at 00:00 UTC and a human
            # may be able to do something about it. The job stays armed either
            # way and tries again in ~15 minutes.
            notify(
                "Download queue blocked",
                "the runner could not proceed; see expire/logs/runner.log",
            )
        return code
    except KeyboardInterrupt:
        # The way a blind run is stopped, since nothing else will stop it. The
        # download it was on has already been signalled and closed its file by
        # the time this is reached; the item stays queued and resumes.
        log("interrupted; what is downloaded is kept and resumes")
        return 130
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


if __name__ == "__main__":
    # Ahead of main() because a self-test must not take the lock or touch the
    # heartbeat: a firing may well be in progress while this is run by hand.
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
