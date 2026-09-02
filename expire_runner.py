#!/data/data/com.termux/files/usr/bin/python3
"""Spend the day's expiring data allowance on a queue of wanted downloads.

The free grant of ~763 MiB lands at 00:00 UTC and whatever is left of it is
wiped at the next 00:00 UTC. This runner works through scripts dropped in
``~/dlq/queue`` in the hour before that deadline — ``dlq settings window``, an
hour unless it is told otherwise — so allowance that would evaporate gets spent
on something asked for instead.

Guarantees, in the order they matter
------------------------------------
1. **The reserve is left afterwards.** 100 MB unless ``dlq settings reserve``
   says otherwise. Enforced against ``today.remainder_bytes``, which the portal
   measures exactly, and re-checked live while a download runs — not merely
   predicted before it starts. The one thing that lifts it is being told to:
   ``settings reserve-when-paid no`` waives it for as long as the reading says
   paid data is there — at least ``settings paid-min`` of it, if a figure has
   been put on it — which is data the reserve was never protecting.
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
sum of those — the figure ``dlq run-now --blind`` says out loud and asks about
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
# quota_widget lives in the zwana-quota checkout: $ZWANA_HOME, a clone beside
# this one, or ~/zwana-quota. expire_sched._zwana_root predicts this same
# resolution so `dlq status` can say which checkout is missing instead of
# letting this import traceback.
_zwana = os.environ.get("ZWANA_HOME")
_beside = HERE.parent / "zwana-quota"
sys.path.insert(1, str(
    Path(_zwana).expanduser().resolve() if _zwana
    else (_beside.resolve() if _beside.is_dir() else Path.home() / "zwana-quota")
))

import quota_widget as qw  # noqa: E402  (from the zwana-quota checkout)
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

#: Everything must be dead this long before the deadline, leaving room for the
#: TERM->KILL escalation to complete before the grant expires.
STOP_MARGIN = 90

#: A single JobScheduler firing gets ~10 minutes before the platform stops it.
#: Budget nine, so an item is asked to stop rather than being cut off mid-write.
FIRING_SECONDS = 9 * 60

#: How often JobScheduler comes round, in seconds — its own periodic floor, and
#: what ``dlq arm`` asks for. A night is therefore not one long run but a row of
#: :data:`FIRING_SECONDS` slices, one per period, and the projection on the
#: screen has to work the night the same way or it would promise a whole
#: evening's throughput to a queue that will only ever be given nine minutes
#: at a time.
JOB_PERIOD = 900

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

#: The three destinations, and which command fills each. Separate because a
#: video, a song and an installer do not belong in the same place on a phone,
#: and one of them is usually wanted in Movies.
#:
#: ``audio`` was split out of ``video`` on 2026-08-28: an audio-only pick from
#: the format list is not a video and a music player is not looking where a
#: video player is. It is a *kind* rather than a rule about file extensions,
#: because what decides it is which row was chosen — and at queue time there
#: is no file yet to have an extension.
#:
#: Order is the order both front ends list them in, so the screen and the
#: command agree without either spelling it.
DEST_KINDS = ("video", "audio", "file")


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


def config_problem() -> str | None:
    """Why ``config.json`` cannot be read, or ``None`` if it can be.

    :func:`load_config` answers a file it cannot parse with an empty dict,
    which is right for a firing — a stray character in a file must not stop a
    night's downloads — and wrong for anything that *writes*: saving on top of
    an empty dict is saving a fresh file holding only the new key, and the
    destinations and settings that were in there are gone with a success line
    printed over them. So everything that sets asks this first and refuses,
    and the two screens and the dump say the same line rather than showing
    every setting reading "default" for a reason nothing states.

    A file that is not there is not a problem: nothing has been set yet, and
    that is what an empty config means everywhere else here. Neither is an
    empty one — a shell redirect leaves that, and there is nothing in it to
    lose.
    """
    try:
        raw = CONFIG_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        found = json.loads(raw)
    except ValueError as exc:
        return f"config.json will not parse: {exc}"
    # Valid JSON that is not an object is the same loss by another route:
    # load_config declines it exactly as quietly, and a save on top of it
    # would take the file with it.
    if not isinstance(found, dict):
        return f"config.json is a {type(found).__name__}, not a set of settings"
    return None


def save_config(config: dict) -> None:
    """Atomic, like the state file: a kill must not leave it half-written."""
    ROOT.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(CONFIG_FILE)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


#: The six things a person may change about how the queue spends, in the order
#: both front ends list them — so a screen and a command cannot disagree about
#: what there is to change. They live in the same ``config.json`` the
#: destinations do, and like the destinations they are read at the moment they
#: are used rather than captured at import: the screen sets them while a firing
#: is in progress, and the next reading has to see it.
#:
#: ``reserve`` is the user's hard floor — this much data must survive the
#: night's downloads. 100 MB by default because that is how the requirement was
#: given, in decimal MB because that is how it was stated. It was a constant
#: until it stopped being a fact about the phone and became a judgement about
#: the month; ``dlq settings reserve`` is where it is made now.
#:
#: ``window`` is how long before the reset the queue may start being worked. A
#: multiple of 15 minutes because a firing is a JobScheduler job that lands
#: about that often, so a window that is not a multiple of one buys nothing.
#:
#: ``paid-min`` is how much paid data has to be on the account before that
#: waiver applies. Zero — the default — is the rule the waiver had before there
#: was a figure to put on it at all: any paid data whatsoever. A figure above
#: zero is for the account whose last few MB of paid data are not worth the
#: reserve they would stand down.
#:
#: ``notify-blocked`` is whether a firing stopped by a fault says so on the
#: phone. That one notification only: an item that failed its last night goes
#: on notifying either way, because nothing else is ever going to mention it.
#:
#: The switches are spelled in their own words rather than in one shared pair,
#: because that is how each reads out loud: a reserve is kept or it is not,
#: automatic downloads are on or they are off, a blocked firing is announced or
#: it is not.
SETTINGS: dict[str, dict] = {
    "window": {
        "key": "window_minutes",
        "default": 60,
        "kind": "minutes",
        "words": None,
        "label": "how early downloads may start",
        "min": 15,
        "max": 1440,
        "step": 15,
    },
    "reserve": {
        "key": "reserve_mb",
        "default": 100,
        "kind": "mb",
        "words": None,
        "label": "data kept back, never spent",
        "min": 0,
        "max": 100_000,
        "step": 1,
    },
    "reserve-when-paid": {
        "key": "reserve_when_paid",
        "default": True,
        "kind": "bool",
        "words": ("yes", "no"),
        "label": "keep it when paid data is there",
    },
    "paid-min": {
        "key": "paid_min_mb",
        "default": 0,
        "kind": "mb",
        "words": None,
        "label": "paid data needed to waive it",
        "min": 0,
        "max": 100_000,
        "step": 1,
    },
    "auto": {
        "key": "auto",
        "default": True,
        "kind": "bool",
        "words": ("on", "off"),
        "label": "let the nightly job download",
    },
    "notify-blocked": {
        "key": "notify_blocked",
        "default": True,
        "kind": "bool",
        "words": ("on", "off"),
        "label": "say when a firing is blocked",
    },
}


def setting_problem(name: str, raw: object) -> str | None:
    """Why *raw* cannot be used as *name*, or ``None`` if it can.

    The one judge, asked of a value typed at a prompt and of a value found in
    ``config.json`` alike — a file that was hand-edited is exactly as likely to
    hold nonsense as a person is to type it, and two judges would eventually
    disagree about which nonsense is fine.

    Says what is wrong in the words the setting is set in, because the line
    goes to whoever typed it and "invalid value" tells them nothing they did
    not know.
    """
    spec = SETTINGS[name]
    if spec["kind"] == "bool":
        if isinstance(raw, bool):
            return None
        return f"{name} is {spec['words'][0]} or {spec['words'][1]}"

    unit = "minutes" if spec["kind"] == "minutes" else "MB"
    # bool is an int in Python and True would otherwise read as one minute.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return f"{name} is a whole number of {unit}"
    if not spec["min"] <= raw <= spec["max"]:
        return f"{name} is {spec['min']} to {spec['max']} {unit}"
    if raw % spec["step"]:
        return f"{name} is a multiple of {spec['step']} {unit}"
    return None


def setting_state(name: str) -> tuple[object, str | None, str]:
    """What ``config.json`` holds for *name*: ``(stored, why refused, where)``.

    *where* is ``"set"`` when the stored value is the one in force and
    ``"default"`` when the built-in one is, and *stored* is what the file
    holds — the evidence a refusal is reported with, since a phone spending by
    a figure nobody recognises is explained by the value, not by the fact that
    there was one.

    The one judge of a question three places used to answer for themselves:
    ``dlq settings``, ``dlq dump`` and the settings screen each decided
    whether a value was in force, two of them on ``config.get(key) is None``
    and one on ``key in config``, so a file holding ``null`` read as "set and
    refused" on the screen and as "default, nothing to say" from the command.
    It is keyed on the key being **present**: a stored ``null`` is a value
    somebody stored, and it is refused like any other value the setting does
    not take.

    A ``config.json`` that will not parse holds nothing anything can read, so
    every setting reads "default" here with nothing to say about it;
    :func:`config_problem` is the line that says why, printed once by each
    front end rather than once per setting.
    """
    spec = SETTINGS[name]
    config = load_config()
    if spec["key"] not in config:
        return None, None, "default"
    stored = config[spec["key"]]
    problem = setting_problem(name, stored)
    return stored, problem, "default" if problem else "set"


def settings() -> dict[str, object]:
    """Every setting's effective value, by name: what is set, or the default.

    **A stored value that fails its rule reads as the default.** It is not an
    error and it is never half-applied: this is read at the top of a firing
    nobody is watching, and a ``config.json`` somebody typed a stray character
    into must not be able to stop a night's downloads or, worse, take out the
    reserve on the way past. What is ignored is not silent — ``dlq settings``
    and ``dlq dump`` both name the value they are declining and why.
    """
    config = load_config()
    out: dict[str, object] = {}
    for name, spec in SETTINGS.items():
        found = config.get(spec["key"], spec["default"])
        out[name] = spec["default"] if setting_problem(name, found) else found
    return out


def parse_setting(name: str, text: str) -> object:
    """Turn typed *text* into a value for *name*, or raise ``ValueError``.

    Generous about how it is written and strict about what it means: the phone
    keyboard makes ``45m``, ``45 min`` and ``2h`` all likelier than the bare
    number, and a unit somebody bothered to type should never be the reason a
    setting does not take. The message on a refusal is the one
    :func:`setting_problem` would give, so a value typed and the same value
    found in the config file are complained about in the same words.

    The word ``default`` is *not* handled here: putting a setting back is
    removing the key, which is the caller's business, and parsing it into a
    value would write the default in as though it had been chosen.
    """
    spec = SETTINGS[name]
    said = text.strip().lower()
    if spec["kind"] == "bool":
        if said in ("on", "yes", "true", "1"):
            return True
        if said in ("off", "no", "false", "0"):
            return False
        raise ValueError(f"{name} is {spec['words'][0]} or {spec['words'][1]}")

    match = re.fullmatch(
        r"(\d+)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)?"
        if spec["kind"] == "minutes"
        else r"(\d+)\s*(mb|m)?",
        said,
    )
    if not match:
        unit = "minutes" if spec["kind"] == "minutes" else "MB"
        raise ValueError(f"{name} is a whole number of {unit}")
    value = int(match.group(1))
    if spec["kind"] == "minutes" and (match.group(2) or "").startswith("h"):
        value *= 60
    problem = setting_problem(name, value)
    if problem:
        raise ValueError(problem)
    return value


def spell_setting(name: str, value: object) -> str:
    """How *value* is written wherever it is shown: ``45 min``, ``100 MB``, ``on``.

    One spelling for the screen, the command, the status line and the dump,
    because a setting that reads ``60`` in one place and ``1h`` in another is
    two settings as far as anyone reading them is concerned.
    """
    spec = SETTINGS[name]
    if spec["kind"] == "bool":
        return spec["words"][0] if value else spec["words"][1]
    return f"{value} min" if spec["kind"] == "minutes" else f"{value} MB"


def window_seconds() -> int:
    """How long before the reset the queue may start being worked, in seconds."""
    return int(settings()["window"]) * 60


def reserve_bytes() -> int:
    """The configured reserve in bytes — what is kept back, never waived.

    Decimal MB, because the requirement was given in decimal MB and a phone
    plan is sold in them. This is the figure that was *set*; what applies to a
    particular reading is :func:`floor_bytes`.
    """
    return int(settings()["reserve"]) * 1_000_000


def reserve_waived(doc: dict | None) -> bool:
    """Whether *doc* is a reading the reserve stands aside for.

    Only with ``reserve-when-paid`` set to no **and** the portal saying there
    is at least ``paid-min`` of paid data left. Both halves matter: the setting
    alone waives nothing, or the floor would be gone on the nights it is the
    only thing left; and paid data alone waives nothing either, since keeping
    the reserve against it is the default and the whole point of the reserve
    for most people.

    Asked of the reading in hand rather than of the night, because paid data
    appears and goes while a download runs — a credit bought at 23:50 waives
    the reserve from the next poll, and the poll after it says so again.
    """
    values = settings()
    if values["reserve-when-paid"] or not doc:
        return False
    # ``paid-min`` at zero is the rule the waiver had before there was a figure
    # to put on it: any paid data at all. A byte is "at all", so the threshold
    # never falls below one — a threshold of zero would waive the reserve on a
    # reading that says there is no paid data, which is the opposite of asked.
    threshold = max(1, int(values["paid-min"]) * 1_000_000)
    # A reading with no paid figure at all is not a reading that says there is
    # paid data; it is one that has not said. Nothing is waived on a maybe.
    return int((doc.get("paid") or {}).get("left_bytes") or 0) >= threshold


def floor_bytes(doc: dict | None) -> int:
    """The reserve that applies to *this* reading: what may not be spent below.

    The figure every guard is enforced against — the budget before a run, the
    projection between portal polls, and the fresh reading each poll brings —
    so that all three answer to the same setting and to the same waiver.
    """
    return 0 if reserve_waived(doc) else reserve_bytes()


def auto_enabled() -> bool:
    """Whether the nightly job's firings may download.

    Off is a *when* rather than a refusal: the job stays armed and goes on
    firing, it just does nothing when it does, and ``dlq run-now`` still
    downloads because a person asking is not the schedule. Nothing about money
    is decided here — the reserve, the per-item caps and the portal reading go
    on deciding everything they decided.
    """
    return bool(settings()["auto"])


def notify_blocked_enabled() -> bool:
    """Whether a firing stopped by a fault is announced on the phone.

    Read at the moment the notification would be posted, like every other
    setting, and covering that notification alone: a malformed item and an
    item that has run out of nights go on notifying with this off, because
    nothing else is going to say so and neither of them repeats nightly. This
    one does — a phone that cannot see the portal says it every ~15 minutes —
    which is the whole reason there is a switch on it.
    """
    return bool(settings()["notify-blocked"])


# --------------------------------------------------------------------------- #
# Where downloads go
# --------------------------------------------------------------------------- #


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
    return deadline, deadline - window_seconds(), deadline - STOP_MARGIN


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

    * ``today.remainder_bytes - floor`` protects the user's reserve, which is
      100 MB unless ``dlq settings reserve`` says otherwise and is nothing at
      all on a reading that waives it. The remainder is measured by the portal,
      so this limit is exact and is the one that carries the guarantee.
    * ``free.left_bytes`` keeps the spending inside the *expiring* allowance
      rather than the paid reserve. It can only over-state, so it is
      discounted: a proportional haircut, plus whatever a stale reading could
      have burned since it was taken.
    """
    free = doc["free"]["left_bytes"]
    haircut = max(FREE_HAIRCUT_FRACTION * free, FREE_HAIRCUT_FLOOR)
    age_penalty = doc["reading"]["age_seconds"] * AGE_BURN_RATE
    from_free = free - haircut - age_penalty

    from_floor = doc["today"]["remainder_bytes"] - floor_bytes(doc) - FLOOR_MARGIN
    return int(max(0, min(from_free, from_floor)))


