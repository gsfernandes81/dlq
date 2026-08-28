#!/data/data/com.termux/files/usr/bin/python3
"""Arm, inspect and tear down the expiring-allowance download queue.

The queue runs in the hour before 00:00 UTC, spending free data that would
otherwise be wiped at the reset. This script manages the Android JobScheduler
registration and draws the two screens; every decision about whether to
actually download lives in :mod:`expire_runner`, which the platform invokes
directly.

The two screens are :func:`compose_status` — what happens next, and what that
turns on — and :func:`compose_list`, where every download is and how much of it
is here. Both are laid out for a phone held in portrait, which is about 40
columns, and the self-test checks every line of both fits down to 32. That is
not a cosmetic rule: a line wider than the screen wraps, and the wrapping is
what pushes the answer off the top of it.

:mod:`expire_runner` draws its own ``--status`` through this module, so the
screen exists once however it is reached; what that end owns is the facts
(``expire_runner.snapshot``), and what this end owns is the layout.

Why JobScheduler, and what it costs
-----------------------------------
It is the only scheduler here that survives Termux being killed and, with
``--persisted``, a reboot — a userspace poll loop does not. The price is
precision: a job cannot be asked to fire at a wall-clock time, and Android may
defer it. That is paid for with a wide window and a job that fires repeatedly
inside it, and the runner tolerates firing late, early, twice or not at all.

Verified on this device: the scheduler honours a script's shebang, so the runner
is invoked as Python directly with no shell wrapper in between. That shebang
must name an absolute Termux path — ``#!/usr/bin/env python3`` is portable
everywhere except Android, which has no ``/usr/bin``, and it fails in the worst
possible way: exit 126 before Python starts, so the job "runs" nightly and
leaves no heartbeat, no log and no lock behind to say why. ``arm`` and
``status`` both check for it; see :func:`shebang_problem`.

Requires the Termux:API app as well as the ``termux-api`` package; without the
app every ``termux-*`` call hangs rather than failing.

Usage::

    dlq [status|list|ui|path NAME|dest|queue|logs|run-now|arm|cancel]

``dlq`` with nothing after it is ``dlq ui`` — see :func:`default_action`,
which falls back to ``status`` when there is no terminal to draw on.

``ui`` is where the queue is *changed* — reordered, renamed, retried, removed,
or told to download something now. Those used to be commands here and are not
any more: a queue is a list with an order, and every one of those actions is
easier to do to a download you are looking at than to a name you have to type
correctly first. What is left on the command line is the three read-only
answers, the settings and the job.

``dlq run-now --blind`` is the answer to ``ic.zwana.io`` being unreachable —
the phone on mobile data, or the portal down. The nightly guards are all read
off that portal, so a blind run has none of them and spends paid data instead;
it says how much and asks first, exactly as the screen's ``n`` does for one
item. It
is also the one run nothing interrupts for the clock, since the deadlines all
belong to an allowance it is not spending, so it runs in the foreground and
ctrl-c is what stops it.

``dlq`` is this file installed on PATH (see ``pyproject.toml``); running
``python3 expire_sched.py …`` from the checkout does exactly the same thing.

``NAME`` is name-ish: any unambiguous part of an item's file name, or its
priority number. :func:`match` decides, and ``dlq names`` prints the same list
the fish completions offer.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ytq lives in its own checkout now: $YTQ_HOME, a clone beside this one, or
# ~/ytq — the same three answers, in the same order, that ytq._sibling gives
# for finding this repo. One resolution here covers every module in this
# checkout that imports this one first (expire_ui deliberately does).
_ytq = os.environ.get("YTQ_HOME")
_beside = Path(__file__).resolve().parent.parent / "ytq"
sys.path.insert(1, str(
    Path(_ytq).expanduser().resolve() if _ytq
    else (_beside.resolve() if _beside.is_dir() else Path.home() / "ytq")
))

import ytq  # noqa: E402  (from the ytq checkout, path fixed up above)

#: The checkout, never this file's directory. Installed non-editable, this
#: module is a copy in site-packages, and every path below has to keep pointing
#: at the queue the nightly runner actually reads — the same anchoring
#: :func:`ytq._root` does, deliberately via the same answer so the two cannot
#: drift apart and arm a runner that watches a different queue.
ROOT = ytq.HERE
RUNNER = ROOT / "expire_runner.py"
QUEUE = ROOT / "queue"
LOGS = ROOT / "logs"
HEARTBEAT = ROOT / "heartbeat"

#: The rest of the runner's tree. Spelled from :data:`ROOT` rather than imported
#: so that listing a queue costs no import, and pinned to the runner's own
#: constants by :func:`_self_test` so the two cannot drift.
WORK = ROOT / "work"
OUT = ROOT / "out"
DONE = ROOT / "done"
FAILED = ROOT / "failed"
LOCK_FILE = ROOT / "runner.lock"

JOB_ID = 2400

#: The floor Android enforces since N; asking for less is silently clamped, so
#: this is as tight as the firing interval gets. A periodic job's cycle is
#: anchored to when it was registered, so arming shortly before the window
#: biases the daily firing into it — a bias only, since Doze can defer a firing
#: and a reboot re-anchors the cycle to boot time.
PERIOD_MS = 900_000


def api(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Call termux-job-scheduler, always with a timeout.

    The package alone is not enough: without the companion app installed the
    call blocks forever instead of failing, so nothing here may wait unbounded.
    """
    return subprocess.run(
        ["termux-job-scheduler", *args], capture_output=True, text=True, timeout=timeout
    )


def api_problem() -> str | None:
    """Why the scheduler cannot be reached, or ``None`` if it can.

    Returned rather than exited on, because the screen asks this question too
    and a screen cannot be answered with ``sys.exit``. :func:`require_api` is
    the command line's half of it.
    """
    try:
        if api("--pending", timeout=20).returncode != 0:
            return "termux-job-scheduler returned an error"
    except FileNotFoundError:
        return "termux-job-scheduler not found - pkg install termux-api"
    except subprocess.TimeoutExpired:
        return "Termux:API did not respond - is the Termux:API app installed?"
    except OSError as exc:
        return f"termux-job-scheduler failed: {exc}"
    return None


def require_api() -> None:
    """Fail clearly rather than hanging when Termux:API is not usable."""
    problem = api_problem()
    if problem:
        sys.exit(f"error: {problem}")


def shebang_problem() -> str | None:
    """Why the platform could not exec the runner, or ``None`` if it can.

    JobScheduler runs the script itself rather than passing it to a shell, so
    the first line decides whether anything happens at all. A shebang naming an
    interpreter that is not there fails with exit 126 before Python starts: no
    heartbeat, no ``runner.log``, not even the lock file being opened. Android
    counts the job as having run, so the queue goes quiet for days and every
    artefact a post-mortem would reach for is simply absent.

    ``#!/usr/bin/env python3`` is the specific trap. It is the portable form
    everywhere else and it is wrong here, because Android has no ``/usr/bin``
    at all — Termux's ``env`` lives under ``$PREFIX``.
    """
    if not RUNNER.exists():
        return (
            f"there is no runner at {RUNNER}; set EXPIRE_HOME to the "
            f"checkout holding queue/README.md"
        )
    first = RUNNER.read_text().splitlines()[0]
    if not first.startswith("#!"):
        return f"{RUNNER.name} has no shebang, so the scheduler cannot exec it"
    interpreter = first[2:].strip().split()[0]
    if not Path(interpreter).exists():
        return (
            f"{RUNNER.name} starts with '{first}' but {interpreter} does "
            f"not exist; the job would fire and die with exit 126. "
            f"Use '#!{sys.executable}'."
        )
    return None


def root_problem() -> str | None:
    """Why :data:`ROOT` is not a queue root, or ``None`` if it is.

    Only reachable via ``EXPIRE_HOME``, which is honoured blindly on purpose so
    a checkout can live anywhere. Pointed at the wrong directory it otherwise
    surfaces as an import traceback from the runner — which imports
    ``quota_widget`` from the zwana-quota checkout — and a traceback says
    nothing about the actual mistake.
    """
    if not (ROOT / "queue" / "README.md").is_file():
        return (
            f"{ROOT} is not a queue root: no queue/README.md in it. Check EXPIRE_HOME."
        )
    if not (_zwana_root() / "quota_widget.py").is_file():
        return (
            f"no quota_widget.py in {_zwana_root()}, which the runner "
            f"imports; clone zwana-quota beside the queue checkout or set "
            f"ZWANA_HOME"
        )
    return None


def _zwana_root() -> Path:
    """Where the runner will look for ``quota_widget``.

    Spelled from :data:`ROOT` rather than this file, because what imports it
    is ``ROOT/expire_runner.py`` — under ``EXPIRE_HOME`` that may not be this
    checkout — and the answer has to be the one THAT copy gives: $ZWANA_HOME,
    a clone beside the queue root, or ~/zwana-quota. expire_runner's own
    resolution is the original; this predicts it for a message that can name
    the missing checkout instead of surfacing as an import traceback.
    """
    override = os.environ.get("ZWANA_HOME")
    if override:
        return Path(override).expanduser().resolve()
    beside = ROOT.parent / "zwana-quota"
    if beside.is_dir():
        return beside.resolve()
    return Path.home() / "zwana-quota"


def do_arm() -> tuple[bool, str]:
    """Register the job. ``(it worked, what to say)``, printing nothing.

    ``--network any`` because the job is pointless without a connection, so let
    the platform hold it until there is one. ``--battery-not-low false`` because
    the default would let a low battery skip the only window of the day.
    ``--persisted true`` so a reboot does not silently end the arrangement.

    Every way this can fail comes back as a sentence rather than as an exit or
    an exception: the screen arms the job as readily as the command line does,
    and the two must not be able to disagree about whether it worked. What the
    scheduler itself says is passed through untouched — on the one machine this
    runs on it is the evidence, and paraphrasing it loses the reason.
    """
    problem = api_problem() or root_problem() or shebang_problem()
    if problem:
        return False, problem
    try:
        RUNNER.chmod(0o700)
    except OSError as exc:
        return False, f"could not make the runner executable: {exc}"
    result = api(
        "--script",
        str(RUNNER),
        "--job-id",
        str(JOB_ID),
        "--period-ms",
        str(PERIOD_MS),
        "--network",
        "any",
        "--battery-not-low",
        "false",
        "--storage-not-low",
        "false",
        "--charging",
        "false",
        "--persisted",
        "true",
    )
    said = (result.stdout.strip() or result.stderr.strip()).strip()
    if result.returncode:
        return False, said or "termux-job-scheduler refused the job"
    return True, said or "job registered"


def arm() -> None:
    """``dlq arm``: register the job, or stop with the reason it did not."""
    worked, said = do_arm()
    if not worked:
        sys.exit(f"error: {said}")
    print(said)


def do_cancel() -> tuple[bool, str]:
    """Unregister the job. ``(it worked, what to say)``, printing nothing."""
    problem = api_problem()
    if problem:
        return False, problem
    result = api("--cancel", "--job-id", str(JOB_ID))
    said = (result.stdout.strip() or result.stderr.strip()).strip()
    if result.returncode:
        return False, said or "termux-job-scheduler refused to cancel it"
    return True, said or "job cancelled"


def cancel() -> None:
    """``dlq cancel``: unregister it, or stop with the reason it did not."""
    worked, said = do_cancel()
    if not worked:
        sys.exit(f"error: {said}")
    print(said)


#: How :func:`job_rows` says the job is registered, and the only place that
#: word is spelled. ``expire_ui`` reads it to decide what its arm and
#: unregister keys say, and a second spelling would put "arm it" on a screen
#: whose own status line says it is armed — each of them right on its own.
ARMED = "armed"


def job_rows() -> list[tuple[str, str, str]]:
    """``(label, text, tone)`` for the job registration and the last firing.

    Summarised rather than dumped. ``--pending`` prints a paragraph per job,
    none of which fits a phone, and the whole of what it is asked here is
    "is our job still registered": if the answer is no, the queue is inert
    however good the rest of the screen looks. The raw text is kept only for
    the one case where it cannot be recognised, since then it is evidence.
    """
    try:
        pending = api("--pending", timeout=20).stdout or ""
    except subprocess.TimeoutExpired:
        return [("job", "Termux:API did not respond - is the app installed?", "1;31")]
    except FileNotFoundError:
        if _runner().on_termux():
            return [("job", "no termux-job-scheduler - pkg install termux-api", "1;31")]
        # Not a fault off the phone: nothing schedules anything here, which is
        # worth saying plainly rather than as a missing command.
        return [("job", "not armed here; the nightly job is the phone's", "90")]
    except OSError as exc:
        # Whatever else the platform does, this screen answers. It is the one
        # that gets opened when something is already wrong.
        return [("job", f"termux-job-scheduler failed: {exc}", "1;31")]

    if str(JOB_ID) in pending:
        every = PERIOD_MS // 60_000
        return [("job", f"{ARMED}, fires every {every}m", "32")]
    if pending.strip():
        return [
            ("job", f"not armed - {_me()} arm", "1;31"),
            ("pending", " ".join(pending.split()), "90"),
        ]
    return [("job", f"not armed - {_me()} arm", "1;31")]


def status() -> int:
    """Draw the status screen. Non-zero if something is broken.

    The composing is :func:`compose_status`, which the runner draws through as
    well; what belongs to this end is the job registration, because the
    scheduler is the half of the arrangement the runner cannot see.
    """
    paint = _paint()
    fatal = root_problem() or (shebang_problem() if not RUNNER.exists() else None)
    if fatal:
        # Nothing below this would be about the queue anyone means, so stop
        # rather than print a page of numbers from the wrong directory. The
        # root goes first here for the same reason: it is what is wrong.
        print(f"root   {_short(ROOT)}")
        print(paint("BROKEN", "1;31"))
        for line in _wrap(fatal, _width() - 2):
            print(f"  {line}")
        return 1

    problem = shebang_problem()
    machinery = job_rows()
    when, what = _last_firing()
    if when or what:
        machinery.append(("last run", f"{when} {what}".strip(), "90"))
    rows = compose_status(_runner().snapshot(), _width(), paint, job=machinery)
    for _, painted in rows:
        print(painted)
    if problem:
        print()
        print(paint("BROKEN", "1;31"))
        for line in _wrap(problem, _width() - 2):
            print(f"  {line}")
    return 1 if problem else 0


