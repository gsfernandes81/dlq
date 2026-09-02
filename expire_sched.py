#!/data/data/com.termux/files/usr/bin/python3
"""Arm, inspect and tear down the expiring-allowance download queue.

The queue runs in the window before 00:00 UTC — an hour unless ``dlq settings
window`` says otherwise — spending free data that would otherwise be wiped at
the reset. This script manages the Android JobScheduler registration and draws
the two screens; every decision about whether to actually download lives in
:mod:`expire_runner`, which the platform invokes directly.

The two screens are :func:`compose_status` — what happens next, and what that
turns on — and :func:`compose_list`, where every download is and how much of it
is here. Both are laid out for a phone held in portrait, which is about 40
columns, and every line of both must fit down to 32. That is not a cosmetic
rule: a line wider than the screen wraps, and the wrapping is what pushes the
answer off the top of it.

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

    dlq [status|list|ui|path NAME|dest|settings|queue|logs|run-now|arm|cancel]

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
#: so that listing a queue costs no import; a test must pin them against the
#: runner's own constants so the two cannot drift.
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
    "off": ("automatic downloads are off", "1;33"),
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
    elif verdict == "off":
        # The one verdict a reader can undo from the phone in their hand, so
        # it carries the undoing. The second half is there because the switch
        # is about the schedule and not about the money, and a screen that
        # says only "off" reads as a queue that cannot be run at all.
        wrapped(
            f"{_me()} settings auto on turns them back on; run-now still works",
            "90",
        )
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
        # The reserve as it applies to tonight, which is not always the
        # reserve as it is set: with reserve-when-paid off and paid data
        # behind the free grant, nothing is being kept back, and the line has
        # to say why — "0 MB is always kept back" is a figure and a lie on the
        # same line. What it is set to is one command away, in `dlq settings`.
        waived = facts.get("reserve_waived")
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
                "waived: paid data is there" if waived else "is always kept back",
                "waived, paid data" if waived else "always kept back",
                "33" if waived else "90",
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

    A ``config.json`` that will not parse stops it before anything is written:
    :func:`expire_runner.load_config` answers one with an empty dict, so a
    save on top of it would be a fresh file holding this destination alone,
    with the other two and every setting gone under a success line.
    """
    runner = _runner()
    broken = runner.config_problem()
    if broken:
        return False, [broken]
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
# What the queue is allowed to spend
# --------------------------------------------------------------------------- #

#: What each setting *does*, in the words a change to it is reported in. The
#: switches carry both halves, because a switch reads as two different
#: sentences rather than as one with a word swapped: "the reserve is kept" and
#: "paid data waives it" are two facts, not one stated twice.
#:
#: ``paid-min`` carries both halves for the same reason, though it is a figure
#: rather than a switch: nought is not "no MB", it is the rule the waiver had
#: before there was a figure — any paid data at all — and a line reading
#: "0 MB is needed" would be saying the opposite of what it does. Both halves
#: name ``reserve-when-paid``, because that is the setting this one qualifies
#: and a figure that does nothing on its own has to say whose figure it is.
#:
#: Keyed by :data:`expire_runner.SETTINGS`, and a test must pin that it still
#: is — a setting added to the runner with nothing said about it here
#: would raise a KeyError on the one line whose whole job is to say what just
#: happened, which is the trap ``FILLED_BY`` guards for the destinations.
SETTING_SAYS: dict[str, object] = {
    "window": "downloads may start {} before the reset",
    "reserve": "{} is kept back",
    "reserve-when-paid": (
        "the reserve is kept even when paid data is there",
        "paid data waives the reserve",
    ),
    "paid-min": (
        "reserve-when-paid no waives the reserve only with at least that much "
        "paid data",
        "reserve-when-paid no waives the reserve on any paid data at all",
    ),
    "auto": (
        "the nightly job downloads when the window opens",
        "the nightly job fires and does nothing; run-now still works",
    ),
    "notify-blocked": (
        "a firing stopped by a fault says so on the phone",
        "a blocked firing is in the log only; a failed item still says so",
    ),
}


def _setting_names() -> str:
    """``window, reserve, ... or notify-blocked`` — all of them, in one line.

    Said the same way wherever they are offered, because a refusal that lists
    them differently from the screen that lists them reads as two different
    sets of settings. Read out of the runner in its order, so a setting added
    there is offered here without being named here as well.
    """
    names = list(_runner().SETTINGS)
    return ", ".join(names[:-1]) + f" or {names[-1]}"


