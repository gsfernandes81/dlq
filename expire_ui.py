#!/data/data/com.termux/files/usr/bin/python3
"""The queue's management screen: reorder, rename, remove, retry, run now.

``dlq ui``. The queue already had two screens and neither of them changes
anything: ``dlq status`` says what happens next, ``dlq list`` says where
everything is. What neither could do is the handful of things that come up once
an item is *in* the queue and wrong — it should go first, it is called the
wrong thing, it should not run at all, it failed three nights and deserves
another one. Those were a rename in ``queue/`` and an edit of ``state.json`` by
hand, and doing them by hand is how the bytes get lost.

Two rules run through this whole file, and both are about the bytes.

**Everything an item owns moves with it.** An item is not only the file in
``queue/``: it is also ``work/<name>/``, where a half-finished download of
several gigabytes sits, ``out/<name>/``, where a finished one waits to be
delivered, and its record in ``state.json``, which holds the attempts and where
the files went. Renaming the queue file alone leaves a paid-for partial under a
name nothing will ever look for again, and the item starts from zero on the
next night without a word. :func:`belongings` is the one place that decides
what an item owns, and every move and every removal is spelled from it.

**Nothing outside the queue's own root is ever deleted.** A delivered file sits
in Downloads among the phone's other files and belongs to whoever asked for it,
not to the queue. Removing an item deletes the item and the scratch it was
downloading into; anything finished in ``out/`` is kept, and the screen says
where it stayed.

A third rule is about *when*: **while the queue is busy nothing may be
changed.** A firing or a ``dlq now`` writes into ``work/<name>/`` and hands
the item to ``done/`` when it completes, so moving either underneath it loses
the download or breaks the archive. Every mutating function starts at
:func:`busy_problem`, which is checked rather than assumed — the guard lives
with the mutation and not with the screen, so it cannot be skipped by a caller.

Where the actions live is itself a safety rule: **the list picks and the item
screen acts.** Every key that changes something is on a screen showing one item
and nothing else, so the item being removed is the one that is on the screen.
It also buys the room to spell each action out in words, which is what makes
this readable at 32 columns where a row of hint keys would not be.

**The listing is the whole queue.** There was a second screen above it — the
status, the job, the destinations, the settings and the key that ran the lot —
and it is gone: what tonight would do is two header lines on the listing
itself, ``n``, ``s`` and ``l`` are on the listing, and the destinations and the
nightly job are rows on the settings page. What replaces the paragraph of
figures is one line drawn *through the queued group*, after the last download
tonight's budget reaches. **That line is computed and is never an item**: it
has no name, no record and no place in the order, the cursor steps over it, and
it is worked out afresh on every draw from whatever order the items are in —
including an order that exists nowhere but under a held item. It is
:func:`expire_runner.plan` over :func:`expire_runner.admit`, which is the same
rule ``fire()`` admits items by, so the screen cannot promise bytes the night
then refuses. Moving an item across the line moves the line; it never moves the
budget.

Layout follows the rest of the queue: Termux in portrait is about 40 columns,
the self-test checks every line of every screen down to 32, and the key hints
are the line that must never be the one clipped — they are the way out.
"""

from __future__ import annotations

import curses
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import expire_sched as sched  # noqa: E402  (sibling module, path fixed up above)
import ytq  # noqa: E402  (reachable because expire_sched put its checkout on sys.path)
import contextlib

#: What a row says is said the same way ``dlq list`` says it, by calling the
#: same functions rather than by respelling them: the state, the progress, the
#: colour it is worth and the name with its extension dropped. This screen owns
#: only the *arrangement*, which differs because a cursor needs rows of a fixed
#: height that can be reversed whole — ``compose_list``'s tight shape separates
#: downloads with blank lines, and a blank line cannot be highlighted.
_display_name = sched._display_name
_state_of = sched._state_of
_progress_of = sched._progress_of
_tone = sched._tone
_fit = ytq.fit
_wrap = sched._wrap

#: The width at which the description column and the long labels fit. Shared
#: with the other two screens so that one terminal does not get two answers
#: about whether it is wide.
WIDE = sched.WIDE

#: The key hints, and the room they get, at the two widths the rest of the
#: queue uses. Drawn at x=1 and clipped at ``width - 1``, so a 40-column phone
#: shows 38 columns of them. What is dropped when the screen is narrower is
#: chosen on ytq's rule: the word that must never be clipped is the way out.
HINTS = {
    # No `s queue` any more: the listing IS the queue's screen, and what `s`
    # opens is the settings. The keys that act on the whole queue are named on
    # the legend row instead (:data:`LEGEND_KEYS`), which leaves this line to
    # the four that work the list itself.
    "list": "↑↓ pick  ⏎ open  m move  q",
    "list-live": "x stop  ⏎ open  m move  q",
    "moving": "↑↓ move it   ⏎ drop it   esc cancel",
    "item": "press a key   q back",
    # The download-now screen promises that x stops it, so x has to work on
    # the screen it was promised from and not only on the listing.
    "item-live": "x stop   press a key   q back",
    "dest": "v video  a audio  f files  q back",
    # Every setting's key is named here rather than only on the screen: these
    # are the keys that spend or stop spending, and a key that is only
    # discoverable by pressing it is not one to leave to a guess. The names
    # went when the sixth setting arrived — six names do not fit a phone — and
    # the letters stayed, because the screen carries each name beside its own
    # letter and this line is the only place the set of them is stated. ``d``
    # and ``j`` joined them when the queue screen went: they are rows on this
    # page now and they answer to keys like the rest of it.
    "settings": "w r p m a n d j  a setting  q back",
    "confirm": "y do it   any other key: no",
    # With a second answer offered, the hint has to admit it exists: "any other
    # key: no" over a screen showing a b is a screen contradicting itself.
    "confirm-two": "a key above   any other: no",
    "log": "↑↓ scroll   q back",
}
TIGHT_HINTS = {
    "list": "↑↓  ⏎ open  m move  q",
    "list-live": "x stop  ⏎ open  m  q",
    "moving": "↑↓ move  ⏎ drop  esc no",
    "item": "a key, or q back",
    "item-live": "x stop  a key  q back",
    "dest": "v vid  a aud  f file  q back",
    # The same letters, and "q back" is the one thing on this line that must
    # survive the narrow phone: the screen itself carries each letter beside
    # the setting it sets.
    "settings": "press w r p m a n d j  q back",
    "confirm": "y do it  else no",
    "confirm-two": "a key above  else no",
    "log": "↑↓ scroll  q back",
}


def hint(name: str, width: int) -> str:
    """The key hints for a screen, at whatever width there is for them."""
    return (TIGHT_HINTS if width < ytq.TIGHT else HINTS)[name]


#: The listing's whole body when there is nothing in the queue at all. A bare
#: ``dlq`` used to bounce off an empty queue onto a message screen and then
#: onto the queue's own; there is no queue screen now and no bounce either, so
#: the listing says it itself and every key on it goes on working.
EMPTY_QUEUE = "nothing queued — ytq or dlq adds something"

#: The three keys that act on the queue as a whole, spelled out on a dim row
#: of their own above the hints. They used to be a screen; the screen is gone
#: and this line is what is left of its signposting.
#:
#: One spelling at every width: its budget is the hints' own — drawn at x=1
#: and clipped at ``width - 1`` — and at 28 characters it already fits the
#: narrowest phone, so there is nothing for a tight version to give up.
#:
#: It gets the row the foot keeps for the live line, and gives it up to both
#: of the things that outrank a signpost: **a download in flight takes it**,
#: because what is being spent now is worth more than where a key goes, and
#: **a flash takes it for the moment it is shown**, which is :func:`_foot`'s
#: own rule that what just happened comes first.
LEGEND_KEYS = "n run now  s settings  l log"


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #

#: An item's file name in full: the priority, a dash, a slug, ``.py``. Stricter
#: than the runner's :data:`expire_runner.ITEM_RE`, which only has to recognise
#: an item; this one has to *make* one, and a name this does not match is a
#: file the runner would ignore for ever without saying so.
NAME_RE = re.compile(r"^(\d{2,})-([^/\\]+)\.py$")


def parse_name(name: str) -> tuple[int, str] | None:
    """``(priority, slug)``, or ``None`` if that is not an item's name."""
    found = NAME_RE.match(name)
    if not found:
        return None
    return int(found.group(1)), found.group(2)


def _slug_of(name: str) -> str:
    """An item's name as this screen shows it: the slug, with no number on it.

    The number *is* the run order, and the list already shows the order by
    being a list. Printing it as well asks someone to read the same fact twice,
    in the harder of the two spellings — and it invites thinking in numbers,
    which is the thing this screen exists to stop: you move a download by
    picking it up and putting it where you want it, not by working out what
    integer lives between two others.

    Nothing needs it typed back any more either. ``dlq now`` and ``dlq open``
    are gone from the command line, and ``dlq path`` takes any unambiguous
    part of a name, which a slug is.
    """
    parsed = parse_name(name)
    return parsed[1] if parsed else _display_name(name)


def _ordinal(number: int) -> str:
    """``1st``, ``2nd``, ``3rd``… — a position said the way a person says it."""
    if 10 <= number % 100 <= 20:
        return f"{number}th"
    return f"{number}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th') }"


def renumber(name: str, number: int) -> str:
    """The same item at a different priority — **the slug untouched**.

    Not spelled through :func:`ytq.slugify`, which is right for something a
    person typed and wrong here: it truncates at 42 characters, so putting a
    long-titled item first would silently shorten its name as well as move it,
    and the name is what ``dlq now`` is given.
    """
    _, slug = parse_name(name) or (0, name)
    return f"{max(0, min(99999, number)):02d}-{slug}.py"


def reslug(name: str, text: str) -> str:
    """The same item under a new name, at the priority it already has.

    Through :func:`ytq.slugify`, because this half *was* typed by a person and
    the rule for what an item may be called is ytq's — one spelling of it, or
    the two front ends produce names that sort against each other differently.
    """
    number, _ = parse_name(name) or (0, "")
    return f"{number:02d}-{ytq.slugify(text)}.py"


# --------------------------------------------------------------------------- #
# What an item owns
# --------------------------------------------------------------------------- #


def belongings(row: dict) -> list[tuple[str, Path]]:
    """Every path this item owns, as ``(what it is, where it is)``.

    The one place that decides what an item is on disk, because every mutation
    below is spelled from it and the failure it prevents is silent: bytes
    already paid for, left under a name nothing will look for again.

    Three of the four things an item owns are here — the item file itself, the
    scratch directory it downloads into, and the outbox it delivers into. The
    fourth is its record in ``state.json``, which is not a path; it is keyed by
    the item's name and is moved by :func:`_move_record` alongside these.

    Its logs are deliberately *not* here. ``logs/<date>-<name>.log`` is the
    record of what happened on a particular night, and the runner's own log
    names the item in prose that cannot be rewritten anyway — so a rename is
    noted in that log instead, and the trail stays readable from both ends.
    """
    found = [("item", row["path"])]
    for what, root in (("work", sched.WORK), ("out", sched.OUT)):
        path = root / row["name"]
        if path.exists():
            found.append((what, path))
    return found


def rename_moves(row: dict, new_name: str) -> list[tuple[Path, Path]]:
    """Where each of the item's belongings goes when it is called *new_name*.

    One line, and that is the point of :func:`belongings`: everything an item
    owns is named after it and stays where it is, so a rename is the same
    rename applied to each. The item file keeps its directory too, which is
    what makes this safe to run on a failed or an archived item — a rename
    never changes which state something is in.
    """
    return [(path, path.with_name(new_name)) for _, path in belongings(row)]


def removal(row: dict, bytes_too: bool = False) -> tuple[list[Path], list[Path]]:
    """``(what a removal deletes, what it leaves behind)``.

    Removing a download from the list is a decision about the *list*. By
    default it costs nothing that was paid for: the item file goes, and the
    bytes stay exactly where they are — the partial in ``work/<name>/`` and
    anything finished in ``out/<name>/``, which the runner could not hand over
    because the destination was unreachable or the item declared none.

    *bytes_too* is the second, explicit answer, offered on the same screen and
    never the default: it also deletes the partial, which is the only thing
    here it is safe to delete, because nothing but this item could ever have
    resumed it. **A finished file in** ``out/`` **is never deleted either
    way** — it is complete and already bought, and deciding to throw one away
    is not a tool's call.

    Nothing outside the queue root is ever in the first list, which the
    self-test pins: an item's delivered file is in Downloads, and it is the
    property of whoever asked for it rather than of the queue.
    """
    goes = [row["path"]]
    kept: list[Path] = []
    work = sched.WORK / row["name"]
    if work.exists():
        # Scratch holding no payload was never paid for — an item that failed
        # before moving a byte still leaves the status file the runner writes —
        # so there is nothing there to keep and it goes either way. Keeping it
        # would leave a directory nothing lists and nothing will ever resume.
        (goes if bytes_too or not row["have"] else kept).append(work)
    out = sched.OUT / row["name"]
    with contextlib.suppress(OSError):
        kept += sorted(path for path in out.iterdir() if path.is_file())
    return goes, kept


# --------------------------------------------------------------------------- #
# The order
# --------------------------------------------------------------------------- #


def slot(others: list[int], pos: int) -> int | None:
    """A priority that puts an item at *pos* among items keyed *others*.

    ``None`` when there is no room — two neighbours one apart, or a queue that
    has run out of two-digit keys at the end. That is not a failure, it is the
    signal to hand out fresh keys to everything (:func:`spread`), and it is
    rare: items arrive ten apart and each insertion only halves one gap.

    Every answer stays inside ``00``–``99`` on purpose. The runner sorts by
    file name, so a three-digit key sorts *before* a two-digit one and an item
    moved to the back would arrive at the front — see :data:`ytq.MAX_PRIORITY`.
    """
    before = others[pos - 1] if pos > 0 else None
    after = others[pos] if pos < len(others) else None
    if before is None and after is None:
        return 10
    if before is None:
        # Half of whatever is at the top rather than ten below it: this is the
        # end of the queue that runs out first, and halving leaves room on both
        # sides of the new head instead of all of it above.
        return after // 2 if after >= 1 else None
    if after is None:
        return before + 10 if before + 10 <= ytq.MAX_PRIORITY else None
    return (before + after) // 2 if after - before >= 2 else None