#: The stamp every log line starts with, split so the date can be lifted off.
STAMPED = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}Z  .*)$")


def tail(path: Path, lines: int) -> None:
    # errors="replace" because a log holds whatever a download printed into it,
    # and refusing to show the log is the worst possible answer to "why did it
    # fail" — which is the only reason anyone asks for it.
    if not path.exists():
        print(f"({path.name} not written yet)")
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    shown = text.splitlines()[-lines:]

    # A phone has about 40 columns and the date spends 11 of them saying the
    # same thing on every line. Lifted to a heading instead — but only when
    # every line on the screen really is that one day, because the log spans
    # nights and a wrong date is worse than a wide one.
    width = _width()
    stamps = [found for found in (STAMPED.match(line) for line in shown) if found]
    days = {found.group(1) for found in stamps}
    narrow = width < WIDE
    if narrow and len(days) == 1 and len(stamps) == len(shown):
        print(_paint()(days.pop(), "90"))
        shown = [found.group(2) for found in stamps]
    for line in shown:
        if not narrow:
            print(line)
            continue
        # Indented continuations, so a reason that runs to three lines still
        # reads as one entry rather than as three quarter-sentences.
        for part in _wrap(line, width, follow="  "):
            print(part)


# --------------------------------------------------------------------------- #
# What the queue holds
# --------------------------------------------------------------------------- #


def _runner():
    """The runner module, imported from the checkout rather than from beside us.

    Imported lazily, and only by the paths that need an item's declared cap:
    everything else here answers from the filesystem, and ``dlq names`` is run
    on every press of the tab key.

    The ``__main__`` check is for the other direction: ``expire_runner
    --status`` draws its screen through this module, and importing the runner
    by name from inside the running runner would build a *second* copy of it —
    same file, separate module, separate state — which is the kind of fault
    that shows up as two figures disagreeing and nothing to explain it.
    """
    main = sys.modules.get("__main__")
    if Path(getattr(main, "__file__", "") or "-").resolve() == RUNNER:
        return main
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import expire_runner

    return expire_runner


def _paths() -> list[tuple[str, Path]]:
    """``(where, path)`` for every item the queue knows about.

    Queued first, because that is the only state from which anything can still
    be told to run; then failed, which someone has to deal with; then done,
    which is history. ``done/`` is a tree of day directories, the other two are
    flat, and ``queue/.staging`` holds half-written files that are deliberately
    not items yet — one walk with one dot rule covers all three.
    """
    found: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for where, root in (("queued", QUEUE), ("failed", FAILED), ("done", DONE)):
        try:
            walked = sorted(root.rglob("*"))
        except OSError:
            continue
        for path in walked:
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            if not ytq.ITEM_RE.match(path.name) or path.name in seen:
                continue
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            seen.add(path.name)
            found.append((where, path))
    return found


def _is_payload(name: str) -> bool:
    """Whether a file is downloaded bytes rather than bookkeeping.

    Everything an item leaves beside its download: yt-dlp's fragment index, a
    merge in progress, ``expire_dl``'s validator sidecar, and the dotfiles both
    use to talk to the runner. None of it was paid for by the data allowance.
    """
    return not (
        name.startswith(".")
        or name.endswith((".ytdl", ".meta.json"))
        or ".temp." in name
    )


def _payload_bytes(work: Path) -> int:
    """How much of this item is actually on the disk.

    Measured from the files rather than read out of ``.status.json``, because
    that file is the item's own *claim*, written every few seconds and left
    behind by whatever killed it. The question "how much has been downloaded"
    has to be answered by the disk, or a report of 400 MiB survives a wipe of
    the 400 MiB.
    """
    total = 0
    try:
        walked = list(work.rglob("*"))
    except OSError:
        return 0
    for path in walked:
        if not _is_payload(path.name):
            continue
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _delivered(name: str, record: dict | None = None) -> list[Path]:
    """The finished files of this item, largest first.

    The record first, because once a download has been handed over to a shared
    folder nothing can find it by looking: Downloads is full of other people's
    files, and an item called ``video`` would match somebody else's
    ``video.mp4``. Scanning ``out/<item>/`` is the fallback for items that have
    not been handed over — no ``DEST``, or a destination that could not be
    reached — and that directory belongs to one item, so it is safe to scan.
    """
    found = [path for path in _noted(record) if _is_file(path)]
    if not found:
        try:
            found = [
                path
                for path in (OUT / name).iterdir()
                if path.is_file()
                and _is_payload(path.name)
                and ".part" not in path.name
            ]
        except OSError:
            return []
    return sorted(found, key=lambda path: -_size(path))


def _is_file(path: Path) -> bool:
    """``path.is_file()``, counting a refusal as "not something I can show you".

    ``Path.is_file`` answers False for a path that is not there and *raises*
    for one it is not allowed to look at — EACCES is not among the errors it
    swallows. On Android that is one revoked storage permission away: every
    delivered path recorded in ``state.json`` starts raising, and since this is
    reached from :func:`items`, what raises is every listing and every screen
    at once rather than one row on one of them.

    Whether the file is really there is not knowable from behind a closed door,
    and it is not this function's question either: :func:`_lost` draws that
    distinction, from the folder rather than from the file.
    """
    try:
        return path.is_file()
    except OSError:
        return False


def _size(path: Path) -> int:
    """How big it is, or nothing if it went between one look and the next."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _noted(record: dict | None) -> list[Path]:
    """Where the runner recorded this item's files, there or not.

    Separate from :func:`_delivered`, which answers what is on the disk now,
    because the two disagree exactly when something has happened worth saying:
    a file that was delivered and then deleted.
    """
    return [Path(raw) for raw in (record or {}).get("delivered") or []]


def _readable(folder: Path) -> bool:
    """Whether this folder's contents can actually be read.

    ``listdir`` rather than ``is_dir`` or ``os.access``, because the case this
    exists for answers yes to both of those and still cannot be read: on
    Android ``/storage/emulated/0/Download`` is *there* before
    ``termux-setup-storage`` has been run and the permission granted — it
    simply raises when you look inside. Anything concluding "the file is not in
    that folder" has to have looked, and this is what looking means.
    """
    try:
        os.listdir(folder)
    except OSError:
        return False
    return True


def _lost(noted: list[Path]) -> str:
    """Why a finished item has no files: ``gone``, or merely ``away``.

    Two different facts that look identical in a listing, and only one of them
    is about the file. A folder whose contents can be *read* and do not include
    it means it was deleted — by whoever wanted the space back, which is a
    normal thing to do with a film. A folder that cannot be read — the card
    out, the storage permission never granted, Android's Downloads raising
    rather than listing until ``termux-setup-storage`` has been run — says
    nothing whatever about the file, and calling that "gone" would write off
    every completed download on the phone on the strength of a permissions
    blip.

    So the distinction is drawn here, once, and everything that acts on a
    missing file acts on this answer rather than on ``files == []``.
    :func:`_readable` is what makes the difference between the two an
    observation rather than an assumption, which is what lets ``dlq ui``
    delete on the strength of it.
    """
    return "gone" if all(_readable(path.parent) for path in noted) else "away"


def _state_items() -> dict:
    """The runner's per-item records, read without importing the runner.

    ``dlq names`` runs on every press of the tab key and must not pay for
    ``quota_widget``; this is the one field it needs out of the state file.
    """
    try:
        found = json.loads((ROOT / "state.json").read_text(encoding="utf-8"))
        return found.get("items", {}) if isinstance(found, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def _stated_total(name: str) -> int:
    """The item's real size, if it has ever learned one from the server.

    Better than the declared cap for a percentage: the cap carries a deliberate
    margin over the measurement, so progress against it reads low and can never
    reach 100% — which is the one thing someone asking "is it nearly done"
    must not be told.
    """
    try:
        report = json.loads((WORK / name / ".status.json").read_text())
        return int(report.get("total_bytes") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def items() -> list[dict]:
    """Every download, with what is known about how far it got."""
    runner = _runner()
    state = runner.load_state().get("items", {})
    found = []
    for where, path in _paths():
        parsed = runner.parse_item(path)
        record = state.get(path.name, {})
        cap = parsed.get("cap") or 0
        stated = _stated_total(path.name)
        noted = _noted(record)
        files = _delivered(path.name, record)
        found.append(
            {
                "name": path.name,
                "path": path,
                "where": where,
                "cap": cap,
                "stated": stated,
                "total": stated or cap,
                "desc": parsed.get("desc") or path.name,
                "dest": parsed.get("dest") or "",
                "error": parsed.get("error"),
                "have": _payload_bytes(WORK / path.name),
                "files": files,
                # Where they were put, whether or not they are still there —
                # after delivery this record is the only thing that knows.
                "recorded": noted,
                # Only ever set on a finished item: a queued one with no files
                # has not lost anything, it has not started.
                "lost": "" if files or where != "done" else _lost(noted),
                "attempts": int(record.get("attempts") or 0),
                "last": record.get("last") or "",
            }
        )
    return found


def match(needle: str, names: list[str]) -> list[str]:
    """The names *needle* picks out, from the first tier of specificity that hits.

    Tiers rather than one pass, so that something which is exactly an item's
    name cannot be made ambiguous by another item merely containing it: an
    unlucky slug should never make a name un-typeable.
    """
    wanted = needle.strip().lower()
    if not wanted:
        return []
    numeric = wanted.isdigit()
    tiers = (
        [name for name in names if name.lower() == wanted],
        [name for name in names if Path(name).stem.lower() == wanted],
        [name for name in names if numeric and name.split("-", 1)[0] == wanted],
        [name for name in names if wanted in name.lower()],
    )
    for tier in tiers:
        if tier:
            return sorted(tier)
    return []


def resolve(needle: str) -> dict | None:
    """The one download *needle* names, or ``None`` after saying why not."""
    rows = items()
    hits = match(needle, [row["name"] for row in rows])
    if not hits:
        print(f"error: no download matches {needle!r}", file=sys.stderr)
        print(f"       {_me()} list shows every one by name", file=sys.stderr)
        return None
    if len(hits) > 1:
        print(f"error: {needle!r} matches {len(hits)} downloads:", file=sys.stderr)
        for name in hits:
            print(f"       {name}", file=sys.stderr)
        return None
    return next(row for row in rows if row["name"] == hits[0])


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def _width() -> int:
    """How many columns there are to lay out in.

    ``shutil`` honours ``COLUMNS`` before asking the terminal, which is what
    makes the layout testable without one. The fallback is 80 rather than
    anything phone-shaped on purpose: no terminal means a pipe or a file, and
    that is not something being read in portrait.
    """
    return max(24, shutil.get_terminal_size(fallback=(80, 24)).columns)


def colour_ok() -> bool:
    """Whether colour may be emitted at all.

    One decision, and public because the runner asks it too: ``expire_runner
    --status`` builds its own :class:`~quota_widget.Paint` (it holds the module
    already, and asking this one for it would import the runner from inside the
    runner) and must not answer this question differently.
    """
    return sys.stdout.isatty() and os.environ.get("TERM") not in (None, "", "dumb")


def _paint():
    """``quota_widget``'s Paint, on only when stdout is a terminal.

    Reused rather than respelled: when colour is safe to emit is one decision,
    already made once, in the module that had to make it first. What is *not*
    reused is its :func:`~quota_widget.grade` — see :func:`_tone`.

    Falls back to plain text if that module cannot be reached at all, because
    the messages that say the checkout is broken are printed by this same
    code, and they must not need a working checkout to come out.
    """
    try:
        return _runner().qw.Paint(colour_ok())
    except Exception:  # noqa: BLE001 - colour is never worth an exception
        return lambda text, code: text


def _state_of(row: dict, compact: bool = False) -> str:
    """The short answer to "is it done, and if not how far in is it"."""
    if row["error"]:
        return "!" if compact else "REJECTED"
    if row["files"]:
        return "done" if compact else "complete"
    # A finished download with nothing to show for it. Said rather than left to
    # fall through to "-", which is the answer for an item that has not started
    # — and under a "done" heading that reads as a download about to happen
    # again, which is the one thing this cannot be: the runner only ever looks
    # in queue/, and this item left it.
    if row.get("lost") == "gone":
        return "gone" if compact else "file gone"
    if row.get("lost") == "away":
        return "away" if compact else "folder away"
    if row["where"] == "failed":
        if not row["attempts"]:
            # Nothing to add: the heading above it already says failed, and a
            # placeholder for an attempt count nobody recorded reads as data.
            return "" if compact else "failed"
        return f"x{row['attempts']}" if compact else f"failed x{row['attempts']}"
    if not row["have"]:
        return "-"
    if not row["total"]:
        return "some" if compact else "started"
    return f"{min(99, int(row['have'] / row['total'] * 100))}%"


def _of(have: int, stated: int, cap: int, compact: bool = False) -> str:
    """``X of Y``, with Y marked as the bound it is when only the cap is known.

    Until a server has stated a size the one figure available is the item's
    declared cap, which is deliberately *larger* than the file. Printing that
    as though it were the size makes a finished download look 70% done.
    """
    bound = "" if stated else "≤"
    joiner = "/" if compact else " of "
    return f"{ytq.human(have)}{joiner}{bound}{ytq.human(stated or cap)}"


def _progress_of(row: dict, compact: bool = False) -> str:
    """What is on the disk, against what it is going to be."""
    if row["error"] or row.get("lost"):
        # Nothing true is left to put here: what it has is nothing, and what it
        # was going to be is a cap it already spent and delivered. The state
        # says what happened; a figure beside it would only be the cap again,
        # reading as a download still to come.
        return ""
    if row["files"]:
        return ytq.human(sum(_size(path) for path in row["files"]))
    if not row["total"]:
        return ytq.human(row["have"])
    return _of(row["have"], row["stated"], row["cap"], compact)


def _tone(row: dict) -> str:
    """The colour a download's state is worth saying in.

    Not :func:`quota_widget.grade`, which is the repo's other colour scale:
    that one grades *data remaining*, so green is a big number, and a download
    is the other way up — 90% is good news. One palette meaning two opposite
    things is worse than no palette.
    """
    if row["error"]:
        return "1;31"  # it will never run as it stands
    if row["files"]:
        return "32"  # here, and finished
    if row["where"] == "failed":
        return "31"
    if row["have"]:
        return "33"  # started, and not finished
    return "90"  # waiting its turn


def _display_name(name: str) -> str:
    """The item's name without its extension.

    Every item is a ``.py``, so the suffix distinguishes nothing and costs
    three of the columns a phone in portrait has not got. :func:`match` takes
    the stem as readily as the whole name, so what is shown is still what can
    be typed back.
    """
    return name[:-3] if name.endswith(".py") else name


#: One spelling of "clipped, and saying so", shared with the module that draws
#: the other half of this terminal.
_fit = ytq.fit


def _wrap(text: str, width: int, follow: str = "") -> list[str]:
    """*text* over as many lines as it needs, with long words left whole.

    The other half of :func:`_fit`, and used where clipping would be wrong:
    reasons, log lines and paths. A reason with its tail cut off is usually
    the half that said what to do about it.

    Long words are not broken, which means a single word wider than the
    terminal — and on a phone that is most absolute paths — overhangs rather
    than being split. That is deliberate: the terminal wrapping a path is
    visibly a wrapped path, whereas a break this code inserts reads as a path
    with a space in it, and these are the lines someone is about to retype.
    """
    return textwrap.wrap(
        text,
        max(8, width),
        subsequent_indent=follow,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [""]


#: Below this the description column goes. A phone in portrait is around 40
#: columns; this is the width at which a name, its state, its progress and a
#: useful amount of description stop fitting on one line together.
WIDE = 72


def compose_list(rows: list[dict], width: int, paint) -> list[tuple[str, str]]:
    """The whole listing as ``(plain, painted)`` pairs, headings included.

    Three shapes, because Termux in portrait is around 40 columns and the
    honest answer at that width is not simply a narrower table:

    * **wide** — name, state, progress and the description.
    * **narrow** — the description goes. It is mostly the title again, and the
      name is already a slug of the title.
    * **tight** — two lines, the name on one and its figures indented beneath.
      The name is what has to be typed back at ``dlq path``, so it is the last
      cell to give up room rather than the first: losing its tail makes two
      downloads look like the same one. It is still clipped if it alone is
      wider than the terminal, which nothing can help.

    Pairs rather than strings for the reason :func:`quota_widget.compose` does
    it: the width that has to be checked is the one with no escape codes in it.
    Columns are measured across every group at once, so they line up down the
    whole listing rather than jumping at each heading.
    """
    compact = width < WIDE
    cells = {
        row["name"]: (
            _display_name(row["name"]),
            _state_of(row, compact),
            _progress_of(row, compact),
            row["error"] or (row["files"][0].name if row["files"] else row["desc"]),
        )
        for row in rows
    }
    name_w = max((len(cell[0]) for cell in cells.values()), default=0)
    state_w = max((len(cell[1]) for cell in cells.values()), default=0)
    prog_w = max((len(cell[2]) for cell in cells.values()), default=0)

    one_line = 2 + name_w + 2 + state_w + 2 + prog_w
    tight = one_line > width
    note_w = 0 if compact else width - one_line - 2

    out: list[tuple[str, str]] = []
    for where in ("queued", "failed", "done"):
        group = [row for row in rows if row["where"] == where]
        if not group:
            continue
        heading = f"{where} ({len(group)})"
        if tight and out:
            # The groups are separated the same way the downloads inside them
            # are, or the first download of one reads as the last of the last.
            out.append(("", ""))
        out.append((heading, paint(heading, "1")))
        for number, row in enumerate(group):
            name, state, progress, note = cells[row["name"]]
            tone = _tone(row)
            if tight:
                # Both lines of a download sit at the same one indent, and what
                # separates one download from the next is a blank line rather
                # than a second level of it. A phone loses two columns to every
                # level, and an indent is a weak signal at 40 of them anyway:
                # the eye reads the gap.
                if number:
                    out.append(("", ""))
                figures = f"{state:>{state_w}}  "
                figures += _fit(progress, max(0, width - 2 - len(figures)))
                titled = f"  {_fit(name, width - 2)}"
                out.append((titled, titled))
                out.append(
                    (f"  {figures}".rstrip(), f"  {paint(figures, tone)}".rstrip())
                )
                continue
            head = f"  {name.ljust(name_w)}  "
            line = f"{head}{state:>{state_w}}  {progress}"
            painted = (
                f"{head}{paint(state.rjust(state_w), tone)}  {paint(progress, tone)}"
            )
            if note_w >= 14:
                clipped = _fit(note, note_w)
                line += f"  {clipped}"
                painted += f"  {paint(clipped, '90')}"
            out.append((line.rstrip(), painted.rstrip()))
    return out


def show_list() -> int:
    """Every download and how much of it is here."""
    rows = items()
    if not rows:
        print(f"nothing queued, done or failed in {ROOT}")
        return 0
    for _, painted in compose_list(rows, _width(), _paint()):
        print(painted)
    return 0


# --------------------------------------------------------------------------- #
# The status screen
# --------------------------------------------------------------------------- #

#: How recently an item must have written its progress file for the download to
#: be called live. It is rewritten every few seconds while one runs.
#:
#: Deliberately not "is the runner's lock held": taking that lock, even for the
#: instant it takes to test it, could make a firing starting in the same second
#: decide a run was already in progress and skip its slice. A status screen may
#: not cost the queue a slice to draw itself.
LIVE_SECONDS = 60

#: The runner's answer, in words, with the tone it is worth saying in. Keyed by
#: :data:`expire_runner.GATE_STATES` plus ``downloading``, which is not a gate
#: state — the gate says what the *next firing* would do, and this says what is
#: happening at this second, which outranks it.
#:
#: Short enough to fit 32 columns unwrapped, because this line is the whole
#: answer to the question the screen was opened to ask; everything under it is
#: the working.
VERDICTS = {
    "downloading": ("downloading now", "1;32"),
    "go": ("window open, downloading", "1;32"),
    "early": ("waiting for tonight", "37"),
    "late": ("done for tonight", "90"),
    "empty": ("nothing queued", "90"),
    "spent": ("no data to spend tonight", "33"),
    "blind": ("PAID: no portal, downloading", "1;33"),
    "no-portal": ("BLOCKED: portal not answering", "1;31"),
    "stale": ("BLOCKED: data reading is stale", "1;31"),
}


def _clock(epoch: float) -> str:
    """``23:00Z`` — the time, to the minute, in the zone the whole queue keeps.

    The runner's, not a second spelling: it writes the same times into the
    heartbeat this screen quotes back. UTC is marked rather than converted —
    the window, the grant and every line of the log are in it, and a screen
    quietly showing ship's time instead would agree with none of them. What
    the reader steers by is the countdown beside it.
    """
    return _runner().clock(epoch)


def _in(seconds: float) -> str:
    """``19h 37m`` / ``37m`` — quota_widget's countdown, on plain seconds."""
    return _runner().qw.countdown(dt.timedelta(seconds=max(0.0, seconds)))