def blind_budget(items: list[dict], state: dict) -> int:
    """How many bytes a run with no portal reading may consume in total.

    With the portal unreachable there is no remainder, no floor and no expiring
    allowance — every figure :func:`spendable_bytes` works from is a portal
    figure. What is left is the only other number in the room: what each item
    declared it needs, less what it has already taken. That is a *cap enforced
    against the items* exactly as it is on a nightly run, never a promise
    believed, and it is deliberately the same figure ``dlq run-now --blind``
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
# What an item may take
# --------------------------------------------------------------------------- #

#: The one refusal that ends a firing rather than skipping an item: with under
#: three quarters of a minute left there is no time for anything behind it
#: either, so :func:`fire` stops the pass instead of walking the rest of the
#: queue. Spelled once, here, and recognised by :func:`fire` by its words —
#: the alternative is fire() keeping a second copy of the 45-second rule, and
#: the two answering differently on the night they disagree.
NO_TIME = "out of time for this firing"


def working_rate(ewma: float) -> float:
    """The throughput a slice is sized against: half what was measured.

    :data:`ewma_bps` is an average over the slices that have run, and the
    slices that ran are the ones that went well. What it is used for is the
    opposite question — will this item still be finished when the firing ends
    — and getting that wrong costs the whole item: for a whole download the
    bytes are spent and nothing is struck off the queue, and a slice sized to
    a night faster than tonight is one the item cannot deliver. Half is the
    margin for a link slower than its own average, and the floor under it is
    there so an interface that has been idle all month cannot project a night
    at a rate no link ever runs at.

    One function because :func:`fire` and :func:`plan` both ask it. The
    measured figure is what travels between them — ``snapshot()["bps"]`` is
    the EWMA itself — so a screen that halved it once more, or not at all,
    would be drawing its line for a night at a different speed from the one
    the runner is going to work.
    """
    return max(0.5 * ewma, 100 * 1024)


def admit(
    item: dict,
    record: dict,
    budget: int,
    rate: float,
    remaining_time: float,
    flying: bool,
    free_disk: int | None = None,
) -> tuple[int, str]:
    """(bytes this item may take in this firing, or 0 and why not).

    The per-item decision, lifted out of :func:`fire`'s loop so that a screen
    can project the night the runner would actually work. **It is one function
    at both ends** for the same reason the arm and cancel verbs are: a cut line
    drawn from a second copy of these rules would promise bytes the runner then
    refuses, and the queue would be committing more than the night will spend —
    which is the one thing the line exists to prevent.

    Pure. Every figure it decides on is handed to it, and the log lines stay
    in :func:`fire`, whose wording for a refusal *is* the text returned here.

    *record* is the item's row in ``state.json`` — only ``part_bytes`` is read
    from it. *rate* is the working rate, :func:`working_rate` of what was
    measured, because both callers have already made that conversion.
    *flying* is a blind run, where the budget is the items' own
    payload declarations and so must not be derated for wire overhead a second
    time. *free_disk* is optional because a projection has one reading for the
    whole night while a firing takes a fresh one per item: ``None`` means the
    disk was not asked about, which is not a reason to refuse an item.
    """
    if remaining_time < 45:
        return 0, NO_TIME
    if free_disk is not None and free_disk < item["cap"] + DISK_SPARE:
        return 0, f"only {human(free_disk)} disk free"

    slice_min = item.get("slice_min")
    if slice_min is None:
        slice_min = SLICE_MIN_BYTES

    if item.get("partial"):
        # A partial item is never refused for being too big; it is given a
        # slice and asked to come back. The slice is derated for wire overhead
        # so the item's own stop lands short of the runner's kill.
        done_already = int(record.get("part_bytes") or 0)
        need = max(0, item["cap"] - done_already)
        if need == 0:
            # Nothing to give it, and no rule below says so: with need at zero
            # the useful minimum is zero too, so the arithmetic would hand back
            # a slice of nothing with no reason attached to it.
            return 0, "nothing left to fetch"
        # The wire derate belongs to a budget that is measured on the wire —
        # the portal's remainder, the interface counters. A blind budget is the
        # items' own payload declarations added up, so derating it here would
        # charge the overhead twice: every partial item would be handed a slice
        # ~7% short of the size it declared and need another run to collect a
        # tail that was never really missing.
        by_budget = budget if flying else int(budget / WIRE_FACTOR)
        # What the clock allows is one of the limits, and only where there is a
        # clock: with no deadline the item is given the whole of what it still
        # needs and takes as long as that takes.
        limits = [need, by_budget]
        if remaining_time != NO_DEADLINE:
            limits.append(int(rate * max(0, remaining_time - 45)))
        cap = max(0, min(limits))

        # The minimum exists to stop nightly churn, but must not block a file
        # that is nearly finished.
        floor_slice = min(slice_min, need)
        if cap < floor_slice:
            left = (
                "no deadline"
                if remaining_time == NO_DEADLINE
                else f"{remaining_time:.0f}s left"
            )
            return 0, (
                f"slice {human(cap)} below the useful minimum "
                f"{human(floor_slice)} (budget {human(budget)}, {left})"
            )
        if cap == 0:
            # Only reachable for an item that declared no minimum at all: a
            # floor of nothing cannot catch a slice of nothing, and the answer
            # must still be a refusal with words on it. Spawning an item to
            # fetch zero bytes spends a firing to strike nothing off the queue.
            return 0, f"nothing left to spend on a slice ({human(budget)} left)"
        return cap, ""

    cap = item["cap"]
    if cap <= 0:
        # A whole item that declared nothing to fetch. Nothing is not more than
        # the budget and takes no time, so both tests below pass it and the cap
        # comes back as zero with an empty reason: `fire()` logged "skip x: "
        # with the sentence missing after the colon, and `plan()` handed the
        # screen a row carrying neither bytes nor a reason for having none.
        # Every refusal here is somebody's answer to "why did that not run".
        return 0, "nothing to fetch"
    if cap > budget:
        return 0, f"needs {human(cap)}, {human(budget)} spendable"
    predicted = cap / rate
    if predicted > remaining_time - 30:
        return 0, f"needs ~{predicted:.0f}s, {remaining_time:.0f}s left this firing"
    return cap, ""


def plan(
    items: list[dict],
    state: dict,
    budget: int,
    rate: float,
    seconds: float,
    flying: bool,
    free_disk: int | None = None,
) -> list[dict]:
    """Tonight's projection: ``[{"name", "bytes", "reason"}]`` in *items* order.

    The night as :func:`fire` would work it, in the order the items are given:
    firings of :data:`FIRING_SECONDS` apiece, one per :data:`JOB_PERIOD`, until
    *seconds* of night is used up; within a firing the queue in order, each item
    through :func:`admit` with the time this firing has left, the budget reduced
    by what was admitted and the clock by how long those bytes take at *rate*. A
    whole item, once admitted, is finished and is not offered again; a partial
    one's progress advances by what it got and it comes back for more in the
    next firing. A pass that admits nothing ends the night — nothing later will
    change the answer, since neither the budget nor the queue grows.

    A *seconds* of :data:`NO_DEADLINE` is the blind night: one pass, no clock,
    exactly as ``run-now --blind`` works the queue until the queue is done.

    *rate* is the throughput as measured — ``snapshot()["bps"]``, straight
    across — and :func:`working_rate` turns it into the one the night is
    worked at, here as in :func:`fire`.

    Pure — no clock, no disk, no config, no portal, and *state* is read and
    never written. The caller hands over every figure, which is what lets the
    screen ask "and if they were in this order instead?" of an ordering that
    exists nowhere but on the screen. What comes back is one entry per item in
    the order given: the bytes it would get across the whole night, and, for an
    item that gets none, the last reason it was turned down.

    The budget is the one thing an ordering cannot change: every item is
    admitted out of what is left, so the total is at most *budget* whichever
    way round the queue is put.
    """
    # The measured figure in, the working one out — the same conversion fire()
    # makes, and made here so that a front end hands over what it has rather
    # than a figure it had to know to halve.
    rate = working_rate(rate)
    # ``state.json``'s items are a mapping keyed by name; a snapshot's are a
    # list of the same items, and the two are easy to hand over the wrong way
    # round. A screen drawing a line must not traceback over that — the items
    # carry their own progress anyway, which is the half that matters here.
    records = (state or {}).get("items", {})
    if not isinstance(records, dict):
        records = {}

    # The simulation's own copy of what each item has already: a partial item's
    # progress moves across the night's firings, and state.json must not. Taken
    # from the item when it carries the figure — a snapshot's items do — and
    # from the state row otherwise, so either half of what snapshot() hands out
    # is enough to plan from.
    part: dict[str, int] = {}
    for item in items:
        stored = records.get(item["name"], {}).get("part_bytes")
        part[item["name"]] = int(item.get("part_bytes", stored) or 0)
    taken = {item["name"]: 0 for item in items}
    reasons = {item["name"]: "" for item in items}
    finished: set[str] = set()

    left = int(budget)
    elapsed = 0.0
    endless = seconds == NO_DEADLINE
    while True:
        if endless:
            remaining = NO_DEADLINE
        else:
            if elapsed >= seconds:
                break
            # The last firing of the night is cut short by the night, exactly
            # as fire() cuts it: firing_stop is the earlier of the stop time
            # and nine minutes from now.
            remaining = min(float(FIRING_SECONDS), seconds - elapsed)

        admitted = 0
        for position, item in enumerate(items):
            name = item["name"]
            if name in finished:
                continue
            got, why = admit(
                item,
                {"part_bytes": part[name]},
                left,
                rate,
                remaining,
                flying,
                free_disk,
            )
            if not got:
                if why == NO_TIME:
                    # This firing is over for everything, not just for this
                    # item — the same break fire() takes. Everything behind it
                    # is out of time for the same reason, and on the last
                    # firing of the night nothing else will ever say so.
                    for behind in items[position:]:
                        if not taken[behind["name"]]:
                            reasons[behind["name"]] = why
                    break
                reasons[name] = why
                continue
            admitted += got
            taken[name] += got
            left = max(0, left - got)
            part[name] += got
            if not item.get("partial"):
                finished.add(name)
            if not endless:
                remaining -= got / rate

        if not admitted or endless:
            break
        elapsed += JOB_PERIOD

    return [
        {
            "name": item["name"],
            "bytes": taken[item["name"]],
            "reason": "" if taken[item["name"]] else reasons[item["name"]],
        }
        for item in items
    ]


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
    and it is what ``dlq now`` has always passed.
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
    spelling of "no deadline", and the one ``dlq now`` has always used. The
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
    :func:`blind_budget` and in the question ``dlq`` asks before starting.

    The floor is re-asked of every fresh reading rather than worked out once at
    the start, because ``reserve-when-paid`` turns on a figure that moves: paid
    data bought mid-run waives the reserve from that poll on, and paid data
    spent to nothing puts it back. A floor decided before the first byte would
    be enforcing an answer to a question nobody asked tonight.
    """
    start_iface = iface_bytes()
    last_portal = now()
    remainder = doc["today"]["remainder_bytes"] if doc else None
    remainder_iface = start_iface
    hard_floor = floor_bytes(doc) + FLOOR_MARGIN
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
                hard_floor = floor_bytes(fresh) + FLOOR_MARGIN
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

    Returns where the files ended up, which is what ``dlq path`` reports.
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