def _setting_said(name: str, value: object) -> str:
    """The one line saying what *name* is now, spelled as both ends spell it."""
    spelled = _runner().spell_setting(name, value)
    says = SETTING_SAYS[name]
    if isinstance(says, tuple):
        return f"{name}: {spelled} — {says[0] if value else says[1]}"
    return f"{name}: {says.format(spelled)}"


def show_settings(argv: list[str]) -> int:
    """Show or set the handful of things a person may change about the spending.

    How early the queue may start, what may never be spent, whether that
    reserve still stands when there is paid data behind it and how much of it
    there has to be, whether the nightly job downloads at all, and whether a
    firing it stopped says so on the phone. They belong to
    :mod:`expire_runner`, which is where the spec, the parsing and the spelling
    live: the screen, this command and the firing itself all read them from
    there, so none of the three can hold its own opinion about what ``2h``
    means or about which stored value is nonsense.

    Laid out the way ``dlq dest`` is — the name as a heading with its facts
    indented under it — for the same reason: on a phone the value and the note
    behind it do not fit on one line beside a name, and the wrapped remainder
    of a hung column starts further right than the thing it belongs to.
    """
    runner = _runner()
    paint = _paint()
    if not argv:
        width = _width()
        values = runner.settings()

        def note(text: str, tone: str) -> None:
            """A line under a setting, wrapped rather than run off the side."""
            for line in _wrap(text, width - 2):
                print(f"  {paint(line, tone)}")

        # The file's own fault first and once, because it is the reason every
        # setting below reads as its default and because nothing can be changed
        # until it is fixed — a list saying "default" all the way down with
        # nothing explaining it is the version of this that gets it rewritten.
        broken = runner.config_problem()
        if broken:
            note(f"✗ {broken}", "31")
            note("those below are the built-in ones; nothing can be set", "90")
            print()

        for number, (name, spec) in enumerate(runner.SETTINGS.items()):
            if number:
                print()
            # A stored value that fails its rule is not the one in force, so
            # the word under it may not read "set": what is shown above is the
            # default, and the red line says which value was declined and why.
            # Reading "set" over the default's figure is the one way this
            # screen could lie about what tonight is going to do. Asked of the
            # runner, which is where the screen and the dump ask it too.
            stored, problem, where = runner.setting_state(name)
            print(paint(name, "1"))
            note(runner.spell_setting(name, values[name]), "")
            note(f"{where}, {spec['label']}", "90")
            if problem:
                note(f"✗ config.json says {stored!r}: {problem}", "31")
        print()
        # The command once and the two forms under it, as `dlq dest` prints
        # them: at 40 columns the command name alone is a third of the line.
        print(f"{_me()} settings")
        forms = [("NAME VALUE", "change one"), ("NAME default", "put it back")]
        form_w = max(len(form) for form, _ in forms)
        for form, does in forms:
            print(f"  {form.ljust(form_w)}   {paint(does, '90')}")
        for line in _wrap(f"NAME is {_setting_names()}", width - 2):
            print(f"  {paint(line, '90')}")
        return 0

    name = argv[0]
    if name not in runner.SETTINGS:
        print(
            f"error: {name!r} is not a setting; try {_setting_names()}",
            file=sys.stderr,
        )
        return 2
    if len(argv) < 2:
        # A name on its own is somebody halfway through changing it, not
        # somebody asking what it is: the bare command already answered that,
        # and guessing which they meant would set nothing while looking as
        # though it had.
        print(f"usage: {_me()} settings {name} VALUE", file=sys.stderr)
        return 2

    # Joined rather than argv[1] alone, so `dlq settings window 45 min` is a
    # sentence the shell may split and this still reads as one value.
    worked, lines = set_setting(name, " ".join(argv[1:]))
    for line in lines:
        if worked:
            print(line)
        else:
            print(f"error: {line}", file=sys.stderr)
    return 0 if worked else 1


