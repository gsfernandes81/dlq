"""The line through the queued list, and what it is allowed to promise.

The cut line is **computed and is never an item**: it has no name, no record
and no place in the order, it is worked out afresh on every draw from whatever
order the items are in — including an order that exists nowhere but under a
held item — and the cursor steps over it. It is
:func:`expire_runner.plan` over :func:`expire_runner.admit`, the same rule
``fire()`` admits items by, so the screen cannot promise bytes the night then
refuses.

What is checked here is that shape rather than any of the words on it: which
rows are above it, that it is emitted as a heading, that every download appears
exactly once whatever the width, and that moving an item moves the line and
never the budget.
"""

from __future__ import annotations

import pytest

MiB = 1024**2
GiB = 1024**3

WIDTHS = [32, 40, 80]


def three(dlq, caps=(100 * MiB, 150 * MiB, 120 * MiB)):
    """``one, two, three``, in that order, at the caps a test chooses."""
    for (number, name), cap in zip(((10, "one"), (20, "two"), (30, "three")), caps,
                                   strict=True):
        dlq.item(f"{number}-{name}.py", cap=cap, partial=False, desc=name)


def facts_for(dlq, free=400 * MiB, **rest):
    """A snapshot of the queue as it is now, with a night that would download."""
    return dlq.facts(portal=dlq.reading(free=free), force=True, **rest)


def order_of(dlq, facts=None):
    return [item["name"] for item in (facts or facts_for(dlq))["items"]]


def rows_of(entries):
    """Just the download rows, as ``(index, first line)``."""
    return [(index, lines[0]) for index, lines in entries if index is not None]


def headings(entries):
    return [lines[0] for index, lines in entries if index is None]


# --------------------------------------------------------------------------- #
# Where it falls
# --------------------------------------------------------------------------- #


def test_the_line_falls_after_the_last_download_tonight_reaches(dlq):
    """"Nothing below this gets anything tonight" — which is not the same as
    counting the items that got something.

    An item can be passed over for a reason of its own while a smaller one
    behind it still runs, and drawing the line by the count would put a
    download that happens below a line saying it does not.
    """
    three(dlq)
    facts = facts_for(dlq, free=400 * MiB)
    order = order_of(dlq, facts)
    planned = dlq.ui.tonight_plan(order, facts)
    reaches, ruled = dlq.ui.cut_index(order, facts, 40, planned)

    got = {entry["name"]: entry["bytes"] for entry in planned}
    assert got["10-one.py"] and got["20-two.py"] and not got["30-three.py"]
    assert reaches == 2
    assert ruled.strip()
    # Everything below the line really does get nothing.
    assert all(not got[name] for name in order[reaches:])


def test_a_night_that_reaches_everything_still_gets_a_line(dlq):
    """"All of this goes tonight" is an answer; a missing line reads as a
    screen that did not work it out."""
    three(dlq, caps=(MiB, MiB, MiB))
    facts = facts_for(dlq, free=600 * MiB)
    order = order_of(dlq, facts)
    reaches, ruled = dlq.ui.cut_index(order, facts, 40)
    assert reaches == len(order)
    assert ruled.strip()
    # Drawn at the foot of the queued group rather than dropped.
    entries = dlq.ui.compose_rows(dlq.sched.items(), 40, cut=(reaches, ruled))
    assert entries[-1][1] == [ruled]


def test_no_reading_yet_means_no_line_at_all(dlq):
    """An honest listing with no line beats a line drawn from figures nobody
    has."""
    three(dlq)
    assert dlq.ui.cut_index(order_of(dlq), None, 40) == (None, "")
    assert dlq.ui.cut_index([], facts_for(dlq), 40) == (None, "")
    assert dlq.ui.tonight_plan(["10-one.py"], None) == []


def test_moving_an_item_moves_the_line_and_never_the_budget(dlq):
    """The one-keypress example, on the screen this time.

    ``one, two | three`` becomes ``one, three | two``: the line still falls
    after exactly as much as the budget allows, just between a different pair
    of names.
    """
    three(dlq)
    facts = facts_for(dlq, free=400 * MiB)
    before = order_of(dlq, facts)
    after = [before[0], before[2], before[1]]

    reaches_before, _ = dlq.ui.cut_index(before, facts, 40)
    reaches_after, _ = dlq.ui.cut_index(after, facts, 40)
    assert reaches_before == reaches_after == 2

    spent = lambda order: sum(  # noqa: E731
        entry["bytes"] for entry in dlq.ui.tonight_plan(order, facts)
    )
    assert spent(before) <= facts["spendable"]
    assert spent(after) <= facts["spendable"]
    # A different pair of names above the line.
    assert before[:2] != after[:2]