def _short(path: Path) -> str:
    """*path* with ``$HOME`` written as ``~``.

    Termux's home is 33 columns before the checkout is reached, which is most
    of a phone screen spent saying the same thing every time.
    """
    home = str(Path.home())
    text = str(path)
    if text == home or text.startswith(home + os.sep):
        return f"~{text[len(home) :]}"
    return text


def _running_now(names: list[str]) -> str:
    """The item being downloaded at this second, or ``""``.

    Measured from the freshness of the item's own progress file, so it catches
    a ``dlq now`` in another terminal as readily as a nightly firing — both
    write it, and neither is visible in the job registration.
    """
    for name in names:
        try:
            age = time.time() - (WORK / name / ".status.json").stat().st_mtime
        except OSError:
            continue
        if 0 <= age < LIVE_SECONDS:
            return name
    return ""


def _last_firing() -> tuple[str, str]:
    """``(when, what)`` the last firing decided, from the heartbeat file."""
    try:
        text = HEARTBEAT.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "", ""
    head, _, rest = text.partition("  ")
    try:
        when = dt.datetime.strptime(head, "%Y-%m-%d %H:%M:%SZ").replace(
            tzinfo=dt.UTC
        )
    except ValueError:
        # A heartbeat this code did not write. Say it verbatim rather than
        # nothing: it is the only record of what the last firing thought.
        return "", text
    age = _runner().qw.since(time.time() - when.timestamp())
    return f"{when.strftime('%H:%MZ')} ({age})", rest.strip()


def compose_status(
    facts: dict,
    width: int | None = None,
    paint=None,
    job: list[tuple[str, str, str]] | None = None,
) -> list[tuple[str, str]]:
    """The status screen as ``(plain, painted)`` pairs, headings included.

    Written for a phone held in portrait, which is about 40 columns, and
    checked down to 32. That is not a narrower version of the old screen: four
    full timestamps and three byte counts laid out as ``label : value`` is a
    wall of figures at any width, and on a phone it is a wall of *wrapped*
    figures. So it is arranged as an answer instead —

    * the verdict, in one line: what happens next, and when;
    * the money, as the derivation it is: what is left, what expires, what is
      held back, and therefore what tonight may spend;
    * the queue, in the shape and with the figures ``dlq list`` uses, because
      two screens disagreeing about a download's progress is worse than either
      of them being wrong;
    * the machinery — job, last firing, root — last, since it is what you read
      only when one of the three above says something unexpected.

    Pairs rather than strings for the reason :func:`compose_list` does it: the
    width that has to be checked is the one with no escape codes in it.
    """
    width = width or _width()
    paint = paint or _paint()
    rows: list[tuple[str, str]] = []

    def row(plain: str = "", painted: str | None = None) -> None:
        rows.append(
            (plain.rstrip(), (plain if painted is None else painted).rstrip())
        )

    def heading(text: str) -> None:
        row(text, paint(text, "1"))

    def wrapped(text: str, tone: str = "", indent: str = "  ") -> None:
        """*text* over as many lines as it needs. Never clipped.

        Everything drawn this way is a reason — why nothing will run, why an
        item was refused — and a reason with its tail cut off is the half that
        says what to do about it.
        """
        for line in _wrap(text, width - len(indent)):
            row(f"{indent}{line}", f"{indent}{paint(line, tone)}" if tone else None)

    def pick(long: str, short: str, used: int) -> str:
        """The fuller phrasing when there is room for it, the shorter when not."""
        return long if used + len(long) <= width else short

    # ---- the verdict ------------------------------------------------------ #
    title = "DOWNLOAD QUEUE"
    when = _clock(facts["now"])
    gap = " " * max(1, width - len(title) - len(when))
    row(f"{title}{gap}{when}", f"{paint(title, '1;36')}{gap}{paint(when, '90')}")

    live = _running_now([item["name"] for item in facts["items"]])
    verdict = "downloading" if live else facts["verdict"]
    headline, tone = VERDICTS.get(verdict, (facts["detail"], ""))
    wrapped(headline, tone)

    if live:
        wrapped(_display_name(live), "90")
    if verdict != "empty":
        wrapped(_timing(facts), "90")
    if facts["forced"]:
        wrapped("--force: the window is being ignored", "90")

    doc = facts["portal"]
    if verdict == "empty":
        wrapped("ytq or dlq to add something", "90")
    elif verdict == "stale" and doc:
        wrapped(f"it is {doc['reading']['age_seconds']:.0f}s old", "90")
    # Nothing said here about why the portal is unreachable, or about which of
    # the two limits left nothing to spend: both are the next block's subject,
    # and it is three lines further down the same screen.

    # ---- the data --------------------------------------------------------- #
    row()
    heading("data")
    if doc is None and facts["blind"]:
        # The same missing reading as below, with the opposite consequence,
        # because someone answered for it. What replaces the portal's figures
        # is the queue's own declaration, so that is the figure shown — and it
        # is named as a ceiling, since an item that finishes early spends less.
        human = _runner().human
        wrapped("no reading: this spends mobile data", "1;33")
        wrapped(f"up to {human(facts['spendable'])}, from what it declared", "33")
        wrapped(facts["portal_problem"], "90")
    elif doc is None:
        wrapped("no reading, so nothing can be spent", "31")
        wrapped(facts["portal_problem"], "90")
        # The way out, said where the missing reading is already the subject
        # rather than up beside the verdict — which is the same reason the
        # verdict block says nothing about the portal. It belongs here for a
        # second reason too: the portal being unreachable is only the *verdict*
        # inside the window, and someone on mobile data at noon needs the line
        # as much as someone reading this at 23:40.
        wrapped(f"{_me()} run-now --blind spends mobile data instead", "90")
    else:
        human = _runner().human
        grade = _runner().qw.grade
        grant = max(1, doc["free"]["grant_bytes"])
        free = doc["free"]["left_bytes"]
        floor = facts["floor_bytes"]
        spendable = facts["spendable"]
        # Read top to bottom it is the arithmetic the runner does: what is
        # there, what of it dies at the reset, what is held back whatever
        # happens, and so what tonight is allowed to spend. Three numbers with
        # no relation stated between them was the old screen's worst line.
        entries = [
            (
                human(doc["today"]["remainder_bytes"]),
                "left today",
                "left today",
                "",
            ),
            (
                human(free),
                f"of it expires at {_clock(facts['deadline'])}",
                f"expires {_clock(facts['deadline'])}",
                grade(free / grant),
            ),
            (
                f"{floor // 1_000_000} MB",
                "is always kept back",
                "always kept back",
                "90",
            ),
            (
                human(spendable),
                "the queue may spend tonight",
                "to spend tonight",
                "32" if spendable > 0 else "31",
            ),
            (
                f"{human(facts['bps'])}/s",
                "measured download speed",
                "measured speed",
                "90",
            ),
        ]
        value_w = max(len(value) for value, _, _, _ in entries)
        for value, long, short, hue in entries:
            cell = value.rjust(value_w)
            note = pick(long, short, 2 + value_w + 1)
            row(
                f"  {cell} {note}",
                f"  {paint(cell, hue)} {paint(note, '90')}",
            )
        if not doc["reading"]["online"]:
            wrapped("the portal says this session is offline", "33")

    # ---- the queue -------------------------------------------------------- #
    row()
    rows.extend(_queue_rows(facts, width, paint))

    # ---- the machinery ---------------------------------------------------- #
    row()
    for label, text, hue in [*(job or []), ("root", _short(facts["root"]), "90")]:
        rows.extend(_labelled(label, text, hue, width, paint))
    return rows


