"""Changing the queue: the order, the names, and taking something off the list.

Done here on a real temporary queue and read back through the real reader,
because both failures this guards against are silent. **Bytes losing their
item** — a rename that moves the queue file and leaves a paid-for partial under
a name nothing will look for again — leaves a queue that looks fine and starts
from zero the next night. **A download missing from the screen it is removed
from** looks exactly like a download that was never there.

Two rules hold everywhere below. Everything an item owns moves with it, which
is :func:`expire_ui.belongings` and every mutation spelled from it. And nothing
outside the queue's own root is ever deleted: a delivered file sits in
Downloads among the phone's other files and belongs to whoever asked for it.
"""

from __future__ import annotations

import fcntl
import json

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

MiB = 1024**2


def queued(dlq):
    """The queued downloads in the order the runner would work them.

    Read off the file names, because that is what the runner sorts: this is the
    order, not a view of it.
    """
    return sorted(path.name for path in (dlq.root / "queue").glob("*.py"))


def slugs(dlq):
    return [dlq.ui.parse_name(name)[1] for name in queued(dlq)]


def stock(dlq, *names, bytes_each=1024):
    """A queue of *names*, ten apart, each with a partial download of its own."""
    for number, name in enumerate(names, start=1):
        item = f"{number * 10:02d}-{name}.py"
        dlq.item(item, cap=100 * MiB)
        work = dlq.root / "work" / item
        work.mkdir(parents=True)
        # The bytes carry the slug, so a partial that ends up under the wrong
        # item can be recognised rather than merely counted.
        (work / "file.part").write_bytes(name.encode() * bytes_each)
    dlq.state({f"{n * 10:02d}-{name}.py": {"attempts": 0} for n, name in
               enumerate(names, start=1)})


def move(dlq, slug, pos):
    """Move the download whose slug is *slug* to queued position *pos*."""
    rows = dlq.sched.items()
    name = next(row["name"] for row in rows if slug in row["name"])
    return dlq.ui.do_reorder(rows, name, pos)


# --------------------------------------------------------------------------- #
# The order
# --------------------------------------------------------------------------- #


def test_the_one_keypress_example(dlq):
    """``one, two, three`` with ``three`` moved up once is ``one, three, two``.

    The user's own example, and the whole of what a move means: the order
    changes and nothing else does.
    """
    stock(dlq, "one", "two", "three")
    said, moved = move(dlq, "three", 1)
    assert moved and said
    assert slugs(dlq) == ["one", "three", "two"]
    # Every download still has its own bytes under its own name.
    for slug, name in zip(slugs(dlq), queued(dlq), strict=True):
        assert (dlq.root / "work" / name / "file.part").read_bytes().startswith(
            slug.encode()
        )


def test_a_move_that_changes_nothing_says_so_and_renames_nothing(dlq):
    stock(dlq, "one", "two", "three")
    before = queued(dlq)
    said, moved = move(dlq, "one", 0)
    assert not moved and said
    assert queued(dlq) == before


def test_only_a_queued_download_has_a_place_in_the_order(dlq):
    dlq.item("10-done.py", where="done/2026-09-01")
    rows = dlq.sched.items()
    said, moved = dlq.ui.do_reorder(rows, "10-done.py", 0)
    assert not moved and said


def test_a_number_is_always_two_digits(dlq):
    """The runner sorts file *names*, so ``100`` would sort before ``20``.

    An item moved to the back would arrive at the front, and the only symptom
    would be things running in the wrong order.
    """
    for number in (0, 5, 99):
        name = dlq.ui.renumber("10-thing.py", number)
        digits = name.split("-", 1)[0]
        assert len(digits) == 2 and digits.isdigit()
    # Nothing ever hands it a bigger one: every key the queue gives out comes
    # from ``slot`` or ``spread``, and both stop at two digits.
    assert all(key <= dlq.ytq.MAX_PRIORITY for key in dlq.ui.spread(90))
    assert dlq.ui.slot([dlq.ytq.MAX_PRIORITY], 1) is None
    # And the slug is untouched: it is what everything else names the item by.
    assert dlq.ui.renumber("10-a-very-long-slug-indeed.py", 20) == (
        "20-a-very-long-slug-indeed.py"
    )