def say_blocked() -> None:
    """Tell someone a firing was stopped by a fault, unless told not to.

    The one notification a setting can turn off, so the setting is asked in
    one place rather than at the call. Off leaves the log line exactly as it
    was: what goes quiet is the phone, never the record of why.
    """
    if not notify_blocked_enabled():
        return
    notify(
        "Download queue blocked",
        "the runner could not proceed; see expire/logs/runner.log",
    )


# --------------------------------------------------------------------------- #
# The firing
# --------------------------------------------------------------------------- #


#: Every answer :func:`gate` can give, in the order it checks them. ``go`` and
#: ``blind`` are the two that download; the rest are why not, and the status
#: screen says each of them in its own words.
GATE_STATES = (
    "off",
    "empty",
    "early",
    "late",
    "no-portal",
    "stale",
    "blind",
    "spent",
    "go",
)

#: The two that are a fault rather than a schedule: nothing is wrong with a
#: queue that is merely waiting, but a runner that cannot see the portal has
#: been stopped by something a person may be able to fix. ``blind`` is not one
#: of them — it is the same missing portal, already answered for by a human.
#: Neither is ``off``: a switch somebody set is the runner doing as it was
#: told, and notifying about it nightly would be the fastest way to teach
#: someone to ignore the one notification that matters.
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
    auto: bool = True,
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
    because that is the one that would have to be fixed first. *auto* is asked
    ahead of all of them, the empty queue included, because it is the answer to
    "why did nothing happen tonight" on every night it is off — a screen that
    says "queue empty" to someone who switched downloading off and forgot has
    told them the truth about the wrong thing.

    *force* overrides the two gates about *when* — the clock and the switch —
    and nothing else: they are both a schedule, and a person typing
    ``run-now`` is saying now. *blind* overrides the two portal gates and only
    those — it turns "there is no reading, so nothing starts" into "there is
    no reading, so this is mobile data", which is a thing only a person can
    decide and is asked for by name; it says nothing about the switch, because
    a missing portal is not a change of mind. Neither of them reaches the empty
    queue or the per-item caps, and neither reaches the money: the reserve, the
    budget and the caps decide what they decided. Note where *blind* does
    **not** appear: past the reading it never gets to, since a portal that
    answers is always preferred to guessing, so a blind run that finds the
    portal up is an ordinary run with the ordinary floor still enforced.
    """
    if not force and not auto:
        return "off", "automatic downloads are off"
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
    as declared — including the two a projection needs, the working time the
    night has left and the free disk, so that a front end can ask :func:`plan`
    what tonight would download without reading the clock a second time and
    getting a different night. How any of it is drawn is
    :func:`expire_sched.compose_status`, which lays out the same terminal
    ``dlq list`` does.
    """
    state = load_state()
    items, rejected = queued_items()
    doc, portal_problem = portal_now()
    # Asked for and actually flying blind are two different things: with the
    # portal up, --blind changes nothing and the screen must not claim it did.
    flying = blind and not usable(doc)
    deadline, window_open, stop_by = deadlines(doc, flying)
    current = now()
    verdict, detail = gate(
        items, doc, window_open, stop_by, current, force, blind, auto_enabled()
    )

    # The working time tonight's projection has. A firing that is going to
    # spend starts now (or when the window opens, if it has not yet) and stops
    # at the stop time; every other verdict is a night that downloads nothing,
    # and saying "no time" is how a projection says so without having to know
    # what any of the verdicts mean. A blind run has no stop time at all —
    # nothing may cut it short for the clock — which is the same NO_DEADLINE
    # the runner works to.
    if verdict == "blind":
        night_seconds: float = NO_DEADLINE
    elif verdict in ("go", "early", "spent"):
        night_seconds = max(0.0, stop_by - max(current, window_open))
    else:
        night_seconds = 0.0

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
        # The reserve as it applies to *this* reading, not as it is set: with
        # ``reserve-when-paid`` off and paid data there, the floor tonight's
        # run is flying against is nothing, and a screen still saying 100 MB
        # would be describing a different night. The setting itself is right
        # underneath, so both ends can say which of the two they mean.
        "floor_bytes": floor_bytes(doc),
        "reserve_waived": reserve_waived(doc),
        "settings": settings(),
        "bps": state.get("ewma_bps", BOOTSTRAP_BPS),
        "max_attempts": MAX_ATTEMPTS,
        # The two figures a projection needs and nothing else here wanted: how
        # much working time tonight has left, and how much disk there is to put
        # it on. Both are gathered where the verdict is, so that what a screen
        # plans and what the gate decided are the same night — a screen that
        # asked the clock again a second later could draw a line for a window
        # that has closed since.
        "night_seconds": night_seconds,
        "free_disk": shutil.disk_usage(str(ROOT)).free,
        "items": [
            {
                "name": item["name"],
                "cap": item["cap"],
                "partial": item["partial"],
                # Carried so a front end can hand the items straight back to
                # plan() in any order it likes, without a second reading of
                # state.json to find what each one already has.
                "slice_min": item["slice_min"],
                "part_bytes": int(
                    records.get(item["name"], {}).get("part_bytes") or 0
                ),
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

    verdict, detail = gate(
        items, doc, window_open, stop_by, current, force, blind, auto_enabled()
    )
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
        free_disk = shutil.disk_usage(str(ROOT)).free
        rate = working_rate(state.get("ewma_bps", BOOTSTRAP_BPS))

        # The decision itself is admit(), which is also what the screen draws
        # its cut line from — so a line saying an item gets bytes tonight is
        # the same answer this loop is about to give it. What stays here is
        # the wording: a refusal is logged as this loop has always logged it,
        # with the reason admit() hands back.
        cap, refusal = admit(
            item,
            state.get("items", {}).get(item["name"], {}),
            budget,
            rate,
            remaining_time,
            flying,
            free_disk,
        )
        if not cap:
            if refusal == NO_TIME:
                # Not this item's refusal but the firing's: there is no time
                # left for anything behind it either.
                log(f"{refusal}; the rest waits for the next")
                break
            log(f"skip {item['name']}: {refusal}")
            continue

        # Only now, so that an item that was never offered a byte does not
        # collect a row in state.json for having been walked past.
        record = state.setdefault("items", {}).setdefault(item["name"], {"attempts": 0})
        if item["partial"]:
            done_already = int(record.get("part_bytes") or 0)
            log(
                f"{item['name']}: slice {human(cap)} of "
                f"{human(max(0, item['cap'] - done_already))} still "
                f"needed ({human(done_already)} done)"
            )

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
    queue rows in particular have to agree with ``dlq list`` down to the
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
            # way and tries again in ~15 minutes — which is also why it is the
            # one notification ``settings notify-blocked off`` can silence.
            say_blocked()
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
    sys.exit(main())