def test_an_item_queued_since_the_reading_gets_nothing_said_about_it(dlq):
    """Saying anything else would be inventing a cap for it."""
    three(dlq)
    facts = facts_for(dlq)
    order = [*order_of(dlq, facts), "40-brand-new.py"]
    planned = dlq.ui.tonight_plan(order, facts)
    assert "40-brand-new.py" not in {entry["name"] for entry in planned}


# --------------------------------------------------------------------------- #
# When there is nothing
# --------------------------------------------------------------------------- #


def test_why_there_is_nothing_is_answered_from_as_far_up_as_it_goes(dlq):
    """A verdict that stops the night outranks any item's own refusal."""
    three(dlq)
    dlq.config({"auto": False})
    facts = dlq.facts(portal=dlq.reading())
    assert facts["verdict"] == "off"
    reaches, ruled = dlq.ui.cut_index(order_of(dlq, facts), facts, 40)
    assert reaches == 0
    assert dlq.ui.TONIGHT_SHORT["off"].split()[0] in ruled


def test_a_night_with_no_budget_blames_the_budget_and_not_the_first_item(dlq):
    """With nothing to spend every item is refused for being bigger than
    nothing, and the first of those refusals reads as a fact about that item.

    "slice 0 B below the useful minimum 32 MiB" is true and is not the answer.
    """
    three(dlq)
    facts = dlq.facts(portal=None, force=True, blind=False)
    assert facts["spendable"] == 0
    reaches, ruled = dlq.ui.cut_index(order_of(dlq, facts), facts, 80)
    assert reaches == 0
    assert "portal" in ruled
    assert "minimum" not in ruled


def test_a_night_that_can_spend_but_cannot_fit_anything_quotes_the_item(dlq):
    """There the answer really is about the items, so it is the runner's own
    first refusal, word for word."""
    three(dlq, caps=(4 * GiB, 4 * GiB, 4 * GiB))
    facts = facts_for(dlq, free=200 * MiB)
    order = order_of(dlq, facts)
    planned = dlq.ui.tonight_plan(order, facts)
    reason = next(entry["reason"] for entry in planned if entry["reason"])
    _, ruled = dlq.ui.cut_index(order, facts, 80, planned)
    assert reason.split(" (")[0][:20] in ruled


@pytest.mark.parametrize("width", WIDTHS)
def test_the_line_never_says_tonight_twice(dlq, width):
    """Several verdicts say it themselves — "done for tonight" — and after a
    lead-in that has just said it they read as a stammer."""
    three(dlq)
    for verdict in dlq.runner.GATE_STATES:
        facts = dict(facts_for(dlq), verdict=verdict, spendable=0)
        _, ruled = dlq.ui.cut_index(order_of(dlq, facts), facts, width)
        assert ruled.count("tonight") <= 1, (verdict, ruled)
        assert len(ruled) <= width


# --------------------------------------------------------------------------- #
# It is a heading, not an item
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("width", WIDTHS)
def test_every_download_appears_exactly_once_whatever_the_width(dlq, width):
    """A download missing from this screen looks exactly like a download that
    is not there — and this is the screen someone removes things from."""
    three(dlq)
    dlq.item("40-failed.py", where="failed")
    dlq.item("50-done.py", where="done/2026-09-01")
    rows = dlq.sched.items()
    facts = facts_for(dlq)
    order = [row["name"] for row in rows if row["where"] == "queued"]
    cut = dlq.ui.cut_index(order, facts, width)
    entries = dlq.ui.compose_rows(rows, width, cut=cut)

    indexes = [index for index, _ in rows_of(entries)]
    assert sorted(indexes) == list(range(len(rows)))


@pytest.mark.parametrize("width", WIDTHS)
def test_the_cut_line_is_emitted_as_a_heading(dlq, width):
    """Index ``None``, exactly like ``queued (3)``.

    That is the whole of how it stays computed rather than becoming an item:
    the cursor skips it, :func:`landed_index` cannot land on it, and no row's
    index moves because it is there.
    """
    three(dlq)
    rows = dlq.sched.items()
    facts = facts_for(dlq, free=400 * MiB)
    order = [row["name"] for row in rows if row["where"] == "queued"]
    reaches, ruled = dlq.ui.cut_index(order, facts, width)

    plain = dlq.ui.compose_rows(rows, width)
    ruled_entries = dlq.ui.compose_rows(rows, width, cut=(reaches, ruled))

    assert ruled in headings(ruled_entries)
    # The rows are the same rows, at the same indexes, in the same order.
    assert rows_of(plain) == rows_of(ruled_entries)