def test_a_place_with_no_room_left_renumbers_the_whole_queue(dlq):
    """Two neighbours one apart, and the move still happens.

    A plain swap of two adjacent items is already a cycle: one of them has to
    be parked on a key nobody wants while the other moves. What is parked on is
    a **real priority**, so a firing that starts in the middle reads a queue
    that is complete and legal, just possibly in an order nobody asked for.
    """
    dlq.item("10-one.py")
    dlq.item("11-two.py")
    said, moved = move(dlq, "two", 0)
    assert moved and said
    assert slugs(dlq) == ["two", "one"]
    assert len(queued(dlq)) == 2


def test_moving_never_loses_or_duplicates_a_download(dlq):
    """Whatever the queue is, and however many times it is rearranged."""
    stock(dlq, "one", "two", "three", "four")
    for slug, pos in [("four", 0), ("one", 3), ("three", 1), ("two", 0)]:
        move(dlq, slug, pos)
        assert sorted(slugs(dlq)) == ["four", "one", "three", "two"]


@given(
    moves=st.lists(
        st.tuples(st.sampled_from(["one", "two", "three"]), st.integers(0, 2)),
        max_size=6,
    )
)
def test_the_screen_order_and_the_runner_order_are_the_same_order(dlq, moves):
    """The property under the cut line: a move changes the order and nothing
    else.

    After any sequence of moves, the order the runner would work — file names,
    sorted — is the order the last move asked for, every download is still
    there exactly once, and every download's bytes are still under its own
    name.
    """
    for path in (dlq.root / "queue").glob("*.py"):
        path.unlink()
    for path in (dlq.root / "work").iterdir():
        for inner in path.iterdir():
            inner.unlink()
        path.rmdir()
    stock(dlq, "one", "two", "three")

    wanted = ["one", "two", "three"]
    for slug, pos in moves:
        rest = [other for other in wanted if other != slug]
        pos = max(0, min(pos, len(rest)))
        said, moved = move(dlq, slug, pos)
        assert said
        if moved:
            wanted = rest[:pos] + [slug] + rest[pos:]
        assert slugs(dlq) == wanted
        for name in queued(dlq):
            assert len(name.split("-", 1)[0]) == 2
            slug_of = dlq.ui.parse_name(name)[1]
            kept = (dlq.root / "work" / name / "file.part").read_bytes()
            assert kept.startswith(slug_of.encode())


def test_a_place_is_a_position_and_never_a_number(dlq):
    """``slot`` answers with room to insert, or ``None`` to renumber the lot."""
    slot = dlq.ui.slot
    assert slot([], 0) == 10
    assert slot([10, 30], 1) == 20
    assert slot([10, 20], 2) == 30
    assert slot([10, 11], 1) is None  # no room between them
    assert slot([dlq.ytq.MAX_PRIORITY], 1) is None  # no room at the back
    assert slot([1], 0) == 0
    assert slot([0], 0) is None
    for others, pos in (([10, 30], 1), ([10, 20], 2), ([], 0), ([40], 0)):
        answer = slot(others, pos)
        assert answer is None or 0 <= answer <= dlq.ytq.MAX_PRIORITY


@given(count=st.integers(1, 90))
def test_fresh_keys_are_spread_and_always_fit_two_digits(dlq, count):
    keys = dlq.ui.spread(count)
    assert len(keys) == count == len(set(keys))
    assert keys == sorted(keys)
    assert all(0 <= key <= dlq.ytq.MAX_PRIORITY for key in keys)


