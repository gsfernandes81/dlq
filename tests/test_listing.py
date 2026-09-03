"""What the queue holds, read off a real queue by the real reader.

:func:`expire_sched.items` is what both listings and every screen are drawn
from, so the failures worth catching here are the ones that look like something
else: a download missing from a listing looks exactly like a download that is
not there, and a finished file that cannot be *looked* for looks exactly like
one that has been deleted.

That last distinction is the one this file spends most of its length on.
``gone`` means a folder was read and the file was not in it — somebody deleted
a film, which is a normal thing to do. ``away`` means the folder could not be
read at all: the card is out, or Android's Downloads is there but raises until
``termux-setup-storage`` has been run. Calling that "gone" would write off
every completed download on the phone on the strength of a permissions blip,
and ``dlq ui`` deletes records on the strength of the answer.
"""

from __future__ import annotations

import json
import os

import pytest

MiB = 1024**2


def named(rows):
    return [row["name"] for row in rows]


def row_for(rows, name):
    return next(row for row in rows if row["name"] == name)


@pytest.fixture
def unreadable(monkeypatch):
    """Make one folder raise when it is listed, the way Android's does.

    A monkeypatched ``listdir`` rather than a ``chmod``, because the suite runs
    as root as readily as it runs as anybody, and root can read a folder with
    no permissions at all — the test would quietly stop testing anything.
    """
    real = os.listdir

    def block(where):
        def listdir(path="."):
            if str(path) == str(where):
                raise PermissionError(13, "Permission denied")
            return real(path)

        monkeypatch.setattr(os, "listdir", listdir)

    return block


# --------------------------------------------------------------------------- #
# Every download, once
# --------------------------------------------------------------------------- #


def test_the_three_places_a_download_can_be(dlq):
    """Queued first, because it is the only one anything can still be told to
    run; then failed, which someone has to deal with; then done, which is
    history."""
    dlq.item("20-queued.py")
    dlq.item("30-failed.py", where="failed")
    dlq.item("10-done.py", where="done/2026-09-01")
    rows = dlq.sched.items()
    assert [row["where"] for row in rows] == ["queued", "failed", "done"]
    assert named(rows) == ["20-queued.py", "30-failed.py", "10-done.py"]


def test_nothing_that_is_not_an_item_is_listed(dlq):
    """The contract README parses as a perfectly valid item, and is not one.

    Neither is a half-written file in ``.staging``, nor a note somebody left in
    the queue directory.
    """
    dlq.item("10-real.py")
    (dlq.root / "queue" / "notes.txt").write_text("a reminder")
    (dlq.root / "queue" / ".staging").mkdir()
    dlq.item("99-half-written.py", where="queue/.staging")
    assert named(dlq.sched.items()) == ["10-real.py"]


def test_a_name_is_listed_once_however_many_places_it_is_in(dlq):
    """A queued item and its own archived copy are one download, not two."""
    dlq.item("10-thing.py")
    dlq.item("10-thing.py", where="done/2026-08-30")
    rows = dlq.sched.items()
    assert named(rows) == ["10-thing.py"]
    assert rows[0]["where"] == "queued"


def test_how_much_of_it_is_here_is_counted_off_the_disk(dlq):
    """Never read out of ``.status.json``: that file is the item's own claim.

    It is written every few seconds and left behind by whatever killed the
    item, so a report of 400 MiB would survive a wipe of the 400 MiB.
    """
    dlq.item("10-thing.py", cap=100 * MiB)
    work = dlq.root / "work" / "10-thing.py"
    work.mkdir(parents=True)
    (work / "thing.iso.part").write_bytes(b"x" * 5000)
    # Everything an item leaves beside its download, none of it paid for by the
    # allowance: the sidecar, yt-dlp's fragment index, a merge in progress, and
    # the dotfiles both use to talk to the runner.
    (work / "thing.iso.part.meta.json").write_bytes(b"m" * 400)
    (work / "thing.ytdl").write_bytes(b"y" * 400)
    (work / "thing.temp.mp4").write_bytes(b"t" * 400)
    (work / ".status.json").write_text(json.dumps({"total_bytes": 20_000}))
    row = row_for(dlq.sched.items(), "10-thing.py")
    assert row["have"] == 5000
    # A size the server has stated beats the declared cap, which carries a
    # margin and so can never reach 100%.
    assert row["stated"] == 20_000
    assert row["total"] == 20_000
    assert dlq.sched._state_of(row) == "25%"