def test_the_cursor_never_lands_on_the_line(dlq):
    """A drop puts the item at a *position* among the queued, and the line has
    none."""
    three(dlq)
    rows = dlq.sched.items()
    for pos in range(-1, len(rows) + 2):
        landed = dlq.ui.landed_index(rows, pos)
        assert landed is not None
        assert rows[landed]["where"] == "queued"
    assert dlq.ui.landed_index([], 0) is None


# --------------------------------------------------------------------------- #
# The item the line falls inside of
# --------------------------------------------------------------------------- #


def test_the_row_that_straddles_the_line_says_how_much_of_it_comes_tonight(dlq):
    """A resumable download above the line is not the same thing as one that
    finishes tonight, and the line alone cannot say which."""
    dlq.item("10-big.py", cap=GiB, partial=True)
    facts = facts_for(dlq, free=300 * MiB)
    order = order_of(dlq, facts)
    planned = dlq.ui.tonight_plan(order, facts)
    share = planned[0]["bytes"]
    assert 0 < share < GiB

    rows = dlq.sched.items()
    entries = dlq.ui.compose_rows(rows, 80, tonight=planned)
    drawn = " ".join(line for _, lines in entries for line in lines)
    assert "tonight" in drawn
    assert dlq.ui._tonight_share(rows[0], share).endswith("tonight")


def test_nothing_is_said_on_a_row_the_night_finishes_or_never_reaches(dlq):
    """On most rows the figure would only be the progress cell said twice, and
    the one row where it is news would be lost among them."""
    row = {"cap": 100 * MiB, "have": 0}
    assert dlq.ui._tonight_share(row, 100 * MiB) == ""
    assert dlq.ui._tonight_share(row, 0) == ""
    assert dlq.ui._tonight_share(row, 40 * MiB)


# --------------------------------------------------------------------------- #
# A download that does not exist yet
# --------------------------------------------------------------------------- #


def test_a_phantom_is_planned_like_any_other_item_and_writes_nothing(dlq):
    """ytq's picker holds a video it has not written on this listing.

    The worth of showing it here is that the cut line and the shares are worked
    out **with it in**, in the place it is being dragged to — so it has to be
    an item the runner's own projection would accept.
    """
    three(dlq)
    before = sorted((dlq.root / "queue").glob("*"))
    row, item = dlq.ui.phantom_of("15-new-video.py", 200 * MiB, True)

    assert row["where"] == "queued" and row["phantom"] is True
    assert row["have"] == 0 and row["stated"] == 0  # nobody has measured it
    facts = facts_for(dlq, free=600 * MiB)
    facts = {**facts, "items": [*facts["items"], item]}
    order = ["10-one.py", "15-new-video.py", "20-two.py", "30-three.py"]
    planned = dlq.ui.tonight_plan(order, facts)
    assert [entry["name"] for entry in planned] == order
    assert next(e for e in planned if e["name"] == "15-new-video.py")["bytes"]

    # Nothing was written: an answer of None and an answer of 3 leave exactly
    # the same queue behind.
    assert sorted((dlq.root / "queue").glob("*")) == before


def test_the_held_keys_are_one_spelling_for_both_things_that_are_held(dlq):
    """An item being moved and a video ytq has not written yet are held by the
    same two arrows; a picker that answered ⏎ differently would be two screens
    that look identical disagreeing about what enter means."""
    import curses

    held = dlq.ui.held_key
    assert held(curses.KEY_UP, 2, 5)[0] == 1
    assert held(curses.KEY_DOWN, 2, 5)[0] == 3
    assert held(curses.KEY_HOME, 2, 5)[0] == 0
    assert held(curses.KEY_END, 2, 5)[0] == 5
    assert held(10, 2, 5) == (2, "take")
    assert held(27, 2, 5) == (2, "leave")
    assert held(ord("z"), 2, 5) == (2, "")
    # Never off the end of the queue, in either direction.
    assert held(curses.KEY_UP, 0, 5)[0] == 0
    assert held(curses.KEY_DOWN, 5, 5)[0] == 5


def test_a_preview_moves_the_row_and_leaves_the_queue_alone(dlq):
    """Nothing is renamed until it is dropped, so ↑↓ cost nothing."""
    three(dlq)
    rows = dlq.sched.items()
    before = sorted((dlq.root / "queue").glob("*.py"))
    shown = dlq.ui.preview(rows, "30-three.py", 0)
    assert [row["name"] for row in shown][:1] == ["30-three.py"]
    assert sorted(row["name"] for row in shown) == sorted(row["name"] for row in rows)
    assert sorted((dlq.root / "queue").glob("*.py")) == before