def _timing(facts: dict) -> str:
    """The one line about the clock: when the window next changes state.

    A blind run has no next state to change to. Saying "closes in" of a run
    that does not close would be the one line on this screen that is simply
    untrue, and it is the line a reader steers by.
    """
    current, window_open, stop_by = facts["now"], facts["window_open"], facts["stop_by"]
    if stop_by == _runner().NO_DEADLINE:
        return "open until the queue is done"
    if current < window_open:
        return f"opens in {_in(window_open - current)} ({_clock(window_open)})"
    if current < stop_by:
        return f"open now, closes in {_in(stop_by - current)}"
    # Past the stop time, so the next window is tomorrow's: the deadline this
    # one hangs off is the reset that is minutes away.
    return f"opens again in {_in(window_open + 86_400 - current)}"


def _labelled(
    label: str, text: str, hue: str, width: int, paint
) -> list[tuple[str, str]]:
    """``label  text``, or the label and then the text indented under it.

    For the block whose values are paths and log lines: unpredictable, often
    longer than a phone screen, and never worth clipping.
    """
    pad = " " * max(1, 10 - len(label))
    if len(label) + len(pad) + len(text) <= width:
        return [(f"{label}{pad}{text}", f"{paint(label, '1')}{pad}{paint(text, hue)}")]
    out = [(label, paint(label, "1"))]
    for line in _wrap(text, width - 2):
        out.append((f"  {line}", f"  {paint(line, hue)}"))
    return out


def _queue_rows(facts: dict, width: int, paint) -> list[tuple[str, str]]:
    """The queued items, laid out as ``dlq list`` lays out the same downloads.

    Same shape and the same two helpers for the figures, so the two screens
    cannot disagree about how far in a download is. What this one adds is the
    fact only the runner holds: how many of an item's three nights are gone.
    """
    compact = width < WIDE
    items, rejected = facts["items"], facts["rejected"]
    out: list[tuple[str, str]] = []

    heading = f"queue ({len(items)})"
    if rejected:
        heading += f", {len(rejected)} rejected"
    out.append((heading, paint(heading, "1")))
    if not items and not rejected:
        out.append(("  empty", paint("  empty", "90")))

    cells = []
    for item in items:
        have = _payload_bytes(WORK / item["name"])
        stated = _stated_total(item["name"])
        row = {
            "error": None,
            "files": [],
            "where": "queued",
            "have": have,
            "stated": stated,
            "cap": item["cap"],
            "total": stated or item["cap"],
            "attempts": item["attempts"],
        }
        # Two spellings for the same fact, as ``ytq`` keeps two sets of hints:
        # this one says an item is one night from being set aside, and the
        # answer at a width it does not fit is a shorter way of saying it, not
        # a screen that stops saying it.
        counted = f"{item['attempts'] + 1}/{facts['max_attempts']}"
        tries = f"try {counted}" if item["attempts"] else ""
        cells.append(
            (
                _display_name(item["name"]),
                _state_of(row, compact),
                _progress_of(row, compact),
                tries,
                _tone(row),
                item["desc"],
                counted if item["attempts"] else "",
            )
        )

    name_w = max((len(cell[0]) for cell in cells), default=0)
    state_w = max((len(cell[1]) for cell in cells), default=0)
    prog_w = max((len(cell[2]) for cell in cells), default=0)
    tries_w = max((len(cell[3]) for cell in cells), default=0)
    tries_col = 2 + tries_w if tries_w else 0
    one_line = 2 + name_w + 2 + state_w + 2 + prog_w + tries_col
    tight = one_line > width
    # The description last and first to go, exactly as in the listing: it is
    # mostly the title again, and the name is already a slug of the title.
    note_w = 0 if compact else width - one_line - 2

    for number, (name, state, progress, tries, tone, desc, counted) in enumerate(cells):
        if tight:
            # One indent for both lines of a download, and a blank line between
            # downloads: the same shape the listing uses, for the same reason.
            if number:
                out.append(("", ""))
            figures = f"{state:>{state_w}}  {progress}"
            room = width - 2
            for marker in (f"  {tries}", f"  {counted}", ""):
                if marker.strip() and len(figures) + len(marker) <= room:
                    figures += marker
                    break
            figures = _fit(figures, room)
            titled = f"  {_fit(name, width - 2)}"
            out.append((titled, titled))
            out.append((f"  {figures}", f"  {paint(figures, tone)}"))
            continue
        head = f"  {name.ljust(name_w)}  "
        line = f"{head}{state:>{state_w}}  {progress.ljust(prog_w)}"
        painted = (
            f"{head}{paint(state.rjust(state_w), tone)}  "
            f"{paint(progress.ljust(prog_w), tone)}"
        )
        if tries_w:
            line += f"  {tries.ljust(tries_w)}"
            painted += f"  {paint(tries.ljust(tries_w), '33')}"
        if note_w >= 14:
            clipped = _fit(desc, note_w)
            line += f"  {clipped}"
            painted += f"  {paint(clipped, '90')}"
        out.append((line.rstrip(), painted.rstrip()))

    for item in rejected:
        if out[-1][0]:
            out.append(("", ""))
        titled = f"  {_fit(_display_name(item['name']), width - 2)}"
        out.append((titled, titled))
        # Word for word what `dlq list` calls it, and the reason in full: an
        # item is rejected for something about the file, and the reason is the
        # only thing on either screen that says what to do about it. It sits at
        # the download's own indent — the blank line above is what says it
        # belongs to the name, and the word REJECTED is what it says.
        said = f"REJECTED  {item['error'] or 'no reason recorded'}"
        for line in _wrap(said, width - 2):
            out.append((f"  {line}", f"  {paint(line, '1;31')}"))

    # Everything that is not queued lives in `dlq list`, and saying how much of
    # it there is stops this screen reading as the whole story.
    elsewhere = [where for where, _ in _paths() if where != "queued"]
    if elsewhere:
        counts = [
            f"{elsewhere.count(where)} {where}"
            for where in ("done", "failed")
            if elsewhere.count(where)
        ]
        if out[-1][0]:
            out.append(("", ""))
        # Wrapped rather than clipped: the half that would go is the command
        # that shows them, which is the only actionable part of the line.
        for line in _wrap(f"{', '.join(counts)} - {_me()} list", width - 2):
            out.append((f"  {line}", f"  {paint(line, '90')}"))
    return out


def show_names() -> int:
    """``name<TAB>state`` per download, which is fish's own completion format.

    Deliberately the cheap walk and not :func:`items`: this runs on every press
    of the tab key, and parsing every item's header to say "queued" would put
    the runner's import in front of the cursor.
    """
    noted = _state_items()
    for where, path in _paths():
        files = _delivered(path.name, noted.get(path.name))
        if files:
            detail = f"complete, {ytq.human(sum(f.stat().st_size for f in files))}"
        else:
            have = _payload_bytes(WORK / path.name)
            detail = f"{where}, {ytq.human(have)} here" if have else where
        print(f"{path.name}\t{detail}")
    return 0


# --------------------------------------------------------------------------- #
# Where downloads go
# --------------------------------------------------------------------------- #

#: Which command fills each destination, for the line that explains it.
FILLED_BY = {"video": "ytq", "audio": "ytq, audio only", "file": "dlq"}


def show_dest(argv: list[str]) -> int:
    """Show or set where finished downloads are put.

    Three of them, because a film, a song and an installer do not belong in
    the same folder on a phone. Resolved when the file is delivered rather
    than when it was queued, so changing one of these moves the things already
    waiting in the queue as well — otherwise it would not be a default, it
    would be a decision taken once and quietly kept.
    """
    runner = _runner()
    paint = _paint()
    if not argv:
        width = _width()
        config = runner.load_config()
        defaults = runner.default_dests()

        def note(text: str, tone: str) -> None:
            """A line under a destination, wrapped rather than run off the side.

            The one that matters is the missing-permission line, which is long,
            and which is the whole reason this command prints anything before
            the data is spent.
            """
            for line in _wrap(text, width - 2):
                print(f"  {paint(line, tone)}")

        # The kind as a heading with its facts under it, rather than the path
        # hung off a first column: on a phone the path is most of the line
        # already, and its second line would start further right than its
        # first. A blank line between the two kinds does what the indent did.
        for number, (kind, where) in enumerate(runner.dests().items()):
            if number:
                print()
            print(paint(kind, "1"))
            note(_short(where), "")
            note(
                f"{'set' if config.get(f'{kind}_dir') else 'default'}, "
                f"used by {FILLED_BY[kind]}",
                "90",
            )
            problem = runner.dest_problem(where)
            if problem:
                note(f"✗ {problem}", "31")
        print()
        # The command once, then the two forms under it: at 40 columns the
        # command name alone is a third of the line, and repeating it pushes
        # what each form does onto a wrap.
        #
        # `KIND` rather than the kinds spelled into both forms, which is what
        # this did while there were two of them: three made the longer form
        # exactly 40 columns, filling a phone's line edge to edge with no
        # margin, and a fourth would have wrapped it. The names go underneath
        # once, where adding one costs nothing.
        print(f"{_me()} dest")
        forms = [("KIND PATH", "change one"), ("KIND default", "put it back")]
        form_w = max(len(form) for form, _ in forms)
        for form, does in forms:
            print(f"  {form.ljust(form_w)}   {paint(does, '90')}")
        named = ", ".join(runner.DEST_KINDS[:-1]) + f" or {runner.DEST_KINDS[-1]}"
        for line in _wrap(f"KIND is {named}", width - 2):
            print(f"  {paint(line, '90')}")
        spelled = ", ".join(_short(p) for p in dict.fromkeys(defaults.values()))
        for line in _wrap(f"defaults: {spelled}", width - 2):
            print(f"  {paint(line, '90')}")
        return 0

    kind = argv[0]
    if kind not in runner.DEST_KINDS:
        print(
            f"error: {kind!r} is not a destination; "
            f"try {' or '.join(runner.DEST_KINDS)}",
            file=sys.stderr,
        )
        return 2
    if len(argv) < 2:
        print(f"usage: {_me()} dest {kind} PATH", file=sys.stderr)
        return 2

    worked, lines = set_dest(kind, argv[1])
    for line in lines:
        if worked:
            print(line)
        else:
            print(f"error: {line}", file=sys.stderr)
    return 0 if worked else 1


def set_dest(kind: str, value: str) -> tuple[bool, list[str]]:
    """Point *kind* at *value*, or at its default. ``(it worked, what to say)``.

    The deciding half of ``dlq dest``, split from the printing half because
    the screen sets destinations too and a second implementation of "is this
    directory usable" is a second answer to it. Nothing here prints or exits;
    what would have gone to stderr comes back as the last line.

    A directory that does not exist is created, one level and no more: a typo
    should not quietly build a tree nobody meant, and its parent missing is the
    signal that this is a typo rather than a new folder.
    """
    runner = _runner()
    config = runner.load_config()
    if value == "default":
        config.pop(f"{kind}_dir", None)
        runner.save_config(config)
        return True, [f"{kind} downloads go to {runner.dests()[kind]} again"]

    said: list[str] = []
    where = Path(value).expanduser()
    if not where.is_absolute():
        where = (Path.cwd() / where).resolve()
    if not where.is_dir():
        if not where.parent.is_dir():
            return False, [f"{where.parent} does not exist"]
        try:
            where.mkdir()
            said.append(f"created {where}")
        except OSError as exc:
            return False, [f"could not create {where}: {exc.strerror}"]
    problem = runner.dest_problem(where)
    if problem:
        return False, [problem]

    config[f"{kind}_dir"] = str(where)
    runner.save_config(config)
    return True, said + [
        f"{kind} downloads ({FILLED_BY[kind]}) now go to {where}",
        "this applies to what is already queued, not just what you queue next",
    ]


# --------------------------------------------------------------------------- #
# Where a download landed
# --------------------------------------------------------------------------- #


def show_path(row: dict) -> int:
    """Print where a download is.

    Only the path goes to stdout, so ``cd (dlq path x)`` works and the exit
    code says whether there is anything there; anything a human needs to know
    besides the path goes to stderr. Opening the file is ``dlq ui``'s ``o``,
    which is the screen that already has the download picked.
    """
    files = row["files"]
    if not files and row.get("lost"):
        # It finished. "Has not finished, and that is where it will land" would
        # be false twice over — the item is archived, so nothing is going to
        # land anywhere — and the path it would print is not the path the file
        # went to. What is printed is where it *was* put, which is still the
        # answer to the question asked, with the exit code saying it is not
        # there now.
        for path in row["recorded"]:
            print(path)
        if not row["recorded"]:
            print(OUT / row["name"])
        why = (
            "it is not there now"
            if row["lost"] == "gone"
            else "that folder cannot be reached from here"
        )
        print(
            f"note: {row['name']} finished and was delivered there; {why}",
            file=sys.stderr,
        )
        return 1
    if not files:
        print(OUT / row["name"])
        print(
            f"note: {row['name']} has not finished; that is where it will land",
            file=sys.stderr,
        )
        return 1
    for path in files:
        print(path)
    return 0