# --------------------------------------------------------------------------- #
# What an item owns
# --------------------------------------------------------------------------- #


def test_everything_an_item_owns_moves_with_it(dlq):
    """The queue file, the partial, the outbox and the record in state.json."""
    stock(dlq, "thing")
    out = dlq.root / "out" / "10-thing.py"
    out.mkdir(parents=True)
    (out / "thing.iso").write_bytes(b"finished")
    dlq.state({"10-thing.py": {"attempts": 2, "part_bytes": 1234}})

    row = dlq.sched.items()[0]
    assert dlq.ui.do_rename(row, "10-other.py") == "now 10-other"

    assert queued(dlq) == ["10-other.py"]
    assert (dlq.root / "work" / "10-other.py" / "file.part").is_file()
    assert (dlq.root / "out" / "10-other.py" / "thing.iso").read_bytes() == b"finished"
    state = json.loads((dlq.root / "state.json").read_text())["items"]
    assert state["10-other.py"]["attempts"] == 2
    assert "10-thing.py" not in state
    # And nothing of the old name is left behind to be found again.
    assert not (dlq.root / "work" / "10-thing.py").exists()


def test_a_rename_is_refused_before_it_can_mix_two_downloads(dlq):
    """A ``work/`` directory left by a removed item still has bytes in it."""
    stock(dlq, "one", "two")
    rows = dlq.sched.items()
    row = rows[0]
    assert dlq.ui.refuse_rename(row, row["name"]) == "that is the name it has"
    assert dlq.ui.refuse_rename(row, "not-an-item-name")
    assert dlq.ui.refuse_rename(row, "20-two.py")  # another item answers to it
    (dlq.root / "work" / "40-orphan.py").mkdir(parents=True)
    assert dlq.ui.refuse_rename(row, "40-orphan.py")
    assert dlq.ui.refuse_rename(row, "40-free.py") is None


def test_a_rename_that_cannot_finish_puts_back_what_it_moved(dlq, monkeypatch):
    """An item whose scratch moved and whose file did not downloads itself again."""
    stock(dlq, "thing")
    row = dlq.sched.items()[0]
    moves = dlq.ui.rename_moves(row, "20-thing.py")
    assert [source.name for source in (pair[0] for pair in moves)] == [
        "10-thing.py",
        "10-thing.py",
    ]

    # The scratch directory refuses to move; the queue file has already gone.
    real = type(moves[0][0]).rename

    def rename(self, target):
        if self.parent.name == "work":
            raise OSError(13, "Permission denied")
        return real(self, target)

    monkeypatch.setattr(type(moves[0][0]), "rename", rename)
    assert dlq.ui._apply(moves)
    monkeypatch.undo()
    # Back where it started, whole.
    assert queued(dlq) == ["10-thing.py"]
    assert (dlq.root / "work" / "10-thing.py" / "file.part").is_file()


# --------------------------------------------------------------------------- #
# Taking one off the list
# --------------------------------------------------------------------------- #


def test_removing_keeps_what_was_paid_for(dlq):
    """The item goes; the partial stays, and so does anything finished."""
    stock(dlq, "thing")
    out = dlq.root / "out" / "10-thing.py"
    out.mkdir(parents=True)
    (out / "thing.iso").write_bytes(b"finished")

    row = dlq.sched.items()[0]
    said = dlq.ui.do_remove(row)
    assert said.startswith("removed")
    assert dlq.sched.items() == []
    assert (dlq.root / "work" / "10-thing.py" / "file.part").is_file()
    assert (out / "thing.iso").is_file()
    assert "10-thing.py" not in json.loads((dlq.root / "state.json").read_text())["items"]