def spread(count: int) -> list[int]:
    """Fresh keys for *count* items, evenly spaced through the two digits.

    Wide gaps while the queue is short, so the next few moves are single
    renames again; narrowing as it grows, because the alternative to a tight
    gap is a key that does not fit in two digits, and that one is not a
    cosmetic problem.
    """
    step = max(1, (ytq.MAX_PRIORITY - 9) // (count + 1))
    return [step * (index + 1) for index in range(count)]


def _rename_bare(name: str, new_name: str) -> str | None:
    """Move a queued item and its belongings, with no log line and no verdict.

    The half of :func:`do_rename` that reordering needs: a reorder is one
    action however many files it has to touch, so the note and the sentence
    belong to the reorder rather than to each rename inside it.
    """
    row = {"name": name, "path": sched.QUEUE / name}
    failure = _apply(rename_moves(row, new_name))
    if failure:
        return failure
    _move_record(name, new_name)
    return None


def _free_key(taken: set[int]) -> int | None:
    """The lowest two-digit key nobody is using or about to use."""
    for key in range(ytq.MAX_PRIORITY + 1):
        if key not in taken:
            return key
    return None


def _reindex(order: list[str]) -> str | None:
    """Give every queued item a fresh key, in this order. A failure, or ``None``.

    The renames are done in whatever order does not collide, which is not
    necessarily the order asked for: an item whose new key is another item's
    current one has to wait for that one to move. When *every* remaining move
    is blocked — a cycle, which a plain swap of two neighbours already is — one
    item is parked on a key nobody wants and the rest unwind behind it.

    The parking key is a **real priority**, not a temporary name, so that at no
    instant does an item stop looking like an item. A firing that starts in the
    middle of this reads a queue that is complete and legal, possibly in an
    order nobody asked for; the alternative — parking on a dotfile — is a queue
    with downloads missing from it, which is the one thing that must never
    happen quietly.
    """
    if len(order) > ytq.MAX_PRIORITY:
        return f"more than {ytq.MAX_PRIORITY} downloads is more than there is room for"
    wanted = {
        name: renumber(name, key)
        for name, key in zip(order, spread(len(order)), strict=True)
    }
    pending = {old: new for old, new in wanted.items() if old != new}
    # Every key that must not be parked on: one an item already answers to, and
    # one something is about to.
    spoken = {parse_name(name)[0] for name in wanted} | {
        parse_name(name)[0] for name in wanted.values()
    }
    guard = 2 * len(wanted) + 4
    while pending:
        guard -= 1
        if guard < 0:  # pragma: no cover - the loop is finite by construction
            return "the order would not settle; nothing else was changed"
        free = [old for old, new in pending.items() if not (sched.QUEUE / new).exists()]
        if not free:
            old = next(iter(pending))
            key = _free_key(spoken)
            if key is None:
                return "no room to reorder; remove something first"
            spoken.add(key)
            parked = renumber(old, key)
            failure = _rename_bare(old, parked)
            if failure:
                return failure
            pending[parked] = pending.pop(old)
            continue
        for old in free:
            failure = _rename_bare(old, pending.pop(old))
            if failure:
                return failure
    return None


def landed_index(rows: list[dict], pos: int) -> int | None:
    """Where the cursor lands after a drop: the row at queued position *pos*.

    By position and never by name, because :func:`do_reorder` *renamed* the
    item — its number is its place — so the name that was picked up finds
    nothing in the re-read list. The drop's whole meaning is "put it at
    *pos* among the queued", so the row now at that position is the one that
    moved, whatever it is called now.
    """
    queued = [index for index, row in enumerate(rows) if row["where"] == "queued"]
    if not queued:
        return None
    return queued[max(0, min(pos, len(queued) - 1))]


def do_reorder(rows: list[dict], name: str, pos: int) -> tuple[str, bool]:
    """Put the queued item *name* at position *pos*: ``(what to say, moved)``.

    One rename when there is room between its new neighbours, and a fresh set
    of keys for the whole queue when there is not. Both are the same action to
    whoever asked for it, which is why neither of them says a number back.
    """
    problem = busy_problem()
    if problem:
        return problem, False
    queued = [row["name"] for row in rows if row["where"] == "queued"]
    if name not in queued:
        return "only a queued download has a place in the order", False
    others = [item for item in queued if item != name]
    pos = max(0, min(pos, len(others)))
    if others[:pos] + [name] + others[pos:] == queued:
        return "left where it was", False
    keys = [parse_name(item)[0] for item in others]
    key = slot(keys, pos)
    target = renumber(name, key) if key is not None else ""
    if target and not (sched.QUEUE / target).exists():
        failure = _rename_bare(name, target)
    else:
        failure = _reindex(others[:pos] + [name] + others[pos:])
    if failure:
        return failure, False
    _note(f"moved {name} to {pos + 1} of {len(queued)}")
    return f"{_slug_of(name)} is {_ordinal(pos + 1)} of {len(queued)}", True


# --------------------------------------------------------------------------- #
# Changing something
# --------------------------------------------------------------------------- #


def busy_problem() -> str | None:
    """Why nothing may be changed at this moment, or ``None``.

    Called first by every mutating function in this module rather than by the
    screens that call them, so that the guard cannot be skipped by a caller who
    did not know it was needed. What it guards is not subtle: a firing or a
    ``dlq now`` is writing into ``work/<name>/`` and will rename the item into
    ``done/`` when it finishes, and moving either underneath it loses the
    download or leaves the archive step renaming a file that is not there.

    :func:`ytq.queue_busy` and not the freshness of a progress file, because
    this is the question the *runner* answers with the lock, and a mutation is
    one of the two things (the other is ``dlq now``) that is entitled to take
    it. The read-only screens use the progress file precisely because they are
    not entitled to.
    """
    if ytq.queue_busy():
        return "the queue is busy — a firing or a download holds it"
    return None


def _taken(new_name: str) -> str | None:
    """Why *new_name* cannot be used, or ``None``.

    Both halves matter. Another item already answering to the name is the
    obvious one. The other is a ``work/`` or ``out/`` directory left behind by
    an item that was removed while its finished files were kept: renaming onto
    it would mix two downloads' bytes into one directory, and the item would
    deliver a file it never downloaded.
    """
    for where, path in sched._paths():
        if path.name == new_name:
            return f"{where} already has a {_display_name(new_name)}"
    for root in (sched.WORK, sched.OUT):
        if (root / new_name).exists():
            return f"{root.name}/{_display_name(new_name)} still has bytes in it"
    return None


def refuse_rename(row: dict, new_name: str) -> str | None:
    """Why this rename must not happen, or ``None``. Says it in one line."""
    problem = busy_problem()
    if problem:
        return problem
    if new_name == row["name"]:
        return "that is the name it has"
    if not NAME_RE.match(new_name):
        return "a name is a number, a dash, a slug and .py"
    return _taken(new_name)


def _apply(moves: list[tuple[Path, Path]]) -> str | None:
    """Do every move, or undo the ones already done and say what stopped it.

    Each move is a rename within the queue root, so each is atomic and none of
    them can half-copy a gigabyte. The set of them is not atomic, which is what
    the rollback is for: an item whose scratch moved and whose item file did
    not is an item that will silently download itself again.

    A rollback that itself fails is the one case nothing can fix, so it is
    written to the runner's log naming both paths — that log is then the only
    record of where the bytes went, and it is the file the next person reads.
    """
    done: list[tuple[Path, Path]] = []
    for source, target in moves:
        try:
            source.rename(target)
        except OSError as exc:
            for was, now in reversed(done):
                try:
                    now.rename(was)
                except OSError:
                    _note(f"ROLLBACK FAILED: {now} should be {was}")
            return f"could not move {source.name}: {exc.strerror or exc}"
        done.append((source, target))
    return None


def _note(message: str) -> None:
    """Append to the runner's own log, without printing it.

    :func:`expire_runner.log` echoes every line to stdout so that a manual run
    shows its reasoning; under curses that is a line drawn onto a screen this
    module does not own, and one that no redraw will clear. Same file, same
    stamp, same reader.

    Management actions belong in that log and not in a second one: what
    happened to an item is one story, and "it failed twice and then somebody
    renamed it" only reads as a story if both halves are in the same file.
    """
    try:
        line = f"{sched._runner().stamp()}  ui: {message}"
        sched.LOGS.mkdir(parents=True, exist_ok=True)
        with (sched.LOGS / "runner.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001 - a record, never a blocker
        pass


def _record(name: str) -> tuple[object, dict, dict]:
    """``(runner, whole state, this item's record)``, creating neither."""
    runner = sched._runner()
    state = runner.load_state()
    items = state.get("items") or {}
    return runner, state, items.get(name) or {}


def _move_record(old: str, new: str) -> None:
    """Carry the item's history over to its new name.

    The attempts, the bytes spent and — for a finished item — the paths its
    files were delivered to. Dropping it would cost ``dlq path`` the only
    answer it has: once a file is in a folder shared with every other app on
    the phone, nothing can work out which one was ours by looking.
    """
    runner, state, _ = _record(old)
    items = state.get("items") or {}
    if old not in items:
        return
    items[new] = items.pop(old)
    state["items"] = items
    runner.save_state(state)


def _edit_record(name: str, changes: dict, drop: tuple[str, ...] = ()) -> None:
    """Set and unset fields of one item's record, leaving the rest alone."""
    runner, state, _ = _record(name)
    items = state.setdefault("items", {})
    record = items.setdefault(name, {})
    record.update(changes)
    for key in drop:
        record.pop(key, None)
    runner.save_state(state)


def do_rename(row: dict, new_name: str) -> str:
    """Rename the item and everything it owns. Returns what to say about it."""
    problem = refuse_rename(row, new_name)
    if problem:
        return problem
    failure = _apply(rename_moves(row, new_name))
    if failure:
        return failure
    _move_record(row["name"], new_name)
    _note(f"renamed {row['name']} -> {new_name}")
    return f"now {_display_name(new_name)}"


def do_remove(row: dict, bytes_too: bool = False, why: str = "removed") -> str:
    """Take the item off the list, keeping every byte unless told otherwise."""
    problem = busy_problem()
    if problem:
        return problem
    goes, kept = removal(row, bytes_too)
    for path in goes:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            return f"could not remove {path.name}: {exc.strerror or exc}"
    runner, state, _ = _record(row["name"])
    if (state.get("items") or {}).pop(row["name"], None) is not None:
        runner.save_state(state)
    if not kept:
        # Only when it is empty: an out/ directory with files in it is the
        # reason those files still exist.
        with contextlib.suppress(OSError):
            (sched.OUT / row["name"]).rmdir()
    bytes_said = (
        f"{ytq.human(row['have'])} {'deleted' if bytes_too else 'kept'}"
        if row["have"]
        else ""
    )
    _note(
        f"{why} {row['name']}"
        + (f" — {bytes_said}" if bytes_said else "")
        + (f", {len(kept)} left in out/" if kept else "")
    )
    # Short on purpose: the flash line is clipped rather than wrapped, so what
    # happened to the bytes goes first and is the half that survives it.
    left = [path for path in kept if path.is_file()]
    parts = ([bytes_said] if bytes_said else []) + (
        [f"{len(left)} kept in out/"] if left else []
    )
    said = f"removed {_slug_of(row['name'])}"
    return f"{said}; {', '.join(parts)}" if parts else said


def forget_gone(rows: list[dict]) -> list[str]:
    """Drop finished downloads whose delivered file has been deleted.

    The queue keeps a done item for one reason: it is the only record of where
    the file went, since a shared Downloads folder cannot be searched for
    "mine". Once the file is not there, the record answers a question nobody
    can ask any more, and leaving it costs a row in every listing that looks
    like work outstanding.

    Two conditions, and both are evidence rather than inference. The folder has
    to be *readable* — :func:`expire_sched._readable` lists it, so a permission
    that was never granted or a card that is out reads as ``away`` and is left
    alone, which is the whole reason this can be automatic at all. And the item
    has to have recorded where it was delivered: with no record there is
    nothing to have looked in, and absence of evidence is not the same fact.

    Never touches the file itself, and never runs while the queue is busy.
    """
    if busy_problem():
        return []
    forgotten = []
    for row in rows:
        if row["where"] != "done" or row.get("lost") != "gone" or not row["recorded"]:
            continue
        folder = row["recorded"][0].parent
        said = do_remove(row, why=f"forgot (nothing of it left in {folder})")
        if said.startswith("removed"):
            forgotten.append(_slug_of(row["name"]))
    return forgotten


def do_requeue(row: dict) -> str:
    """Put a failed item back in the queue, with its attempts wiped.

    The wipe is not a courtesy: an item is set aside once its attempts reach
    ``MAX_ATTEMPTS``, so one put back with them still on the clock is given up
    on again by the first firing that touches it — a retry that retries
    nothing. What is deliberately kept is ``work/<name>/``, so the retry
    resumes from whatever the three failed nights did manage to buy.
    """
    problem = busy_problem()
    if problem:
        return problem
    if row["where"] != "failed":
        return f"it is in {row['where']}, not failed"
    # Not :func:`_taken`, which would find this very item sitting in failed/.
    # The one collision that matters here is a queued item of the same name.
    if (sched.QUEUE / row["name"]).exists():
        return f"the queue already has a {_display_name(row['name'])}"
    failure = _apply([(row["path"], sched.QUEUE / row["name"])])
    if failure:
        return failure
    _edit_record(row["name"], {"attempts": 0, "stalls": 0}, drop=("retired",))
    _note(f"put {row['name']} back in the queue, attempts cleared")
    return f"{_display_name(row['name'])} is queued again"


def do_clear_tries(row: dict) -> str:
    """Give a queued item its three nights back."""
    problem = busy_problem()
    if problem:
        return problem
    if not row["attempts"]:
        return "it has no failed tries"
    _edit_record(row["name"], {"attempts": 0, "stalls": 0})
    _note(f"cleared {row['attempts']} failed tries on {row['name']}")
    return f"{row['attempts']} failed tries cleared"


# --------------------------------------------------------------------------- #
# Curses
# --------------------------------------------------------------------------- #

_addstr = ytq._addstr

#: The colours :func:`expire_sched._tone` asks for, as curses attributes.
#: Keyed by the ANSI code that function returns, because that function is where
#: the decision "what colour is this row worth" is made and this is only the
#: translation of it — a tone missing from here is a row that loses its colour
#: without anything saying so, which the self-test pins against.
TONES = {
    "1;31": (curses.COLOR_RED, curses.A_BOLD),
    "31": (curses.COLOR_RED, 0),
    "32": (curses.COLOR_GREEN, 0),
    "33": (curses.COLOR_YELLOW, 0),
    "90": (None, curses.A_DIM),
    "1;33": (curses.COLOR_YELLOW, curses.A_BOLD),
    "head": (curses.COLOR_CYAN, 0),
}


def ink(win) -> dict[str, int]:
    """``tone -> curses attribute``, with no colour at all if there is none.

    Every step is allowed to fail and leave the attribute at its bare bold or
    dim, which is exactly "draw it plain": this runs in Termux, over ssh, and
    in whatever a scheduled shell inherits, and a screen that needs colour to
    be readable is a screen that is unreadable somewhere.
    """
    got = {tone: attr for tone, (_, attr) in TONES.items()}
    try:
        if not curses.has_colors():
            return got
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        return got
    index = 0
    for tone, (colour, attr) in TONES.items():
        if colour is None:
            continue
        index += 1
        try:
            curses.init_pair(index, colour, -1)
        except curses.error:
            continue
        got[tone] = curses.color_pair(index) | attr
    return got


# --------------------------------------------------------------------------- #
# What may be done to one item
# --------------------------------------------------------------------------- #


def actions_for(row: dict) -> list[tuple[str, str]]:
    """The keys this item offers, in the order they are worth offering.

    By state, and only the ones that mean something: an archived download has
    no priority to change and a rejected one cannot be told to run. What is
    *not* filtered here is anything that depends on the moment rather than on
    the item — the lock, mostly — because an action that quietly vanishes
    between one look and the next reads as a bug. Those are offered and then
    explained when they are pressed.
    """
    where = row["where"]
    offered: list[tuple[str, str]] = []
    if where == "queued" and not row["error"] and row["cap"] > row["have"]:
        offered.append(("n", "download it now"))
    if where == "failed":
        offered.append(("u", "put it back in the queue"))
    if where == "queued":
        offered.append(("m", "move it in the queue"))
    if where in ("queued", "failed"):
        offered.append(("r", "rename"))
    if where == "queued" and row["attempts"]:
        offered.append(("t", f"clear {row['attempts']} failed tries"))
    if row["files"]:
        offered.append(("o", "open the file"))
    offered.append(("l", "its log"))
    offered.append(("d", "remove from the list" if where == "done" else "remove"))
    return offered


def progress_bar(have: int, total: int, width: int) -> str:
    """``[====····] 44%`` — the queue's one visual for how far in a download is.

    Spelled the way ``quota_widget`` spells its own bar, ``=`` against ``·``,
    rather than block characters: this is drawn on a phone, and the block
    glyphs are East-Asian *ambiguous* width, so a terminal that renders them
    double leaves the bar a column wider than it was measured at. The same
    argument that keeps ``⚠`` off ytq's notices.

    With no total there is no fraction to draw and the caller gets ``""`` —
    a full-looking bar over an unknown size is the one reading worse than
    none.
    """
    if total <= 0 or width < 12:
        return ""
    fraction = min(1.0, max(0.0, have / total))
    pct = f"{min(99, int(fraction * 100))}%" if fraction < 1 else "100%"
    track = max(4, width - len(pct) - 4)
    filled = int(round(fraction * track))
    return f"[{'=' * filled}{'·' * (track - filled)}] {pct}"


def _with_live(row: dict, reading: tuple[int, int] | None) -> dict:
    """*row* with its byte figures replaced by the download's own report.

    The row's ``have`` is counted off the disk when the listing is read; the
    report is written by the download itself and is newer. Both were on the
    item screen at once — the head from one, the foot from the other — and a
    second apart they disagreed, which is how the screen came to show two
    different sizes for one file and no way to tell which was stale. One
    reading feeds the word, the figures and the bar now, so they cannot.
    """
    if reading is None:
        return row
    have, total = reading
    fresh = dict(row)
    fresh["have"] = have
    if total:
        # Only once the server has stated a size: `_of` prints `≤` against the
        # declared cap until then, and inventing a total here would turn that
        # honest bound into a figure nobody measured.
        fresh["stated"] = total
        fresh["total"] = total
    return fresh


def item_lines(
    row: dict,
    width: int,
    place: tuple[int, int] | None = None,
    downloading: bool = False,
    reading: tuple[int, int] | None = None,
) -> list[str]:
    """The facts about one item, laid out for the screen it is going on.

    Wrapped rather than clipped, because these are the sentences that say why
    an item was rejected or where its file went, and half of either is no
    answer. The state and the figures lead, in the same words the listing uses,
    and they take a second line rather than lose the figures — the figures are
    what the screen was opened for. A path is the one thing that may still be
    clipped: it is a single unbreakable word, usually longer than a phone, and
    the ellipsis says so.
    """
    compact = width < WIDE
    room = max(12, width - 4)
    lines: list[str] = []
    shown = _with_live(row, reading)
    # `where` is the DIRECTORY the item file sits in, and an item being
    # downloaded is still in queue/ — it only moves when it finishes. So the
    # screen said "queued" over a download in flight. The word says what is
    # happening; the directory is still what decides everything below.
    head_word = "downloading" if downloading else row["where"]
    head = f"{head_word} · {_state_of(shown, compact)}".rstrip(" ·")
    figures = _progress_of(shown, compact)
    if figures and len(head) + 2 + len(figures) <= room:
        lines.append(f"{head}  {figures}")
    else:
        lines.append(head)
        if figures:
            lines.append(figures)
    # Not on a finished one, where the attempts are how many nights it took
    # and "try 1/3" reads as nights it still has left.
    if place and row["where"] == "queued":
        # Where it is in the run order, said as a position rather than as the
        # number it is stored under: the number is an implementation detail of
        # sorting file names, and nothing asks anyone to type it any more.
        lines.append(f"{_ordinal(place[0])} of {place[1]} in the queue")
    if row["attempts"] and row["where"] != "done":
        tries = f"try {row['attempts']}/{sched._runner().MAX_ATTEMPTS}"
        lines += _wrap(f"{tries} · last {row['last']}" if row["last"] else tries, room)
    if row["error"]:
        lines += _wrap(f"rejected: {row['error']}", room)
    if row["desc"] and row["desc"] != row["name"]:
        lines += _wrap(row["desc"], room)
    if row["files"]:
        lines += _wrap(f"here: {sched._short(row['files'][0])}", room, "  ")
    elif row.get("lost"):
        # Not "→ where it will land": nothing is going to land. Where it *was*
        # put is the only thing anyone can act on, and it is the one fact that
        # dies with this record.
        where = row["recorded"][0] if row["recorded"] else sched.OUT / row["name"]
        lines += _wrap(f"was: {sched._short(Path(where))}", room, "  ")
        lines += _wrap(
            (
                "deleted since"
                if row["lost"] == "gone"
                else "that folder cannot be reached from here"
            ),
            room,
        )
    else:
        lines += _wrap(f"→ {sched._short(Path(ytq.landing(row['dest'])))}", room, "  ")
    return [_fit(line, room) for line in lines]


# --------------------------------------------------------------------------- #
# What tonight would download
# --------------------------------------------------------------------------- #

#: The verdict line's phrase for each of :data:`expire_sched.VERDICTS`, in the
#: spelling a phone has room for. The *long* spelling is not here on purpose:
#: it is the status screen's own headline, read straight out of ``VERDICTS``,
#: so the listing and ``dlq status`` cannot come to two different words for the
#: same night. This table only adds the short one, for the width where the
#: headline and the clock will not both fit — and the self-test pins that every
#: verdict has an entry, because one the gate grew and this did not would draw
#: the narrow phone a blank where the answer goes.
TONIGHT_SHORT = {
    "downloading": "downloading",
    "go": "window open",
    "early": "waiting",
    "late": "done tonight",
    "empty": "nothing queued",
    "off": "auto is off",
    "spent": "no data to spend",
    "blind": "PAID: no portal",
    "no-portal": "BLOCKED: no portal",
    "stale": "BLOCKED: stale data",
}

#: What the verdict line says before the first reading has come back. The
#: screen opens on it, so it has to be the truth about the screen rather than
#: about the night: nothing is known yet and something is being done about it.
ASKING = "tonight: asking zwana…"


def _when(facts: dict) -> tuple[str, str]:
    """``(the clock and the countdown, the countdown alone)`` for tonight.

    :func:`expire_sched._timing` says the same thing on a line of its own and
    is the right length for one. This line carries the verdict as well and has
    about thirty columns to do it in on a phone, so it needs a shorter form and
    then a shorter one again. Nothing here is a second *fact*: it is the same
    three figures out of the same snapshot, put in the order that survives
    being clipped — the clock first, because that is the half a reader steers
    by once the countdown is more than an hour.
    """
    current, window_open, stop_by = facts["now"], facts["window_open"], facts["stop_by"]
    if stop_by == sched._runner().NO_DEADLINE:
        return "no stop time", "no stop time"
    if current < window_open:
        left = sched._in(window_open - current)
        return f"opens {sched._clock(window_open)} ({left})", f"opens in {left}"
    if current < stop_by:
        left = sched._in(stop_by - current)
        return f"closes {sched._clock(stop_by)} ({left})", f"closes in {left}"
    left = sched._in(window_open + 86_400 - current)
    return f"opens again {sched._clock(window_open)} ({left})", f"back in {left}"


def _pick(options: list[str], room: int) -> str:
    """The first of *options* that fits *room*, or the last one clipped.

    The listing's two header lines are each written three or four ways — the
    verdict with the clock, the verdict alone, the short verdict — and this is
    the one rule that chooses between them, so a line added later cannot pick
    differently from the line above it.
    """
    return next(
        (text for text in options if len(text) <= room), _fit(options[-1], room)
    )


def tonight_lines(facts: dict | None, width: int, live: str = "") -> list[str]:
    """The two lines under the title bar: the verdict, then the figures.

    The same two facts ``dlq status`` opens with, kept in view while the queue
    is worked instead of a screen away — and taken from the same snapshot, so
    the listing cannot say one thing about tonight while the status screen says
    another. Pure: everything it needs is in *facts*, which is what lets the
    self-test spell out a night rather than wait for one.

    *live* is the download in flight if there is one, ours or a firing's. It
    outranks every verdict for the same reason it does on the status screen:
    what is being spent now is the answer to "what is happening", and the
    window it is being spent inside is not.

    ``None`` for *facts* is the moment the screen opens, before the reading has
    come back. It says so rather than showing a blank or, worse, a figure of
    zero — see :class:`Tonight`, which is what is doing the asking.
    """
    room = max(8, width - 2)
    if not facts:
        return [_fit(ASKING, room), ""]
    verdict = "downloading" if live else facts["verdict"]
    if live:
        head = _fit(f"downloading now: {_slug_of(live)}", room)
    else:
        long = sched.VERDICTS.get(verdict, (facts["detail"], ""))[0]
        short = TONIGHT_SHORT.get(verdict, long)
        # An empty queue has no next state worth naming: the window opening on
        # nothing is not a thing to wait for.
        tails = [""] if verdict == "empty" else [f", {when}" for when in _when(facts)]
        head = _pick(
            [
                f"tonight: {phrase}{tail}"
                for tail in [*dict.fromkeys(tails), ""]
                for phrase in (long, short)
            ],
            room,
        )
    return [head, _fit(_tonight_figures(facts, room), room)]


def _tonight_figures(facts: dict, room: int) -> str:
    """The second header line: what there is to spend, or why there is nothing.

    Four different questions wearing one line, and which of them is being asked
    is the verdict's business rather than this line's: with the switch off the
    figures are beside the point and the way back on is not, with no reading
    there are no figures at all, and on a blind run the figure is the queue's
    own declaration and has to say whose data it is spending.
    """
    if facts["verdict"] == "off":
        me = sched._me()
        return _pick(
            [f"{me} settings auto on turns it back on", f"{me} settings auto on"],
            room,
        )
    size = ytq.human(max(0, facts["spendable"]))
    if facts["blind"]:
        return _pick(
            [f"{size} on mobile data, nothing counting", f"{size} on mobile data"],
            room,
        )
    doc = facts["portal"]
    if doc is None:
        # The portal's own words for why it did not answer. Not shortened and
        # not replaced by "no reading": it is the only line on this screen that
        # says what to do about it.
        return facts["portal_problem"] or "no reading, so nothing can be spent"
    free = ytq.human(doc["free"]["left_bytes"])
    return _pick(
        [
            f"{size} to spend · {free} expires {sched._clock(facts['deadline'])}",
            f"{size} to spend · {free} expires",
            f"{size} to spend",
        ],
        room,
    )


def tonight_plan(order: list[str], facts: dict | None) -> list[dict]:
    """What each queued item would get tonight, in the order it is given.

    :func:`expire_runner.plan` and nothing else — **the runner's own
    projection, over the runner's own budget**. That is the whole safety of the
    cut line: the rule that decides which items the night reaches is
    :func:`expire_runner.admit`, the same function ``fire()`` calls at
    midnight, so a line drawn here cannot promise bytes the night then refuses.
    A second copy of the arithmetic would be a screen committing more than the
    queue will spend, which is the one thing this exists to prevent.

    The order is the screen's, which is the point: the items are handed over in
    whatever order they are on the screen — including an order that exists
    nowhere but under a held item — and what comes back is what *that* order
    would download. The budget is not the screen's to change.

    An item the reading does not know about — queued since it was taken — is
    not in the projection at all, and so gets nothing tonight until the next
    reading. Saying anything else would be inventing a cap for it.

    No ``state.json`` is passed: a snapshot's items carry their own
    ``part_bytes``, which is the only thing :func:`plan` reads a state for.
    """
    if not facts:
        return []
    known = {item["name"]: item for item in facts["items"]}
    return sched._runner().plan(
        [known[name] for name in order if name in known],
        {},
        facts["spendable"],
        facts["bps"],
        facts["night_seconds"],
        facts["blind"],
        facts["free_disk"],
    )


def _rule(spellings: list[str], width: int) -> str:
    """``── text ─────`` across the listing, in the fullest spelling that fits.

    Ordered longest first, and the last one is the one that has to fit any
    phone — a rule with nothing readable on it is worse than no rule, since it
    is drawn *between* two downloads and would read as a separator.
    """
    room = max(8, width - 1)
    text = _pick(spellings, room - 6)
    drawn = f"── {text} "
    return _fit(drawn + "─" * max(2, room - len(drawn)), room)


def cut_index(
    order: list[str], facts: dict | None, width: int
) -> tuple[int | None, str]:
    """``(how many queued items tonight reaches, the line to say so)``.

    The cut line, and it is **computed rather than an item**: it has no name,
    no record and no place in the order — it is worked out afresh from wherever
    the items are, every draw, including live from a preview order while one of
    them is being held. Nothing about it can be picked up, dropped, renamed or
    removed, which is why the cursor never stops on it.

    The count is the position after the last item that gets any bytes, so what
    the line means is exactly "nothing below this gets anything tonight". That
    is not always the same as counting the items that got something: an item
    can be passed over for a reason of its own — too big for what is left, no
    disk — while a smaller one behind it still runs, and drawing the line by
    the count would put an item that downloads below a line saying it does not.

    ``None`` when there is no reading yet: an honest listing with no line is
    better than a line drawn from figures nobody has.

    When nothing gets anything the line goes to the top of the queued group and
    says why, which is the only place the answer can be. A verdict that stops
    the whole night is quoted in the status screen's own words; otherwise it is
    the first refusal :func:`expire_runner.admit` gave, which is the runner
    explaining itself in the same sentence it would log.
    """
    if not facts or not order:
        return None, ""
    planned = tonight_plan(order, facts)
    got = {entry["name"]: entry["bytes"] for entry in planned}
    last = -1
    for position, name in enumerate(order):
        if got.get(name, 0):
            last = position
    if last >= 0:
        total = ytq.human(sum(got.values()))
        return last + 1, _rule(
            [f"tonight ends here: {total}", f"tonight: {total}"], width
        )
    verdict = facts["verdict"]
    if verdict in (*sched._runner().GATE_GO, "early"):
        # The night is one that downloads, so the answer is about the items:
        # the runner's own first refusal, word for word.
        why = next(
            (entry["reason"] for entry in planned if entry["reason"]),
            "nothing fits tonight",
        )
    else:
        why = sched.VERDICTS.get(verdict, (facts["detail"], ""))[0]
    # Three spellings, because a refusal out of `admit` carries its working in
    # brackets — "(budget 0 B, 3122s left)" — and that is the half worth losing
    # first: the sentence in front of it is the answer, the bracket is how it
    # was arrived at.
    return 0, _rule(
        [f"nothing tonight: {why}", f"nothing tonight: {why.split(' (')[0]}",
         "nothing tonight"],
        width,
    )


# --------------------------------------------------------------------------- #
# The listing, with a cursor on it
# --------------------------------------------------------------------------- #


def compose_rows(
    rows: list[dict],
    width: int,
    live: str = "",
    cut: tuple[int, str] | None = None,
) -> list[tuple[int | None, list[str]]]:
    """The whole listing as ``(which row, its lines)``, headings included.

    A heading is ``None``; everything else carries the index of the row in
    *rows*, so the cursor and the screen cannot disagree about which download
    is which. **Every row appears exactly once**, whatever the width — the
    self-test pins that, because a download missing from this screen looks
    exactly like a download that is not there, and this is the screen someone
    removes things from.

    *cut* is :func:`cut_index`'s answer: ``(how many queued rows tonight
    reaches, the line to draw)``. It is emitted **as a heading** — index
    ``None``, exactly like ``queued (3)`` — which is the whole of how the cut
    line stays computed rather than becoming an item: the cursor skips it,
    :func:`landed_index` cannot land on it, and no row's index moves because it
    is there. The self-test pins both halves.

    Two shapes, on the same rule the listing uses: one line each while the
    name, the state and the figures fit together, and two lines each when they
    do not. The name is the last cell to give up room, because losing its tail
    makes two downloads look like the same one — and here that is the
    difference between removing one and removing the other.
    """
    compact = width < WIDE
    cells = [
        (
            _slug_of(row["name"]),
            # An arrow on whatever is downloading at this second, ours or a
            # firing's. The figures beside it move on their own while the
            # screen is open, which is the other half of saying it is live.
            ("↓" if row["name"] == live else "") + _state_of(row, compact),
            _progress_of(row, compact),
            row["error"] or (row["files"][0].name if row["files"] else row["desc"]),
        )
        for row in rows
    ]
    name_w = max((len(cell[0]) for cell in cells), default=0)
    state_w = max((len(cell[1]) for cell in cells), default=0)
    prog_w = max((len(cell[2]) for cell in cells), default=0)
    # One column in hand at the right: curses treats a write into the last cell
    # of a line as an error, so the whole screen is laid out one narrower.
    room = width - 1
    one_line = 2 + name_w + 2 + state_w + 2 + prog_w
    tight = one_line > room
    note_w = 0 if compact else room - one_line - 2

    out: list[tuple[int | None, list[str]]] = []
    for where in ("queued", "failed", "done"):
        group = [index for index, row in enumerate(rows) if row["where"] == where]
        if not group:
            continue
        out.append((None, [f"{where} ({len(group)})"]))
        for at, index in enumerate(group):
            if cut and where == "queued" and at == cut[0]:
                out.append((None, [cut[1]]))
            name, state, progress, note = cells[index]
            if tight:
                figures = f"{state:>{state_w}}  {progress}"
                out.append(
                    (
                        index,
                        [
                            f"  {_fit(name, room - 2)}",
                            f"    {_fit(figures, room - 4)}",
                        ],
                    )
                )
                continue
            line = f"  {name.ljust(name_w)}  {state:>{state_w}}  {progress}"
            if note_w >= 14:
                line += f"  {_fit(note, note_w)}"
            out.append((index, [line[:room].rstrip()]))
        # A night that reaches every queued item still gets its line, at the
        # foot of the group: "all of this goes tonight" is an answer, and a
        # missing line reads as a screen that did not work it out.
        if cut and where == "queued" and cut[0] >= len(group):
            out.append((None, [cut[1]]))
    return out


def _bar(win, paint: dict, text: str, tone: str = "head") -> None:
    """The screen's identity, across the top, in reverse."""
    width = win.getmaxyx()[1]
    _addstr(
        win,
        0,
        0,
        _fit(text, width - 1).ljust(width - 1),
        curses.A_REVERSE | curses.A_BOLD | paint.get(tone, 0),
    )


def _foot(win, paint: dict, flash: str, keys: str, live: str = "") -> None:
    """The two lines every screen ends with: what just happened, and the keys.

    The keys are last and are never allowed to be the line that is lost, which
    is why the flash above them is clipped rather than wrapped: a message about
    something that has already happened is worth less than the way out.
    """
    height, width = win.getmaxyx()
    if live:
        _addstr(win, height - 3, 1, _fit(live, width - 2), curses.A_BOLD)
    if flash:
        _addstr(
            win,
            height - 4 if live else height - 3,
            1,
            _fit(flash, width - 2),
            paint.get("1;33", 0),
        )
    _addstr(win, height - 2, 1, keys, curses.A_DIM)


def preview(rows: list[dict], name: str, pos: int) -> list[dict]:
    """*rows* with the queued download *name* shown at position *pos*.

    Nothing is renamed until it is dropped: moving is done on a copy of the
    list, so ↑ and ↓ cost nothing and a move that is thought better of costs
    nothing either. It is also the only honest way to show it — the row has to
    be where it is going to be, not where it is.
    """
    queued = [row for row in rows if row["where"] == "queued"]
    rest = [row for row in rows if row["where"] != "queued"]
    moved = [row for row in queued if row["name"] == name]
    others = [row for row in queued if row["name"] != name]
    pos = max(0, min(pos, len(others)))
    return others[:pos] + moved + others[pos:] + rest


def draw_list(
    win,
    paint: dict,
    queue,
    cursor: int,
    top: int,
    flash: str,
    moving: str = "",
    pos: int = 0,
) -> int:
    """Draw the listing and return the line it was scrolled to.

    Four lines are laid down before the downloads: the title bar, the two
    header lines saying what tonight would do (:func:`tonight_lines`), and a
    blank. Inside the queued group the cut line says where tonight's spending
    stops, and it is worked out **here, every draw, from the order on the
    screen** — which is what makes it follow a held item as ↑↓ move it, and
    what makes it recompute after a drop without anything having to remember
    to ask.

    The spare row above the keys carries :data:`LEGEND_KEYS` on the screens
    that have nothing else to say there — no download in flight, no flash,
    nothing in the air — because this is the screen a bare ``dlq`` opens and
    everything the old queue screen did is behind those three keys.
    """
    win.erase()
    height, width = win.getmaxyx()
    shown = preview(queue.rows, moving, pos) if moving else queue.rows
    if moving:
        cursor = next(
            (index for index, row in enumerate(shown) if row["name"] == moving), cursor
        )
        _bar(win, paint, f" moving {_slug_of(moving)} ", "1;33")
    else:
        _bar(
            win,
            paint,
            (
                f" queue — {len(queue.rows)} in {sched._short(sched.ROOT)} "
                if width >= WIDE
                else " queue "
            ),
        )
    facts = queue.tonight.facts
    header = tonight_lines(facts, width, queue.live)
    if queue.tonight.note:
        # A reading that failed keeps the figures the last one gave and says so
        # where the verdict goes: stale figures with a word about them are
        # worth more than a screen that has gone blank.
        header[0] = _fit(queue.tonight.note, width - 2)
    for offset, text in enumerate(header):
        _addstr(win, 1 + offset, 1, text, curses.A_BOLD if not offset else 0)
    order = [row["name"] for row in shown if row["where"] == "queued"]
    reaches, ruled = cut_index(order, facts, width)
    entries = compose_rows(
        shown, width, queue.live, None if reaches is None else (reaches, ruled)
    )
    flat = [(index, text) for index, lines in entries for text in lines]
    listed = max(1, height - 7)
    if not flat:
        # An empty queue is a listing with nothing in it, not a different
        # screen: the header still says what tonight would do, and every key
        # still works.
        flat = [(None, text) for text in _wrap(EMPTY_QUEUE, width - 2)]
    mine = [line for line, (index, _) in enumerate(flat) if index == cursor]
    if mine:
        top = min(top, mine[0])
        top = max(top, mine[-1] - listed + 1)
    top = max(0, min(top, max(0, len(flat) - listed)))
    for offset in range(listed):
        line = top + offset
        if line >= len(flat):
            break
        index, text = flat[line]
        if index is None:
            attr = curses.A_BOLD
        elif index == cursor:
            attr = curses.A_REVERSE | (curses.A_BOLD if moving else 0)
        else:
            attr = paint.get(_tone(shown[index]), 0)
        _addstr(
            win, 4 + offset, 0, text.ljust(width - 1) if index == cursor else text, attr
        )
    live = "" if moving else queue.live_line(width)
    if not moving and not live and not flash:
        _addstr(win, height - 3, 1, _fit(LEGEND_KEYS, width - 2), curses.A_DIM)
    _foot(
        win,
        paint,
        flash,
        hint(
            "moving" if moving else ("list-live" if queue.mine() else "list"),
            width,
        ),
        live,
    )
    win.refresh()
    return top


def list_screen(
    win, paint: dict, queue, cursor: int, flash: str, start_moving: str = ""
) -> tuple[str, int]:
    """The listing, until a key leaves it. Returns ``(what next, cursor)``.

    Two modes. Normally this screen picks — ↑↓ and enter — and the item screen
    acts, so there is no way to change the wrong download. The exception is
    moving one, which is the single action whose whole effect is *where it is
    in this list*: it is picked up here, moved with the same two keys that were
    already moving the cursor, and dropped. Nothing is renamed until it is
    dropped, and everything else is locked out while it is in the air.

    This is also the whole queue's screen now. ``n`` runs it, ``s`` opens the
    settings and ``l`` the runner's log — the three keys the queue's own screen
    used to hold, on the screen a bare ``dlq`` opens rather than one further
    in. Each of them re-asks what tonight would do afterwards, because each of
    them can change the answer.
    """
    top = 0
    moving = start_moving
    pos = 0
    if moving:
        queued = [row["name"] for row in queue.rows if row["where"] == "queued"]
        pos = queued.index(moving) if moving in queued else 0
    while True:
        cursor = max(0, min(cursor, len(queue.rows) - 1))
        # Taken here and nowhere else: the thread only ever fills a slot, and
        # the facts the screen draws from are the ones this line assigned.
        queue.tonight.collect()
        queue.tonight.refresh()
        top = draw_list(win, paint, queue, cursor, top, flash, moving, pos)
        # Blocking while nothing is moving, so an idle screen costs no wakeups
        # at all; a second is fast enough to watch a download by. A quarter of
        # one while a reading is in the air, so the header fills itself in when
        # it lands rather than at the next keypress. Not otherwise while an
        # item is being moved: a redraw underneath a move would be the queue
        # rearranging itself around a decision that has not been taken.
        if queue.tonight.pending:
            wait = 250
        elif queue.moving() and not moving:
            wait = 1000
        else:
            wait = -1
        win.timeout(wait)
        try:
            key = win.getch()
        finally:
            win.timeout(-1)

        if moving:
            room = len([row for row in queue.rows if row["where"] == "queued"]) - 1
            if key in (curses.KEY_UP, ord("k")):
                pos -= 1
            elif key in (curses.KEY_DOWN, ord("j")):
                pos += 1
            elif key == curses.KEY_MOUSE:
                pos += ytq.read_wheel()
            elif key == curses.KEY_HOME:
                pos = 0
            elif key == curses.KEY_END:
                pos = room
            elif key in (curses.KEY_ENTER, 10, 13, ord("m")):
                said, moved = do_reorder(queue.rows, moving, pos)
                if moved:
                    queue.receipts.append(said)
                dropped, moving, flash = moving, "", said
                queue.read()
                if moved:
                    queue.tonight.start()
                # A drop that moved something renamed it, so the cursor
                # follows the POSITION it was dropped at (landed_index); one
                # that did not still knows the name it kept.
                found = (
                    landed_index(queue.rows, pos)
                    if moved
                    else queue.index_of(dropped)
                )
                cursor = cursor if found is None else found
            elif key in (ord("q"), 27):
                moving, flash = "", "left where it was"
            pos = max(0, min(pos, max(0, room)))
            continue

        if key == -1:
            # A download's figures are a second old at worst, and the row it is
            # on stays under the cursor even if the queue reordered underneath.
            mine_was = queue.mine()
            name = queue.rows[cursor]["name"] if queue.rows else ""
            queue.read()
            found = queue.index_of(name)
            cursor = cursor if found is None else found
            flash = queue.said()
            if mine_was and not queue.mine():
                # A run of ours ending is the one moment the figures behind
                # this screen really do change, and the moment someone is
                # watching for.
                queue.tonight.start()
            continue
        if key in (ord("q"), 27):
            return "quit", cursor
        if key == ord("x"):
            # Only ours: the nightly firing is not this screen's to stop, and
            # saying "stopping it" over a download that carries on is worse
            # than the key doing nothing.
            if queue.mine():
                flash = queue.stop_mine()
        elif key == ord("m"):
            if queue.rows and queue.rows[cursor]["where"] == "queued":
                moving = queue.rows[cursor]["name"]
                pos = [
                    row["name"] for row in queue.rows if row["where"] == "queued"
                ].index(moving)
                flash = ""
            else:
                flash = "only a queued download has a place in the order"
        elif key in (curses.KEY_UP, ord("k")):
            cursor -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor += 1
        elif key == curses.KEY_MOUSE:
            cursor += ytq.read_wheel()
        elif key == curses.KEY_NPAGE:
            cursor += 5
        elif key == curses.KEY_PPAGE:
            cursor -= 5
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = len(queue.rows) - 1
        elif key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
            if queue.rows:
                return "open", cursor
        elif key == ord("n"):
            flash, started = run_now(win, paint, queue)
            if started:
                queue.receipts.append(flash)
            queue.read()
            queue.tonight.start()
        elif key == ord("s"):
            flash, changed = settings_screen(win, paint)
            if changed:
                queue.receipts.append(flash)
                # The window, the reserve and the switch all change what
                # tonight would download, so the line above the list is wrong
                # until it has been asked again.
                queue.tonight.start()
        elif key == ord("l"):
            runner_log(win, paint)
        elif 32 <= key < 127:
            # Every key that changes a download is on the download's own
            # screen, and pressing one here used to do nothing at all.
            flash = "⏎ opens it; its keys are there"


# --------------------------------------------------------------------------- #
# One item, and what may be done to it
# --------------------------------------------------------------------------- #


def item_screen(win, paint: dict, queue, row: dict, flash: str) -> str:
    """One item and its actions, until a key leaves it. Returns that key.

    Every mutating key in the whole program is on this screen, under a bar
    naming the item it will act on. That is the safety rule: there is no way to
    remove the wrong download, because the only download on the screen is the
    one being removed.
    """
    keys = dict(actions_for(row))
    while True:
        win.erase()
        height, width = win.getmaxyx()
        _bar(win, paint, f" {_slug_of(row['name'])} ")
        # Asked once per draw and handed to both the head and the foot, so the
        # two can never be a second apart from each other.
        downloading = queue.downloading(row["name"])
        reading = ytq.now_progress(row["name"]) if downloading else None
        line = 2
        for text in item_lines(
            row, width, queue.place(row["name"]), downloading, reading
        ):
            if line >= height - 4:
                break
            _addstr(win, line, 2, text)
            line += 1
        line += 1
        for key, label in keys.items():
            if line >= height - 4:
                break
            _addstr(win, line, 2, key, curses.A_BOLD | paint.get("head", 0))
            _addstr(win, line, 5, _fit(label, width - 7))
            line += 1
        # When the download on screen is THIS item, the foot is a bar rather
        # than a second copy of the figures already in the head. When it is
        # some other item, the live line still names it — that is not
        # duplication, it is the only place that says so.
        if downloading:
            shown = _with_live(row, reading)
            live = progress_bar(shown["have"], shown["total"], width - 2)
        else:
            live = queue.live_line(width)
        _foot(
            win,
            paint,
            flash,
            hint("item-live" if queue.mine() else "item", width),
            live,
        )
        win.refresh()

        win.timeout(1000 if queue.moving() else -1)
        try:
            key = win.getch()
        finally:
            win.timeout(-1)
        if key == -1:
            return "tick"
        if key in (ord("q"), 27, curses.KEY_LEFT):
            return "q"
        if key == ord("x") and queue.mine():
            flash = queue.stop_mine()
            continue
        if 0 <= key < 256 and chr(key) in keys:
            return chr(key)
        # A key that does nothing says so. Silence is what made the removal bug
        # take three tries to disbelieve: on a screen where some keys work,
        # nothing happening is indistinguishable from something refusing.
        flash = "that key does nothing here" if 32 <= key < 127 else ""


def ask(
    win, paint: dict, title: str, prompt: str, initial: str, note: str = ""
) -> str | None:
    """One field, on a screen of its own. ``None`` if it was backed out of."""
    win.erase()
    height, width = win.getmaxyx()
    _bar(win, paint, f" {title} ")
    _addstr(win, 2, 2, _fit(prompt, width - 4), curses.A_BOLD)
    for offset, text in enumerate(_wrap(note, width - 4) if note else []):
        if 6 + offset < height - 2:
            _addstr(win, 6 + offset, 2, text, curses.A_DIM)
    _addstr(win, height - 2, 1, "⏎ ok   esc cancel", curses.A_DIM)
    win.refresh()
    return ytq.text_input(win, 4, 2, initial, max(8, width - 5))


def confirm(
    win,
    paint: dict,
    title: str,
    lines: list[str],
    answers: list[tuple[str, str]] | None = None,
    tone: str = "1;31",
) -> str:
    """Ask before something irreversible or something metered.

    Returns the key that was pressed of the ones offered, or ``""`` for no —
    and no is every other key, which is the way round it has to be on a phone,
    where the mis-taps this screen exists to catch are far more likely than a
    deliberate press of exactly ``y``.

    *answers* spells out more than one of them, for the question that has two:
    remove this, and remove this *and* delete the bytes. Both are on the same
    screen because the difference between them is the whole decision, and a
    screen that asked twice would be answered twice without being read once.
    Each entry carries **every key that means it**, so an answer can keep a key
    alive on an item where it has nothing extra to do — see
    :func:`removal_answers` for why that matters more than it sounds.
    """
    answers = answers or [("y", "")]
    win.erase()
    height, width = win.getmaxyx()
    _bar(win, paint, title, tone)
    at = 2
    for index, text in enumerate(lines):
        for wrapped in _wrap(text, width - 4):
            if at >= height - 4:
                break
            _addstr(win, at, 2, wrapped, curses.A_BOLD if index == 0 else 0)
            at += 1
        at += 1
    for keys, label in answers:
        if not label or at >= height - 3:
            continue
        _addstr(win, at, 2, _fit(f"{' or '.join(keys)}  {label}", width - 4))
        at += 1
    _addstr(
        win,
        height - 2,
        1,
        hint("confirm-two" if any(label for _, label in answers) else "confirm", width),
        curses.A_DIM,
    )
    win.refresh()
    key = win.getch()
    taken = "".join(keys for keys, _ in answers)
    if 0 <= key < 256 and chr(key) in taken:
        return chr(key)
    return ""


def item_log(name: str) -> Path | None:
    """The newest log this item wrote, or ``None``.

    Both spellings are matched: the runner writes ``<date>-<item>.log`` and a
    ``dlq now`` writes ``<date>-now-<item>.log``.
    """
    try:
        found = sorted(sched.LOGS.glob(f"*-{name}.log"))
    except OSError:
        return None
    return found[-1] if found else None


def log_screen(win, paint: dict, row: dict) -> None:
    """The item's own log for its last night, scrollable.

    Not the runner's log, which is the reasoning about the whole queue —
    ``dlq logs`` is that. This is the one file that says what *this* download
    was doing when it stopped, which is the question a failed item raises.
    """
    path = item_log(row["name"])
    if path is None:
        ytq.message(
            win,
            [
                "no log under this name",
                "a rename leaves the earlier ones under the old name — they "
                "are still in logs/",
                "dlq logs is the runner's own reasoning about the queue",
            ],
        )
        return
    file_screen(win, paint, path)


def file_screen(win, paint: dict, path: Path) -> None:
    """A log file, scrollable, until ``q``.

    One scroller for two logs: an item's own, which says what a download was
    doing when it stopped, and the runner's, which says what the queue as a
    whole decided. They are read for different questions and drawn the same
    way, and the drawing is the part that has to survive a phone in portrait.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError as exc:
        ytq.message(win, ["could not read the log", str(exc)])
        return

    # Opened at the end, the way `dlq logs` and every tail does: a download's
    # log is read to find out why it stopped, and that is the last line.
    top = len(text)
    while True:
        win.erase()
        height, width = win.getmaxyx()
        _bar(win, paint, f" {path.name} ")
        # Wrapped at the width there is, with continuations indented: a log
        # line cut off at the right is usually the half that said why.
        lines: list[str] = []
        for line in text:
            lines += _wrap(line, width - 3, "  ")
        listed = max(1, height - 4)
        top = max(0, min(top, max(0, len(lines) - listed)))
        for offset in range(listed):
            if top + offset >= len(lines):
                break
            _addstr(win, 2 + offset, 1, lines[top + offset])
        _addstr(win, height - 2, 1, hint("log", width), curses.A_DIM)
        win.refresh()
        key = win.getch()
        if key in (ord("q"), 27, curses.KEY_LEFT):
            return
        if key in (curses.KEY_UP, ord("k")):
            top -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            top += 1
        elif key == curses.KEY_NPAGE:
            top += listed
        elif key == curses.KEY_PPAGE:
            top -= listed
        elif key == curses.KEY_HOME:
            top = 0
        elif key == curses.KEY_END:
            top = len(lines)


# --------------------------------------------------------------------------- #
# The queue itself: what tonight would do, and the keys that change it
# --------------------------------------------------------------------------- #


def waiting(win, paint: dict, title: str, note: str, work):
    """Do *work* off the screen while the screen says what it is waiting for.

    Returns ``("ok", answer)``, ``("left", "")`` if it was abandoned, or
    ``("failed", why)``.

    Everything this is used for reads the crew portal or the Android job
    scheduler, and both are allowed to take tens of seconds — the portal's own
    timeout is 30 seconds a request and it makes several. A screen that simply
    called one would be a screen that had frozen: nothing drawn, nothing said,
    and no way out. So the call happens in a daemon thread, the wait is drawn
    and counted, and ``q`` walks away from it. The answer of an abandoned
    reading is dropped rather than kept, because arriving late on top of a
    later screen is worse than not arriving.
    """
    answer: list = []
    trouble: list[str] = []

    def run() -> None:
        try:
            answer.append(work())
        except Exception as exc:  # noqa: BLE001 - a screen may not die of one
            trouble.append(f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    started = time.monotonic()
    while True:
        worker.join(0.15)
        if not worker.is_alive():
            if trouble:
                return "failed", trouble[0]
            return ("ok", answer[0]) if answer else ("failed", "no answer")
        win.erase()
        height, width = win.getmaxyx()
        _bar(win, paint, title)
        _addstr(win, 2, 2, _fit(note, width - 4), curses.A_BOLD)
        _addstr(win, 4, 2, f"{int(time.monotonic() - started)}s", curses.A_DIM)
        _addstr(win, height - 2, 1, "q stop waiting", curses.A_DIM)
        win.refresh()
        win.timeout(150)
        try:
            key = win.getch()
        finally:
            win.timeout(-1)
        if key in (ord("q"), 27):
            return "left", ""


def tonight_facts(force: bool = False, blind: bool = False) -> dict:
    """What the runner says about tonight — its snapshot, straight across.

    One door, so that everything on this screen that talks about tonight — the
    two header lines, the cut line, the figure the run-now confirm names — is
    reading one set of figures taken at one moment. This screen decides nothing
    about the queue; it draws what the runner would do and offers the keys that
    change it.
    """
    return sched._runner().snapshot(force=force, blind=blind)


class Tonight:
    """The reading the main screen draws from, taken off the main screen.

    :func:`expire_runner.snapshot` reads the crew portal, which is a network
    call over a phone's wifi and takes a second or several. The listing is
    redrawn on every keypress, so calling it inline would be a screen that
    freezes each time somebody presses ↓. It is done in a daemon thread
    instead, and the listing draws whatever the last one brought back.

    **The thread never touches** :attr:`facts`. It fills a slot; the main
    thread empties it in :meth:`collect`, which is the only line in the program
    that assigns the facts the screen is drawn from. That is the whole of the
    thread safety here, and it is worth saying rather than assuming: curses
    draws from one thread, and a screen that read a half-assigned reading would
    be a screen showing figures that were never true together.

    A read that raises keeps the reading that is already there and leaves a
    line saying so. Blanking the figures because one refresh failed would throw
    away the only answer the screen has, and an old reading with a word about
    it is worth more than no reading at all.
    """

    #: How old a reading may get before the screen quietly asks for another.
    #: Longer than a redraw by a long way — the portal is a network call and
    #: asking it on every draw is hammering, not watching — and shorter than
    #: the window, so a screen left open through 23:00Z sees it open.
    STALE = 60.0

    def __init__(self) -> None:
        self.facts: dict | None = None
        #: What to say instead of the verdict, when the last read failed.
        self.note = ""
        self.taken = 0.0
        self._answer: list[dict] = []
        self._trouble: list[str] = []
        self._worker: threading.Thread | None = None

    @property
    def pending(self) -> bool:
        """Whether a reading is in the air. The screen polls while it is."""
        return self._worker is not None and self._worker.is_alive()

    def start(self) -> None:
        """Ask for a fresh reading, unless one is already on its way."""
        if self.pending:
            return
        self._answer, self._trouble = [], []

        def run() -> None:
            try:
                self._answer.append(tonight_facts())
            except Exception as exc:  # noqa: BLE001 - a screen may not die of one
                self._trouble.append(f"{type(exc).__name__}: {exc}")

        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def refresh(self) -> None:
        """Ask again if what is on the screen has gone stale."""
        if not self.pending and time.monotonic() - self.taken >= self.STALE:
            self.start()

    def collect(self) -> bool:
        """Take a finished reading, on the main thread. ``True`` if it landed."""
        if self._worker is None or self._worker.is_alive():
            return False
        self._worker = None
        self.taken = time.monotonic()
        if self._answer:
            self.facts = self._answer.pop()
            self.note = ""
            return True
        if self._trouble:
            self.note = _fit(f"tonight: could not read it — {self._trouble.pop()}", 200)
        return False


def armed(job: list[tuple[str, str, str]]) -> bool:
    """Whether the nightly job is registered, according to *job*.

    Read off :func:`expire_sched.job_rows`, which is what the status screen
    prints, so the screen and the key cannot disagree about it. The word is
    pinned at both ends by the self-test: spelled differently, this screen
    would offer "arm it" over a status line saying it is armed, and both would
    look right on their own.
    """
    return any(text.startswith(sched.ARMED) for _, text, _ in job)


def run_note(facts: dict) -> list[str]:
    """What the run-the-queue confirm says. The number is the whole point.

    The same question the item screen's ``n`` asks, asked of the whole queue,
    and answered from the runner's own figures: :func:`expire_runner.snapshot`
    is what decided the verdict and what named the number, so the figure agreed
    to here is the figure the run is bounded by rather than one worked out
    twice.

    Two versions, on where the phone is. With the portal answering this is the
    vessel's network and the bytes are counted like everything else. Without
    it, nothing is counting: the phone is on mobile data, or on vessel wifi
    with the portal down, and those are indistinguishable from here — so both
    are said, and the number is said in both.
    """
    queued = len(facts["items"])
    items = f"{queued} item{'' if queued == 1 else 's'}"
    size = ytq.human(max(0, facts["spendable"]))
    if not facts["blind"]:
        return [
            f"run the queue now? {items}",
            f"up to {size} of the vessel's allowance",
            "free data that expires at 00:00Z either way",
            "it runs in the background — x stops it",
        ]
    return [
        f"run the queue now? {items}",
        "zwana does not answer, so nothing is counting this",
        f"up to {size} on whatever the phone is actually using",
        "on mobile data that is metered and charged to you",
        "on vessel wifi it means the portal is down — worth checking first",
        "it runs in the background — x stops it",
    ]


class Firing:
    """The whole-queue run this screen started, if it started one.

    The item screen's ``n`` hands one download to a detached ``dlq now``; this
    hands the whole queue to a detached runner, which is the same process the
    nightly job fires and takes the same lock. Detached for the same reason:
    the download has to outlive the screen that started it, and the screen has
    to stay usable while it runs.
    """

    def __init__(self) -> None:
        self.child: subprocess.Popen | None = None
        self.blind = False

    @property
    def alive(self) -> bool:
        return self.child is not None and self.child.poll() is None

    def start(self, blind: bool) -> Path:
        """Spawn the runner and return the file its output is going to."""
        sched.LOGS.mkdir(parents=True, exist_ok=True)
        log = sched.LOGS / f"{time.strftime('%Y-%m-%d', time.gmtime())}-now-queue.log"
        handle = log.open("a", encoding="utf-8")
        try:
            self.child = subprocess.Popen(
                sched.queue_run_argv(blind),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(sched.ROOT),
            )
        finally:
            handle.close()
        self.blind = blind
        return log

    #: **SIGINT, not SIGTERM.** The download is not in the runner's process
    #: group — :func:`expire_runner.run_item` gives every item a session of its
    #: own so that the group can be killed without killing the runner — so a
    #: signal to the runner is not a signal to the download. What stops the
    #: download is the runner *unwinding*: ``run_item`` catches anything coming
    #: out of its supervisor and kills the item's tree on the way past. Only an
    #: interrupt does that. SIGTERM kills the runner where it stands and leaves
    #: yt-dlp running with nothing watching it and nothing left to stop it —
    #: spending data, on a blind run, until the phone is rebooted. This is the
    #: ctrl-c the runner is written for, sent from a screen instead of a
    #: terminal.
    SIGNAL = signal.SIGINT

    def stop(self) -> None:
        """Interrupt the run, which is what ctrl-c would have done.

        The part file stays on disk and the item stays in the queue, so the
        nightly window carries on from where this stopped — the same bargain
        the item-level stop makes, and the reason stopping one is cheap.
        """
        if self.child is None:
            return
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(self.child.pid), self.SIGNAL)


# --------------------------------------------------------------------------- #
# The actions themselves
# --------------------------------------------------------------------------- #


def removal_note(row: dict) -> list[str]:
    """What the remove screen has to say, in the order it has to say it.

    The bytes first, because that is the part that cost money and the part that
    cannot be got back without paying for it again.
    """
    _, kept = removal(row)
    files = [path for path in kept if path.is_file()]
    lines = [f"remove {_slug_of(row['name'])} from the list?"]
    if row["have"]:
        lines.append(f"the {ytq.human(row['have'])} already downloaded stays in work/")
    if files:
        lines.append(
            f"{len(files)} finished file{'s' if len(files) > 1 else ''} "
            f"stay{'' if len(files) > 1 else 's'} in out/"
        )
    if row["files"] and not files:
        lines.append("the file it delivered is not touched")
    return lines


def removal_answers(row: dict) -> list[tuple[str, str]]:
    """The keys the remove screen takes, as ``(keys, what they do)``.

    **The keys never change.** Which of them is worth offering separately does:
    a download with bytes on the disk has two different answers — keep them or
    delete them — and one with none has a single answer wearing two keys.

    That distinction used to be drawn by *withholding* ``b``, and it made the
    screen unusable in the way that is hardest to see: on an item that had
    downloaded nothing, ``b`` was not offered, so pressing it fell through to
    "any other key: no" and the download stayed. The item screen said nothing
    was removed, quietly, exactly as it would have if you had meant it — and it
    did the same thing on the next run, and the run after that. A key that is
    live on one download and means "no" on the next is worse than a key that is
    not there at all.
    """
    if row["have"]:
        size = ytq.human(row["have"])
        return [
            ("y", f"remove it, keep the {size}"),
            ("b", f"remove it and delete the {size}"),
        ]
    return [("yb", "remove it")]


def reachable() -> bool:
    """Whether the crew portal answers, within a couple of seconds.

    In a thread with a hard join, so a name lookup that hangs on a captive
    network cannot hang the screen: what does not answer in two seconds has not
    answered, which is the same conclusion by a shorter route. Erring towards
    "no" is erring towards the loud version of the question.
    """
    answer: list[bool] = []
    worker = threading.Thread(
        target=lambda: answer.append(sched._runner().portal_reachable()),
        daemon=True,
    )
    worker.start()
    worker.join(2.0)
    return bool(answer and answer[0])


def now_note(row: dict, seen: bool) -> list[str]:
    """What the download-now screen says. The number is the whole point.

    Two versions of one question, because the answer to "what will this cost"
    depends on where the phone is. With the portal answering, this is the
    vessel's network and the download is counted the way everything else is —
    it is outside the nightly window, so it comes out of what has been paid for
    rather than out of what was going to expire. Without it, *nothing* is
    counting: either the phone is on mobile data, or it is on vessel wifi with
    the portal down, and those are indistinguishable from here — so both are
    said, and the number is said in both.
    """
    size = ytq.human(max(0, row["cap"] - row["have"]))
    if seen:
        return [
            f"download {_slug_of(row['name'])} now?",
            f"spends {size} of the vessel's allowance",
            "the free half is the nightly window; this is outside it",
            "it runs in the background — x stops it",
        ]
    return [
        f"download {_slug_of(row['name'])} now?",
        "zwana does not answer, so nothing is counting this",
        f"{size} on whatever the phone is actually using",
        "on mobile data that is metered and charged to you",
        "on vessel wifi it means the portal is down — worth checking first",
        "it runs in the background — x stops it",
    ]


def _act_rename(win, paint, queue, row) -> tuple[str, bool, str]:
    _, slug = parse_name(row["name"]) or (0, "")
    typed = ask(
        win,
        paint,
        _display_name(row["name"]),
        "name — what it is listed and logged as",
        slug,
        "lowercase, dashes for anything else; ctrl-U clears the field. The "
        "finished file keeps the name it was downloaded with.",
    )
    if not typed:
        return "", False, row["name"]
    new = reslug(row["name"], typed)
    if new == row["name"]:
        return "", False, row["name"]
    said = do_rename(row, new)
    return said, said == f"now {_display_name(new)}", new


def _act_remove(win, paint, queue, row) -> tuple[str, bool, str]:
    answer = confirm(win, paint, " remove ", removal_note(row), removal_answers(row))
    if not answer:
        return "nothing removed", False, row["name"]
    said = do_remove(row, bytes_too=answer == "b")
    return said, said.startswith("removed"), ""


def _act_requeue(win, paint, queue, row) -> tuple[str, bool, str]:
    said = do_requeue(row)
    return said, said.endswith("is queued again"), row["name"]


def _act_tries(win, paint, queue, row) -> tuple[str, bool, str]:
    said = do_clear_tries(row)
    return said, said.endswith("cleared"), row["name"]


def _act_open(win, paint, queue, row) -> tuple[str, bool, str]:
    if not row["files"]:
        return "there is no file yet", False, row["name"]
    code = sched._open(row["files"][0], quiet=True)
    if code:
        return "nothing here would open it", False, row["name"]
    return f"opened {row['files'][0].name}", False, row["name"]


def _act_log(win, paint, queue, row) -> tuple[str, bool, str]:
    log_screen(win, paint, row)
    return "", False, row["name"]


def _act_now(win, paint, queue, row) -> tuple[str, bool, str]:
    """Hand the item to a detached ``dlq now``, exactly as ytq does.

    The same spawn, so an interrupted download resumes rather than restarts,
    the item stays in the queue until it finishes, and the nightly window can
    carry on from where this stopped.
    """
    if queue.running.alive:
        return (
            f"{_display_name(queue.running.name)} is already downloading",
            False,
            row["name"],
        )
    problem = busy_problem()
    if problem:
        return problem, False, row["name"]
    if not (ytq.HERE / "expire_sched.py").is_file():
        return "the queue manager is not beside the queue", False, row["name"]
    # Drawn before the wait, not after it: two seconds of a screen that has
    # not changed reads as a key that did not register.
    win.erase()
    _bar(win, paint, " download now ", "head")
    _addstr(win, 2, 2, "asking whether zwana answers…")
    win.refresh()
    seen = reachable()
    answer = confirm(
        win,
        paint,
        " download now " if seen else " download now — NOT COUNTED ",
        now_note(row, seen),
        tone="head" if seen else "1;31",
    )
    if not answer:
        return "nothing downloaded", False, row["name"]
    queue.running.start(row["name"])
    _note(
        f"started {row['name']} now, outside the window"
        + ("" if seen else " — portal unreachable, uncounted data")
    )
    said = f"downloading {_slug_of(row['name'])}"
    return said if seen else f"{said} — NOT COUNTED", True, row["name"]


def run_now(win, paint, queue) -> tuple[str, bool]:
    """Run the whole queue now: ask once, with the figures, then let it go.

    Once. This is the whole-queue spelling of the item screen's ``n`` and it
    makes the same bargain — the number is said, the counting is said, and one
    key answers it. What it deliberately does not do is ask again for the
    blind case: an unreachable portal changes what the screen says, never how
    many times it says it, because a second confirmation is not a second
    decision. It is the same decision, taken by someone who has already read
    the first screen.

    What it *will not* do is claim to have started something the runner would
    refuse. Past the stop time, with the allowance spent, or with nothing
    queued, the runner's own verdict is reported instead — those are not the
    screen being careful, they are the run not happening.
    """
    if queue.moving():
        return "something is downloading already", False
    if not any(row["where"] == "queued" for row in queue.rows):
        # Said here rather than by a portal reading that would say it a second
        # later: an empty queue is the one refusal this screen already knows.
        return "nothing queued to run", False
    problem = busy_problem()
    if problem:
        return problem, False
    state, offer = waiting(
        win,
        paint,
        " run the queue now ",
        "asking zwana what tonight has…",
        lambda: sched._runner().snapshot(force=True, blind=True),
    )
    if state == "left":
        return "nothing started", False
    if state == "failed":
        return f"could not read the queue: {offer}", False
    if offer["verdict"] not in ("go", "blind"):
        # The runner's own words for why it would not run: an empty queue, an
        # allowance already spent, or the far side of tonight's stop time.
        return offer["detail"], False
    seen = not offer["blind"]
    if not confirm(
        win,
        paint,
        " run the queue now " if seen else " run the queue — NOT COUNTED ",
        run_note(offer),
        tone="head" if seen else "1;31",
    ):
        return "nothing started", False
    log = queue.firing.start(blind=offer["blind"])
    _note(
        f"started the whole queue now, outside the window (log {log.name})"
        + ("" if seen else " — portal unreachable, uncounted data")
    )
    said = f"running the queue — {len(offer['items'])} items"
    return said if seen else f"{said} — NOT COUNTED", True


def arm_job(win, paint) -> tuple[str, bool]:
    """Register the nightly job — :func:`expire_sched.do_arm`, and nothing else.

    The same function ``dlq arm`` calls, which is the rule every verb living at
    both ends is under: a screen and a command that disagree about whether
    arming worked leave nobody able to tell.
    """
    state, said = waiting(
        win, paint, " arm ", "asking Android's scheduler…", sched.do_arm
    )
    if state == "left":
        return "nothing changed", False
    if state == "failed":
        return said, False
    worked, text = said
    if worked:
        _note(f"armed the nightly job from the screen: {text}")
    return text, worked


def cancel_job(win, paint, job) -> tuple[str, bool]:
    """Unregister the job — the one action here whose damage is silence.

    Confirmed not because it is hard to undo, but because what follows is
    nothing at all: no firing, no log, no notification, and a queue that looks
    exactly like a queue waiting for tonight.
    """
    if not armed(job):
        return "it is not armed; j registers it", False
    if not confirm(
        win,
        paint,
        " unregister ",
        [
            "stop the nightly job?",
            "nothing downloads by itself after this",
            "a arms it again, and the queue is untouched either way",
        ],
    ):
        return "still armed", False
    state, said = waiting(
        win, paint, " unregister ", "asking Android's scheduler…", sched.do_cancel
    )
    if state == "left":
        return "nothing changed", False
    if state == "failed":
        return said, False
    worked, text = said
    if worked:
        _note(f"unregistered the nightly job from the screen: {text}")
    return text, worked


def runner_log(win, paint) -> None:
    """The runner's own reasoning about the whole queue — ``l`` on the listing.

    Not an item's log, which is ``l`` on the item screen and says what one
    download was doing. This is the file that says why a night did or did not
    happen, and it is the answer to the question the header lines raise.
    """
    path = sched.LOGS / "runner.log"
    if not path.is_file():
        ytq.message(
            win,
            [
                "the runner has not written a log yet",
                f"it would be {sched._short(path)}",
                "an item's own log is l on its screen",
            ],
        )
        return
    file_screen(win, paint, path)


#: One key per destination, in :data:`expire_runner.DEST_KINDS` order. Spelled
#: here rather than inside the screen so the check that every kind is reachable
#: reads the same tuple the screen does — a short zip does not raise, it just
#: leaves the last kind with no key, which is the failure worth catching.
DEST_KEYS = (ord("v"), ord("a"), ord("f"))


def dest_screen(win, paint: dict) -> tuple[str, bool]:
    """Where finished downloads go — every destination, and any one changed.

    The only setting the queue has, and the last thing that could be changed
    from the command line alone. It matters more than it sounds on a phone:
    the default is Android's Downloads, which does not exist until
    ``termux-setup-storage`` has been run, and a destination that cannot be
    written to is a download that finishes and then stays in ``out/``.
    """
    runner = sched._runner()
    flash = ""
    while True:
        win.erase()
        height, width = win.getmaxyx()
        _bar(win, paint, " where downloads go ")
        line = 2
        kinds = list(runner.DEST_KINDS)
        config = runner.load_config()
        for kind, where in runner.dests().items():
            if line >= height - 4:
                break
            _addstr(win, line, 2, kind, curses.A_BOLD | paint.get("head", 0))
            line += 1
            for text in _wrap(sched._short(where), width - 6, "  "):
                _addstr(win, line, 4, text)
                line += 1
            note = "set" if config.get(f"{kind}_dir") else "default"
            _addstr(
                win,
                line,
                4,
                _fit(f"{note}, used by {sched.FILLED_BY[kind]}", width - 6),
                curses.A_DIM,
            )
            line += 1
            problem = runner.dest_problem(where)
            if problem:
                for text in _wrap(f"✗ {problem}", width - 6, "  "):
                    _addstr(win, line, 4, text, paint.get("31", 0))
                    line += 1
            line += 1
        _foot(win, paint, flash, hint("dest", width))
        win.refresh()
        key = win.getch()
        if key in (ord("q"), 27, curses.KEY_LEFT):
            return flash, False
        # Zipped rather than indexed, which is what let `audio` be added on
        # 2026-08-28 by extending one tuple: a destination past the end of the
        # key map makes it short rather than raising, and the screen goes on
        # working for the ones it knows. The hint names the same keys.
        picked = dict(zip(DEST_KEYS, kinds, strict=False)).get(key)
        if picked is None:
            flash = "that key does nothing here" if 32 <= key < 127 else ""
            continue
        typed = ask(
            win,
            paint,
            f" {picked} ",
            "folder finished downloads are moved into",
            str(runner.dests()[picked]),
            "ctrl-U clears the field. Type default to put the built-in one "
            "back. One level is created if it is not there.",
        )
        if not typed:
            flash = "left where it was"
            continue
        worked, said = sched.set_dest(picked, typed)
        flash = said[-1] if said else ""
        if worked:
            _note(f"{picked} downloads now go to {runner.dests()[picked]}")
            return flash, True


#: One key per setting, in :data:`expire_runner.SETTINGS` order, and the same
#: arrangement :data:`DEST_KEYS` is in for the same reason: a setting added to
#: the runner and not here would go unreachable rather than raise, since a
#: short zip is not an error. ``p`` is the reserve-when-*p*aid switch, whose
#: name is too long to take its own initial twice over; ``m`` is the *m*inimum
#: of paid data that switch wants, sitting next to it as it does in the list;
#: ``n`` is the *n*otification. None of them is ``q`` or ``x``, which are the
#: way out and the way to stop a download everywhere else in the queue.
SETTING_KEYS = (ord("w"), ord("r"), ord("p"), ord("m"), ord("a"), ord("n"))

#: The two rows that are not settings in ``config.json`` but belong on this
#: page all the same — they are the rest of what the queue's own screen used to
#: hold, and each of them is a thing you set once and then forget. ``d`` opens
#: :func:`dest_screen`; ``j`` arms the nightly job or unregisters it. They are
#: laid out exactly as the six above them and :func:`settings_body` counts them
#: as settings, so neither can be the row that scrolls off a short phone.
PAGE_KEYS = (ord("d"), ord("j"))


def _page_rows(job: list[tuple[str, str, str]] | None) -> list[tuple[str, str, str]]:
    """``(name, value, what it means)`` for the ``d`` and ``j`` rows.

    The destinations are one row rather than three: three paths is the
    destinations *screen*, and this page's job is to say whether they are worth
    opening. One folder for all three kinds is the common case and is worth
    naming; anything else is a count and a key.

    The job's own words are :func:`expire_sched.job_rows`', unchanged — the
    same text ``dlq status`` prints, so this page and that one cannot disagree
    about whether the nightly firing is registered. It is read once on the way
    in rather than per draw, because ``termux-job-scheduler`` can take seconds.
    """
    dests = sched._runner().dests()
    folders = {str(path) for path in dests.values()}
    return [
        (
            "destinations",
            (
                sched._short(next(iter(dests.values())))
                if len(folders) == 1
                else f"{len(folders)} folders"
            ),
            "where finished downloads are moved to",
        ),
        (
            "nightly job",
            job[0][1] if job else "not read",
            "the firing that downloads while you sleep",
        ),
    ]


def settings_lines(
    width: int, job: list[tuple[str, str, str]] | None = None
) -> list[tuple[int, str, str]]:
    """The settings screen's body: ``(indent, text, tone)`` per line.

    Pure and separate from the screen that draws it, on this file's rule about
    layout: what has to be checked is that nothing is clipped at 32 columns,
    and a curses screen cannot be measured offline. The tones are :data:`TONES`
    keys so the screen only has to look each one up.

    A stored value that fails its rule is said out loud in red, because the
    runner's answer to one is to use the default and carry on: silence would
    leave a ``config.json`` saying 100 minutes over a screen saying 60 with
    nothing between them to explain it. Whether a stored value is the one in
    force is :func:`expire_runner.setting_state`'s answer, not this screen's:
    a screen and a command disagreeing about what the file holds is the same
    fault as disagreeing about whether a change took.

    A file that will not parse at all is the first line, in the same red: it
    is why every setting below reads as its built-in one, and pressing any of
    the keys will be refused until it is fixed.
    """
    runner = sched._runner()
    values = runner.settings()
    lines: list[tuple[int, str, str]] = []
    broken = runner.config_problem()
    if broken:
        lines += [(2, text, "31") for text in _wrap(f"✗ {broken}", width - 4, "  ")]
    for letter, (name, spec) in zip(
        SETTING_KEYS, runner.SETTINGS.items(), strict=False
    ):
        if lines:
            lines.append((0, "", ""))
        stored, problem, note = runner.setting_state(name)
        # The value is on the name's line rather than under it, which is what
        # keeps six settings on a phone: they are two or three words each,
        # unlike the destinations' paths, and the four lines a block would
        # otherwise take put the last setting off the bottom of the screen.
        head = f"{chr(letter)}  {name}  {runner.spell_setting(name, values[name])}"
        lines.append((2, _fit(head, width - 4), "head"))
        # A value that is in the file but is being ignored is not "set": what
        # is in force is the built-in one, and the red line below says why.
        lines += [
            (5, text, "90")
            for text in _wrap(f"{note} · {spec['label']}", width - 6, "  ")
        ]
        if problem:
            lines += [
                (5, text, "31")
                for text in _wrap(
                    f"✗ config.json says {stored!r}: {problem}", width - 6, "  "
                )
            ]
    for letter, (name, value, label) in zip(PAGE_KEYS, _page_rows(job), strict=True):
        lines.append((0, "", ""))
        head = f"{chr(letter)}  {name}  {value}"
        lines.append((2, _fit(head, width - 4), "head"))
        lines += [(5, text, "90") for text in _wrap(label, width - 6, "  ")]
    return lines


def settings_body(
    width: int, height: int, job: list[tuple[str, str, str]] | None = None
) -> list[tuple[int, str, str]]:
    """:func:`settings_lines` cut down to what a screen this tall can show.

    The screen's own rule, kept out here so it is checked rather than trusted:
    the failure it exists for is a setting scrolling off the bottom, which is a
    setting nobody knows is there — and the last of them, ``notify-blocked``,
    sits under ``auto``, which is the one that stops the queue downloading at
    all.

    Two things are given up, in this order. The blank lines between the blocks
    go first, because they cost nothing but air. If it still does not fit —
    which is a 20-row phone at 32 columns, where eight rows' meanings wrap to
    two lines each — the grey line under each row goes too, and what is left is
    every row's key, name and value, plus anything red. That is the trade this
    screen makes: the word behind a value ("set" or "default") and what the
    setting means are worth giving up, since the meaning is in the docs and on
    the wide screen; a figure the phone is going to spend by, and a value
    ``config.json`` holds that is being ignored, are not.

    ``d`` and ``j`` count as settings here: they are not in ``config.json``,
    but a row nobody knows is there is the same failure whichever file it comes
    from — and ``j`` is the one that says whether anything downloads at night
    at all.
    """
    body = settings_lines(width, job)
    room = height - 6
    if len(body) > room:
        body = [line for line in body if line[1]]
    if len(body) > room:
        body = [line for line in body if line[2] != "90"]
    return body


def settings_screen(win, paint: dict) -> tuple[str, bool]:
    """What the queue may spend and how early — the settings that change it.

    :func:`dest_screen`'s shape, over the settings rather than the
    destinations, and deciding exactly as little: every value goes through
    :func:`expire_sched.set_setting`, which is the same function ``dlq
    settings`` sets through, so a screen and a command cannot disagree about
    whether a value was taken.

    The switches flip where they stand — there is nothing to type, and a
    prompt asking for the word "off" over a screen already showing "on" is a
    step for nothing. The numbers open a field with the current number in
    it: the unit is the setting's, not something anyone should have to spell,
    and the note says so along with the way to put the built-in one back,
    which is the word ``default`` typed into the same field.

    A change returns, the way a destination does, so that what it now says
    lands in the receipts and the listing's header is re-read with it in force:
    turning automatic downloads off changes the verdict at the top of that
    screen, and it should be seen to.

    ``d`` and ``j`` are the two rows that are not values in ``config.json``.
    They do not return, because what they change is *on this page* — the
    destinations row and the job row are what they change — so the answer is
    shown where the question was asked, and the receipt is carried out on the
    way past instead.
    """
    runner = sched._runner()
    flash = ""
    changed = False
    state, job = waiting(
        win, paint, " settings ", "asking Android's scheduler…", sched.job_rows
    )
    # A scheduler that will not answer is not a reason to withhold the window
    # and the reserve: the page draws, and the job row says what happened.
    job = job if state == "ok" else [("job", f"could not read it: {job}", "1;31")]
    while True:
        win.erase()
        height, width = win.getmaxyx()
        _bar(win, paint, " settings ")
        line = 2
        body = settings_body(width, height, job)
        for indent, text, tone in body:
            if line >= height - 4:
                break
            attr = paint.get(tone, 0) if tone else 0
            if tone == "head":
                attr |= curses.A_BOLD
            _addstr(win, line, indent, text, attr)
            line += 1
        _foot(win, paint, flash, hint("settings", width))
        win.refresh()
        key = win.getch()
        if key in (ord("q"), 27, curses.KEY_LEFT):
            return flash, changed
        if key == ord("d"):
            said, moved = dest_screen(win, paint)
            flash, changed = said, changed or moved
            continue
        if key == ord("j"):
            said, worked = (
                cancel_job(win, paint, job) if armed(job) else arm_job(win, paint)
            )
            flash, changed = said, changed or worked
            if worked:
                # The row above says whether it is armed, and the answer just
                # moved: ask the scheduler again rather than draw the old one.
                state, fresh = waiting(
                    win,
                    paint,
                    " settings ",
                    "asking Android's scheduler…",
                    sched.job_rows,
                )
                job = fresh if state == "ok" else job
            continue
        picked = dict(zip(SETTING_KEYS, runner.SETTINGS, strict=False)).get(key)
        if picked is None:
            flash = "that key does nothing here" if 32 <= key < 127 else ""
            continue
        spec = runner.SETTINGS[picked]
        value = runner.settings()[picked]
        if spec["kind"] == "bool":
            # The word it is *not*, said in the setting's own pair, so the
            # setter parses exactly what the screen would have printed.
            worked, said = sched.set_setting(
                picked, spec["words"][1] if value else spec["words"][0]
            )
        else:
            unit = "Minutes" if spec["kind"] == "minutes" else "MB"
            steps = f", in steps of {spec['step']}" if spec["step"] > 1 else ""
            typed = ask(
                win,
                paint,
                f" {picked} ",
                spec["label"],
                str(value),
                f"{unit}{steps}, {spec['min']} to {spec['max']}. ctrl-U clears "
                "the field. Type default to put the built-in one back.",
            )
            if not typed:
                flash = "left as it was"
                continue
            worked, said = sched.set_setting(picked, typed)
        flash = said[-1] if said else ""
        if worked:
            now = runner.spell_setting(picked, runner.settings()[picked])
            _note(f"{picked} is now {now}")
            return flash, True


#: Keys the *screen* handles rather than the item: moving a download is the one
#: action whose effect can only be seen in the list, so pressing it there goes
#: back to the list rather than doing anything on the spot.
SCREEN_KEYS = {"m"}

#: Key to what it does. Pinned against :func:`actions_for` by the self-test, so
#: an action that is offered and does nothing cannot ship.
ACTS = {
    "r": _act_rename,
    "d": _act_remove,
    "u": _act_requeue,
    "t": _act_tries,
    "o": _act_open,
    "l": _act_log,
    "n": _act_now,
}


# --------------------------------------------------------------------------- #
# The model the screens draw
# --------------------------------------------------------------------------- #


class Queue:
    """Every download, and whatever is being downloaded at this second.

    Re-read rather than patched after each action: the item list is a walk of
    three directories and a state file on local disk, which costs nothing, and
    an in-memory copy that drifts from the queue is how a screen ends up
    offering to remove something that is no longer there.

    Reading is also where the queue forgets downloads whose delivered file has
    been deleted — see :func:`forget_gone`. It happens here rather than on a
    timer or in the runner because this is the only place that both looks at
    every item and is entitled to change one; and it says so afterwards, since
    a row disappearing while someone watches is either explained or alarming.
    """

    def __init__(self) -> None:
        self.running = ytq.Running()
        #: The whole-queue run this screen started, if it started one.
        self.firing = Firing()
        #: What tonight would download, read off the screen's own thread.
        self.tonight = Tonight()
        self.rows: list[dict] = []
        self.live = ""
        #: What this session changed, printed once curses is torn down.
        self.receipts: list[str] = []
        self._forgot: list[str] = []
        self.read()
        self.tonight.start()

    def read(self) -> None:
        self.rows = sched.items()
        forgotten = forget_gone(self.rows)
        if forgotten:
            self.rows = sched.items()
            self._forgot += forgotten
            self.receipts += [
                f"forgot {name}: its file had been deleted" for name in forgotten
            ]
        self.live = sched._running_now([row["name"] for row in self.rows])

    def said(self) -> str:
        """What the last read did on its own, said once and then dropped."""
        if not self._forgot:
            return ""
        names = ", ".join(self._forgot)
        many = len(self._forgot) > 1
        self._forgot = []
        return _fit(f"forgot {names} — the file{'s' if many else ''} had gone", 200)

    def index_of(self, name: str) -> int | None:
        for index, row in enumerate(self.rows):
            if row["name"] == name:
                return index
        return None

    def place(self, name: str) -> tuple[int, int] | None:
        """``(position, how many)`` in the run order, for a queued download."""
        queued = [row["name"] for row in self.rows if row["where"] == "queued"]
        if name not in queued:
            return None
        return queued.index(name) + 1, len(queued)

    def moving(self) -> bool:
        """Whether anything is downloading, ours or the nightly job's."""
        return self.mine() or bool(self.live)

    def mine(self) -> bool:
        """Whether what is running is this screen's to stop.

        Both spellings of it: one download started with ``n`` on the item
        screen, and the whole queue started with ``n`` on the listing. A
        nightly firing is neither, and ``x`` must not offer to stop one —
        saying "stopping it" over a download that carries on is worse than
        the key doing nothing.
        """
        return self.running.alive or self.firing.alive

    def stop_mine(self) -> str:
        """Stop whichever of ours is running, and say which."""
        if self.running.alive:
            self.running.stop()
            return "stopping it; what is downloaded is kept"
        if self.firing.alive:
            self.firing.stop()
            return "stopping the run; what is downloaded is kept"
        return ""

    def downloading(self, name: str) -> bool:
        """Whether *name* is the download in flight, ours or the runner's.

        Both halves matter: ``running`` is the one this session started with
        ``n``, ``live`` is what :func:`sched._running_now` sees writing its
        status file — a nightly firing, or a ``dlq now`` in another terminal.
        """
        return self.live == name or (self.running.alive and self.running.name == name)

    def live_line(self, width: int) -> str:
        """The one line that says something is downloading, or nothing.

        Ours first: a download this session started is the one the ``x`` key
        stops, and saying so is what makes that key make sense.
        """
        if self.running.alive:
            return self.running.line(width)
        if self.live:
            return ytq.progress_line(self.live, ytq.now_progress(self.live), width)
        if self.firing.alive:
            # Started, and not downloading yet: the runner reads the portal and
            # picks an item first. A blank line here reads as a run that never
            # started, which is the one thing it is not.
            return _fit("running the queue — starting", width - 2)
        return ""


def app(win) -> list[str]:
    """The whole screen: pick something, then do something to it.

    Two screens, and the listing is the first one — there is no third to step
    up to any more. What the queue's own screen held is on the listing (``n``,
    ``s``, ``l``) and behind ``s`` (the destinations, the nightly job), so an
    empty queue no longer bounces anywhere: it is a listing with nothing in it,
    under a header that still says what tonight would have done.

    Returns the changes made, to be printed once curses is torn down — the
    same shape ytq uses, and for the same reason: what a session did should
    survive the screen it did it on, because the terminal is where anyone
    looks to see what they just agreed to.
    """
    curses.curs_set(0)
    win.keypad(True)
    # Touch scrolling, ytq's own switch: wheels only, so a tap can never
    # press a key — this screen has keys that fire downloads.
    ytq.enable_touch_scroll()
    paint = ink(win)
    queue = Queue()
    cursor = 0
    flash = queue.said()
    screen = "list"
    pick_up = ""

    while True:
        cursor = max(0, min(cursor, max(0, len(queue.rows) - 1)))

        if screen == "list":
            what, cursor = list_screen(win, paint, queue, cursor, flash, pick_up)
            flash, pick_up = "", ""
            if what == "quit":
                return queue.receipts
            screen = "item"
            continue

        row = queue.rows[cursor]
        key = item_screen(win, paint, queue, row, flash)
        flash = ""
        if key == "q":
            screen = "list"
            continue
        if key == "m":
            # The one action the item screen cannot show the result of: it
            # hands back to the list with the download already in the air.
            screen, pick_up = "list", row["name"]
            continue
        if key == "tick":
            queue.read()
            flash = queue.said()
            found = queue.index_of(row["name"])
            if found is None:
                screen = "list"
            else:
                cursor = found
            continue
        act = ACTS.get(key)
        if act is None:
            continue
        flash, changed, focus = act(win, paint, queue, row)
        if changed:
            queue.receipts.append(flash)
            # A removal, a requeue, a rename, a download started: each of them
            # changes what tonight would download, and the line that says so is
            # wrong until it has been asked again.
            queue.tonight.start()
        queue.read()
        found = queue.index_of(focus) if focus else None
        if found is None:
            screen = "list"
        else:
            cursor = found


# --------------------------------------------------------------------------- #
# Checking it
# --------------------------------------------------------------------------- #


def _self_test() -> int:
    """Offline checks on the names, the moves and the layout. No terminal.

    Two failures are worth the length of this, and both are silent.

    **Bytes losing their item.** A rename that moves the queue file and not
    ``work/`` leaves gigabytes of paid-for download under a name nothing looks
    for, and the item starts again from zero on the next night without a word
    in any log. So the moves are checked by *doing* them, on a real temporary
    queue read through :func:`expire_sched.items` — the same reader the screens
    use, so the two cannot drift apart.

    **A download quietly missing from the screen.** This is the screen things
    are removed from, and a row that did not fit is indistinguishable from a
    row that is not there. So every width is checked to draw every item exactly
    once, and the figures are checked to be the thing that never gets clipped.
    """
    import contextlib
    import itertools
    import json
    import tempfile

    passed = failed = 0
    terminal = sys.stdout

    def check(label: str, got, want) -> None:
        nonlocal passed, failed
        if got == want:
            passed += 1
        else:
            failed += 1
            print(f"FAIL {label}: got {got!r}, want {want!r}", file=terminal)

    def at_most(label: str, got: int, limit: int) -> None:
        nonlocal passed, failed
        if got <= limit:
            passed += 1
        else:
            failed += 1
            print(f"FAIL {label}: {got} exceeds {limit}", file=terminal)

    # ----------------------------------------------------------------- names
    check(
        "a name is a number, a dash, a slug and .py",
        parse_name("40-a-b.py"),
        (40, "a-b"),
    )
    check("one digit is not a priority", parse_name("4-x.py"), None)
    check("a day directory is not an item", parse_name("2026-08-20"), None)
    check("a name cannot hold a path", parse_name("40-../x.py"), None)
    long_slug = "a" * 60
    check(
        "renumbering keeps every character of a long name",
        renumber(f"40-{long_slug}.py", 10),
        f"10-{long_slug}.py",
    )
    check(
        "a typed name goes through ytq's rule",
        reslug("40-x.py", "Some Talk!"),
        "40-some-talk.py",
    )
    check("renaming keeps the priority", reslug("60-x.py", "other"), "60-other.py")
    check("ui is one of dlq's actions", sched._action(["ui"]), "ui")

    # ------------------------------------------------------------------ hints
    check("both hint sets answer the same screens", set(HINTS), set(TIGHT_HINTS))
    for name, text in HINTS.items():
        at_most(f"the {name} hints fit a phone", len(text), ytq.HINT_WIDTH)
    for name, text in TIGHT_HINTS.items():
        at_most(f"the tight {name} hints fit", len(text), ytq.TIGHT_WIDTH)
    for name in HINTS:
        check(f"{name} picks the tight hints at 32", hint(name, 32), TIGHT_HINTS[name])
        check(f"{name} picks the full hints at 80", hint(name, 80), HINTS[name])

    # The signpost on the screen a bare `dlq` opens. It is drawn where the
    # hints are drawn, so it gets their budget; and the three keys it names are
    # the whole of what the queue's own screen used to be.
    for width in (32, 40, 80):
        at_most(f"the legend row fits {width} columns", len(LEGEND_KEYS), width - 2)
    for key in ("n ", "s ", "l "):
        check(f"the legend names {key.strip()}", key in LEGEND_KEYS, True)
    check("and nothing on it sends you to a screen that is gone",
          "queue" in LEGEND_KEYS, False)

    # ------------------------------------------------------ the nightly job
    armed_job = [("job", f"{sched.ARMED}, fires every 15m", "32")]
    idle_job = [("job", "not armed - dlq arm", "1;31")]
    check("an armed job reads as armed", armed(armed_job), True)
    # The one that would be wrong with `in` instead of `startswith`, and the
    # symptom is an arm key offering to arm what the line above says is armed.
    check("and 'not armed' does not", armed(idle_job), False)
    check("nor does a machine with no scheduler at all", armed([]), False)

    # `dest_screen` zips its keys against the runner's kinds, which is what let
    # `audio` be added by extending a tuple — and is also what would leave a
    # kind with no key at all, silently, since a short zip does not raise. So
    # the key map has to be at least as long as the list of kinds, and the hint
    # has to name every key at both widths.
    kinds = sched._runner().DEST_KINDS
    check("every destination has a key to reach it", len(DEST_KEYS) >= len(kinds), True)
    for letter, kind in zip(DEST_KEYS, kinds, strict=False):
        for span in (80, 32):
            check(
                f"and the {kind} key is on the hints at {span}",
                f"{chr(letter)} " in hint("dest", span),
                True,
            )

    sample = [
        {
            "name": "40-alpha.py",
            "cap": 500_000_000,
            "partial": 0,
            "desc": "a test download",
            "attempts": 0,
        }
    ]

    # ------------------------------------------------------- what tonight says
    # The two header lines. Every verdict the gate can reach has to have words
    # for it at every width, because this is the first thing anyone reads and a
    # blank line where the answer goes is a screen that has given up.
    for verdict, (headline, _hue) in sched.VERDICTS.items():
        check(
            f"the {verdict} verdict has a short spelling",
            verdict in TONIGHT_SHORT,
            True,
        )
        # The long spelling is the status screen's own headline, read out of
        # VERDICTS rather than copied: two screens with two sets of words for
        # one night is the failure this avoids by not having a second set.
        at_most(
            f"and the short one is shorter than {headline!r}",
            len(TONIGHT_SHORT[verdict]),
            len(headline),
        )
    for verdict in sched.VERDICTS:
        if verdict == "downloading":
            continue
        facts = sched._fake_facts(verdict, items=sample)
        for width in (32, 40, 80):
            lines = tonight_lines(facts, width)
            check(f"{verdict} at {width} is two lines", len(lines), 2)
            for line in lines:
                at_most(f"a {verdict} header line fits {width}", len(line), width - 2)
            check(
                f"and the {verdict} verdict line says something",
                lines[0].startswith("tonight: "),
                True,
            )
    # Before the first reading has come back. Not a blank and not a zero: the
    # screen opens on this, and a figure nobody has read is worse than a word.
    for width in (32, 40, 80):
        opening = tonight_lines(None, width)
        check(f"with no reading yet it says it is asking at {width}",
              opening, [_fit(ASKING, width - 2), ""])
    # The download in flight outranks every verdict, exactly as it does on the
    # status screen: what is being spent now is the answer to what is happening.
    check(
        "a download in flight is what the line says",
        tonight_lines(sched._fake_facts("early", items=sample), 80, "40-alpha.py")[0],
        "downloading now: alpha",
    )
    # Each of the four questions the figures line answers.
    check(
        "the figures name what may be spent and what expires",
        tonight_lines(sched._fake_facts("early", items=sample), 80)[1].startswith(
            f"{ytq.human(480 * 1024 * 1024)} to spend · "
        ),
        True,
    )
    check(
        "and at 40 they are the two figures without the clock",
        tonight_lines(sched._fake_facts("early", items=sample), 40)[1].endswith(
            "expires"
        ),
        True,
    )
    check(
        "the switch being off carries the way to turn it back on",
        "settings auto on" in tonight_lines(sched._fake_facts("off"), 80)[1],
        True,
    )
    blind_line = tonight_lines(sched._fake_facts("blind", items=sample), 80)[1]
    check("a blind night says whose data it is", "mobile data" in blind_line, True)
    check("and that nothing is counting it", "counting" in blind_line, True)
    check(
        "no reading at all is the portal's own complaint",
        "credentials" in tonight_lines(sched._fake_facts("no-portal"), 80)[1],
        True,
    )

    # ------------------------------------------------------------ the cut line
    # Three items about the same size and a budget for two of them. The bytes
    # are the runner's own arithmetic, so this pins the screen's use of it and
    # not a second copy: what is checked is where the line falls.
    trio = ["10-one.py", "20-two.py", "30-three.py"]
    trio_rows = [{"name": name, "where": "queued"} for name in trio]
    trio_facts = sched._fake_facts(
        "go",
        spendable=450 * 1024 * 1024,
        bps=8 * 1024 * 1024,
        items=[
            {"name": name, "cap": 200 * 1024 * 1024, "desc": name, "attempts": 0}
            for name in trio
        ],
    )
    check("no reading is no line", cut_index(trio, None, 40), (None, ""))
    check("and neither is an empty queue", cut_index([], trio_facts, 40), (None, ""))
    cut, drawn = cut_index(trio, trio_facts, 40)
    check("two of the three fit tonight", cut, 2)
    # What it names is what tonight SPENDS, not what it is allowed to: the two
    # are different numbers and only one of them is a promise.
    check(
        "and the line says how much that is",
        ytq.human(2 * 200 * 1024 * 1024) in drawn,
        True,
    )
    # THE ONE KEYPRESS. `three` is picked up and moved up once; it lands above
    # `two`, and the line recomputes to fall between them. Nothing was renamed
    # to do it and no cursor stopped on the line — it is worked out from the
    # preview order and from nothing else.
    lifted = [row["name"] for row in preview(trio_rows, "30-three.py", 1)]
    check("one keypress puts three second", lifted, [trio[0], trio[2], trio[1]])
    moved_cut, moved_line = cut_index(lifted, trio_facts, 40)
    check("and the line falls between three and two", moved_cut, 2)
    check("still naming the same night's spending", moved_line, drawn)
    check(
        "which is to say two is now the one below it",
        lifted[moved_cut],
        "20-two.py",
    )
    # The mirror of it: `two` pushed down once puts the same pair above the
    # line, which is the same fact arrived at from the other end.
    pushed = [row["name"] for row in preview(trio_rows, "20-two.py", 2)]
    check("pushing two down once is the same order", pushed, lifted)
    check("and the same line", cut_index(pushed, trio_facts, 40), (moved_cut, moved_line))
    # The line's own width, at the three the rest of this file is checked at.
    for width in (32, 40, 80):
        for order in (trio, lifted):
            _, text = cut_index(order, trio_facts, width)
            at_most(f"the cut line fits {width}", len(text), width - 1)
            check(f"and is drawn as a rule at {width}", text[:2], "──")
    check(
        "at 32 it is the short spelling",
        cut_index(trio, trio_facts, 32)[1].startswith("── tonight: "),
        True,
    )

    # Nothing tonight: the line goes to the top of the group and says why.
    # A verdict that stops the night is quoted in the status screen's words;
    # a night that would run and cannot afford anything gets the runner's own
    # refusal, which is the sentence it would have logged.
    for verdict in ("off", "late", "no-portal", "stale", "empty", "spent"):
        stopped = sched._fake_facts(
            verdict, items=trio_facts["items"], spendable=0, bps=8 * 1024 * 1024
        )
        where, text = cut_index(trio, stopped, 80)
        check(f"a {verdict} night reaches nothing", where, 0)
        check(
            f"and the line names the {verdict} verdict",
            sched.VERDICTS[verdict][0] in text,
            True,
        )
    broke = sched._fake_facts(
        "go", items=trio_facts["items"], spendable=1024, bps=8 * 1024 * 1024
    )
    where, text = cut_index(trio, broke, 80)
    check("a night that can afford nothing reaches nothing", where, 0)
    check(
        "and says so in the runner's own words",
        "spendable" in text,
        True,
    )

    # ------------------------------------------- it can never promise too much
    # The invariant the whole line rests on, checked the only way it is worth
    # checking: over every order the screen could be put in. Reordering changes
    # which items the budget reaches. It never changes the budget.
    four = [
        {"name": "10-part-small.py", "cap": 300 * 1024 * 1024, "partial": 1,
         "slice_min": 32 * 1024 * 1024, "part_bytes": 100 * 1024 * 1024,
         "desc": "", "attempts": 0},
        {"name": "20-part-big.py", "cap": 4 * 1024 * 1024 * 1024, "partial": 1,
         "slice_min": 32 * 1024 * 1024, "part_bytes": 0, "desc": "", "attempts": 0},
        {"name": "30-whole.py", "cap": 150 * 1024 * 1024, "partial": 0,
         "desc": "", "attempts": 0},
        {"name": "40-too-big.py", "cap": 9 * 1024 * 1024 * 1024, "partial": 0,
         "desc": "", "attempts": 0},
    ]
    budget = 400 * 1024 * 1024
    for shape in ("go", "blind"):
        spread_facts = sched._fake_facts(
            shape, items=four, spendable=budget, bps=4 * 1024 * 1024
        )
        for order in itertools.permutations([item["name"] for item in four]):
            names = list(order)
            planned = tonight_plan(names, spread_facts)
            spent = sum(entry["bytes"] for entry in planned)
            at_most(
                f"a {shape} night in {names} spends no more than the budget",
                spent,
                spread_facts["spendable"],
            )
            reaches, _ = cut_index(names, spread_facts, 40)
            got = {entry["name"] for entry in planned if entry["bytes"]}
            check(
                f"and everything it reaches is above the line in {names}",
                {name for name in names[: reaches or 0]} >= got,
                True,
            )
            check(
                f"with nothing below it getting anything in {names}",
                {name for name in names[reaches or 0 :]} & got,
                set(),
            )

    # --------------------------------------------------------------- settings
    # The screen that changes what the queue may spend. Three failures worth
    # the length of this: a setting the runner grew that no key reaches, since
    # a short zip leaves it unreachable rather than raising; a line clipped on
    # a 32-column phone, which is a figure about money going missing; and a
    # value in config.json that is being ignored with nothing on the screen
    # saying so, which is a screen and a file disagreeing in silence.
    runner = sched._runner()
    check(
        "every setting has a key to reach it",
        len(SETTING_KEYS) >= len(runner.SETTINGS),
        True,
    )
    for letter, name in zip(SETTING_KEYS, runner.SETTINGS, strict=False):
        for span in (80, 32):
            check(
                f"and the {name} key is on the settings hints at {span}",
                f"{chr(letter)} " in hint("settings", span),
                True,
            )
    # Everything below reads a config file of our own. The real one is the
    # phone's own: a self-test that answers out of it changes its answers the
    # day somebody sets a reserve, and one that writes it would set one.
    was_config = runner.CONFIG_FILE
    try:
        with tempfile.TemporaryDirectory() as folder:
            runner.CONFIG_FILE = Path(folder) / "config.json"

            def store(config: dict) -> None:
                runner.CONFIG_FILE.write_text(json.dumps(config), encoding="utf-8")

            for said, stored in (
                ("nothing set", {}),
                ("a window set", {"window_minutes": 120}),
                ("a window stored that is refused", {"window_minutes": 100}),
            ):
                store(stored)
                for width in (32, 40, 80):
                    for indent, text, _colour in settings_lines(width):
                        at_most(
                            f"a settings line with {said} fits {width} columns",
                            indent + len(text),
                            width - 1,
                        )
                    blocks = [
                        tone for _, _, tone in settings_lines(width) if tone == "head"
                    ]
                    check(
                        f"every setting has a block at {width} with {said}",
                        len(blocks),
                        len(runner.SETTINGS) + len(PAGE_KEYS),
                    )

            store({})
            texts = [text for _, text, _ in settings_lines(80)]
            check(
                "with nothing set the built-in window is shown",
                any(runner.spell_setting("window", 60) in text for text in texts),
                True,
            )
            check(
                "and it says the value is the built-in one",
                any(text.startswith("default · ") for text in texts),
                True,
            )
            # A setting scrolling off the bottom is the failure this screen
            # cannot have: `auto` stops the queue downloading at all and
            # `notify-blocked` sits below it. What the screen gives up to fit
            # is settings_body's business — the blanks, then the grey meaning
            # lines — and what it must come to is the shortest phone there is:
            # 20 rows, less the bar, the flash, the keys and the margins.
            for width in (32, 40, 80):
                shown = settings_body(width, 20)
                at_most(
                    f"every setting fits a 20-row phone at {width}",
                    len(shown),
                    20 - 6,
                )
                check(
                    f"and every one of them is named at {width}",
                    [tone for _, _, tone in shown].count("head"),
                    len(runner.SETTINGS) + len(PAGE_KEYS),
                )
                check(
                    f"with its value beside it at {width}",
                    all(
                        runner.spell_setting(name, runner.settings()[name]) in text
                        for name, (_, text, _) in zip(
                            runner.SETTINGS,
                            [line for line in shown if line[2] == "head"],
                            strict=False,
                        )
                    ),
                    True,
                )
            # A screen with the room keeps what the short one gave up.
            check(
                "a tall screen keeps what each setting means",
                any(tone == "90" for _, _, tone in settings_body(40, 40)),
                True,
            )
            store({"window_minutes": 120})
            texts = [text for _, text, _ in settings_lines(80)]
            check(
                "a value that is set is the one shown",
                any("120 min" in text for text in texts),
                True,
            )
            check(
                "and it says it was set",
                any(text.startswith("set · ") for text in texts),
                True,
            )
            # The one the runner is quiet about: it uses the default and does
            # not raise, so this screen is where the file gets contradicted.
            store({"window_minutes": 100})
            drawn = settings_lines(80)
            texts = [text for _, text, _ in drawn]
            check(
                "a refused value falls back to the built-in one",
                any("60 min" in text for text in texts),
                True,
            )
            check(
                "and the value being ignored is named",
                any(text.startswith("✗ config.json says 100") for text in texts),
                True,
            )
            check(
                "in the colour a problem is said in",
                {tone for _, text, tone in drawn if text.startswith("✗")},
                {"31"},
            )
            check(
                "and it is not called set",
                any(text.startswith("set · ") for text in texts),
                False,
            )

            # The same rule with a problem taking up room: the wrapped red
            # line may cost the meaning lines, never a setting's name and never
            # the value being declined.
            for width in (32, 40):
                shown = settings_body(width, 20)[: 20 - 6]
                check(
                    f"every setting is still named on a 20-row phone at {width}",
                    [tone for _, _, tone in shown].count("head"),
                    len(runner.SETTINGS) + len(PAGE_KEYS),
                )
                declined = "✗ config.json says 100"
                check(
                    f"and the value being ignored is still said at {width}",
                    any(text.startswith(declined) for _, text, _ in shown),
                    True,
                )

            # A stored null. The runner refuses it like any other value it
            # cannot use, and this screen and `dlq settings` have to say the
            # same thing about it: the pair used to disagree, this screen
            # calling it refused and the command calling it nothing at all.
            store({"auto": None})
            drawn = settings_lines(80)
            texts = [text for _, text, _ in drawn]
            check(
                "a stored null is named as the value being declined",
                any(text.startswith("✗ config.json says None") for text in texts),
                True,
            )
            check(
                "and the setting reads as the built-in one",
                any(text.startswith("default · ") for text in texts),
                True,
            )
            check("which is what the switch is", runner.auto_enabled(), True)

            # A file that will not parse. Every setting falls back, and the
            # screen would otherwise show four defaults with nothing saying
            # why — and its keys would rewrite the file down to one key.
            runner.CONFIG_FILE.write_text('{"auto": false, }\n', encoding="utf-8")
            for width in (32, 40, 80):
                drawn = settings_lines(width)
                for indent, text, _colour in drawn:
                    at_most(
                        f"a settings line over a broken config fits {width}",
                        indent + len(text),
                        width - 1,
                    )
                check(
                    f"the file's own fault is said at {width}",
                    any(
                        text.startswith("✗ config.json will not")
                        for _, text, _ in drawn
                    ),
                    True,
                )
                check(
                    f"in the colour a problem is said in at {width}",
                    {tone for _, text, tone in drawn if text.startswith("✗")},
                    {"31"},
                )
            check(
                "and no setting claims to be set out of it",
                any(text.startswith("set · ") for _, text, _ in settings_lines(80)),
                False,
            )
            # And the key that would have written it refuses, with the same
            # line: a screen that flashed "auto is now off" over a file it had
            # just replaced with one key is the failure being pinned.
            worked, said = sched.set_setting("auto", "off")
            check("a change over a broken config is refused", worked, False)
            check(
                "and the flash is the file's own fault",
                said[-1].startswith("config.json will not parse"),
                True,
            )

            # The two rows that are not values in this file. Both are laid out
            # like the six that are, both answer to a key on the same hint
            # line, and neither may be the row that scrolls off a short phone —
            # `j` is the one that says whether anything downloads at night at
            # all, which is the question `auto` above it only half answers.
            store({})
            for width in (32, 40, 80):
                for job in (armed_job, idle_job, None):
                    drawn = settings_lines(width, job)
                    for indent, text, _colour in drawn:
                        at_most(
                            f"a settings line with the two rows fits {width}",
                            indent + len(text),
                            width - 1,
                        )
                    heads = [text for _, text, tone in drawn if tone == "head"]
                    check(
                        f"the eight rows all have a block at {width}",
                        len(heads),
                        len(runner.SETTINGS) + len(PAGE_KEYS),
                    )
                    check(
                        f"the destinations row is one of them at {width}",
                        any(text.startswith("d  destinations") for text in heads),
                        True,
                    )
                    check(
                        f"and the nightly job is the last of them at {width}",
                        heads[-1].startswith("j  nightly job"),
                        True,
                    )
                    shown = settings_body(width, 20, job)
                    at_most(
                        f"and every one of them fits a 20-row phone at {width}",
                        len(shown),
                        20 - 6,
                    )
                    check(
                        f"with none of them dropped at {width}",
                        [tone for _, _, tone in shown].count("head"),
                        len(runner.SETTINGS) + len(PAGE_KEYS),
                    )
            # The job row is `job_rows`' own words, not a second spelling of
            # them: a page saying "not armed" over a status screen saying armed
            # is two screens each right on their own.
            check(
                "the job row says what the status screen says",
                any(
                    text.endswith(armed_job[0][1])
                    for _, text, tone in settings_lines(80, armed_job)
                    if tone == "head"
                ),
                True,
            )
            for letter in PAGE_KEYS:
                for span in (80, 32):
                    check(
                        f"and {chr(letter)} is on the settings hints at {span}",
                        f"{chr(letter)} " in hint("settings", span),
                        True,
                    )
    finally:
        runner.CONFIG_FILE = was_config

    # What the run-the-queue confirm says. The same three things the item-level
    # one is checked for: the number is said before it is spent, one key
    # answers it, and the uncounted version says what is not counting it.
    for blind in (False, True):
        facts = sched._fake_facts("blind" if blind else "go", items=sample)
        said = "\n".join(run_note(facts))
        check(
            f"the number is said before it is spent (blind={blind})",
            ytq.human(facts["spendable"]) in said,
            True,
        )
        check(f"and one key answers it (blind={blind})", said.count("?"), 1)
        check(f"and how many downloads it is (blind={blind})", "1 item" in said, True)
    loud = "\n".join(run_note(sched._fake_facts("blind", items=sample)))
    check("an unreachable portal says nothing is counting", "counting" in loud, True)
    check("and says the phone's own data is what pays", "metered" in loud, True)
    check(
        "and does not claim to know which it is",
        "vessel wifi" in loud and "mobile data" in loud,
        True,
    )
    # The figure is the runner's own, not one worked out here: whatever the
    # snapshot says is spendable is what the offer names.
    tight = sched._fake_facts("blind", items=sample, spendable=12_345_678)
    check(
        "the offer names the snapshot's figure",
        ytq.human(12_345_678) in "\n".join(run_note(tight)),
        True,
    )

    # Waiting for something slow, without the screen going dead. Both answers
    # that never touch the terminal: one that comes straight back, and one
    # that raises — a screen may not die of a portal read.
    check(
        "an answer that comes at once is the answer",
        waiting(None, {}, "", "", lambda: 7),
        ("ok", 7),
    )
    state, why = waiting(None, {}, "", "", lambda: 1 / 0)
    check("and a failure is a reason, not a traceback", state, "failed")
    check("which names what went wrong", "ZeroDivisionError" in why, True)

    # ------------------------------------------- the reading the screen waits
    # The listing is redrawn on every keypress and the portal takes seconds, so
    # the reading is done in a thread. Two things are pinned, and both are
    # about what the main thread is allowed to see. **The thread never assigns
    # the facts**: it fills a slot, and collect() — called from the draw loop
    # and nowhere else — is the one line that moves a reading onto the screen,
    # so a half-taken reading can never be drawn. And **a read that fails keeps
    # the one before it** and leaves a line saying so, because an old reading
    # with a word about it beats a screen that has gone blank.
    was_reader = tonight_facts

    def settled(holder: Tonight) -> None:
        for _ in range(400):
            if not holder.pending:
                return
            time.sleep(0.01)

    try:
        globals()["tonight_facts"] = lambda: {"verdict": "go", "reading": 1}
        holder = Tonight()
        check("nothing is on the screen before it has been asked", holder.facts, None)
        holder.start()
        settled(holder)
        check("the thread does not assign what is drawn", holder.facts, None)
        check("the main thread does, in collect", holder.collect(), True)
        check("and what it took is what came back", holder.facts["reading"], 1)
        check("a fresh reading is not asked for again", holder.pending, False)

        def blow_up() -> dict:
            raise OSError("no route to the portal")

        globals()["tonight_facts"] = blow_up
        holder.start()
        settled(holder)
        check("a failed read lands nothing", holder.collect(), False)
        check("and keeps the reading before it", holder.facts["reading"], 1)
        check("saying so in one line", "no route to the portal" in holder.note, True)
        check("that goes where the verdict goes", holder.note.startswith("tonight: "),
              True)
        globals()["tonight_facts"] = lambda: {"verdict": "go", "reading": 2}
        holder.start()
        settled(holder)
        holder.collect()
        check("a reading that works takes the note away", holder.note, "")
        check("and is the one on the screen", holder.facts["reading"], 2)
    finally:
        globals()["tonight_facts"] = was_reader

    # ------------------------------------------------------------- the order
    check("room between two items is used", slot([10, 30], 1), 20)
    check("the top of an empty queue", slot([], 0), 10)
    check("above everything", slot([20, 30], 0), 10)
    check("with room left on both sides of it", slot([2, 30], 0), 1)
    check("and above something already at the top", slot([5, 30], 0), 2)
    check("below everything", slot([10, 20], 2), 30)
    check("no room between neighbours one apart", slot([10, 11], 1), None)
    check("nor at the top when there is nothing below zero", slot([0, 10], 0), None)
    # Two digits, always: the runner sorts file names, so a three-digit key
    # sorts *before* a two-digit one and "move it to the back" would put it at
    # the front. No room is the signal to hand out fresh keys, not to grow one.
    check("nor past the last two-digit key", slot([99], 1), None)
    check("fresh keys are spread through the two digits", spread(3), [22, 44, 66])
    check(
        "and get tighter as the queue grows",
        spread(30),
        [2 * (index + 1) for index in range(30)],
    )
    check("every fresh key fits two digits", max(spread(89)) <= ytq.MAX_PRIORITY, True)
    check(
        "and they sort the way they are numbered",
        sorted(f"{key:02d}-x.py" for key in spread(12)),
        [f"{key:02d}-x.py" for key in spread(12)],
    )
    check("a position reads as a position", _ordinal(3), "3rd")
    # The two sentences a screen says when a key does nothing. They are flashes,
    # which are clipped rather than wrapped, so they have to fit the floor.
    for said in ("that key does nothing here", "⏎ opens it; its keys are there"):
        at_most(f"{said!r} fits the narrowest screen", len(said), 32 - 2)
    check(
        "the number is not what the screen calls it", _slug_of("40-a-film.py"), "a-film"
    )

    # ------------------------------------------------- the keys a screen takes
    # The failure this pins is one that shipped: `b` was withheld from an item
    # that had downloaded nothing, so pressing it fell through to "any other
    # key: no" and the download stayed — silently, and identically on every
    # rerun and relaunch, because the screen looked exactly as it does when the
    # answer really was no. Which answers are worth *spelling out* may depend on
    # the item. Which keys are *taken* may not.
    shapes = [
        {"have": 0, "name": "40-a.py", "files": [], "where": "queued"},
        {"have": 1_000_000, "name": "40-a.py", "files": [], "where": "queued"},
        {"have": 0, "name": "40-a.py", "files": [Path("x")], "where": "done"},
        {"have": 5, "name": "40-a.py", "files": [], "where": "failed"},
    ]
    taken = {"".join(keys for keys, _ in removal_answers(shape)) for shape in shapes}
    check("the remove screen takes the same keys whatever the item", taken, {"yb"})
    check(
        "and every one of them is spelled out on it",
        all(label for _, label in removal_answers(shapes[0])),
        True,
    )

    # ---------------------------------------------------------------- a queue
    @contextlib.contextmanager
    def tree():
        """A real queue in a temporary directory, read by the real reader."""
        runner = sched._runner()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "expire"
            here = {
                "ROOT": root,
                "QUEUE": root / "queue",
                "WORK": root / "work",
                "OUT": root / "out",
                "DONE": root / "done",
                "FAILED": root / "failed",
                "LOGS": root / "logs",
            }
            for path in here.values():
                path.mkdir(parents=True, exist_ok=True)
            kept_sched = {name: getattr(sched, name) for name in here}
            kept_runner = {name: getattr(runner, name) for name in here}
            kept_state = runner.STATE_FILE
            kept_here = ytq.HERE
            try:
                for name, path in here.items():
                    setattr(sched, name, path)
                    setattr(runner, name, path)
                runner.STATE_FILE = root / "state.json"
                # queue_busy opens ytq.HERE/runner.lock, and the real one must
                # not be touched by a check.
                ytq.HERE = root
                yield root
            finally:
                for name, path in kept_sched.items():
                    setattr(sched, name, path)
                for name, path in kept_runner.items():
                    setattr(runner, name, path)
                runner.STATE_FILE = kept_state
                ytq.HERE = kept_here

    def build(root: Path) -> dict:
        """Four items: queued and part-downloaded, failed, done, done-and-gone."""
        item = "# EXPIRE: v1\n# EXPECT_BYTES: 1000000\n# DESC: {}\n"
        (root / "queue" / "40-alpha.py").write_text(item.format("alpha the first"))
        (root / "failed" / "60-beta.py").write_text(item.format("beta the second"))
        day = root / "done" / "2026-08-20"
        day.mkdir(parents=True, exist_ok=True)
        (day / "50-gamma.py").write_text(item.format("gamma the third"))
        # Finished, delivered, and then deleted by whoever wanted the space
        # back. It has no file and it is not waiting to download one.
        (day / "20-delta.py").write_text(item.format("delta the deleted"))

        work = root / "work" / "40-alpha.py"
        work.mkdir(parents=True, exist_ok=True)
        (work / "alpha.part").write_bytes(b"a" * 400_000)
        (work / ".status.json").write_text(json.dumps({"total_bytes": 2_000_000}))
        outbox = root / "out" / "60-beta.py"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / "beta.iso").write_bytes(b"b" * 1_000)

        # Delivered, and therefore outside the queue root entirely: this is the
        # file that must survive every removal below.
        landed = root.parent / "Download"
        landed.mkdir(parents=True, exist_ok=True)
        (landed / "gamma.mp4").write_bytes(b"g" * 500)
        (root / "state.json").write_text(
            json.dumps(
                {
                    "items": {
                        "40-alpha.py": {
                            "attempts": 1,
                            "bytes": 400_000,
                            "last": "2026-08-20 22:41:03Z",
                        },
                        "60-beta.py": {"attempts": 3, "retired": "failed"},
                        "50-gamma.py": {
                            "retired": "done",
                            "delivered": [str(landed / "gamma.mp4")],
                        },
                        "20-delta.py": {
                            "retired": "done",
                            "delivered": [str(landed / "delta.mp4")],
                        },
                    }
                }
            )
        )
        return {"delivered": landed / "gamma.mp4"}

    def by_name(rows: list[dict]) -> dict:
        return {row["name"]: row for row in rows}

    def state_items() -> dict:
        return sched._runner().load_state().get("items") or {}

    with tree() as root:
        facts = build(root)
        rows = by_name(sched.items())
        check(
            "the temporary queue reads back",
            sorted(rows),
            ["20-delta.py", "40-alpha.py", "50-gamma.py", "60-beta.py"],
        )
        check(
            "the deleted one knows its file is gone",
            rows["20-delta.py"]["lost"],
            "gone",
        )
        alpha, beta, gamma = (
            rows["40-alpha.py"],
            rows["60-beta.py"],
            rows["50-gamma.py"],
        )
        check("the queued item is queued", alpha["where"], "queued")
        check("its partial is measured from the disk", alpha["have"], 400_000)
        check(
            "the done item knows where its file went",
            gamma["files"],
            [facts["delivered"]],
        )

        # What an item owns, and where a removal may reach.
        owned = dict(belongings(alpha))
        check("an item owns its file and its scratch", sorted(owned), ["item", "work"])
        check(
            "everything an item owns is inside the queue root",
            all(str(path).startswith(str(root)) for path in owned.values()),
            True,
        )
        goes, kept = removal(gamma)
        check(
            "a delivered file is never in what a removal deletes",
            facts["delivered"] in goes,
            False,
        )
        check(
            "a removal reaches nothing outside the queue root",
            all(str(path).startswith(str(root)) for path in goes),
            True,
        )
        check(
            "finished files in out/ are kept, not deleted",
            removal(beta)[1],
            [root / "out" / "60-beta.py" / "beta.iso"],
        )

        # Refusals, before anything moves.
        check(
            "a name it already has is refused",
            refuse_rename(alpha, "40-alpha.py"),
            "that is the name it has",
        )
        check(
            "a name the runner would ignore is refused",
            refuse_rename(alpha, "alpha.py"),
            "a name is a number, a dash, a slug and .py",
        )
        check(
            "a name another item answers to is refused",
            refuse_rename(alpha, "60-beta.py"),
            "failed already has a 60-beta",
        )
        # The one nobody thinks of: an item removed while its finished files
        # were kept leaves a directory behind, and renaming onto it would mix
        # two downloads' bytes together in one place.
        (root / "out" / "70-orphan.py").mkdir(parents=True, exist_ok=True)
        check(
            "a name with bytes still under it is refused",
            refuse_rename(alpha, "70-orphan.py"),
            "out/70-orphan still has bytes in it",
        )

        # The rename itself: the bytes and the history have to come with it.
        check(
            "the rename says what it did",
            do_rename(alpha, "10-alpha.py"),
            "now 10-alpha",
        )
        check("the item file moved", (root / "queue" / "10-alpha.py").is_file(), True)
        check(
            "nothing is left under the old name",
            (root / "queue" / "40-alpha.py").exists(),
            False,
        )
        check(
            "the partial download came with it",
            (root / "work" / "10-alpha.py" / "alpha.part").stat().st_size,
            400_000,
        )
        check(
            "the old scratch is gone", (root / "work" / "40-alpha.py").exists(), False
        )
        check(
            "the attempts came with it",
            state_items().get("10-alpha.py", {}).get("attempts"),
            1,
        )
        check("the old record is gone", "40-alpha.py" in state_items(), False)
        check(
            "the rename is in the runner's log",
            "renamed 40-alpha.py -> 10-alpha.py"
            in (root / "logs" / "runner.log").read_text(),
            True,
        )
        moved = by_name(sched.items())["10-alpha.py"]
        check("a renamed item is still queued", moved["where"], "queued")
        check("and still knows what it has", moved["have"], 400_000)

        # A rename never changes which state something is in.
        check(
            "an archived item can be renamed",
            do_rename(gamma, "50-gamma-talk.py"),
            "now 50-gamma-talk",
        )
        check(
            "and stays in the day it was archived in",
            (root / "done" / "2026-08-20" / "50-gamma-talk.py").is_file(),
            True,
        )
        check(
            "with the path to its delivered file intact",
            state_items().get("50-gamma-talk.py", {}).get("delivered"),
            [str(facts["delivered"])],
        )

        # A half-done rename must leave nothing behind: the first move is undone.
        first = root / "queue" / "10-alpha.py"
        parked = root / "queue" / "11-alpha.py"
        problem = _apply(
            [(first, parked), (root / "work" / "no-such-item", root / "work" / "x")]
        )
        check("a move that cannot be made says so", bool(problem), True)
        check("and the moves already made are undone", first.is_file(), True)
        check("with nothing left half-renamed", parked.exists(), False)

    with tree() as root:
        build(root)
        rows = by_name(sched.items())
        # Nothing may be changed while the queue is busy.
        was_busy = ytq.queue_busy
        try:
            ytq.queue_busy = lambda: True
            busy = "the queue is busy — a firing or a download holds it"
            check(
                "a rename waits for the lock",
                do_rename(rows["40-alpha.py"], "10-alpha.py"),
                busy,
            )
            check("a removal waits for the lock", do_remove(rows["40-alpha.py"]), busy)
            check("a requeue waits for the lock", do_requeue(rows["60-beta.py"]), busy)
            check(
                "clearing tries waits for the lock",
                do_clear_tries(rows["40-alpha.py"]),
                busy,
            )
        finally:
            ytq.queue_busy = was_busy
        check(
            "and the queue is exactly as it was",
            sorted(by_name(sched.items())),
            ["20-delta.py", "40-alpha.py", "50-gamma.py", "60-beta.py"],
        )
        check(
            "with its bytes untouched",
            (root / "work" / "40-alpha.py" / "alpha.part").stat().st_size,
            400_000,
        )

    with tree() as root:
        facts = build(root)
        rows = by_name(sched.items())
        # Removing is a decision about the list, not about the bytes: what was
        # paid for stays put unless the second key on the confirm screen says
        # otherwise.
        said = do_remove(rows["40-alpha.py"])
        check(
            "removing says what became of the bytes",
            said,
            "removed alpha; 391 KiB kept",
        )
        check("the item is gone", (root / "queue" / "40-alpha.py").exists(), False)
        check(
            "and the download it had already paid for is still there",
            (root / "work" / "40-alpha.py" / "alpha.part").stat().st_size,
            400_000,
        )
        check("and so is its record", "40-alpha.py" in state_items(), False)
        # ...and the second answer, which is the only one that deletes bytes,
        # and still never the finished ones in out/.
        said = do_remove(rows["50-gamma.py"], bytes_too=True)
        check("the other answer says so too", said, "removed gamma")

        # An item that failed before moving a byte still has a work directory,
        # holding the status file the runner writes. There is nothing paid for
        # in it, so it goes with the item whichever key was pressed — and both
        # keys have to work.
        (root / "queue" / "70-empty.py").write_text(
            "# EXPIRE: v1\n# EXPECT_BYTES: 1000\n# DESC: failed before a byte\n"
        )
        (sched.WORK / "70-empty.py").mkdir(parents=True, exist_ok=True)
        (sched.WORK / "70-empty.py" / ".status.json").write_text("{}")
        empty = by_name(sched.items())["70-empty.py"]
        check("an item that downloaded nothing has nothing to keep", empty["have"], 0)
        check("and removing it says so plainly", do_remove(empty), "removed empty")
        check(
            "and takes the scratch with it",
            (sched.WORK / "70-empty.py").exists(),
            False,
        )
        check("and it is really gone", "70-empty.py" in by_name(sched.items()), False)

        said = do_remove(rows["60-beta.py"])
        check(
            "a finished file in out/ is kept, and said so",
            said,
            "removed beta; 1 kept in out/",
        )
        check(
            "and is still there",
            (root / "out" / "60-beta.py" / "beta.iso").is_file(),
            True,
        )

        do_remove(rows["50-gamma.py"])
        check(
            "removing a done item leaves its delivered file alone",
            facts["delivered"].is_file(),
            True,
        )

    with tree() as root:
        build(root)
        rows = by_name(sched.items())
        check(
            "a failed item goes back to the queue",
            do_requeue(rows["60-beta.py"]),
            "60-beta is queued again",
        )
        check("as a queue file", (root / "queue" / "60-beta.py").is_file(), True)
        # The whole point: three attempts already spent means the first firing
        # to touch it would give up on it again.
        check("with its attempts wiped", state_items()["60-beta.py"]["attempts"], 0)
        check("and no longer retired", "retired" in state_items()["60-beta.py"], False)
        check(
            "a queued item is not requeued",
            do_requeue(by_name(sched.items())["40-alpha.py"]),
            "it is in queued, not failed",
        )
        check(
            "clearing tries says how many",
            do_clear_tries(by_name(sched.items())["40-alpha.py"]),
            "1 failed tries cleared",
        )
        check("and clears them", state_items()["40-alpha.py"]["attempts"], 0)
        check(
            "clearing nothing says so",
            do_clear_tries(by_name(sched.items())["40-alpha.py"]),
            "it has no failed tries",
        )

    # ------------------------------------------------- one reading, one truth
    # Both of these were on screen at once and disagreed: the head said
    # "queued · 44%  20 MiB/44 MiB" over a download in flight while the foot
    # said "↓ 20 MiB / 44 MiB" from a fresher read of the same file.
    base = {
        "name": "40-clip.py", "where": "queued", "cap": 50_000_000,
        "stated": 44_000_000, "total": 44_000_000, "have": 20_000_000,
        "files": [], "error": None, "attempts": 0, "last": "", "desc": "",
        "dest": "video", "lost": "", "recorded": [],
    }
    # The word: an item being downloaded is still IN queue/, and the screen
    # used to say so. `where` still decides everything else.
    plain = item_lines(base, 40)
    check("a still item says where it is", plain[0].startswith("queued"), True)
    live = item_lines(base, 40, downloading=True, reading=(20_000_000, 44_000_000))
    check("one in flight says what it is doing", live[0].startswith("downloading"), True)
    check("and the queue position survives the word",
          any("in the queue" in row for row in
              item_lines(base, 40, (1, 7), True, (20_000_000, 44_000_000))), True)

    # The figures: the head takes the download's own report, so the number
    # beside the word is the number the bar is drawn from.
    moved = item_lines(base, 40, downloading=True, reading=(33_000_000, 44_000_000))
    check("the head takes the fresher reading", "31 MiB" in moved[0], True)
    check("and the stale one is gone", "19 MiB" in moved[0], False)
    fresh = _with_live(base, (33_000_000, 44_000_000))
    check("the bar is drawn from that same reading",
          progress_bar(fresh["have"], fresh["total"], 30).endswith("] 75%"), True)
    check("no reading leaves the row alone", _with_live(base, None), base)
    # A total the server has not stated must not be invented: `_of` prints the
    # declared cap with a `≤` until then, and a made-up total loses that.
    unstated = dict(base, stated=0, total=50_000_000)
    check("an unstated size stays unstated",
          _with_live(unstated, (20_000_000, 0))["stated"], 0)

    # The bar itself: it refuses rather than lying, and carries no glyph whose
    # width a phone might double (the rule that keeps ⚠ off ytq's notices).
    check("a bar with no total is no bar", progress_bar(20, 0, 30), "")
    check("nor is one with no room", progress_bar(20, 40, 8), "")
    check("a full bar says 100%", progress_bar(44, 44, 30).endswith("] 100%"), True)
    check("an empty one is all track", "=" in progress_bar(0, 44, 30), False)
    for span in (14, 24, 40, 80):
        drawn = progress_bar(21, 44, span)
        at_most(f"the bar fits {span}", len(drawn), span)
        check(
            f"and carries no wide glyph at {span}",
            set(drawn) <= set("[]=· 0123456789%"),
            True,
        )

    # ------------------------------------------------- moving one about
    def queue_names() -> list[str]:
        return [row["name"] for row in sched.items() if row["where"] == "queued"]

    with tree() as root:
        build(root)
        for name in ("10-one.py", "20-two.py", "30-three.py"):
            (root / "queue" / name).write_text("# EXPIRE: v1\n# EXPECT_BYTES: 10\n")
        (root / "work" / "30-three.py").mkdir(parents=True)
        (root / "work" / "30-three.py" / "part").write_bytes(b"z" * 500)

        rows = sched.items()
        said, moved = do_reorder(rows, "30-three.py", 0)
        check(
            "moving one says where it landed, not what it is called",
            said,
            "three is 1st of 4",
        )
        check("and it is where it was put", queue_names()[0], "05-three.py")
        landed = landed_index(sched.items(), 0)
        check(
            "the cursor's landing row is the moved item under its new name",
            sched.items()[landed]["name"] if landed is not None else None,
            "05-three.py",
        )
        # The whole point of doing this here rather than with mv: a move takes
        # the paid-for bytes with it, every time, because it is the same rename
        # everything else in this file is.
        check(
            "and the download it had already paid for came too",
            (root / "work" / "05-three.py" / "part").stat().st_size,
            500,
        )
        check(
            "nothing was moved that was not asked for",
            sorted(queue_names())[1:],
            ["10-one.py", "20-two.py", "40-alpha.py"],
        )

        # No room: neighbours one apart, so the whole queue is dealt fresh keys
        # rather than the item being given a three-digit one.
        rows = sched.items()
        do_reorder(rows, "05-three.py", 1)  # between 10 and 20 -> 15
        check("a midpoint is used while there is one", queue_names()[1], "15-three.py")
        for name in ("10-one.py", "20-two.py"):
            check(f"{name} was not disturbed", name in queue_names(), True)
        # 10, 15, 20, 40 -> put three between 10 and 15: no integer between
        # 11 and 14? there is. Squeeze it properly instead.
        (root / "queue" / "11-tight.py").write_text(
            "# EXPIRE: v1\n# EXPECT_BYTES: 10\n"
        )
        rows = sched.items()
        said, moved = do_reorder(rows, "15-three.py", 1)  # between 10 and 11
        check("with no room, everything is dealt fresh keys", moved, True)
        check(
            "and the order asked for is the order got",
            [_slug_of(name) for name in queue_names()],
            ["one", "three", "tight", "two", "alpha"],
        )
        landed = landed_index(sched.items(), 1)
        check(
            "and the landing row survives a full re-deal of the keys",
            _slug_of(sched.items()[landed]["name"]) if landed is not None else None,
            "three",
        )
        check("an empty queue has nowhere to land", landed_index([], 0), None)
        check(
            "a clamped drop lands on the last queued row",
            landed_index(sched.items(), 99),
            landed_index(sched.items(), len(queue_names()) - 1),
        )
        check(
            "all of them two digits",
            all(len(name.split("-")[0]) == 2 for name in queue_names()),
            True,
        )
        check(
            "and the bytes are still with their item",
            (root / "work" / f"{queue_names()[1]}" / "part").stat().st_size,
            500,
        )

        # A swap of two neighbours is a cycle: each wants the other's key, and
        # neither can move until one of them is parked somewhere nobody wants.
        rows = sched.items()
        before = queue_names()
        said, moved = do_reorder(rows, before[0], 1)
        check(
            "a swap comes out the other way round",
            [_slug_of(n) for n in queue_names()][:2],
            [_slug_of(before[1]), _slug_of(before[0])],
        )
        check("with nothing lost in the middle of it", len(queue_names()), len(before))

    # -------------------------------------------------- forgetting a download
    with tree() as root:
        facts = build(root)
        rows = sched.items()
        check("the deleted one is what gets forgotten", forget_gone(rows), ["delta"])
        left = [row["name"] for row in sched.items()]
        check("and it is gone from the list", "20-delta.py" in left, False)
        check("the one whose file is there is untouched", "50-gamma.py" in left, True)
        check(
            "and the file itself was never touched", facts["delivered"].is_file(), True
        )
        check("nothing is forgotten twice", forget_gone(sched.items()), [])

        # The gate: a folder that cannot be read is not evidence of anything,
        # and the record is the only thing that knows where the file went.
        # sched._shut_out is the chmod, plus what has to stand in for it when
        # the checks run as root and the kernel lets it through — one copy,
        # beside the helpers it shuts the door on; see its docstring.
        folder = facts["delivered"].parent
        with sched._shut_out(folder):
            rows = sched.items()
            check("an unreadable folder is not a deleted file", forget_gone(rows), [])
            check(
                "it is not even called gone",
                by_name(rows)["50-gamma.py"]["lost"],
                "away",
            )
        facts["delivered"].unlink()
        check(
            "once it can be read, it can be concluded from",
            forget_gone(sched.items()),
            ["gamma"],
        )

    # ---------------------------------------------------------------- screens
    with tree() as root:
        build(root)
        rows = sched.items()
        for width in (32, 40, 55, 72, 100):
            drawn = compose_rows(rows, width)
            for _, lines in drawn:
                for line in lines:
                    at_most(f"a row fits {width} columns", len(line), width - 1)
            listed = [index for index, _ in drawn if index is not None]
            check(
                f"every download is on the screen at {width}",
                sorted(listed),
                list(range(len(rows))),
            )
            check(f"and only once at {width}", len(listed), len(set(listed)))
            # The same, with a cut line through the queued group — and this is
            # the pin that says the line is not an item: it is a heading, so no
            # row's index moves because it is there and the cursor cannot stop
            # on it. A line that took an index would silently shift every row
            # below it by one, and the cursor would then act on its neighbour.
            queued_at = [
                index for index, row in enumerate(rows) if row["where"] == "queued"
            ]
            for where in range(len(queued_at) + 1):
                ruled = compose_rows(rows, width, "", (where, "── tonight ──"))
                for _, lines in ruled:
                    for line in lines:
                        at_most(f"a ruled row fits {width} columns", len(line), width - 1)
                marked = [index for index, _ in ruled if index is not None]
                check(
                    f"every download is still on the screen at {width} cut {where}",
                    marked,
                    listed,
                )
                check(
                    f"and the line took no index at {width} cut {where}",
                    len(ruled) - len(drawn),
                    1,
                )
            for row in rows:
                facts = item_lines(row, width)
                for line in facts:
                    at_most(f"an item's facts fit {width}", len(line), width - 3)
                # The figures are what the screen is for: they may move to a
                # line of their own, but they are never the thing clipped off.
                figures = _progress_of(row, width < WIDE)
                check(
                    f"{row['name']} keeps its figures whole at {width}",
                    figures in "\n".join(facts),
                    True,
                )
            for row in rows:
                # Both versions of the paid question: the loud one is longer,
                # and it is the one that must not wrap off the screen.
                for line in (
                    removal_note(row) + now_note(row, True) + now_note(row, False)
                ):
                    at_most(
                        f"a confirmation line fits {width}",
                        len(_wrap(line, width - 4)[0]),
                        width - 4,
                    )

        # Every state a row can be in has a colour this screen can draw.
        for row in rows:
            check(
                f"{row['name']} has a tone that can be painted",
                _tone(row) in TONES,
                True,
            )
        shapes = [
            {
                "error": "no header",
                "files": [],
                "where": "queued",
                "have": 0,
                "total": 0,
            },
            {
                "error": None,
                "files": [Path("x")],
                "where": "done",
                "have": 1,
                "total": 1,
            },
            {"error": None, "files": [], "where": "failed", "have": 1, "total": 2},
            {"error": None, "files": [], "where": "queued", "have": 1, "total": 2},
            {"error": None, "files": [], "where": "queued", "have": 0, "total": 0},
        ]
        for shape in shapes:
            check(f"a {shape['where']} row can be painted", _tone(shape) in TONES, True)

        # What is offered, and that pressing it does something.
        rows = by_name(sched.items())
        offered = {name: dict(actions_for(row)) for name, row in rows.items()}
        for name, keys in offered.items():
            check(
                f"{name} offers only keys that act",
                set(keys) <= set(ACTS) | SCREEN_KEYS,
                True,
            )
            for key, label in keys.items():
                at_most(f"{name}'s {key} label fits a phone", len(label) + 5, 32 - 1)
        check(
            "a done item is not offered a priority",
            "p" in offered["50-gamma.py"],
            False,
        )
        check("nor a download", "n" in offered["50-gamma.py"], False)
        check("but is offered its file", "o" in offered["50-gamma.py"], True)
        check(
            "a failed item is offered the queue back",
            "u" in offered["60-beta.py"],
            True,
        )
        # The screen is the one place that could put an archived item back to
        # work, and losing its file must not be what unlocks that: the answer
        # to a deleted download is to queue it again, not to rearm the record
        # of the one that already happened.
        gone = offered["20-delta.py"]
        check(
            "a done item whose file is gone is offered nothing that runs it",
            {"n", "u", "m", "r", "t"} & set(gone),
            set(),
        )
        check("only its log and its removal", sorted(gone), ["d", "l"])
        check(
            "a queued item can be downloaded now", "n" in offered["40-alpha.py"], True
        )
        check("and its tries cleared", "t" in offered["40-alpha.py"], True)
        check(
            "every item can be removed",
            all("d" in keys for keys in offered.values()),
            True,
        )
        rejected = dict(rows["40-alpha.py"], error="no 'EXPIRE: v1' header")
        check(
            "a rejected item is not offered a download",
            "n" in dict(actions_for(rejected)),
            False,
        )
        spent = dict(rows["40-alpha.py"], have=rows["40-alpha.py"]["cap"])
        check(
            "nor is one that has taken its whole cap",
            "n" in dict(actions_for(spent)),
            False,
        )
        every = set()
        for where in ("queued", "failed", "done"):
            for extra in ({}, {"attempts": 2}, {"files": [Path("x")]}):
                every |= set(
                    dict(actions_for(dict(rows["40-alpha.py"], where=where, **extra)))
                )
        check(
            "no action is defined that nothing offers",
            (set(ACTS) | SCREEN_KEYS) - every,
            set(),
        )
        # What the listing's n key actually runs. Spawned rather than
        # described: the failure this pins is the screen and the command line
        # drifting into two different runs — one of them without --force,
        # which downloads nothing and looks exactly like one that did.
        spawned: list[list[str]] = []

        class _Fake:
            def __init__(self, argv, **_):
                spawned.append(list(argv))
                self.pid = os.getpid()

            def poll(self):
                return 0

        was_popen = subprocess.Popen
        try:
            subprocess.Popen = _Fake  # type: ignore[misc]
            firing = Firing()
            log = firing.start(blind=True)
            check("the screen runs the runner", spawned[0][1], str(sched.RUNNER))
            check(
                "the same way the command does",
                spawned[0],
                sched.queue_run_argv(True),
            )
            check("with the clock gate off", "--force" in spawned[0], True)
            check("and blind only when it is", "--blind" in spawned[0], True)
            check("its output is kept beside the item logs", log.parent, sched.LOGS)
            # The signal, pinned: SIGTERM would kill the runner where it
            # stands and leave the download running in its own session, which
            # on a blind run is data spent with nothing watching it.
            check(
                "stopping it is the ctrl-c the runner unwinds on",
                Firing.SIGNAL,
                signal.SIGINT,
            )
            firing.start(blind=False)
            check(
                "an ordinary run is not blind",
                "--blind" in spawned[1],
                False,
            )
        finally:
            subprocess.Popen = was_popen  # type: ignore[misc]

        # The number is said before it is spent, whichever question is asked,
        # and the loud one has to say what is *not* counting it as well.
        alpha = rows["40-alpha.py"]
        size = ytq.human(alpha["cap"] - alpha["have"])
        for seen in (True, False):
            said = "\n".join(now_note(alpha, seen))
            check(f"the number is said before it is spent ({seen})", size in said, True)
            check(f"and one key answers it ({seen})", said.count("?"), 1)
        loud = "\n".join(now_note(alpha, False))
        check(
            "an unreachable portal says nothing is counting", "counting" in loud, True
        )
        check("and says the phone's own data is what pays", "metered" in loud, True)
        check(
            "and does not claim to know which it is",
            "vessel wifi" in loud and "mobile data" in loud,
            True,
        )

    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #


def run() -> int:
    """The screen, with the terminal set up and torn down around it."""
    if not sys.stdout.isatty():
        print(
            "dlq ui needs a terminal; dlq list is the same queue, printed",
            file=sys.stderr,
        )
        return 2
    problem = sched.root_problem()
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 1
    os.environ.setdefault("ESCDELAY", "25")
    changes = curses.wrapper(app)
    for line in changes:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--self-test" in argv:
        return _self_test()
    if argv:
        print(f"usage: {Path(sys.argv[0]).name} [--self-test]", file=sys.stderr)
        print("       dlq ui is the same screen", file=sys.stderr)
        return 2
    return run()


if __name__ == "__main__":
    sys.exit(main())