def test_what_is_here_is_every_payload_file_added_up(dlq):
    """A download that arrived in pieces has all of them on the disk.

    This figure is what ``dlq now`` subtracts from the declaration to say what
    it will spend, and what the progress cell counts against, so measuring
    only one of the pieces would ask for agreement to buy bytes already
    bought.
    """
    dlq.item("10-one.py", cap=100 * MiB)
    work = dlq.root / "work" / "10-one.py"
    work.mkdir(parents=True)
    for number, size in ((1, 3 * MiB), (2, 5 * MiB), (3, 2 * MiB)):
        (work / f"part{number}.iso").write_bytes(b"x" * size)
    (work / "nested").mkdir()
    (work / "nested" / "more.iso").write_bytes(b"x" * MiB)

    row = next(row for row in dlq.sched.items() if row["name"] == "10-one.py")
    assert row["have"] == 11 * MiB


def test_an_item_with_no_stated_size_is_measured_against_its_cap(dlq):
    dlq.item("10-thing.py", cap=1000)
    row = row_for(dlq.sched.items(), "10-thing.py")
    assert row["stated"] == 0
    assert row["total"] == 1000
    assert "≤" in dlq.sched._progress_of(row)


def test_a_file_the_runner_could_not_parse_is_listed_with_its_reason(dlq):
    """It has to be on the screen: it is the only thing that says what to do."""
    (dlq.root / "queue" / "10-broken.py").write_text("# nothing declared\n")
    row = row_for(dlq.sched.items(), "10-broken.py")
    assert row["error"]
    assert dlq.sched._state_of(row) == "REJECTED"
    assert dlq.sched._state_of(row, compact=True) == "!"


# --------------------------------------------------------------------------- #
# gone, and away
# --------------------------------------------------------------------------- #


def delivered(dlq, name, where):
    """A finished item whose file the runner recorded delivering to *where*."""
    dlq.item(name, where="done/2026-09-01")
    dlq.state({name: {"delivered": [str(where)], "attempts": 0}})


def test_a_file_that_is_there_is_the_answer_however_it_was_delivered(dlq):
    landing = dlq.root / "landing"
    landing.mkdir()
    (landing / "thing.iso").write_bytes(b"x" * 10)
    delivered(dlq, "10-thing.py", landing / "thing.iso")
    row = row_for(dlq.sched.items(), "10-thing.py")
    assert row["files"] == [landing / "thing.iso"]
    assert row["lost"] == ""
    assert dlq.sched._state_of(row) == "complete"


def test_a_readable_folder_without_the_file_in_it_means_gone(dlq):
    """Deleted by whoever wanted the space back, which is normal."""
    landing = dlq.root / "landing"
    landing.mkdir()
    delivered(dlq, "10-thing.py", landing / "thing.iso")
    row = row_for(dlq.sched.items(), "10-thing.py")
    assert row["files"] == []
    assert row["lost"] == "gone"
    assert dlq.sched._state_of(row) == "file gone"


def test_a_folder_that_cannot_be_read_says_nothing_about_the_file(dlq, unreadable):
    """Android's Downloads is *there* before the permission is granted.

    ``is_dir`` and ``os.access`` both answer yes to it; it simply raises when
    you look inside. Anything concluding "the file is not in that folder" has
    to have looked.
    """
    landing = dlq.root / "landing"
    landing.mkdir()
    delivered(dlq, "10-thing.py", landing / "thing.iso")
    unreadable(landing)
    row = row_for(dlq.sched.items(), "10-thing.py")
    assert row["lost"] == "away"
    assert dlq.sched._state_of(row) == "folder away"