def _open(target: Path, quiet: bool = False) -> int:
    """Hand the file to Android, or to a desktop if this is not the phone.

    *quiet* is for ``dlq ui``, which calls this from inside curses: an opener
    writing to the terminal there draws over a screen it does not own, and
    nothing redraws it away. The caller reports the exit code instead.
    """
    for opener in ("termux-open", "xdg-open"):
        if shutil.which(opener) is None:
            continue
        try:
            return subprocess.run(
                [opener, str(target)], timeout=60, capture_output=quiet
            ).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            if not quiet:
                print(f"error: {opener} failed: {exc}", file=sys.stderr)
            return 1
    if not quiet:
        print(f"error: no termux-open or xdg-open to open {target}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- #
# Downloading one item now
# --------------------------------------------------------------------------- #


def _confirm(question: str) -> bool:
    """Ask before spending metered data. Only ever called with someone to ask."""
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def run_one(row: dict, assume_yes: bool = False) -> int:
    """Download one queued item now, outside the nightly window.

    Every guard the runner carries is about the *expiring* allowance: spend it
    before it is wiped, leave 100 MB behind, never reach into the paid reserve.
    Asking for a download now is asking to spend the paid reserve on purpose, so
    those guards do not apply here and — this is the part worth being explicit
    about — they are not quietly reimplemented as something weaker either. The
    item is run with the remainder of its own declared cap as its slice and no
    deadline, and the user is told the number first.

    What is kept is everything that makes it safe to stop: the same work
    directory, so ctrl-c resumes rather than restarts and the nightly window can
    finish what this started; the runner's lock, so this cannot race a firing
    into the same ``.part``; and the runner's own archive step, so a finished
    item leaves the queue exactly as a nightly one would.
    """
    runner = _runner()
    if row["error"]:
        print(
            f"error: {row['name']} is not a runnable item: {row['error']}",
            file=sys.stderr,
        )
        return 1
    if row["where"] != "queued":
        print(
            f"error: {row['name']} is in {row['where']}/, not the queue",
            file=sys.stderr,
        )
        if row["files"]:
            print(
                f"       it is already downloaded: {row['files'][0]}", file=sys.stderr
            )
        return 1

    remaining = row["cap"] - row["have"]
    if remaining <= 0:
        print(
            f"error: {row['name']} has already taken the {ytq.human(row['cap'])} "
            f"it declared; re-queue it with a larger EXPECT_BYTES",
            file=sys.stderr,
        )
        return 1

    paint = _paint()
    width = _width()
    narrow = width < WIDE
    # Short labels on a phone, so the line that says what this costs is not the
    # line that wraps. "of mobile data" stays at either width: it is the whole
    # warning, and it is what is dropped first if this is ever shortened again.
    label = 5 if narrow else 12
    here = "here".ljust(label)
    spend = ("spend" if narrow else "will spend").ljust(label)
    print(_fit(f"{_display_name(row['name'])}  {row['desc']}", width))
    if row["have"]:
        print(f"{here} : {_of(row['have'], row['stated'], row['cap'], narrow)}")
    # Bold amber, because this is the only line here that costs anything, and
    # it is the one a tired person scrolls past.
    cost = paint(f"{ytq.human(remaining)} of mobile data, now", "1;33")
    print(f"{spend} : {cost}")
    if not assume_yes:
        if not sys.stdin.isatty():
            # Not the same as being told no: nobody was asked. Said as an error
            # so an unattended caller cannot read silence as a refusal.
            print(
                "error: this spends data and there is nobody to ask; pass --yes",
                file=sys.stderr,
            )
            return 2
        if not _confirm("download it now?"):
            print("nothing downloaded")
            return 0

    handle = LOCK_FILE.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            "error: a nightly firing holds the lock; it is downloading already",
            file=sys.stderr,
        )
        return 1
    try:
        return _run_item(runner, row, remaining)
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def queue_run_argv(blind: bool) -> list[str]:
    """How the whole queue is run on purpose: the runner, forced, maybe blind.

    One spelling, because there are three callers — ``run-now``, ``run-now
    --blind``, and the screen's own key — and the difference between them must
    be the question they ask, never the run they get.

    ``--force`` is what makes "now" mean now. Without it the runner answers
    "not yet: window opens 23:00Z" and exits, which is the right answer to a
    *firing* and no answer at all to somebody who has just typed run-now: the
    window exists to schedule spending, not to forbid it. It overrides the
    clock gate and nothing else — the floor, the per-item caps and the portal
    reading are all still enforced, and are what actually keep this honest.

    ``--blind`` is only ever passed when the portal could not be read, and even
    then the runner re-checks: a reading that answers wins over a guess, so a
    blind run that finds the portal up is an ordinary run with the floor intact.
    """
    return [sys.executable, str(RUNNER), "--force"] + (["--blind"] if blind else [])


def run_blind(assume_yes: bool = False) -> int:
    """Fire the whole queue with no portal reading, on ordinary mobile data.

    What ``dlq now`` is for one item, this is for the queue, and for the same
    reason: every nightly guard is derived from ``ic.zwana.io`` — spend the
    allowance that is expiring, leave 100 MB of it behind, stay out of the paid
    reserve — and when the phone is on mobile data there is no portal to derive
    any of them from. So they do not apply, they are not quietly reimplemented
    as something weaker, and the number is said before anyone is asked.

    The number is :func:`expire_runner.blind_budget`, called rather than
    re-derived here, because the whole point of a confirmation is that the
    figure agreed to and the figure spent are the same one.

    Run in the foreground and not detached, because nothing else will stop it:
    a blind run has no deadline — the deadlines all belong to an allowance it
    is not spending — so ctrl-c is the way out, and the terminal is where it
    has to be reachable from.
    """
    runner = _runner()
    items, rejected = runner.queued_items()
    if not items:
        print(f"nothing queued in {_short(QUEUE)}")
        if rejected:
            print(f"({len(rejected)} rejected - {_me()} status says why)")
        return 0

    total = runner.blind_budget(items, runner.load_state())
    paint = _paint()
    narrow = _width() < WIDE
    # The same two-column shape `dlq now` uses, and the same rule about which
    # words survive a narrow screen: "of mobile data" is the whole warning.
    label = 5 if narrow else 12
    queued = ("queue" if narrow else "the queue").ljust(label)
    spend = ("spend" if narrow else "will spend").ljust(label)
    count = f"{len(items)} item{'' if len(items) == 1 else 's'}"
    amount = ytq.human(total)
    # A ceiling, not an estimate: an item that finishes inside its declaration
    # spends less, and none of them may spend more. Said with the listing's own
    # ``≤`` where the words for it will not fit.
    cost = f"≤{amount} mobile data" if narrow else f"up to {amount} of mobile data"
    print(f"{queued} : {count}, no portal{'' if narrow else ' reading'}")
    print(f"{spend} : {paint(cost, '1;33')}")
    if not assume_yes:
        if not sys.stdin.isatty():
            print(
                "error: this spends data and there is nobody to ask; pass --yes",
                file=sys.stderr,
            )
            return 2
        if not _confirm("run the queue now?"):
            print("nothing downloaded")
            return 0
    print("ctrl-c stops it; what is downloaded is kept and resumes")
    try:
        return subprocess.run(queue_run_argv(blind=True)).returncode
    except KeyboardInterrupt:
        # The runner got the same ctrl-c through the terminal's process group
        # and has already stopped its download and said so. Nothing to forward,
        # and a traceback here would bury the line that matters.
        return 130


def _run_item(runner, row: dict, slice_bytes: int) -> int:
    """Spawn the item, follow it, and dispose of it as the runner would."""
    work, out = WORK / row["name"], OUT / row["name"]
    for directory in (work, out, LOGS):
        directory.mkdir(parents=True, exist_ok=True)

    run_id = f"{int(time.time())}-{os.getpid()}"
    env = dict(os.environ)
    env.update(
        {
            "EXPIRE_BUDGET_BYTES": str(slice_bytes),
            "EXPIRE_SLICE_BYTES": str(slice_bytes),
            "EXPIRE_TOTAL_BYTES": str(row["cap"]),
            "EXPIRE_RUN_ID": run_id,
            # No stop time. The 00:00 UTC deadline exists to land inside the
            # expiring grant, and this run is deliberately outside it.
            "EXPIRE_STOP_EPOCH": "0",
            "EXPIRE_WORK": str(work),
            "EXPIRE_OUT": str(out),
        }
    )

    day = time.strftime("%Y-%m-%d", time.gmtime())
    log_path = LOGS / f"{day}-{row['name']}.log"
    launch = (
        [str(row["path"])]
        if os.access(row["path"], os.X_OK)
        else ["bash", str(row["path"])]
    )
    if _width() < WIDE:
        # The path is never clipped — it is the thing you would paste at a
        # pager — so it gets a line to itself rather than a label and a wrap.
        print("log :")
        print(log_path)
        print("ctrl-c stops it; progress is kept")
    else:
        print(f"log          : {log_path}")
        print("ctrl-c stops it; what is downloaded is kept and resumes")

    with log_path.open("a") as sink:
        sink.write(f"\n===== {stamp()} dlq now slice={slice_bytes} =====\n")
        sink.flush()
        # Deliberately no setsid: the item stays in this terminal's foreground
        # process group, so ctrl-c reaches it directly and it stops itself the
        # same cooperative way a deadline would, leaving a resumable file.
        child = subprocess.Popen(
            launch, stdout=sink, stderr=subprocess.STDOUT, env=env, cwd=str(work)
        )
        code = _follow(child, work / ".status.json", run_id, row)

    state = runner.load_state()
    record = state.setdefault("items", {}).setdefault(row["name"], {"attempts": 0})
    report = _report(work / ".status.json", run_id)
    if report:
        record["part_bytes"] = int(report.get("part_bytes") or 0)
    record["last"] = stamp()
    record["last_exit"] = code

    import contextlib
    import io

    paint = _paint()
    narrow = _width() < WIDE
    if code == 0:
        # archive() reports through the runner's log, which both appends to
        # runner.log and echoes to the terminal. The append is the record and
        # is kept; the echo is an 88-column line wrapping three times on a
        # phone immediately above the line that says the same thing better.
        with contextlib.redirect_stdout(io.StringIO()):
            runner.archive(
                {
                    "path": row["path"],
                    "name": row["name"],
                    # archive() hands the file to its destination, which it
                    # cannot do without knowing what that is.
                    "dest": row["dest"],
                },
                state,
            )
        runner.save_state(state)
        files = _delivered(row["name"], state.get("items", {}).get(row["name"]))
        landed = files[0] if files else out
        # The path is never clipped and never shuffled along by a label: it is
        # the thing you paste at mpv or a file manager.
        if narrow:
            print(paint("complete:", "1;32"))
            print(landed)
        else:
            print(paint("complete:", "1;32"), landed)
        return 0

    runner.save_state(state)
    if code == runner.EX_TEMPFAIL:
        # Re-read rather than reusing the row: the run itself is usually the
        # thing that first learned the file's real size.
        here = _of(_payload_bytes(work), _stated_total(row["name"]), row["cap"])
        print(paint("not finished:", "33"), f"{here} here")
        print("it stays queued, and the nightly window will carry on")
        return 0
    # No strike for a manual run: the item's three attempts are three *nights*,
    # and a ctrl-c must not be able to spend one of them.
    print(paint(f"failed: the item exited {code}", "1;31"), file=sys.stderr)
    print(f"see {log_path}", file=sys.stderr)
    return 1


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())


def _report(path: Path, run_id: str) -> dict | None:
    """The item's status file, but only if it is about this run.

    The run id is what tells tonight's report from one an item left behind
    before it died last week.
    """
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict) or report.get("run_id") != run_id:
        return None
    return report


def _follow(child: subprocess.Popen, status: Path, run_id: str, row: dict) -> int:
    """Draw the item's own progress while it runs, and wait for it."""
    live = sys.stdout.isatty()
    paint = _paint()
    # One column in hand: a line drawn right up to the last cell wraps, and a
    # wrapped progress line leaves a trail of them rather than redrawing.
    width = _width() - 1
    started = time.time()
    last = 0.0
    try:
        while child.poll() is None:
            time.sleep(1.0)
            report = _report(status, run_id)
            moment = time.time()
            if report is None or (not live and moment - last < 30):
                continue
            last = moment
            have = int(report.get("part_bytes") or 0)
            total = int(report.get("total_bytes") or 0) or row["total"]
            taken = int(report.get("payload_bytes_this_slice") or 0)
            rate = f"{ytq.human(taken / max(1.0, moment - started))}/s"
            done = f"{have / total * 100:.0f}%" if total else ""
            of = f" of {ytq.human(total)}" if total and width >= 40 else ""
            line = f"  {paint(done, '1;33')} {ytq.human(have)}{of}  {rate}"
            plain = f"  {done} {ytq.human(have)}{of}  {rate}"
            if len(plain) > width:
                line = plain = f"  {done} {ytq.human(have)}  {rate}"
            print(
                f"\r{line}{' ' * max(0, width - len(plain))}" if live else plain,
                end="" if live else "\n",
                flush=True,
            )
    except KeyboardInterrupt:
        # The child already got the same ctrl-c through the terminal's process
        # group; there is nothing to forward, only a progress line to stop
        # drawing over whatever it says on its way out.
        print("\ninterrupted; letting it close the file cleanly")
    if live:
        print()
    try:
        return child.wait(timeout=180)
    except subprocess.TimeoutExpired:
        child.kill()
        return child.wait()


def _fake_facts(verdict: str = "early", **changes) -> dict:
    """A :func:`expire_runner.snapshot` for a given verdict, with no portal.

    The reading itself comes from ``quota_widget._fake``, so the document this
    screen is drawn from has the shape :func:`quota_widget.derive` really
    produces rather than the shape this module imagines it does.
    """
    runner = _runner()
    current = 1_785_758_400.0  # 2026-08-03 12:00:00Z, pinned like the runner's
    deadline = current + 12 * 3600
    facts = {
        "root": ROOT,
        "now": current,
        "forced": False,
        "blind": verdict == "blind",
        "verdict": verdict,
        "detail": verdict,
        "deadline": deadline,
        "window_open": (
            current if verdict == "blind" else deadline - runner.WINDOW_SECONDS
        ),
        "stop_by": (
            runner.NO_DEADLINE
            if verdict == "blind"
            else deadline - runner.STOP_MARGIN
        ),
        "portal": (
            None
            if verdict in ("no-portal", "blind")
            else runner.qw._fake(1_200_000_000)
        ),
        "portal_problem": "no credentials: set zwana_username and "
        "zwana_password in ~/zwana-quota/.env (or export them)",
        "spendable": 0 if verdict == "spent" else 480 * 1024 * 1024,
        "floor_bytes": runner.FLOOR_BYTES,
        "bps": 800 * 1024,
        "max_attempts": runner.MAX_ATTEMPTS,
        "items": [],
        "rejected": [],
    }
    facts.update(changes)
    return facts