def test_the_second_answer_deletes_the_partial_and_never_the_finished_file(dlq):
    """The partial is the only thing here it is safe to delete: nothing but
    this item could ever have resumed it."""
    stock(dlq, "thing")
    out = dlq.root / "out" / "10-thing.py"
    out.mkdir(parents=True)
    (out / "thing.iso").write_bytes(b"finished")

    row = dlq.sched.items()[0]
    dlq.ui.do_remove(row, bytes_too=True)
    assert not (dlq.root / "work" / "10-thing.py").exists()
    assert (out / "thing.iso").is_file()


def test_nothing_outside_the_queue_root_is_ever_deleted(dlq):
    """A delivered file is the property of whoever asked for it."""
    landing = dlq.root.parent / "elsewhere"
    landing.mkdir()
    (landing / "thing.iso").write_bytes(b"theirs")
    dlq.item("10-thing.py", where="done/2026-09-01")
    dlq.state({"10-thing.py": {"delivered": [str(landing / "thing.iso")]}})

    row = dlq.sched.items()[0]
    for bytes_too in (False, True):
        goes, _ = dlq.ui.removal(row, bytes_too)
        for path in goes:
            assert dlq.root in path.parents or path == dlq.root
    dlq.ui.do_remove(row, bytes_too=True)
    assert (landing / "thing.iso").read_bytes() == b"theirs"


def test_scratch_that_holds_nothing_paid_for_goes_either_way(dlq):
    """An item that failed before moving a byte still leaves a status file.

    Keeping that would leave a directory nothing lists and nothing will resume.
    """
    dlq.item("10-thing.py")
    work = dlq.root / "work" / "10-thing.py"
    work.mkdir(parents=True)
    (work / ".status.json").write_text("{}")
    row = dlq.sched.items()[0]
    assert row["have"] == 0
    dlq.ui.do_remove(row)
    assert not work.exists()


def test_the_keys_the_remove_screen_answers_to_never_vary(dlq):
    """A key live on one download and meaning "no" on the next is worse than
    one that is not there at all.

    That is how the removal bug hid: on an item that had downloaded nothing,
    ``b`` fell through to "any other key: no" and the download stayed, quietly,
    exactly as it would have if you had meant it.
    """
    stock(dlq, "with-bytes")
    dlq.item("20-without.py")
    rows = dlq.sched.items()
    with_bytes = next(row for row in rows if row["have"])
    without = next(row for row in rows if not row["have"])

    def letters(answers):
        return {key for keys, _ in answers for key in keys}

    assert letters(dlq.ui.removal_answers(with_bytes)) == {"y", "b"}
    assert letters(dlq.ui.removal_answers(without)) == {"y", "b"}
    # What differs is only whether they are worth offering separately.
    assert len(dlq.ui.removal_answers(with_bytes)) == 2
    assert len(dlq.ui.removal_answers(without)) == 1
    assert all(label.strip() for _, label in dlq.ui.removal_answers(without))


def test_a_done_download_whose_file_was_deleted_removes_itself(dlq):
    """It answered "where did it go", and nothing can ask that any more."""
    landing = dlq.root / "landing"
    landing.mkdir()
    dlq.item("10-thing.py", where="done/2026-09-01")
    dlq.state({"10-thing.py": {"delivered": [str(landing / "thing.iso")]}})

    rows = dlq.sched.items()
    assert rows[0]["lost"] == "gone"
    assert dlq.ui.forget_gone(rows) == ["thing"]
    assert dlq.sched.items() == []


def test_a_folder_that_cannot_be_read_is_never_acted_on(dlq, monkeypatch):
    """Unreadable is ``away``, and away is left alone.

    This is the whole reason forgetting can be automatic at all: a permissions
    blip must not write off every completed download on the phone.
    """
    landing = dlq.root / "landing"
    landing.mkdir()
    dlq.item("10-thing.py", where="done/2026-09-01")
    dlq.state({"10-thing.py": {"delivered": [str(landing / "thing.iso")]}})

    import os

    real = os.listdir
    monkeypatch.setattr(
        os,
        "listdir",
        lambda path=".": (_ for _ in ()).throw(PermissionError(13, "no"))
        if str(path) == str(landing)
        else real(path),
    )
    rows = dlq.sched.items()
    assert rows[0]["lost"] == "away"
    assert dlq.ui.forget_gone(rows) == []
    monkeypatch.undo()
    assert len(dlq.sched.items()) == 1