def test_a_file_that_cannot_be_looked_at_costs_one_row_and_not_every_screen(
    dlq, monkeypatch
):
    """One revoked storage permission would otherwise take out every listing.

    ``Path.is_file`` answers False for a path that is not there and *raises*
    for one it is not allowed to look at, and this is reached from ``items()``.
    """
    landing = dlq.root / "landing"
    landing.mkdir()
    target = landing / "thing.iso"
    target.write_bytes(b"x" * 10)
    delivered(dlq, "10-thing.py", target)
    dlq.item("20-other.py")

    real = os.stat

    def stat(path, *args, **kw):
        if str(path) == str(target):
            raise PermissionError(13, "Permission denied")
        return real(path, *args, **kw)

    monkeypatch.setattr(os, "stat", stat)
    rows = dlq.sched.items()
    assert named(rows) == ["20-other.py", "10-thing.py"]
    assert row_for(rows, "10-thing.py")["files"] == []


def test_a_queued_download_has_not_lost_anything(dlq):
    """It has not started; only a finished item can be missing its file."""
    dlq.item("10-thing.py")
    assert row_for(dlq.sched.items(), "10-thing.py")["lost"] == ""


def test_where_it_was_put_outlives_the_file(dlq):
    """The record is the only thing that knows, once it is in a shared folder."""
    landing = dlq.root / "landing"
    landing.mkdir()
    delivered(dlq, "10-thing.py", landing / "thing.iso")
    row = row_for(dlq.sched.items(), "10-thing.py")
    assert row["recorded"] == [landing / "thing.iso"]
    assert dlq.sched.show_path(row) == 1  # it finished, and it is not there now


# --------------------------------------------------------------------------- #
# Naming one
# --------------------------------------------------------------------------- #


def test_a_name_is_matched_from_the_first_tier_that_hits(dlq):
    """An unlucky slug must never make another item un-typeable."""
    names = ["10-talk.py", "20-talk-two.py", "30-other.py"]
    match = dlq.sched.match
    assert match("10-talk.py", names) == ["10-talk.py"]  # the whole name
    assert match("10-talk", names) == ["10-talk.py"]  # the stem
    assert match("30", names) == ["30-other.py"]  # the number
    assert match("talk", names) == ["10-talk.py", "20-talk-two.py"]  # a part
    assert match("", names) == []
    assert match("nothing like it", names) == []


def test_a_name_that_matches_nothing_or_too_much_resolves_to_nothing(dlq, capsys):
    dlq.item("10-talk.py")
    dlq.item("20-talk-two.py")
    assert dlq.sched.resolve("nothing") is None
    assert dlq.sched.resolve("talk") is None
    assert dlq.sched.resolve("10-talk")["name"] == "10-talk.py"
    said = capsys.readouterr().err
    assert "10-talk.py" in said and "20-talk-two.py" in said


def test_the_extension_is_dropped_where_it_distinguishes_nothing(dlq):
    """Every item is a ``.py``, and three columns is a lot of a phone."""
    assert dlq.sched._display_name("10-talk.py") == "10-talk"
    assert dlq.sched._display_name("10-talk") == "10-talk"
    # What is shown is still what can be typed back.
    assert dlq.sched.match("10-talk", ["10-talk.py"]) == ["10-talk.py"]


# --------------------------------------------------------------------------- #
# The queue root itself
# --------------------------------------------------------------------------- #


def test_both_front_ends_and_the_runner_agree_where_the_queue_is(dlq):
    """Anchored to the checkout, never to ``__file__``.

    The alternative is a nightly job firing faithfully onto an empty queue and
    saying nothing about it.
    """
    assert dlq.sched.ROOT == dlq.runner.ROOT == dlq.ytq.HERE
    for name in ("QUEUE", "WORK", "OUT", "DONE", "FAILED", "LOGS"):
        assert getattr(dlq.sched, name) == getattr(dlq.runner, name)
    assert dlq.sched.LOCK_FILE == dlq.runner.LOCK_FILE