def set_setting(name: str, text: str) -> tuple[bool, list[str]]:
    """Set *name* to *text*, or back to its default. ``(worked, what to say)``.

    The deciding half of ``dlq settings``, split from the printing half for
    the reason :func:`set_dest` is: the screen sets these too, and a second
    place deciding what ``2h`` means is a second answer to it. Nothing here
    prints or exits — what would have gone to stderr comes back as the last
    line, and on a success the last line is what the setting *is* now, which
    is what the screen flashes and what the command prints.

    A ``config.json`` that will not parse is refused here for the reason
    :func:`set_dest` refuses it: a save on top of an unreadable file is a save
    of everything else in it, thrown away.

    ``default`` is handled here rather than in
    :func:`expire_runner.parse_setting`, exactly as it is for the
    destinations: putting a setting back is *removing* the key, and parsing
    the word into a value would write today's built-in figure into
    ``config.json`` as though somebody had chosen it — where it would then
    outlive any change of mind about what the built-in figure should be.
    """
    runner = _runner()
    if name not in runner.SETTINGS:
        return False, [f"{name!r} is not a setting; try {_setting_names()}"]
    # Before anything else, and before the value is even read: what is on the
    # disk cannot be added to if it cannot be parsed, and saving anyway would
    # replace the destinations and every other setting with this one.
    broken = runner.config_problem()
    if broken:
        return False, [broken]
    config = runner.load_config()
    key = runner.SETTINGS[name]["key"]
    if text.strip().lower() == "default":
        config.pop(key, None)
        runner.save_config(config)
        return True, [f"{_setting_said(name, runner.settings()[name])} (the default)"]
    try:
        value = runner.parse_setting(name, text)
    except ValueError as problem:
        # The runner's own words rather than a second phrasing of them: a
        # value refused at the prompt and the same value declined out of
        # config.json have to be complained about identically, or one fault
        # reads as two.
        return False, [str(problem)]
    config[key] = value
    runner.save_config(config)
    return True, [_setting_said(name, value)]


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
    before it is wiped, leave the reserve behind, never reach into the paid
    reserve. Asking for a download now is asking to spend the paid reserve on
    purpose, so those guards do not apply here and — this is the part worth
    being explicit about — they are not quietly reimplemented as something
    weaker either. The item is run with the remainder of its own declared cap
    as its slice and no deadline, and the user is told the number first.

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
    allowance that is expiring, leave the reserve behind, stay out of the paid
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


#: Every action the dispatcher understands, with the one-line help the usage
#: block prints, in the order it prints them.
HELP = (
    ("status", "what happens next, and what it turns on"),
    ("list", "every download, and how much of it is here"),
    ("ui", "change it: reorder, rename, remove, retry, download now"),
    ("path NAME", "where a finished download landed"),
    ("dest", "show or set where finished downloads are put"),
    ("settings", "show or set the window, the reserve and automatic downloads"),
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

    def _settings() -> None:
        runner = _runner()
        values = runner.settings()
        print(f"  {'config.json':<18}: {runner.CONFIG_FILE}")
        # The whole file being unreadable outranks anything said about one
        # setting: it is why every one below reads "default", and it is why the
        # person filing the report could not change any of them.
        broken = runner.config_problem()
        if broken:
            print(f"    {broken}")
        for name in runner.SETTINGS:
            stored, problem, where = runner.setting_state(name)
            spelled = runner.spell_setting(name, values[name])
            print(f"  {name:<18}: {spelled:<8} ({where})")
            # The value being declined, not just the fact that one is: a
            # config.json somebody hand-edited is the reason the phone is
            # spending a figure nobody recognises, and the value is the
            # evidence for that.
            if problem:
                print(f"    ignoring {stored!r} from config.json: {problem}")
        # Whether the reserve is standing tonight needs a portal reading, and
        # a bug report may not go on the network to be written; what is here
        # is the rule it will be applied by.
        print(
            f"  {'reserve, in bytes':<18}: "
            f"{runner.reserve_bytes():,} when it stands"
        )

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
    guarded("settings", _settings)
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
    The default may never be an action that *does* something either; a test
    must pin that, because the failure would be a bare command that arms the
    job or spends data.
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
    # Pulled out before the action is read, so --yes works on either side of it.
    assume_yes = "--yes" in argv
    argv = [arg for arg in argv if arg != "--yes"]

    # A URL is the queue ask itself — `dlq <url>` queues a direct file
    # download, uniform with `ytq <url>`. Routed before the verbs so a verb
    # can never shadow a URL (no verb contains "://"). The queuer keeps its
    # own module (dlq.py, with its own flags); this is only the door.
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
    elif action == "settings":
        return show_settings(rest)
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