def test_an_item_with_no_record_of_where_it_went_is_left_alone(dlq):
    """With no record there is nothing to have looked in.

    Absence of evidence is not the same fact as evidence of absence.
    """
    dlq.item("10-thing.py", where="done/2026-09-01")
    rows = dlq.sched.items()
    assert dlq.ui.forget_gone(rows) == []
    assert len(dlq.sched.items()) == 1


# --------------------------------------------------------------------------- #
# Retrying, and the guard over all of it
# --------------------------------------------------------------------------- #


def test_a_failed_item_comes_back_with_its_nights_and_its_bytes(dlq):
    """The wipe is not a courtesy: an item put back with its attempts still on
    the clock is given up on again by the first firing that touches it."""
    dlq.item("10-thing.py", where="failed")
    work = dlq.root / "work" / "10-thing.py"
    work.mkdir(parents=True)
    (work / "file.part").write_bytes(b"x" * 2048)
    dlq.state({"10-thing.py": {"attempts": 3, "stalls": 2, "retired": "failed"}})

    row = dlq.sched.items()[0]
    assert dlq.ui.do_requeue(row).endswith("is queued again")
    assert queued(dlq) == ["10-thing.py"]
    record = json.loads((dlq.root / "state.json").read_text())["items"]["10-thing.py"]
    assert record["attempts"] == 0 and record["stalls"] == 0
    assert "retired" not in record
    assert (work / "file.part").stat().st_size == 2048


def test_clearing_the_tries_of_a_queued_item(dlq):
    dlq.item("10-thing.py")
    dlq.state({"10-thing.py": {"attempts": 2}})
    row = dlq.sched.items()[0]
    assert dlq.ui.do_clear_tries(row).endswith("cleared")
    assert json.loads((dlq.root / "state.json").read_text())["items"]["10-thing.py"][
        "attempts"
    ] == 0
    assert dlq.ui.do_clear_tries(dlq.sched.items()[0]) == "it has no failed tries"


def test_nothing_may_be_changed_while_the_queue_is_busy(dlq):
    """A firing is writing into ``work/<name>/`` and will rename the item into
    ``done/`` when it finishes; moving either underneath it loses the download.

    The guard lives with the mutation and not with the screen, so it cannot be
    skipped by a caller who did not know it was needed — which is why every one
    of them is asked here rather than one standing for the rest.
    """
    stock(dlq, "one", "two")
    rows = dlq.sched.items()
    before = queued(dlq)

    handle = (dlq.root / "runner.lock").open("w")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert dlq.ui.busy_problem()
        said = [
            dlq.ui.do_reorder(rows, rows[0]["name"], 1)[0],
            dlq.ui.do_rename(rows[0], "40-new.py"),
            dlq.ui.do_remove(rows[0]),
            dlq.ui.do_clear_tries(rows[0]),
            dlq.ui.do_requeue(rows[0]),
            dlq.ui.refuse_rename(rows[0], "40-new.py"),
        ]
        assert all(answer == dlq.ui.busy_problem() for answer in said)
        assert dlq.ui.forget_gone(rows) == []
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()

    assert queued(dlq) == before
    assert (dlq.root / "work" / before[0] / "file.part").is_file()
    assert dlq.ui.busy_problem() is None


