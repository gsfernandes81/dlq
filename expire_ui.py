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
every line of every screen must fit down to 32, and the key hints
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
    # The same screen with an item that does not exist yet held on it — ytq
    # asking where a video it is about to write should go. Its own pair rather
    # than "moving"'s, because nothing is being dropped and nothing is being
    # cancelled: what ⏎ takes is a place, and esc leaves the video where it
    # would have gone anyway, which is last.
    "pick": "↑↓ move it  ⏎ take the place  esc no",
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
    "pick": "↑↓ move  ⏎ take  esc no",
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

    Nothing outside the queue root is ever in the first list, which a test
    must pin: an item's delivered file is in Downloads, and it is the
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
#: without anything saying so, which a test must pin against.
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

#: The verdict line's phrase for each of :data:`expire_sched.VERDICTS` — **one
#: phrase per verdict, at every width**. The status screen's own headline is
#: not here on purpose: it is read straight out of ``VERDICTS`` by everything
#: that wants the long form, so the listing and ``dlq status`` cannot come to
#: two different words for the same night. What this table adds is the phrase
#: that goes *after* "tonight:", which is why none of them says "tonight"
#: itself — the headlines do ("waiting for tonight", "done for tonight"), and
#: the wide screen used to print the word twice in one line. A test must pin
#: an entry for every verdict, because one the gate grew and this did not would
#: draw a blank where the answer goes.
TONIGHT_SHORT = {
    "downloading": "downloading",
    "go": "window open",
    "early": "waiting",
    "late": "done",
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
    another. Pure: everything it needs is in *facts*, which is what lets a
    test spell out a night rather than wait for one.

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
    if live:
        head = _fit(f"downloading now: {_slug_of(live)}", room)
    else:
        verdict = facts["verdict"]
        # One phrase for the verdict at every width, and only the clock behind
        # it gives way. The wide screen used to be given the status screen's
        # long headline instead and read "tonight: waiting for tonight, opens
        # 23:00Z (46m)" — the same word twice in the line that is read first
        # and at a glance. The headline is not wrong there; it is wrong *after*
        # a "tonight:", and this line is the only place with one in front.
        phrase = TONIGHT_SHORT.get(
            verdict, sched.VERDICTS.get(verdict, (facts["detail"], ""))[0]
        )
        # An empty queue has no next state worth naming: the window opening on
        # nothing is not a thing to wait for.
        tails = [""] if verdict == "empty" else [f", {when}" for when in _when(facts)]
        head = _pick(
            [f"tonight: {phrase}{tail}" for tail in [*dict.fromkeys(tails), ""]],
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


def _nothing_to_spend(facts: dict) -> str:
    """Why tonight has no budget at all — the answer above every item's own.

    Three ways for the budget to be nought with the night otherwise willing,
    and they are not the same thing to do something about: a portal that did
    not answer is the phone being off the vessel's wifi, a stale reading is
    one that answered with yesterday's figures, and a reading with nothing
    spendable in it is a night where the allowance is genuinely gone. Only the
    last of them is about the data.
    """
    if facts["portal"] is None:
        return "no portal reading"
    if not sched._runner().usable(facts["portal"]):
        return "reading is stale"
    return "no data to spend"


def cut_index(
    order: list[str],
    facts: dict | None,
    width: int,
    planned: list[dict] | None = None,
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
    says why, which is the only place the answer can be — and the answer is
    taken from as far up as it goes. A verdict that stops the whole night is
    quoted in the status screen's own words; a night with no budget at all says
    why there is none (:func:`_nothing_to_spend`), because with nothing to
    spend every item is refused and the first refusal would blame the item for
    it; only where there is a budget and the items still do not fit is an
    item's own refusal the answer, which is :func:`expire_runner.admit`
    explaining itself in the same sentence it would log.
    """
    if not facts or not order:
        return None, ""
    # *planned* is :func:`tonight_plan` on this same *order*, handed in by the
    # one caller that also needs it for the rows — the projection is asked for
    # once per draw and both the line and the rows are drawn from that one
    # answer, so a row cannot say a download gets bytes on a night the line
    # below it says it does not. Left out, it is asked for here.
    if planned is None:
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
    if verdict not in (*sched._runner().GATE_GO, "early"):
        why = sched.VERDICTS.get(verdict, (facts["detail"], ""))[0]
        short = TONIGHT_SHORT.get(verdict, why)
    elif facts["spendable"] <= 0 and not facts["blind"]:
        # Why there is nothing to spend comes *before* any item's own refusal.
        # With a budget of nought every item is turned down for being bigger
        # than nothing, and the first of those refusals reads as a fact about
        # that item — "slice 0 B below the useful minimum 32 MiB" — on a night
        # whose actual answer is that no reading arrived. The item is not the
        # reason; the missing budget is, and this says which kind it is.
        why = short = _nothing_to_spend(facts)
    else:
        # A night that downloads, with something to spend on it: the answer
        # really is about the items, so it is the runner's own first refusal,
        # word for word.
        why = next(
            (entry["reason"] for entry in planned if entry["reason"]),
            "nothing fits tonight",
        )
        # A refusal out of `admit` carries its working in brackets — "(budget
        # 0 B, 3122s left)" — and that is the half worth losing first.
        short = why.split(" (")[0]
    # The reason is what a phone keeps. The lead-in goes before it does: at 32
    # columns there are twenty-five for both, and "nothing tonight" is already
    # the shape of the line — a rule across the top of the queued group with no
    # item on it — while the reason is the half nothing else on the screen
    # says. A line reading `── nothing tonight ──` and no more is the screen
    # knowing the answer and declining to give it.
    spellings = [
        f"nothing tonight: {why}",
        f"nothing tonight: {short}",
        why,
        short,
        "nothing tonight",
    ]
    # And never the word twice in one line. Several of the verdicts' headlines
    # say "tonight" themselves — "done for tonight", "no data to spend tonight"
    # — and after a lead-in that has just said it they read as a stammer. The
    # spelling is dropped rather than reworded: `VERDICTS` stays the one set of
    # words for a night, and there is always another rung under this one.
    return 0, _rule(
        [text for text in spellings if text.count("tonight") <= 1], width
    )


# --------------------------------------------------------------------------- #
# The listing, with a cursor on it
# --------------------------------------------------------------------------- #


def _beside(share: str) -> str:
    """The share as it reads next to the figures: ``" · 46 MiB tonight"``.

    One spelling, because it is measured in one place and drawn in another and
    a lead-in counted at one width and drawn at a second is a clipped line.
    """
    return f" · {share}" if share else ""


def _tonight_share(row: dict, planned: int) -> str:
    """``46 MiB tonight`` for a download the night reaches only part of.

    The one row where the cut line is not the whole answer: the item that
    straddles it. A resumable download is handed whatever is left of the budget
    when the projection reaches it — 46 MiB of a 210 MiB item — and it sits
    *above* the line, correctly, because it does get bytes tonight; but the row
    read exactly like the two above it, which finish. This is the figure that
    says how much of it tonight actually buys.

    Nothing for an item the night finishes, and nothing for one it never
    reaches: on most rows the figure would only be the progress cell's own
    number said twice, and the one row where it is news would be lost in them.
    """
    if planned <= 0:
        return ""
    need = max(0, row.get("cap", 0) - row.get("have", 0))
    if planned >= need:
        return ""
    return f"{ytq.human(planned)} tonight"


def compose_rows(
    rows: list[dict],
    width: int,
    live: str = "",
    cut: tuple[int, str] | None = None,
    tonight: list[dict] | None = None,
) -> list[tuple[int | None, list[str]]]:
    """The whole listing as ``(which row, its lines)``, headings included.

    A heading is ``None``; everything else carries the index of the row in
    *rows*, so the cursor and the screen cannot disagree about which download
    is which. **Every row appears exactly once**, whatever the width — the
    a test must pin that, because a download missing from this screen looks
    exactly like a download that is not there, and this is the screen someone
    removes things from.

    *cut* is :func:`cut_index`'s answer: ``(how many queued rows tonight
    reaches, the line to draw)``. It is emitted **as a heading** — index
    ``None``, exactly like ``queued (3)`` — which is the whole of how the cut
    line stays computed rather than becoming an item: the cursor skips it,
    :func:`landed_index` cannot land on it, and no row's index moves because it
    is there. A test must pin both halves.

    *tonight* is :func:`tonight_plan`'s answer for the same order — passed in
    rather than worked out here, so the rows and the cut line are drawn from
    one projection. It puts ``· 46 MiB tonight`` on the one row that needs it,
    the item the night only gets part of the way through
    (:func:`_tonight_share`); it is on the row at every width, taking a line of
    its own on a phone too narrow to hold it beside the figures, because a row
    above the line that does not finish tonight is exactly the thing the line
    alone cannot say.

    Two shapes, on the same rule the listing uses: one line each while the
    name, the state and the figures fit together, and two lines each when they
    do not. The name is the last cell to give up room, because losing its tail
    makes two downloads look like the same one — and here that is the
    difference between removing one and removing the other.
    """
    compact = width < WIDE
    # What each queued row gets tonight, by name. An item the projection does
    # not mention — anything that is not queued, or queued since the reading —
    # gets nothing said about it rather than a nought.
    got = {entry["name"]: entry["bytes"] for entry in tonight or []}
    shares = [_tonight_share(row, got.get(row["name"], 0)) for row in rows]
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
    # The share is measured as part of the figures cell, so a row carrying one
    # is what decides whether the listing still fits on one line each.
    prog_w = max(
        (
            len(cell[2]) + len(_beside(share))
            for cell, share in zip(cells, shares, strict=True)
        ),
        default=0,
    )
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
            share = shares[index]
            if tight:
                figures = f"{state:>{state_w}}  {progress}"
                lines = [f"  {_fit(name, room - 2)}"]
                if len(figures) + len(_beside(share)) <= room - 4:
                    lines.append(f"    {_fit(figures + _beside(share), room - 4)}")
                else:
                    # A third line rather than a clipped second one: what would
                    # fall off the end is the only figure on the row that is
                    # about tonight, and the row is the only place it is said.
                    lines.append(f"    {_fit(figures, room - 4)}")
                    if share:
                        lines.append(f"    {_fit(share, room - 4)}")
                out.append((index, lines))
                continue
            line = (
                f"  {name.ljust(name_w)}  {state:>{state_w}}  "
                f"{progress}{_beside(share)}"
            )
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


def phantom_of(name: str, cap: int, partial: bool) -> tuple[dict, dict]:
    """A download that does not exist yet, as ``(its row, its plan entry)``.

    ytq's picker (:func:`pick_place`) holds a video it has not written on this
    listing so that somebody can see where it lands. That takes two things and
    exactly two: a row for the screen to draw, and an item for
    :func:`expire_runner.plan` to count — because the whole worth of showing it
    here is that the cut line and the shares are worked out **with it in**, in
    the order it is being dragged to.

    Spelled once, here, rather than at the two ends: a row the reader would
    never have produced and a plan entry the runner would never have been
    handed are both silent — the screen draws something plausible and lies
    about the night. The row is what :func:`expire_sched.items` gives a queued
    item that has fetched nothing; the entry is what
    :func:`expire_runner.snapshot` gives one.

    Nothing here is ever written. The pair lives for as long as the screen is
    open and goes when it closes; what puts a file anywhere is :func:`place`,
    afterwards, once ytq has actually written the item.
    """
    row = {
        "name": name,
        "where": "queued",
        "cap": cap,
        # No server has stated a size for a file nobody has asked for yet, so
        # the figures cell reads "0 B of ≤300 MiB" — the declared cap with the
        # ``≤`` that says it is a bound. An invented total would be the one
        # number on this screen nobody measured.
        "stated": 0,
        "total": cap,
        "have": 0,
        "desc": "",
        "error": "",
        "files": [],
        "lost": "",
        #: Only ever true here, and read by nothing that changes anything: it
        #: is what a reader of this list can ask to tell the row apart from a
        #: download that is really in the queue.
        "phantom": True,
    }
    item = {
        "name": name,
        "cap": cap,
        "partial": partial,
        "slice_min": sched._runner().SLICE_MIN_BYTES,
        "part_bytes": 0,
    }
    return row, item


def draw_list(
    win,
    paint: dict,
    queue,
    cursor: int,
    top: int,
    flash: str,
    moving: str = "",
    pos: int = 0,
    phantom: tuple[dict, dict] | None = None,
) -> int:
    """Draw the listing and return the line it was scrolled to.

    Four lines are laid down before the downloads: the title bar, the two
    header lines saying what tonight would do (:func:`tonight_lines`), and a
    blank. Inside the queued group the cut line says where tonight's spending
    stops, and it is worked out **here, every draw, from the order on the
    screen** — which is what makes it follow a held item as ↑↓ move it, and
    what makes it recompute after a drop without anything having to remember
    to ask. The same projection tells the rows how much of the item that
    straddles the line comes tonight, so the two cannot disagree.

    The spare row above the keys carries :data:`LEGEND_KEYS` on the screens
    that have nothing else to say there — no download in flight, no flash,
    nothing in the air — because this is the screen a bare ``dlq`` opens and
    everything the old queue screen did is behind those three keys.

    *phantom* is :func:`phantom_of`'s pair for an item that does not exist yet
    — ytq's picker, and nothing else, passes one. Its row joins the list and
    its plan entry joins the reading's items, so everything below this line
    happens once, over a queue with one more thing in it: the same preview, the
    same projection, the same cut. There is no second drawing of the listing
    anywhere, which is the point — a picker with its own would be a screen
    promising a night the queue's own screen disagrees with.
    """
    win.erase()
    height, width = win.getmaxyx()
    rows = queue.rows if phantom is None else [*queue.rows, phantom[0]]
    shown = preview(rows, moving, pos) if moving else rows
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
                f" queue — {len(rows)} in {sched._short(sched.ROOT)} "
                if width >= WIDE
                else " queue "
            ),
        )
    facts = queue.tonight.facts
    if phantom is not None and facts:
        # A copy, and only the items replaced: the reading itself belongs to
        # the thread that took it and to every other screen drawn from it, and
        # an item nobody has queued must not end up in the figures that
        # `dlq status` and the run-now confirm are read from.
        facts = {**facts, "items": [*facts["items"], phantom[1]]}
    header = tonight_lines(facts, width, queue.live)
    if queue.tonight.note:
        # A reading that failed keeps the figures the last one gave and says so
        # where the verdict goes: stale figures with a word about them are
        # worth more than a screen that has gone blank.
        header[0] = _fit(queue.tonight.note, width - 2)
    for offset, text in enumerate(header):
        _addstr(win, 1 + offset, 1, text, curses.A_BOLD if not offset else 0)
    order = [row["name"] for row in shown if row["where"] == "queued"]
    # One projection per draw, and both the line and the rows are drawn from
    # it: the line says where tonight stops, the rows say how much of the item
    # that straddles it comes tonight, and asking twice would be two answers to
    # one question with nothing on the screen saying which was which.
    planned = tonight_plan(order, facts)
    reaches, ruled = cut_index(order, facts, width, planned)
    entries = compose_rows(
        shown,
        width,
        queue.live,
        None if reaches is None else (reaches, ruled),
        planned,
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
            ("pick" if phantom else "moving")
            if moving
            else ("list-live" if queue.mine() else "list"),
            width,
        ),
        live,
    )
    win.refresh()
    return top


def held_key(key: int, pos: int, room: int) -> tuple[int, str]:
    """One key while a download is held: ``(where it is now, what it settled)``.

    ``""`` while it is still in the air, ``"take"`` for the place it is at,
    ``"leave"`` for none of it. Pure, and the one place the held keys are
    spelled — an item being moved in the queue and a video ytq has not written
    yet are held by the same two arrows, and a picker that answered ⏎
    differently from the listing would be two screens that look identical
    disagreeing about what enter means.

    Everything else does nothing at all. This is the screen with a download in
    the air on it, and a stray key here would be a key acting on a queue in a
    state nobody can see.
    """
    if key in (curses.KEY_UP, ord("k")):
        pos -= 1
    elif key in (curses.KEY_DOWN, ord("j")):
        pos += 1
    elif key == curses.KEY_HOME:
        pos = 0
    elif key == curses.KEY_END:
        pos = room
    elif key in (curses.KEY_ENTER, 10, 13, ord("m")):
        return max(0, min(pos, max(0, room))), "take"
    elif key in (ord("q"), 27):
        return max(0, min(pos, max(0, room))), "leave"
    return max(0, min(pos, max(0, room))), ""


def holding(
    win,
    paint: dict,
    queue,
    name: str,
    pos: int,
    top: int = 0,
    flash: str = "",
    phantom: tuple[dict, dict] | None = None,
) -> tuple[int | None, int]:
    """The listing with *name* in the air, until a key settles it.

    Returns ``(the position taken, the line it was scrolled to)``, and ``None``
    for the position when it was left alone.

    **One loop for both things that are held.** ``m`` on the listing holds a
    download that is in the queue; :func:`pick_place` holds a video that is not
    in it yet. They are the same screen — the same title bar, the same two
    arrows, the same cut line moving under the item as it goes — and they are
    this function rather than two copies of it, because the reason to show
    either of them is the line, and a line drawn twice is a line that can be
    drawn two ways.

    Nothing is renamed and nothing is written here. What is decided is a
    position; :func:`do_reorder` and :func:`place` are what act on one.
    """
    # How many places there are to be in. Counted once, because nothing is
    # re-read while an item is held: an item that is in the queue has its own
    # place among the others, and one that is not — a phantom — has a place
    # after the last of them.
    room = len([row for row in queue.rows if row["where"] == "queued"]) - (
        0 if phantom else 1
    )
    # nomut: start
    while True:
        # Taken here and nowhere else while an item is held: the thread only
        # ever fills a slot, and the facts the screen draws from are the ones
        # this line assigned.
        queue.tonight.collect()
        queue.tonight.refresh()
        top = draw_list(win, paint, queue, 0, top, flash, name, pos, phantom)
        # A quarter of a second while a reading is in the air, so the header
        # fills itself in when it lands rather than at the next keypress, and
        # blocking otherwise. Never the second-long tick the listing uses to
        # watch a download by: a redraw underneath a held item would be the
        # queue rearranging itself around a decision that has not been taken.
        win.timeout(250 if queue.tonight.pending else -1)
        try:
            key = win.getch()
        finally:
            win.timeout(-1)
        if key == curses.KEY_MOUSE:
            # A flick scrolls the held item, and only a keypress spends.
            pos = max(0, min(pos + ytq.read_wheel(), max(0, room)))
            continue
        pos, settled = held_key(key, pos, room)
        if settled == "take":
            return pos, top
        if settled == "leave":
            return None, top
    # nomut: end


def list_screen(
    win, paint: dict, queue, cursor: int, flash: str, start_moving: str = ""
) -> tuple[str, int]:
    """The listing, until a key leaves it. Returns ``(what next, cursor)``.

    Two modes. Normally this screen picks — ↑↓ and enter — and the item screen
    acts, so there is no way to change the wrong download. The exception is
    moving one, which is the single action whose whole effect is *where it is
    in this list*: it is picked up here, held by :func:`holding` — the same
    loop ytq's picker is — moved with the two keys that were already moving the
    cursor, and dropped. Nothing is renamed until it is dropped, and everything
    else is locked out while it is in the air.

    This is also the whole queue's screen now. ``n`` runs it, ``s`` opens the
    settings and ``l`` the runner's log — the three keys the queue's own screen
    used to hold, on the screen a bare ``dlq`` opens rather than one further
    in. Each of them re-asks what tonight would do afterwards, because each of
    them can change the answer.
    """
    top = 0
    holds = start_moving
    # nomut: start
    while True:
        if holds:
            # Picked up: the listing goes on being drawn, by :func:`holding`,
            # with everything but the two arrows locked out until it is put
            # down. What comes back is a place, and nothing has been renamed to
            # get it.
            moving, holds = holds, ""
            queued = [row["name"] for row in queue.rows if row["where"] == "queued"]
            chosen, top = holding(
                win,
                paint,
                queue,
                moving,
                queued.index(moving) if moving in queued else 0,
                top,
                flash,
            )
            if chosen is None:
                flash = "left where it was"
                continue
            said, moved = do_reorder(queue.rows, moving, chosen)
            if moved:
                queue.receipts.append(said)
            flash = said
            queue.read()
            if moved:
                queue.tonight.start()
            # A drop that moved something renamed it, so the cursor follows the
            # POSITION it was dropped at (landed_index); one that did not still
            # knows the name it kept.
            found = (
                landed_index(queue.rows, chosen) if moved else queue.index_of(moving)
            )
            cursor = cursor if found is None else found
            continue
        cursor = max(0, min(cursor, len(queue.rows) - 1))
        # Taken here and nowhere else: the thread only ever fills a slot, and
        # the facts the screen draws from are the ones this line assigned.
        queue.tonight.collect()
        queue.tonight.refresh()
        top = draw_list(win, paint, queue, cursor, top, flash)
        # Blocking while nothing is moving, so an idle screen costs no wakeups
        # at all; a second is fast enough to watch a download by. A quarter of
        # one while a reading is in the air, so the header fills itself in when
        # it lands rather than at the next keypress.
        if queue.tonight.pending:
            wait = 250
        elif queue.moving():
            wait = 1000
        else:
            wait = -1
        win.timeout(wait)
        try:
            key = win.getch()
        finally:
            win.timeout(-1)

        if key == -1:
            # A download's figures are a second old at worst, and the row it is
            # on stays under the cursor even if the queue reordered underneath.
            mine_was = queue.mine()
            name = queue.rows[cursor]["name"] if queue.rows else ""
            queue.read()
            found = queue.index_of(name)
            cursor = cursor if found is None else found
            # `or flash`, and that word is the whole of it: this branch runs
            # four times a second while a reading is in the air, and a plain
            # assignment made every one of those a message being cleared —
            # "armed the nightly job", "forgot x — the file had gone" — a
            # quarter of a second after it was put there, on the screen that
            # was waiting to say it. `said()` still speaks the moment it has
            # something, since it is asked first and only an empty answer
            # leaves what was already there.
            flash = queue.said() or flash
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
                holds, flash = queue.rows[cursor]["name"], ""
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
            # What comes back is the receipts, not a sentence to print here:
            # the settings page says what it did on its own rows, where there
            # is room to say it whole, and this screen is left as it was found
            # — legend keys and all — rather than wearing a clipped message
            # about a page somebody has already left.
            changes = settings_screen(win, paint)
            flash = ""
            if changes:
                queue.receipts += changes
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
    # nomut: end


# --------------------------------------------------------------------------- #
# A place for something that is not queued yet
# --------------------------------------------------------------------------- #


def pick_place(
    win, name: str, cap: int, partial: bool, pos: int | None = None
) -> int | None:
    """dlq's listing with *name* held: ↑↓ move it, ⏎ takes the place, esc leaves.

    Returns the queued position chosen (0-based, among the queued items that
    exist on disk — the same *pos* :func:`do_reorder` takes), or ``None`` when
    it is left alone.

    **ytq's door, and it is this screen and not a copy of it.** The video does
    not exist yet: what is held is a phantom (:func:`phantom_of`), a row and a
    plan entry that live for as long as the screen is open, so the cut line and
    the shares are drawn with the new item counted in whatever place it is
    being dragged to. Nothing here writes anything — an answer of ``None`` and
    an answer of 3 leave exactly the same queue behind. Putting the file
    somewhere is :func:`place`, afterwards, once ytq has written it.

    *pos* is a place already chosen — somebody re-opening the picker to think
    again. ``None`` starts it where the item would land if nobody said
    otherwise, which is last: the number ytq is about to give it.

    *win* is the caller's window, so this is drawn inside ytq's own curses
    session and hands it back untouched when it returns.
    """
    curses.curs_set(0)
    win.keypad(True)
    # ytq's own switch, and the same rule: wheels only, so a tap on a screen
    # that ends in a download cannot press a key.
    ytq.enable_touch_scroll()
    queue = Queue()
    room = len([row for row in queue.rows if row["where"] == "queued"])
    return holding(
        win,
        ink(win),
        queue,
        name,
        room if pos is None else max(0, min(pos, room)),
        phantom=phantom_of(name, cap, partial),
    )[0]


def place(name: str, pos: int) -> tuple[str, bool]:
    """Put the queued item file *name* at position *pos*: ``(what to say, moved)``.

    :func:`do_reorder` on a fresh read of the queue — **the one numbering
    rule**, the same one ``m`` on the listing goes through, so a place taken
    from ytq and a place taken here cannot be slotted or renumbered differently
    and cannot disagree about whether the queue is too busy to be touched.

    The read is fresh because the queue is not ytq's: the picker's listing was
    drawn before the item was written, and a firing or another terminal may
    have moved something since. *pos* is a position among the items that are
    there **now**, which is what a position means everywhere else in this file.
    """
    return do_reorder(sched.items(), name, pos)


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
    # nomut: start
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
    # nomut: end


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
    # nomut: start
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
    # nomut: end


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
    # nomut: start
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
    # nomut: end


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
    pinned at both ends by a test: spelled differently, this screen
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


def cancel_note() -> list[str]:
    """What the unregister confirm says — out here so it can be checked.

    The last line is the way back, and the key on it is read from
    :data:`PAGE_KEYS` rather than typed out: it said ``a`` for as long as the
    queue had a screen of its own with arming on it, and went on saying ``a``
    after the key moved. ``a`` is the *auto* switch now, so the sentence
    offering the way back was sending someone to the key that stops the queue
    downloading at all — a confirm can be wrong about its own screen, and this
    is the one line where being wrong costs the nightly job.
    """
    return [
        "stop the nightly job?",
        "nothing downloads by itself after this",
        f"{chr(PAGE_KEYS[1])} arms it again, and the queue is untouched "
        "either way",
    ]


def cancel_job(win, paint, job) -> tuple[str, bool]:
    """Unregister the job — the one action here whose damage is silence.

    Confirmed not because it is hard to undo, but because what follows is
    nothing at all: no firing, no log, no notification, and a queue that looks
    exactly like a queue waiting for tonight.
    """
    if not armed(job):
        return f"it is not armed; {chr(PAGE_KEYS[1])} registers it", False
    if not confirm(win, paint, " unregister ", cancel_note()):
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


def dest_screen(win, paint: dict) -> list[str]:
    """Where finished downloads go — every destination, and any one changed.

    The only setting the queue has, and the last thing that could be changed
    from the command line alone. It matters more than it sounds on a phone:
    the default is Android's Downloads, which does not exist until
    ``termux-setup-storage`` has been run, and a destination that cannot be
    written to is a download that finishes and then stays in ``out/``.

    The settings page's rule, one level down: a change **stays here**, the
    three rows are redrawn with the new folder on them, and what happened is
    said in the said area under them (:func:`said_lines`) rather than clipped
    onto the flash row a page up. Only ``q`` leaves, and what it hands back is
    the receipts — one sentence per change that took — for the settings page to
    add to its own. The rows come first when the screen is short, exactly as
    they do above: a folder nobody can see is worse than a sentence nobody
    sees, since the sentence is about a folder that is on the screen anyway.
    """
    runner = sched._runner()
    flash = ""
    said = ""
    receipts: list[str] = []
    # nomut: start
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
        for indent, text, tone in said_lines(said, width):
            if line >= height - 4:
                break
            _addstr(win, line, indent, text, paint.get(tone, 0))
            line += 1
        _foot(win, paint, flash, hint("dest", width))
        win.refresh()
        key = win.getch()
        if key in (ord("q"), 27, curses.KEY_LEFT):
            return receipts
        # Zipped rather than indexed, which is what let `audio` be added on
        # 2026-08-28 by extending one tuple: a destination past the end of the
        # key map makes it short rather than raising, and the screen goes on
        # working for the ones it knows. The hint names the same keys.
        picked = dict(zip(DEST_KEYS, kinds, strict=False)).get(key)
        if picked is None:
            # The flash row keeps this screen's own one-liners and nothing
            # else; the said area is cleared with it, so the screen never says
            # two things in two places at once.
            flash = "that key does nothing here" if 32 <= key < 127 else ""
            said = ""
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
            flash, said = "left where it was", ""
            continue
        worked, told = sched.set_dest(picked, typed)
        flash, said = "", told[-1] if told else ""
        if worked:
            _note(f"{picked} downloads now go to {runner.dests()[picked]}")
            receipts.append(said)
    # nomut: end


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


def _page_rows(
    job: list[tuple[str, str, str]] | None,
) -> list[tuple[str, list[str], str]]:
    """``(name, the value's spellings, what it means)`` for ``d`` and ``j``.

    The destinations are one row rather than three: three paths is the
    destinations *screen*, and this page's job is to say whether they are worth
    opening. One folder for all three kinds is the common case and is worth
    naming; anything else is a count and a key.

    That value is a **path**, which is the one thing on this page that does not
    shorten by itself: ``~``-written, Android's own Downloads folder is 28
    columns and a 40-column phone leaves nineteen for it, so the row read
    ``d  destinations  /storage/emulated/0…`` — a clip that names no folder at
    all and hides the very word that would have answered the question. The
    second spelling is the last component, ``Download``, which is the half a
    person recognises; the whole path is one keypress away on the screen ``d``
    opens. :func:`_pick` chooses between them, as it does for every other line
    on this screen that is written more than one way.

    The job's own words are :func:`expire_sched.job_rows`', unchanged — the
    same text ``dlq status`` prints, so this page and that one cannot disagree
    about whether the nightly firing is registered. It is read once on the way
    in rather than per draw, because ``termux-job-scheduler`` can take seconds.
    """
    dests = sched._runner().dests()
    folders = {str(path) for path in dests.values()}
    one = sched._short(next(iter(dests.values())))
    return [
        (
            "destinations",
            (
                [one, Path(one).name or one]
                if len(folders) == 1
                else [f"{len(folders)} folders"]
            ),
            "where finished downloads are moved to",
        ),
        (
            "nightly job",
            [job[0][1] if job else "not read"],
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
    for letter, (name, spellings, label) in zip(
        PAGE_KEYS, _page_rows(job), strict=True
    ):
        lines.append((0, "", ""))
        # The value is fitted against what is actually left of the line rather
        # than clipped off the end of it: what falls off a path is its folder,
        # which is the whole of what the row was saying.
        head = f"{chr(letter)}  {name}  "
        head += _pick(spellings, max(8, width - 4 - len(head)))
        lines.append((2, _fit(head, width - 4), "head"))
        lines += [(5, text, "90") for text in _wrap(label, width - 6, "  ")]
    return lines


#: How many lines the said area is given. Three hold the longest sentence a
#: setting has to say on a 40-column phone; a fourth would be a page whose foot
#: moves about as the sentences change length.
SAID_LINES = 3

#: The said area's tone — the flash row's own, because it is the same thing
#: being said, in the one place there is room to say it whole.
SAID_TONE = "1;33"


def said_lines(said: str, width: int) -> list[tuple[int, str, str]]:
    """What just changed, wrapped, as body lines: the **said area**.

    Wrapped on the page rather than clipped onto the foot's flash row, because
    the sentence *is* the answer. "auto: off — the nightly job fires and does
    nothing; run-now still works" is 67 columns and a phone shows 38 of them,
    so the flash row said "auto: off — the nightly job fires and does…" — the
    half that does not say what it means, printed over the legend besides.

    Empty for an empty sentence, so a page with nothing to say lays down
    nothing rather than a blank line where a sentence would have been.
    """
    if not said:
        return []
    return [(2, text, SAID_TONE) for text in _wrap(said, width - 4, "  ")[:SAID_LINES]]


def settings_body(
    width: int,
    height: int,
    job: list[tuple[str, str, str]] | None = None,
    said: str = "",
) -> list[tuple[int, str, str]]:
    """:func:`settings_lines`, plus the said area, cut to a screen this tall.

    The screen's own rule, kept out here so it is checked rather than trusted:
    the failure it exists for is a setting scrolling off the bottom, which is a
    setting nobody knows is there — and the last of them, ``notify-blocked``,
    sits under ``auto``, which is the one that stops the queue downloading at
    all.

    Three things are given up, in this order. The blank lines between the
    blocks go first, because they cost nothing but air. Then the grey line
    under each row — which is a 20-row phone at 32 columns, where eight rows'
    meanings wrap to two lines each. Last of the three, and only then, the said
    area. It goes after the meanings rather than before them because it is the
    reason the page stayed: giving it up first would leave the phone that most
    needs the sentence the one screen that never shows it, while the meaning of
    a setting is in the docs and on the wide screen. It still goes **before any
    row's key, name or value and before anything red**, which is the trade this
    screen makes and does not vary: a figure the phone is going to spend by,
    and a value ``config.json`` holds that is being ignored, outrank a message
    about something that has already happened.

    ``d`` and ``j`` count as settings here: they are not in ``config.json``,
    but a row nobody knows is there is the same failure whichever file it comes
    from — and ``j`` is the one that says whether anything downloads at night
    at all.
    """
    body = settings_lines(width, job)
    told = said_lines(said, width)
    if told:
        body += [(0, "", ""), *told]
    room = height - 6
    if len(body) > room:
        body = [line for line in body if line[1]]
    if len(body) > room:
        body = [line for line in body if line[2] != "90"]
    if len(body) > room:
        body = [line for line in body if line[2] != SAID_TONE]
    return body


def settings_screen(win, paint: dict) -> list[str]:
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

    **A change stays on this page**, redrawn with the new value, and says what
    it did in the said area under the rows. It used to return on the first
    change — which put somebody back on the listing they did not ask for, with
    a sentence too long for the flash row clipped over the legend keys, one
    keypress into a page of six settings they had come to set. Nothing else
    was wrong with it: the receipt, the log line and the re-read of tonight all
    still happen, just at ``q`` rather than at the first change.

    What comes back is therefore the **receipts** — one sentence per change
    that took, in the order they were made, for the caller to add to the
    session's and print once curses is down. An empty list is a page that
    changed nothing, which is also what tells the listing there is nothing to
    re-read tonight's reading for.

    ``d`` and ``j`` are the two rows that are not values in ``config.json``,
    and they come back here the same way: the destinations page hands up its
    own receipts, the job's confirm and its ``waiting()`` screen happen where
    they always did, and the page they leave is this one with the answer on it.
    """
    runner = sched._runner()
    flash = ""
    said = ""
    receipts: list[str] = []
    state, job = waiting(
        win, paint, " settings ", "asking Android's scheduler…", sched.job_rows
    )
    # A scheduler that will not answer is not a reason to withhold the window
    # and the reserve: the page draws, and the job row says what happened.
    job = job if state == "ok" else [("job", f"could not read it: {job}", "1;31")]
    # nomut: start
    while True:
        win.erase()
        height, width = win.getmaxyx()
        _bar(win, paint, " settings ")
        line = 2
        body = settings_body(width, height, job, said)
        for indent, text, tone in body:
            if line >= height - 4:
                break
            attr = paint.get(tone, 0) if tone else 0
            if tone == "head":
                attr |= curses.A_BOLD
            _addstr(win, line, indent, text, attr)
            line += 1
        # The foot is the hints and nothing else but the screen's own two
        # one-line refusals: everything a setter or a page said is wrapped in
        # the said area above, so no sentence is ever both clipped and whole.
        _foot(win, paint, flash, hint("settings", width))
        win.refresh()
        key = win.getch()
        if key in (ord("q"), 27, curses.KEY_LEFT):
            return receipts
        if key in PAGE_KEYS:
            # Off the same tuple the two rows are laid out from and the hints
            # are spelled from. It was two literals, and a key that moved in
            # PAGE_KEYS would have gone on opening the screen it used to —
            # which is how the unregister confirm came to offer `a`. The
            # unpack is the check: a third row added there stops here, loudly,
            # rather than becoming a key nothing answers.
            dest_key, job_key = PAGE_KEYS
            if key == dest_key:
                got = dest_screen(win, paint)
                receipts += got
                # The destinations page says its own changes on its own rows;
                # what this page shows is the last of them, since that row is
                # the one that just moved.
                flash, said = "", got[-1] if got else ""
            else:
                text, worked = (
                    cancel_job(win, paint, job) if armed(job) else arm_job(win, paint)
                )
                flash, said = "", text
                if worked:
                    receipts.append(text)
                    # The row above says whether it is armed, and the answer
                    # just moved: ask the scheduler again rather than draw the
                    # old one.
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
            # One of the two things this screen says for itself, and both are
            # short enough for the flash row. The said area is cleared with it:
            # a key that did nothing over a sentence about the last change that
            # did is a screen saying two things at once.
            flash = "that key does nothing here" if 32 <= key < 127 else ""
            said = ""
            continue
        spec = runner.SETTINGS[picked]
        value = runner.settings()[picked]
        if spec["kind"] == "bool":
            # The word it is *not*, said in the setting's own pair, so the
            # setter parses exactly what the screen would have printed.
            worked, told = sched.set_setting(
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
                flash, said = "left as it was", ""
                continue
            worked, told = sched.set_setting(picked, typed)
        # Taken or refused, the setter's last sentence goes in the said area:
        # a refusal says which figures were allowed, and that is the sentence
        # somebody is about to type against.
        flash, said = "", told[-1] if told else ""
        if worked:
            now = runner.spell_setting(picked, runner.settings()[picked])
            _note(f"{picked} is now {now}")
            receipts.append(said)
    # nomut: end


#: Keys the *screen* handles rather than the item: moving a download is the one
#: action whose effect can only be seen in the list, so pressing it there goes
#: back to the list rather than doing anything on the spot.
SCREEN_KEYS = {"m"}

#: Key to what it does. A test must pin it against :func:`actions_for`, so
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

    # nomut: start
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
    # nomut: end


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
    if argv:
        print(f"usage: {Path(sys.argv[0]).name}", file=sys.stderr)
        print("       dlq ui is the same screen", file=sys.stderr)
        return 2
    return run()


if __name__ == "__main__":
    sys.exit(main())