def test_a_directory_that_is_not_a_queue_root_is_said_so_rather_than_traced(dlq):
    """``EXPIRE_HOME`` is honoured blindly on purpose, so this is the check."""
    assert dlq.sched.root_problem() is None
    (dlq.root / "queue" / "README.md").unlink()
    problem = dlq.sched.root_problem()
    assert problem and "EXPIRE_HOME" in problem


def test_a_missing_sibling_checkout_is_named_rather_than_imported(dlq, monkeypatch):
    """``dlq status`` says which checkout is missing instead of tracebacking."""
    monkeypatch.setenv("ZWANA_HOME", str(dlq.root / "nowhere"))
    problem = dlq.sched.root_problem()
    assert problem and "quota_widget.py" in problem
    assert dlq.sched._zwana_root() == dlq.root / "nowhere"


# --------------------------------------------------------------------------- #
# What is happening at this second
# --------------------------------------------------------------------------- #


def test_a_download_is_live_while_its_progress_file_is_fresh(dlq):
    """Measured from the file the item writes, so it catches a ``dlq now`` in
    another terminal as readily as a nightly firing — both write it, and
    neither is visible in the job registration.

    Deliberately not "is the runner's lock held": taking that lock, even for
    the instant it takes to test it, could make a firing starting in the same
    second decide a run was already in progress and skip its slice.
    """
    import os
    import time

    dlq.item("10-thing.py")
    work = dlq.root / "work" / "10-thing.py"
    work.mkdir(parents=True)
    report = work / ".status.json"
    report.write_text("{}")

    assert dlq.sched._running_now(["10-thing.py"]) == "10-thing.py"
    assert dlq.sched._running_now(["20-other.py"]) == ""

    old = time.time() - dlq.sched.LIVE_SECONDS - 5
    os.utime(report, (old, old))
    assert dlq.sched._running_now(["10-thing.py"]) == ""


def test_the_last_firing_is_read_back_out_of_the_heartbeat(dlq):
    """~96 firings a day are no-ops, so they overwrite one file rather than
    filling the log; this is the line that says what the last one decided."""
    assert dlq.sched._last_firing() == ("", "")
    dlq.runner.heartbeat("queue empty")
    when, what = dlq.sched._last_firing()
    assert when and what == "queue empty"
    # A heartbeat this code did not write is said verbatim: it is the only
    # record of what the last firing thought.
    (dlq.root / "heartbeat").write_text("something else entirely\n")
    assert dlq.sched._last_firing() == ("", "something else entirely")


def test_a_log_of_one_day_lifts_the_date_off_every_line(dlq, capsys, monkeypatch):
    """A phone has about 40 columns and the date spends eleven of them saying
    the same thing on every line — but only when every line really is that one
    day, because the log spans nights and a wrong date is worse than a wide
    one.
    """
    monkeypatch.setenv("COLUMNS", "40")
    log = dlq.root / "logs" / "runner.log"
    log.write_text(
        "2026-09-01 23:00:01Z  window open\n"
        "2026-09-01 23:00:02Z  start 10-thing.py\n"
    )
    dlq.sched.tail(log, 40)
    said = capsys.readouterr().out
    assert said.splitlines()[0].strip() == "2026-09-01"
    assert "2026-09-01" not in "\n".join(said.splitlines()[1:])

    log.write_text(
        "2026-09-01 23:00:01Z  window open\n"
        "2026-09-02 23:00:02Z  window open\n"
    )
    dlq.sched.tail(log, 40)
    said = capsys.readouterr().out
    assert "2026-09-01" in said and "2026-09-02" in said


def test_a_log_that_is_not_there_says_so_rather_than_nothing(dlq, capsys):
    dlq.sched.tail(dlq.root / "logs" / "nothing.log", 40)
    assert "not written yet" in capsys.readouterr().out