@given(
    number=st.integers(0, 99),
    words=st.text(min_size=1, max_size=40),
)
def test_a_name_survives_being_taken_apart_and_put_back(dlq, number, words):
    """The number, the slug and the suffix, and back again.

    A rename goes through ytq's slugify because that half was typed by a
    person and the rule for what an item may be called is ytq's — one spelling
    of it, or the two front ends produce names that sort against each other
    differently. A move does not, because it must not silently shorten a long
    name as well as move it.
    """
    slug = dlq.ytq.slugify(words)
    assume(slug and dlq.ui.NAME_RE.match(f"{number:02d}-{slug}.py"))
    name = f"{number:02d}-{slug}.py"

    assert dlq.ui.parse_name(name) == (number, slug)
    assert dlq.ui.renumber(name, number) == name
    assert dlq.ui.reslug(name, slug) == name
    assert dlq.ui._slug_of(name) == slug
    # And moving it keeps the slug whatever the number becomes.
    assert dlq.ui.parse_name(dlq.ui.renumber(name, 99))[1] == slug


@pytest.mark.parametrize(
    ("name", "parsed"),
    [
        ("10-thing.py", (10, "thing")),
        ("00-a.py", (0, "a")),
        ("100-thing.py", (100, "thing")),
        ("thing.py", None),
        ("1-thing.py", None),
        ("10-thing.txt", None),
        ("10-a/b.py", None),
    ],
)
def test_what_an_item_may_be_called(dlq, name, parsed):
    """Stricter than the runner's own rule, because this one has to *make* one.

    A name this does not match is a file the runner would ignore for ever
    without saying so.
    """
    assert dlq.ui.parse_name(name) == parsed


# --------------------------------------------------------------------------- #
# Every key that is offered does something
# --------------------------------------------------------------------------- #


def test_every_action_an_item_offers_is_one_the_screen_can_carry_out(dlq):
    """An action that is offered and does nothing cannot ship.

    ``actions_for`` decides what a download is worth being asked, ``ACTS`` is
    what each of those keys runs, and ``SCREEN_KEYS`` is the one whose effect
    can only be seen back on the list. A key in the first and in neither of the
    others is a key that reads as broken.
    """
    stock(dlq, "queued")
    dlq.item("20-failed.py", where="failed")
    dlq.item("30-done.py", where="done/2026-09-01")
    (dlq.root / "queue" / "40-broken.py").write_text("# nothing declared\n")
    landing = dlq.root / "landing"
    landing.mkdir()
    (landing / "film.mp4").write_bytes(b"x")
    dlq.state({"30-done.py": {"delivered": [str(landing / "film.mp4")]}})

    offered = set()
    for row in dlq.sched.items():
        keys = [key for key, _ in dlq.ui.actions_for(row)]
        assert len(keys) == len(set(keys)), row["name"]
        for key, label in dlq.ui.actions_for(row):
            assert label.strip()
            assert key in dlq.ui.ACTS or key in dlq.ui.SCREEN_KEYS, key
        offered |= set(keys)
    # And the ones that only make sense somewhere are only offered there.
    assert offered >= {"n", "u", "m", "r", "o", "l", "d"}


def test_what_is_offered_depends_on_where_the_download_is(dlq):
    """An archived download has no priority to change and a rejected one
    cannot be told to run."""
    dlq.item("10-queued.py")
    dlq.item("20-failed.py", where="failed")
    dlq.item("30-done.py", where="done/2026-09-01")
    rows = {row["name"]: row for row in dlq.sched.items()}
    keys = lambda name: {key for key, _ in dlq.ui.actions_for(rows[name])}  # noqa: E731

    assert "n" in keys("10-queued.py") and "m" in keys("10-queued.py")
    assert "u" in keys("20-failed.py") and "n" not in keys("20-failed.py")
    assert "m" not in keys("30-done.py") and "u" not in keys("30-done.py")
    # Removing is offered everywhere: it is a decision about the *list*.
    for name in rows:
        assert "d" in keys(name)