def _self_test() -> int:
    """Offline checks on the anchoring and the listing. No API, no network.

    Most of it guards one failure: a non-editable install puts this module in
    site-packages, and paths taken from ``__file__`` there would arm a runner
    that reads an empty queue beside itself. That fails silently — the job is
    registered, fires nightly, finds nothing, and says so in a heartbeat nobody
    reads. The same mistake is what the ``work/`` and ``out/`` checks below
    pin: a listing anchored anywhere else reports an empty queue rather than
    an error, which reads as "nothing is downloading" instead of "I am looking
    in the wrong place".
    """
    import contextlib
    import io
    import tempfile

    passed = failed = 0

    @contextlib.contextmanager
    def _quiet():
        """Capture a command's output, so checking it does not print it."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield out, err

    # Bound now, so a check made inside _quiet() still reports to the terminal
    # rather than into the buffer it is inspecting.
    terminal = sys.stdout

    def check(label: str, got, want) -> None:
        nonlocal passed, failed
        if got == want:
            passed += 1
        else:
            failed += 1
            print(f"FAIL {label}: got {got!r}, want {want!r}", file=terminal)

    def at_most(label: str, got: int, limit: int) -> None:
        """The same shape ``quota_widget`` uses for its own width checks."""
        nonlocal passed, failed
        if got <= limit:
            passed += 1
        else:
            failed += 1
            print(f"FAIL {label}: {got} exceeds {limit}", file=terminal)

    check("the root is ytq's root, not this file's dir", ROOT, ytq.HERE)
    check("the queue is the one ytq writes into", QUEUE, ytq.QUEUE)
    check("the runner is looked for in the root", RUNNER.parent, ROOT)
    check(
        "the root holds the queue contract",
        (ROOT / "queue" / "README.md").is_file(),
        True,
    )
    check("the runner is there", RUNNER.is_file(), True)
    check("the root passes its own check", root_problem(), None)

    # -- the dump, and the shim ------------------------------------------- #
    # dump exists to be run against broken trees, so what is pinned is that
    # it FINISHES whatever the tree holds, and that its evidence sections
    # come out. Run against the real root, whose contents this must not
    # depend on — the section headers and the root's own path are the
    # contract, not any particular item.
    with _quiet() as (out, _):
        code = dump()
    said = out.getvalue()
    check("dump finishes", code, 0)
    for want in ("== environment", "== roots", "== state.json", "== items", "== logs"):
        check(f"dump carries {want}", want in said, True)
    check("dump names the queue root", str(ROOT) in said, True)
    check("dump reports the gate in words", "gate" in said, True)

    # The compatibility shim: a pre-split item inserts only the queue root
    # and imports ytdl_item — the shim there must answer with the REAL module
    # from the ytq checkout, by replacing itself in sys.modules. Driven in a
    # subprocess because that is exactly how an item does it.
    repo = Path(__file__).resolve().parent
    check("the shim file exists at the queue root", (repo / "ytdl_item.py").is_file(), True)
    if (repo / "ytdl_item.py").is_file():
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "import ytdl_item; print(ytdl_item.__file__)",
                str(repo),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        check("a pre-split item's import survives the shim", probe.returncode, 0)
        landed = probe.stdout.strip()
        check(
            "and lands on the ytq checkout's module",
            landed.endswith("ytdl_item.py") and str(repo) not in landed,
            True,
        )

    saved_root = globals()["ROOT"]
    try:
        globals()["ROOT"] = ROOT / "queue"  # a real dir, but not a queue root
        problem = root_problem()
        check(
            "a wrong EXPIRE_HOME is named as such",
            bool(problem) and "EXPIRE_HOME" in problem,
            True,
        )
    finally:
        globals()["ROOT"] = saved_root

    # The shebang trap: an interpreter that does not exist means exit 126 with
    # no log, no heartbeat and no lock to explain it. The runner carries the
    # phone's shebang wherever the repo is checked out, so off Termux that
    # interpreter is legitimately absent and this cannot be asserted; what is
    # asserted there is that the objection is *that* one and no other, which is
    # the same check minus the fact about the machine running it.
    problem = shebang_problem()
    if ytq.shebang_here():
        check("the runner's shebang is executable here", problem, None)
    else:
        check(
            "the runner still carries the phone's shebang, and only that is "
            "missing here",
            (problem or "").startswith(f"{RUNNER.name} starts with '{ytq.SHEBANG}'"),
            True,
        )

    saved = globals()["RUNNER"]
    try:
        globals()["RUNNER"] = ROOT / "no-such-runner.py"
        problem = shebang_problem()
        check(
            "a missing runner is named as such",
            bool(problem) and "EXPIRE_HOME" in problem,
            True,
        )
    finally:
        globals()["RUNNER"] = saved

    # Deleted, or merely out of reach: one of those is about the file and the
    # other is about the phone, and only the first may ever be acted on.
    with tempfile.TemporaryDirectory() as raw:
        here = Path(raw)
        check("nothing recorded and nothing found is gone", _lost([]), "gone")
        check(
            "a folder that is there and has not got it: gone",
            _lost([here / "film.mp4"]),
            "gone",
        )
        check(
            "a folder that is not there at all: away, not gone",
            _lost([here / "no-such-card" / "film.mp4"]),
            "away",
        )
        check(
            "and one unreachable path is enough to hold the whole verdict",
            _lost([here / "film.mp4", here / "no-such-card" / "other.mp4"]),
            "away",
        )
        # The case the whole distinction exists for, and the one `is_dir` gets
        # wrong: on Android, Downloads is *there* before the storage permission
        # has been granted and raises only when something looks inside. If this
        # ever reads "gone", every finished download on the phone is deleted
        # from the queue's memory the first time somebody revokes it.
        locked = here / "locked"
        locked.mkdir()
        (locked / "film.mp4").write_text("x")
        os.chmod(locked, 0)
        try:
            check(
                "a folder that is there and unreadable is away",
                _lost([locked / "film.mp4"]),
                "away",
            )
            check("even though it is a directory", locked.is_dir(), True)
            check("and _readable is what tells them apart", _readable(locked), False)
        finally:
            os.chmod(locked, 0o755)
        check(
            "a folder that can be listed can be concluded from", _readable(locked), True
        )
        # And nothing may *raise* on the way to saying so. Path.is_file lets
        # EACCES through, and this is reached from items(), so one revoked
        # permission would take out every screen the queue has rather than one
        # row on one of them.
        os.chmod(locked, 0)
        try:
            check(
                "an unreadable file is not an exception",
                _is_file(locked / "film.mp4"),
                False,
            )
            check("and neither is its size", _size(locked / "film.mp4"), 0)
            check(
                "so a listing still comes out",
                _delivered("50-x.py", {"delivered": [str(locked / "film.mp4")]}),
                [],
            )
        finally:
            os.chmod(locked, 0o755)

    # The word the screen reads to decide whether the job is registered. Both
    # ends: "armed, fires every 15m" says yes, and every way of saying no has
    # to fail to start with it — "not armed - dlq arm" is the one that would
    # read as a yes to a check written with `in` instead of `startswith`.
    check(
        "the armed row says so first",
        f"{ARMED}, fires every 15m".startswith(ARMED),
        True,
    )
    for said in (
        "not armed - dlq arm",
        "not armed here; the nightly job is the phone's",
    ):
        check(f"{said!r} does not read as armed", said.startswith(ARMED), False)

    check("an unknown action is refused", _action(["nonsense"]), None)
    check("a bare command opens the screen", default_action(True), "ui")
    # The historical default, and still the one anything without a terminal
    # gets: `dlq | less`, an ssh command with no tty, a line in a script. A
    # curses app in any of those is a usage error where an answer used to be.
    check("off a terminal it is the status screen", default_action(False), "status")
    # Neither spelling of the default may be an action that *does* something:
    # the failure this pins is a bare `dlq` that arms the job or spends data.
    for interactive in (True, False):
        check(
            f"the default changes nothing (tty={interactive})",
            default_action(interactive) in ("ui", "status", "list"),
            True,
        )
    check("and it is a real action", _action([]) is not None, True)
    for action in ACTIONS + HIDDEN:
        check(f"{action} is dispatchable", _action([action]), action)
    check("--now is the option spelling of now", _action(["--now", "x"]), "now")
    check(
        "every named action is a real one",
        set(NAMED) <= set(ACTIONS) | set(HIDDEN),
        True,
    )
    # The one command in here that nothing types and everything spawns. It is
    # out of the usage block and out of the completions, and it still has to
    # dispatch: ytq's n key and dlq ui's are both `dlq now NAME --yes`.
    check("now is still dispatchable", _action(["now", "x"]), "now")
    check("but it is not offered as a command", "now" in ACTIONS, False)
    check("and open is gone; the screen opens files", "open" in ACTIONS, False)

    # The runner imports quota_widget from the zwana-quota checkout, so `dlq
    # status` only works where that checkout is reachable — which is exactly
    # what root_problem() reports when it is not.
    check(
        "quota_widget is reachable from the runner",
        (_zwana_root() / "quota_widget.py").is_file(),
        True,
    )

    # The runner is the one that decides where work/, out/, done/ and the lock
    # are. These are spelled again here so a listing costs no import, and this
    # is what stops the two spellings drifting into a `dlq list` that reports
    # an empty queue while the runner is downloading into a different tree.
    runner = _runner()
    check("the imported runner is the checkout's", runner.ROOT, ROOT)
    for label, ours, theirs in (
        ("queue", QUEUE, runner.QUEUE),
        ("work", WORK, runner.WORK),
        ("out", OUT, runner.OUT),
        ("done", DONE, runner.DONE),
        ("failed", FAILED, runner.FAILED),
        ("logs", LOGS, runner.LOGS),
        ("the lock", LOCK_FILE, runner.LOCK_FILE),
    ):
        check(f"{label} is where the runner puts it", ours, theirs)

    # Name-ish matching. The tiers exist so that a name which is exactly an
    # item cannot be made ambiguous by another item merely containing it.
    names = [
        "40-ubuntu.py",
        "50-ubuntu-server.py",
        "60-talk.py",
        "90-40-ubuntu.py-mirror.py",
    ]
    check(
        "a full name is exactly itself", match("40-ubuntu.py", names), ["40-ubuntu.py"]
    )
    check("the stem is enough", match("40-ubuntu", names), ["40-ubuntu.py"])
    check("so is the priority number", match("40", names), ["40-ubuntu.py"])
    check("a substring finds it", match("talk", names), ["60-talk.py"])
    check("case does not matter", match("60-TALK", names), ["60-talk.py"])
    check(
        "an ambiguous substring stays ambiguous",
        match("ubuntu", names),
        ["40-ubuntu.py", "50-ubuntu-server.py", "90-40-ubuntu.py-mirror.py"],
    )
    check("nothing matches nothing", match("nope", names), [])
    check("an empty needle matches nothing", match("  ", names), [])

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)

        def touch(path: Path, size: int) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as handle:
                handle.truncate(size)

        # Names nothing real would collide with: items() reads the runner's own
        # state.json, which on the phone has entries in it.
        live, idle = "41-selftest-live.py", "71-selftest-idle.py"
        gone, kept = "31-selftest-gone.py", "21-selftest-kept.py"

        # What is on the disk, against everything an item leaves beside it that
        # is not downloaded payload. Counting any of these would report data
        # that was never bought.
        work = root / "work" / live
        touch(work / "dl" / "v.mp4.part", 3000)
        touch(work / "dl" / "v.f137.mp4", 2000)
        check("parts and streams are payload", _payload_bytes(work), 5000)
        touch(work / ".status.json", 400)
        touch(work / ".ytdl.json", 400)
        touch(work / "dl" / "v.mp4.ytdl", 400)
        touch(work / "dl" / "v.temp.mp4", 5000)
        touch(work / "v.iso.part.meta.json", 400)
        check("bookkeeping is not payload", _payload_bytes(work), 5000)
        check("an item that has not started is 0", _payload_bytes(root / "nope"), 0)

        # The whole tree, through the module's own globals.
        keys = ("QUEUE", "WORK", "OUT", "DONE", "FAILED")
        saved = {name: globals()[name] for name in keys}
        globals().update(
            QUEUE=root / "queue",
            WORK=root / "work",
            OUT=root / "out",
            DONE=root / "done",
            FAILED=root / "failed",
        )
        try:
            header = "# EXPIRE: v1\n# EXPECT_BYTES: 10000\n# PARTIAL: yes\n# DESC: a\n"
            # Written non-executable deliberately: the runner rejects an item
            # whose shebang interpreter is missing, and these have to parse off
            # the phone as well as on it.
            for directory, name in (
                (QUEUE, live),
                (QUEUE, idle),
                (FAILED, gone),
                (DONE / "2026-08-01", kept),
            ):
                directory.mkdir(parents=True, exist_ok=True)
                (directory / name).write_text(header)
            touch(QUEUE / ".staging" / "81-halfwritten.py", 10)
            touch(QUEUE / "README.md", 10)
            touch(OUT / kept / "film.mp4", 9000)
            # A photo saved into the queue matches the item naming and is not
            # UTF-8. It used to take the whole listing down with a traceback,
            # and the runner with it; it has to be one rejected row instead.
            (QUEUE / "51-selftest-photo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xe3\xe3")

            photo = "51-selftest-photo.png"
            check(
                "queued first, then failed, then done, and nothing else",
                [(where, path.name) for where, path in _paths()],
                [
                    ("queued", live),
                    ("queued", photo),
                    ("queued", idle),
                    ("failed", gone),
                    ("done", kept),
                ],
            )

            rows = {row["name"]: row for row in items()}
            check(
                "a delivered item reads as complete",
                _state_of(rows[kept]),
                "complete",
            )
            check(
                "and reports the delivered file, not the leftovers",
                _progress_of(rows[kept]),
                ytq.human(9000),
            )
            # A finished download whose file was deleted. It cannot run again
            # -- the runner only ever looks in queue/, and this item left it --
            # so the whole risk here is that the listing implies it will: "-"
            # and a cap is exactly how a download that has not started reads.
            (OUT / kept / "film.mp4").unlink()
            after = {row["name"]: row for row in items()}
            check(
                "a deleted file is not a download waiting",
                _state_of(after[kept]),
                "file gone",
            )
            check(
                "and says so in four columns too", _state_of(after[kept], True), "gone"
            )
            check(
                "with no figure beside it that reads as a download to come",
                _progress_of(after[kept]),
                "",
            )
            check("it is still filed under done", after[kept]["where"], "done")
            # The property all of that rests on, pinned so it stays true: the
            # runner is handed queue/ and nothing else, so an archived item is
            # not a candidate however its file ends up.
            runner = _runner()
            saved_queue = runner.QUEUE
            try:
                runner.QUEUE = QUEUE
                good, bad = runner.queued_items()
            finally:
                runner.QUEUE = saved_queue
            check(
                "and deleting its file does not rearm it",
                kept in [item["name"] for item in good + bad],
                False,
            )
            touch(OUT / kept / "film.mp4", 9000)

            check("progress is a percentage", _state_of(rows[live]), "50%")
            # No server has stated a size yet, so the only figure available is
            # the declared cap, which is deliberately larger than the file.
            check(
                "an unmeasured total is shown as the bound it is",
                _progress_of(rows[live]),
                f"{ytq.human(5000)} of ≤{ytq.human(10000)}",
            )
            check("an item that has not started is not 0%", _state_of(rows[idle]), "-")
            check("a failed item says so", _state_of(rows[gone]), "failed")
            # The whole point of the rejected row: one bad file is one line,
            # not a traceback where the listing should have been.
            check(
                "a file that is not an item is one row",
                _state_of(rows[photo]),
                "REJECTED",
            )
            check(
                "and the row says what is wrong with it",
                "not a text file" in (rows[photo]["error"] or ""),
                True,
            )

            # The claim against the disk. A status file left behind by a kill
            # says whatever it last managed to write; the bytes are the bytes.
            (WORK / live / ".status.json").write_text(
                json.dumps({"part_bytes": 9_000_000, "total_bytes": 10000})
            )
            # Once a download is handed over to a shared folder, only the
            # record says which file was it — Downloads has other people's
            # files in it, and an item called "video" would find one.
            landed = Path(raw) / "Download" / "somebody-elses.mp4"
            landed.parent.mkdir(parents=True, exist_ok=True)
            touch(landed, 4242)
            check(
                "a recorded delivery is used, wherever it went",
                _delivered(kept, {"delivered": [str(landed)]}),
                [landed],
            )
            check(
                "a record pointing at a file that has been deleted falls back",
                _delivered(kept, {"delivered": [str(landed.parent / "gone.mp4")]}),
                [OUT / kept / "film.mp4"],
            )
            check(
                "and no record at all scans out/",
                _delivered(kept),
                [OUT / kept / "film.mp4"],
            )

            claimed = {row["name"]: row for row in items()}[live]
            check("the disk outranks the item's own claim", claimed["have"], 5000)
            check(
                "but the size it states is believed, nothing else knowing it",
                _stated_total(live),
                10000,
            )
            check(
                "and a measured total stops being shown as a bound",
                _progress_of(claimed),
                f"{ytq.human(5000)} of {ytq.human(10000)}",
            )

            # Not subscripted directly: resolve() may answer None, and a check
            # that crashes instead of failing reports nothing at all.
            # Every line of a listing has to fit the terminal it is printed
            # into, for the same reason quota_widget's face has to fit the
            # tile: a line wider than the screen is not an error, it is a
            # download whose progress wrapped onto a line of its own. 32 is a
            # floor well under anything real, not a target.
            plain = _runner().qw.Paint(False)
            longest = max(_display_name(row["name"]) for row in items())
            for width in (32, 40, 48, 56, 72, 80, 120):
                composed = [line for line, _ in compose_list(items(), width, plain)]
                at_most(
                    f"listing at {width}",
                    max((len(line) for line in composed), default=0),
                    width,
                )
                # The name is what has to be typed back at `dlq now`, so it is
                # the last cell to give up room and never the first: a clipped
                # one makes two downloads look like the same download.
                check(
                    f"the name is whole at {width}",
                    any(longest in line for line in composed),
                    True,
                )
            check(
                "the .py nobody has to type is not shown",
                any(
                    line.endswith(".py") for line, _ in compose_list(items(), 40, plain)
                ),
                False,
            )

            # ---- the status screen ---------------------------------------- #
            #
            # Same rule as the listing, and it bites harder here: a line too
            # wide for the terminal wraps, and every wrapped line pushes the
            # verdict — the one line the screen exists to show — further off
            # the top of a phone screen.
            runner = _runner()
            check(
                "every answer the gate can give has words for it",
                set(runner.GATE_STATES) <= set(VERDICTS),
                True,
            )
            demo_items = [
                {
                    "name": live,
                    "cap": 10000,
                    "partial": True,
                    "desc": "Ubuntu 24.04 desktop ISO",
                    "attempts": 1,
                },
                {
                    "name": idle,
                    "cap": 530_000_000,
                    "partial": True,
                    "desc": "a talk nobody has watched yet",
                    "attempts": 0,
                },
            ]
            demo_rejected = [
                {"name": photo, "error": "not a text file, so not an item"}
            ]
            demo_job = [
                ("job", "armed, fires every 15m", "32"),
                ("last run", "23:14Z (9m ago) queue empty", "90"),
            ]

            # A download in progress outranks whatever the gate would decide,
            # and it is read from the item's own progress file rather than
            # from the runner's lock: testing that lock could make a firing
            # starting in the same second think one was already under way.
            check("a live item is noticed", _running_now([live, idle]), live)
            check(
                "and a live download is what the screen leads with",
                compose_status(
                    _fake_facts("early", items=demo_items), 40, plain
                )[1][0].strip(),
                VERDICTS["downloading"][0],
            )
            os.utime(WORK / live / ".status.json", (0, time.time() - 3600))
            check("a stale progress file is not a download", _running_now([live]), "")
            check("and neither is a missing one", _running_now([idle]), "")

            for verdict in runner.GATE_STATES:
                facts = _fake_facts(
                    verdict,
                    items=[] if verdict == "empty" else demo_items,
                    rejected=[] if verdict == "empty" else demo_rejected,
                )
                for width in (32, 40, 48, 56, 72, 80, 120):
                    drawn = [
                        line
                        for line, _ in compose_status(facts, width, plain, job=demo_job)
                    ]
                    at_most(
                        f"status {verdict} at {width}",
                        max(len(line) for line in drawn),
                        width,
                    )
                    # Second line of the screen, at every width, unwrapped:
                    # the answer comes before its working, or the working is
                    # all a phone shows.
                    check(
                        f"status {verdict} at {width} leads with the verdict",
                        drawn[1].strip(),
                        VERDICTS[verdict][0],
                    )

            def said(verdict: str, width: int) -> str:
                """The screen as one string, so a wrap cannot hide a word.

                What is checked below is that the screen *says* something, not
                where it breaks the line saying it — the widths are already
                pinned by the at_most sweep above.
                """
                return " ".join(
                    line.strip()
                    for line, _ in compose_status(
                        _fake_facts(verdict, items=demo_items), width, plain
                    )
                )

            # A screen that says "blocked" and stops there leaves the reader
            # with the phone in their hand and nothing to do with it. The
            # ordinary cause is being on mobile data, and it has an answer, so
            # the screen carries the answer at every width.
            for width in (32, 40, 80):
                check(
                    f"a blocked screen says the way through at {width}",
                    "--blind" in said("no-portal", width),
                    True,
                )
                flying = said("blind", width)
                # Both halves, because either alone reads wrong: without the
                # cost it is a run like any other, and without the ceiling
                # there is no answer to "how much is this going to cost me".
                check(
                    f"and a blind one says what it is spending at {width}",
                    ("mobile data" in flying, "up to" in flying),
                    (True, True),
                )
                check(
                    f"never that nothing can be spent at {width}",
                    "nothing can be spent" in flying,
                    False,
                )

            # The two screens draw the same downloads. Disagreeing about how
            # far one is would leave nothing to believe: both are measured
            # from the disk, so they are made to share the measuring.
            figure = _of(5000, 10000, 10000, True)
            queued = _fake_facts("early", items=demo_items)
            check(
                "list and status agree on the figures",
                (
                    any(figure in line for line, _ in compose_list(items(), 40, plain)),
                    any(
                        figure in line
                        for line, _ in compose_status(queued, 40, plain)
                    ),
                ),
                (True, True),
            )
            # An item on its last night is the one fact here that no other
            # screen carries, so it survives every width: spelled out where
            # there is room, counted where there is not, dropped nowhere.
            for width, spelling in ((40, "try 2/3"), (32, "2/3")):
                check(
                    f"the nights an item has left are shown at {width}",
                    any(
                        spelling in line
                        for line, _ in compose_status(queued, width, plain)
                    ),
                    True,
                )

            saved_beat = globals()["HEARTBEAT"]
            try:
                globals()["HEARTBEAT"] = root / "heartbeat"
                check("no heartbeat is not an error", _last_firing(), ("", ""))
                HEARTBEAT.write_text("2026-08-03 11:00:00Z  queue empty\n")
                when, what = _last_firing()
                check(
                    "a heartbeat is dated to the minute",
                    when.startswith("11:00Z"),
                    True,
                )
                check("and says what was decided", what, "queue empty")
                HEARTBEAT.write_text("something else wrote this")
                check(
                    "an unrecognised heartbeat is quoted rather than dropped",
                    _last_firing(),
                    ("", "something else wrote this"),
                )
            finally:
                globals()["HEARTBEAT"] = saved_beat

            check("home is written as ~", _short(Path.home() / "dlq"), "~/dlq")
            check("a path outside it is left alone", _short(Path("/data/x")), "/data/x")

            # Nothing that wraps may invent a break inside a path: these lines
            # are read to find out where something is, and half a path with a
            # space in it is a path that does not exist.
            deep = "/storage/emulated/0/Download/some-very-long-name.mkv"
            check(
                "a path is never broken to fit",
                any(deep in line for line in _wrap(f"it went to {deep}", 32)),
                True,
            )

            # The log's date heading. Worth the two checks because getting it
            # wrong is silent: every line would carry a date it did not have.
            log_file = root / "runner.log"
            log_file.write_text(
                "2026-08-03 11:00:00Z  first\n2026-08-03 11:30:00Z  second\n"
            )
            saved_width = os.environ.get("COLUMNS")
            os.environ["COLUMNS"] = "40"
            try:
                with _quiet() as (out, _):
                    tail(log_file, 40)
                drawn = out.getvalue().splitlines()
                check("one day is lifted to a heading", drawn[0], "2026-08-03")
                check("and the lines lose it", drawn[1], "11:00:00Z  first")

                log_file.write_text(
                    "2026-08-03 23:50:00Z  first\n2026-08-04 00:01:00Z  second\n"
                )
                with _quiet() as (out, _):
                    tail(log_file, 40)
                check(
                    "two days keep their dates, since the queue runs at midnight",
                    out.getvalue().splitlines()[0],
                    "2026-08-03 23:50:00Z  first",
                )
            finally:
                if saved_width is None:
                    os.environ.pop("COLUMNS", None)
                else:
                    os.environ["COLUMNS"] = saved_width

            check("resolving picks one row", (resolve("41") or {}).get("name"), live)
            with _quiet() as (_, err):
                check("an unknown name resolves to nothing", resolve("zzz"), None)
                check(
                    "and says which command lists them",
                    "list" in err.getvalue(),
                    True,
                )
            with _quiet() as (out, _):
                # Non-zero, because `cp (dlq path x) .` must not quietly copy
                # nothing; the path itself is still printed, to say where.
                check(
                    "an unfinished download has no path yet",
                    show_path(rows[idle]),
                    1,
                )
                check(
                    "and stdout is only ever the path",
                    out.getvalue().strip(),
                    str(OUT / idle),
                )
            with _quiet() as (out, _):
                check("a finished one prints its file", show_path(rows[kept]), 0)
                check(
                    "which is the delivered file, not the directory",
                    out.getvalue().strip(),
                    str(OUT / kept / "film.mp4"),
                )

            # `run-now --blind` is the one command here that asks the user to
            # agree to a sum of money, so its two lines obey the same width
            # rule the screens do: the line carrying the figure is exactly the
            # one that must not wrap, and a big figure is what wraps it.
            # Done last, and through the runner's own globals, because it adds
            # an item that every listing check above counts.
            (QUEUE / "11-selftest-big.py").write_text(
                "# EXPIRE: v1\n# EXPECT_BYTES: 6000000000\n# DESC: a big one\n"
            )
            # CONFIG_FILE with them: the destinations are checked by setting
            # them, and the real one is the phone's own — a self-test that
            # writes it moves where finished downloads go.
            runner_keys = {
                "QUEUE": QUEUE,
                "STATE_FILE": root / "state.json",
                "CONFIG_FILE": root / "config.json",
            }
            was = {name: getattr(runner, name) for name in runner_keys}
            saved_width = os.environ.get("COLUMNS")
            try:
                for name, value in runner_keys.items():
                    setattr(runner, name, value)
                check(
                    "the figure agreed to is the runner's own",
                    runner.blind_budget(runner.queued_items()[0], {}),
                    6_000_020_000,
                )
                for width in (32, 40, 80):
                    os.environ["COLUMNS"] = str(width)
                    with _quiet() as (out, _):
                        # No tty and no --yes: it prints the offer, refuses to
                        # assume an answer, and starts nothing.
                        code = run_blind()
                    check(f"nobody to ask is not a yes at {width}", code, 2)
                    drawn = out.getvalue().splitlines()
                    at_most(
                        f"the blind offer at {width}",
                        max(len(line) for line in drawn),
                        width,
                    )
                    check(
                        f"and it says what it costs at {width}",
                        "mobile data" in " ".join(drawn),
                        True,
                    )
                # One spelling of "run the whole queue", and the term that
                # makes now mean now. Without --force the runner answers "not
                # yet: window opens 23:00Z" and exits, and the failure is a
                # command that looks like it ran and downloaded nothing.
                for blind in (False, True):
                    argv = queue_run_argv(blind)
                    check(
                        f"a run-now ignores the clock gate (blind={blind})",
                        "--force" in argv,
                        True,
                    )
                    check(
                        f"and runs the checkout's own runner (blind={blind})",
                        argv[1],
                        str(RUNNER),
                    )
                    check(
                        f"--blind is passed only when asked for (blind={blind})",
                        "--blind" in argv,
                        blind,
                    )

                # Every destination the runner knows has to have a command
                # named against it, or `dlq dest` and the screen both raise a
                # KeyError the moment somebody adds a kind — which is exactly
                # what adding `audio` on 2026-08-28 would have done.
                check(
                    "every destination says which command fills it",
                    sorted(FILLED_BY),
                    sorted(runner.DEST_KINDS),
                )
                # Audio is its own destination and not an alias for video: a
                # song delivered among the films is one the music player will
                # not offer. Its default is the same folder as the rest, which
                # on the phone is Android's Downloads.
                check("audio is a destination", "audio" in runner.DEST_KINDS, True)
                check(
                    "and defaults where the others do",
                    runner.default_dests()["audio"],
                    runner.default_dests()["video"],
                )
                # Set separately, or it is not a setting at all.
                sound = root / "songs"
                worked, _ = set_dest("audio", str(sound))
                check("audio is set on its own", worked, True)
                check("and moves only itself", runner.dests()["audio"], sound)
                check(
                    "leaving video where it was",
                    runner.dests()["video"],
                    runner.default_dests()["video"],
                )
                set_dest("audio", "default")

                # Setting a destination, through the one function both the
                # command and the screen call. A directory one level down is
                # made; two levels is the typo it looks like.
                target = root / "landing"
                worked, said = set_dest("video", str(target))
                check("a destination one level down is created", worked, True)
                check("and it is there", target.is_dir(), True)
                check(
                    "and it is what the queue will use",
                    runner.dests()["video"],
                    target,
                )
                check(
                    "and the change says it covers what is queued already",
                    any("already queued" in line for line in said),
                    True,
                )
                worked, said = set_dest("video", str(root / "no" / "such" / "place"))
                check("a missing parent is refused", worked, False)
                check(
                    "and says which parent",
                    any("does not exist" in line for line in said),
                    True,
                )
                check(
                    "and the destination is left where it was",
                    runner.dests()["video"],
                    target,
                )
                worked, _ = set_dest("video", "default")
                check("and the default can be put back", worked, True)
                check(
                    "which is the built-in one again",
                    runner.dests()["video"],
                    runner.default_dests()["video"],
                )
            finally:
                for name, value in was.items():
                    setattr(runner, name, value)
                (QUEUE / "11-selftest-big.py").unlink(missing_ok=True)
                if saved_width is None:
                    os.environ.pop("COLUMNS", None)
                else:
                    os.environ["COLUMNS"] = saved_width
        finally:
            globals().update(saved)

    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


#: Every action the dispatcher understands, with the one-line help the usage
#: block prints, in the order it prints them.
HELP = (
    ("status", "what happens next, and what it turns on"),
    ("list", "every download, and how much of it is here"),
    ("ui", "change it: reorder, rename, remove, retry, download now"),
    ("path NAME", "where a finished download landed"),
    ("dest", "show or set where finished downloads are put"),
    ("queue", "just the queued item files"),
    ("logs", "last 40 lines of the runner log"),
    ("dump", "everything a bug report needs, in one paste"),
    ("run-now", "run the whole queue now; --blind if the portal is unreachable"),
    ("arm", "register the nightly job"),
    ("cancel", "unregister it"),
)
ACTIONS = tuple(entry[0].split()[0] for entry in HELP)

#: Actions that take a name-ish argument.
NAMED = ("path", "now")

#: Not in the usage block, and neither of them is a command anybody types.
#:
#: ``names`` is shell completion's own entry point, a machine-readable spelling
#: of ``list``. ``now`` is the machine interface between the screens and the
#: runner: ``ytq``'s ``n`` key and ``dlq ui``'s both spawn ``dlq now NAME
#: --yes`` detached, because a download has to outlive the screen that started
#: it and the runner's lock has to be taken by whatever is downloading. The
#: asking that ``--yes`` skips happened on the screen, which is also where the
#: reachability of the portal is put in front of someone; a bare ``dlq now``
#: typed at a shell would ask a worse version of the same question.
HIDDEN = ("names", "now")

#: ``--now`` was asked for as an option and reads as one; ``dlq`` has no
#: options, only actions, so it is one of those wearing the other's clothes.
ALIASES = {"--now": "now", "--list": "list"}


def _me() -> str:
    """This command as it was invoked.

    ``dlq`` on PATH and ``expire_sched.py`` in the checkout — printing the
    other one sends people to the wrong place.

    The exceptions are the other front ends drawing this module's screens:
    the runner's own ``--status``, and ``expire_ui`` composing the status onto
    its queue screen. Every command spelled with this belongs to *this* file —
    ``list``, ``run-now``, ``arm`` — and neither of those files has any of
    them, so its name in front of one is an instruction that fails when it is
    followed. Hence the test is "was this module invoked", not "was some other
    module invoked": a front end that does not exist yet gets the right answer
    without having to be listed here.
    """
    name = Path(sys.argv[0]).name
    if name and name in (Path(__file__).name, "dlq"):
        return name
    return "dlq" if shutil.which("dlq") else Path(__file__).name


def dump(target: str | None = None) -> int:
    """One paste of everything a bug report needs. Plain lines, no fitting.

    Built for the moment a download fails on the phone and the fix is being
    worked out somewhere else entirely: the environment, how each sibling
    checkout resolved, what the gate thinks, the state rows, the head of
    each interesting item (its sys.path lines are usually the evidence), and
    the tails of the newest logs. Every section is guarded — this runs
    AGAINST broken trees, so a section that cannot be read says so and the
    rest still comes out.
    """

    def section(title: str) -> None:
        print(f"\n== {title}")

    def guarded(title: str, body) -> None:
        section(title)
        try:
            body()
        except Exception as exc:  # noqa: BLE001 - the dump must finish
            print(f"  (unreadable: {exc})")

    print(f"dlq dump  {stamp()}")

    def _environment() -> None:
        print(f"  python     : {sys.version.split()[0]}  {sys.executable}")
        for name in ("EXPIRE_HOME", "YTQ_HOME", "ZWANA_HOME"):
            value = os.environ.get(name)
            if value:
                print(f"  {name} = {value}")
        tool = shutil.which("yt-dlp") or "(not on PATH)"
        print(f"  yt-dlp     : {tool}")
        if tool != "(not on PATH)":
            try:
                said = subprocess.run(
                    [tool, "--version"], capture_output=True, text=True, timeout=15
                )
                print(f"  version    : {said.stdout.strip() or said.stderr.strip()}")
            except Exception as exc:  # noqa: BLE001
                print(f"  version    : (did not answer: {exc})")

    def _roots() -> None:
        print(f"  queue root : {ROOT}")
        print(f"    queue/README.md      : {(ROOT / 'queue' / 'README.md').is_file()}")
        print(f"    expire_runner.py     : {(ROOT / 'expire_runner.py').is_file()}")
        print(f"    ytdl_item.py shim    : {(ROOT / 'ytdl_item.py').is_file()}")
        ytq_dir = Path(ytq.__file__).resolve().parent
        print(f"  ytq        : {ytq_dir}")
        print(f"    ytdl_item.py         : {(ytq_dir / 'ytdl_item.py').is_file()}")
        zwana = _zwana_root()
        print(f"  zwana      : {zwana}")
        print(f"    quota_widget.py      : {(zwana / 'quota_widget.py').is_file()}")
        print(f"  gate       : {root_problem() or 'ok'}")
        print(f"  shebang    : {shebang_problem() or 'ok'}")

    def _state() -> None:
        raw = (ROOT / "state.json")
        if not raw.is_file():
            print("  (no state.json)")
            return
        state = json.loads(raw.read_text())
        for name, record in sorted(state.items()):
            if isinstance(record, dict):
                brief = {
                    key: record[key]
                    for key in ("strikes", "last_error", "error", "done", "delivered")
                    if key in record
                }
                print(f"  {name}: {brief if brief else record}")
            else:
                print(f"  {name}: {record}")

    def _items() -> None:
        rows = items()
        wanted = [
            row
            for row in rows
            if (target and target in row["name"])
            or (not target and (row["where"] == "failed" or row["error"]))
        ]
        if not wanted and not target:
            # Nothing failed: the queue's heads still say how items import.
            wanted = [row for row in rows if row["where"] == "queued"][:2]
        if not wanted:
            print("  (no matching item)")
        for row in wanted:
            print(f"  -- {row['name']}  where={row['where']}  error={row['error']}")
            try:
                head = row["path"].read_text(encoding="utf-8").splitlines()[:14]
                for line in head:
                    print(f"     {line}")
            except OSError as exc:
                print(f"     (unreadable: {exc})")

    def _logs() -> None:
        if not LOGS.is_dir():
            print("  (no logs directory)")
            return
        logs = sorted(LOGS.glob("*.log"), key=lambda p: p.stat().st_mtime)
        picked = [
            path
            for path in logs
            if target is None or target in path.name or path.name == "runner.log"
        ][-3:]
        if not picked:
            print("  (no logs)")
        for path in picked:
            print(f"  -- {path.name} (last 40 lines)")
            try:
                for line in path.read_text(errors="replace").splitlines()[-40:]:
                    print(f"     {line}")
            except OSError as exc:
                print(f"     (unreadable: {exc})")

    guarded("environment", _environment)
    guarded("roots", _roots)
    guarded("state.json", _state)
    guarded("items", _items)
    guarded("logs", _logs)
    return 0


def default_action(interactive: bool | None = None) -> str:
    """What a bare ``dlq`` does: open the screen.

    The screen is where the queue is worked on, so typing the command with
    nothing after it lands there rather than on a page of figures — and every
    read-only answer is a key away from it, which is not true the other way
    round.

    Off a terminal it is ``status`` instead, which is what a bare ``dlq``
    always did. That is not a nicety: ``dlq | less``, an ssh command with no
    tty, a line in a script — all of those used to print the status screen, and
    curses in any of them is a usage error where there used to be an answer.
    The default may never be an action that *does* something either; that is
    what the self-test pins, because the failure would be a bare command that
    arms the job or spends data.
    """
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    return "ui" if interactive else "status"


def _action(argv: list[str]) -> str | None:
    """The action ``argv`` selects: :func:`default_action` if there is none."""
    action = argv[0] if argv else default_action()
    action = ALIASES.get(action, action)
    return action if action in ACTIONS or action in HIDDEN else None


def usage() -> int:
    """The commands, in as much detail as the terminal has room for.

    A phone in portrait gets the names alone rather than a wrapped blurb per
    command: twenty lines of help scrolled off the top is not more helpful
    than ten, and the docs are where the detail actually lives.
    """
    width = _width()
    paint = _paint()
    print(f"usage: {_me()} [command] [NAME]", file=sys.stderr)
    if width >= 54:
        # Wrapped into the blurb's own column rather than left to fold back to
        # the left margin, where the continuation reads as another command.
        column = 14  # "  " + the 11-wide name + a space
        for name, blurb in HELP:
            lines = _wrap(blurb, width - column)
            print(f"  {paint(f'{name:<11}', '1')} {lines[0]}", file=sys.stderr)
            for extra in lines[1:]:
                print(f"{' ' * column}{extra}", file=sys.stderr)
    else:
        line = "  "
        for name in ACTIONS:
            if len(line) + len(name) + 2 > width:
                print(line.rstrip(), file=sys.stderr)
                line = "  "
            line += f"{name}  "
        print(line.rstrip(), file=sys.stderr)
    print("", file=sys.stderr)
    print("no command: the screen", file=sys.stderr)
    print("a URL: queue it as a direct file download", file=sys.stderr)
    print("NAME: part of a name, or its number", file=sys.stderr)
    print("docs: ~/dlq/docs/download-queue.md", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    # Defaulted because the console script entry point calls this with no
    # arguments; running the file directly passes sys.argv[1:] itself.
    if argv is None:
        argv = sys.argv[1:]
    if "--self-test" in argv:
        return _self_test()
    # Pulled out before the action is read, so --yes works on either side of it.
    assume_yes = "--yes" in argv
    argv = [arg for arg in argv if arg != "--yes"]

    # A URL is the queue ask itself — `dlq <url>` queues a direct file
    # download, uniform with `ytq <url>`. Routed before the verbs so a verb
    # can never shadow a URL (no verb contains "://"). The queuer keeps its
    # own module (dlq.py, its own flags and self-test); this is only the door.
    if argv and "://" in argv[0]:
        import dlq

        return dlq.main(argv)

    action = _action(argv)
    rest = argv[1:]
    if action in NAMED:
        if not rest:
            print(f"usage: {_me()} {action} NAME", file=sys.stderr)
            print(f"       {_me()} list shows every download by name", file=sys.stderr)
            return 2
        row = resolve(rest[0])
        if row is None:
            return 1
        if action == "now":
            return run_one(row, assume_yes)
        return show_path(row)
    elif action == "arm":
        arm()
        print()
        return status()
    elif action == "status":
        return status()
    elif action == "list":
        return show_list()
    elif action == "names":
        return show_names()
    elif action == "ui":
        problem = root_problem()
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 1
        # Imported here and not at the top: this is the only action that needs
        # curses, and `dlq names` runs on every press of the tab key.
        import expire_ui

        return expire_ui.run()
    elif action == "dest":
        return show_dest(rest)
    elif action == "cancel":
        cancel()
    elif action == "logs":
        tail(LOGS / "runner.log", 40)
    elif action == "dump":
        return dump(rest[0] if rest else None)
    elif action == "queue":
        # Same filter the runner applies: .staging and __pycache__ are not
        # items, and listing them here reads as a queue with junk in it.
        width = _width()
        for path in sorted(QUEUE.glob("*")):
            if path.name.startswith(".") or not path.is_file():
                continue
            size = f"{path.stat().st_size:,}"
            # The whole file name, always: this is the raw view, and the names
            # are what the other commands are given. The size moves under it
            # rather than the name losing its tail.
            if 9 + 2 + len(path.name) <= width:
                print(f"{size:>9}  {path.name}")
            else:
                print(path.name)
                print(f"  {size} bytes")
    elif action == "run-now":
        problem = root_problem() or shebang_problem()
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 1
        if "--blind" in rest:
            return run_blind(assume_yes)
        return subprocess.run(queue_run_argv(blind=False)).returncode
    else:
        return usage()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