def test_every_colour_a_row_can_be_worth_can_be_drawn(dlq):
    """A tone missing from the screen's table is a row that loses its colour
    with nothing saying so.

    ``expire_sched._tone`` is where "what colour is this row worth" is decided;
    ``expire_ui.TONES`` is only the translation of it into curses attributes.
    """
    rows = [
        {"error": "no header", "files": [], "where": "queued", "have": 0},
        {"error": None, "files": [object()], "where": "done", "have": 1},
        {"error": None, "files": [], "where": "failed", "have": 0},
        {"error": None, "files": [], "where": "queued", "have": 10},
        {"error": None, "files": [], "where": "queued", "have": 0},
    ]
    for row in rows:
        assert dlq.sched._tone(row) in dlq.ui.TONES
    # Four different answers, so the palette is saying something.
    assert len({dlq.sched._tone(row) for row in rows}) == len(rows)


# --------------------------------------------------------------------------- #
# What a screen says before it spends, and before it stops the nightly job
# --------------------------------------------------------------------------- #


def test_the_word_the_job_row_uses_is_the_word_the_key_reads(dlq):
    """Spelled differently, the screen would offer "arm it" over a status line
    saying it is armed — and both would look right on their own."""
    armed_row = [("job", f"{dlq.sched.ARMED}, fires every 15m", "32")]
    assert dlq.ui.armed(armed_row) is True
    assert dlq.ui.armed([("job", "not armed - dlq arm", "1;31")]) is False
    assert dlq.ui.armed([]) is False


def test_the_confirm_that_stops_the_nightly_job_names_the_key_that_starts_it(dlq):
    """A confirm can be wrong about its own screen, and this is the one line
    where being wrong costs the nightly job.

    It said ``a`` for as long as the queue had a screen of its own with arming
    on it, and went on saying ``a`` after the key moved — by which time ``a``
    was the switch that stops the queue downloading at all.
    """
    lines = dlq.ui.cancel_note()
    assert lines and all(line.strip() for line in lines)
    assert chr(dlq.ui.PAGE_KEYS[1]) in lines[-1]
    assert chr(dlq.ui.SETTING_KEYS[0]) not in lines[-1].split()[0]


def test_the_number_is_the_whole_point_of_asking(dlq):
    """Both confirms say what it costs, and both say who is counting it."""
    dlq.item("10-thing.py", cap=200 * MiB)
    row = dlq.sched.items()[0]
    facts = dlq.facts(portal=dlq.reading(free=300 * MiB), force=True)

    for note, size in (
        (dlq.ui.now_note(row, seen=True), dlq.ytq.human(200 * MiB)),
        (dlq.ui.run_note(facts), dlq.ytq.human(facts["spendable"])),
    ):
        assert any(size in line for line in note)
        assert all(line.strip() for line in note)

    # With no portal answering, nothing is counting it — and that is said as
    # well as the number, because mobile data and a portal that is down look
    # identical from here.
    blind_facts = dlq.facts(blind=True, force=True)
    for note in (dlq.ui.now_note(row, seen=False), dlq.ui.run_note(blind_facts)):
        said = " ".join(note)
        assert "mobile data" in said
        assert "counting" in said


def test_what_an_item_owns_is_decided_in_one_place(dlq):
    """Three of the four things: the item file, the scratch it downloads into
    and the outbox it delivers into. The fourth is its record in state.json,
    which is not a path and is moved alongside them.
    """
    stock(dlq, "thing")
    row = dlq.sched.items()[0]
    owned = dict(dlq.ui.belongings(row))
    assert owned["item"] == dlq.root / "queue" / "10-thing.py"
    assert owned["work"] == dlq.root / "work" / "10-thing.py"
    assert "out" not in owned  # there is none yet

    (dlq.root / "out" / "10-thing.py").mkdir(parents=True)
    assert "out" in dict(dlq.ui.belongings(dlq.sched.items()[0]))

    # Its logs are deliberately not owned: a night's log is the record of that
    # night, and the runner's own log names the item in prose anyway.
    for _, path in dlq.ui.belongings(row):
        assert path.parent != dlq.root / "logs"
