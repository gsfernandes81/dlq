"""What a night may spend, and the rule that decides it item by item.

:func:`expire_runner.admit` is the whole of the per-item decision and
:func:`expire_runner.plan` is the night walked against it. Both are pure, which
is what lets a screen ask "and if they were in this order instead?" — and it is
also what makes the guarantee checkable rather than argued: **the sum of what a
projection promises is never more than the budget it was given, for any
ordering of the items.** That is proved here by permutation and by random
nights rather than by one queue that happens to work out.

The rest of this file is the reasons an item is turned down. Each of them is
somebody's answer to "why did that not run", so each is pinned by the
*behaviour* — nothing is admitted, and a sentence comes back with it — rather
than by the words, which are free to improve.
"""

from __future__ import annotations

import itertools

from hypothesis import given
from hypothesis import strategies as st

MiB = 1024**2
GiB = 1024**3

#: A rate nothing is refused for being too slow at, so the tests below are
#: about the budget rather than about the clock.
FAST = 50 * MiB


def item(name, cap, partial=True, part=None, slice_min=None):
    """A queue item as :func:`expire_runner.snapshot` hands one over.

    *part* left out is an item that carries no progress figure of its own,
    which is the half of ``snapshot`` a firing does not have — there the
    progress comes from ``state.json`` instead.
    """
    return {
        "name": name,
        "cap": cap,
        "partial": partial,
        **({} if part is None else {"part_bytes": part}),
        **({} if slice_min is None else {"slice_min": slice_min}),
    }


def total(planned):
    return sum(entry["bytes"] for entry in planned)


# --------------------------------------------------------------------------- #
# The budget is the one thing an ordering cannot change
# --------------------------------------------------------------------------- #


@given(
    caps=st.lists(st.integers(1, 4 * GiB), min_size=1, max_size=5),
    partial=st.lists(st.booleans(), min_size=5, max_size=5),
    budget=st.integers(0, 3 * GiB),
    rate=st.floats(50_000, 20 * MiB),
    seconds=st.integers(0, 6 * 3600),
)
def test_a_night_never_promises_more_than_the_budget(
    dlq, caps, partial, budget, rate, seconds
):
    """Whatever the queue is and whatever order it is in, the sum fits.

    The property, in the words the runner's own docstring puts it in: every
    item is admitted out of what is *left*, so the total is at most the budget
    whichever way round the queue is put. Random nights rather than one, and
    every ordering of each, because the failure this guards against is an
    arithmetic slip that only shows on some arrangements.
    """
    items = [
        item(f"{10 + n:02d}-item-{n}.py", cap, partial=partial[n])
        for n, cap in enumerate(caps)
    ]
    for order in itertools.permutations(items):
        planned = dlq.runner.plan(list(order), {}, budget, rate, seconds, False)
        assert total(planned) <= budget
        assert len(planned) == len(items)
        assert [entry["name"] for entry in planned] == [row["name"] for row in order]


def test_reordering_moves_the_line_and_never_the_budget(dlq):
    """The user's own one-keypress example, at the projection underneath it.

    ``one, two | three`` becoming ``one, three | two`` after moving ``three``
    up once: what tonight reaches is a different pair of names, and what
    tonight spends is the same budget either way.
    """
    budget = 300 * MiB
    one = item("10-one.py", 100 * MiB, partial=False)
    two = item("20-two.py", 150 * MiB, partial=False)
    three = item("30-three.py", 120 * MiB, partial=False)

    before = dlq.runner.plan([one, two, three], {}, budget, FAST, 3600, False)
    after = dlq.runner.plan([one, three, two], {}, budget, FAST, 3600, False)

    reached = lambda planned: {e["name"] for e in planned if e["bytes"]}  # noqa: E731
    assert reached(before) == {"10-one.py", "20-two.py"}
    assert reached(after) == {"10-one.py", "30-three.py"}
    assert total(before) <= budget and total(after) <= budget
    # The one that fell below the line is refused, and told why.
    assert next(e for e in after if e["name"] == "20-two.py")["reason"]


def test_a_projection_writes_nothing_it_was_shown(dlq):
    """Pure: the items handed in come back unchanged, and no state is written.

    The screen hands :func:`plan` its own list in an order that exists nowhere
    but on the screen. If planning mutated either, moving the cursor would
    change the queue.
    """
    items = [item("10-a.py", 200 * MiB), item("20-b.py", 200 * MiB)]
    before = [dict(row) for row in items]
    state = {"items": {"10-a.py": {"part_bytes": 5 * MiB}}}
    dlq.runner.plan(items, state, 500 * MiB, FAST, 3600, False)
    assert items == before
    assert state == {"items": {"10-a.py": {"part_bytes": 5 * MiB}}}
    assert not (dlq.root / "state.json").exists()


def test_progress_comes_from_the_item_or_from_the_state(dlq):
    """Either half of what ``snapshot`` hands out is enough to plan from.

    A snapshot's items carry their own ``part_bytes``; ``state.json`` holds the
    same figure keyed by name. Both have to answer, because the screen passes
    the first and a firing has the second.
    """
    cap = 200 * MiB
    carried = dlq.runner.plan(
        [item("10-a.py", cap, part=cap - MiB)], {}, GiB, FAST, 3600, False
    )
    stored = dlq.runner.plan(
        [item("10-a.py", cap)],
        {"items": {"10-a.py": {"part_bytes": cap - MiB}}},
        GiB,
        FAST,
        3600,
        False,
    )
    assert total(carried) == total(stored) == MiB


def test_a_state_of_the_wrong_shape_does_not_stop_the_screen(dlq):
    """A snapshot's items are a list; ``state.json``'s are a mapping.

    They are easy to hand over the wrong way round, and a screen drawing a line
    must not traceback over it — the items carry their own progress anyway.
    """
    planned = dlq.runner.plan(
        [item("10-a.py", 50 * MiB)],
        {"items": [{"name": "10-a.py"}]},
        GiB,
        FAST,
        3600,
        False,
    )
    assert total(planned) == 50 * MiB


# --------------------------------------------------------------------------- #
# What a single item may take, and why not
# --------------------------------------------------------------------------- #


def admit(dlq, row, **changes):
    """:func:`expire_runner.admit` with everything else out of the way."""
    call = {
        "record": {"part_bytes": row.get("part_bytes", 0)},
        "budget": GiB,
        "rate": FAST,
        "remaining_time": 600.0,
        "flying": False,
        "free_disk": None,
    }
    call.update(changes)
    return dlq.runner.admit(
        row,
        call["record"],
        call["budget"],
        call["rate"],
        call["remaining_time"],
        call["flying"],
        call["free_disk"],
    )


def test_out_of_time_ends_the_firing_for_everything_behind_it(dlq):
    """The one refusal that is the firing's rather than the item's.

    With under three quarters of a minute left there is no time for anything
    behind it either, so :func:`plan` walks no further — and everything below
    still gets the reason said, because on the last firing of the night nothing
    else will ever say it.
    """
    got, why = admit(dlq, item("10-a.py", MiB), remaining_time=44)
    assert (got, why) == (0, dlq.runner.NO_TIME)
    # A second more and the firing is not over: whatever happens to this item
    # is about this item, and the queue behind it is still walked.
    assert admit(dlq, item("10-a.py", MiB), remaining_time=45)[1] != dlq.runner.NO_TIME
    assert admit(dlq, item("10-a.py", MiB, partial=False), remaining_time=45)[0] > 0

    planned = dlq.runner.plan(
        [item(f"{10 * n + 10:02d}-a{n}.py", MiB, partial=False) for n in range(3)],
        {},
        GiB,
        FAST,
        44,
        False,
    )
    assert total(planned) == 0
    assert {entry["reason"] for entry in planned} == {dlq.runner.NO_TIME}


def test_an_item_bigger_than_the_disk_is_refused(dlq):
    """And a projection that did not ask about the disk is not a refusal."""
    row = item("10-a.py", 100 * MiB, partial=False)
    tight = 100 * MiB + dlq.runner.DISK_SPARE - 1
    assert admit(dlq, row, free_disk=tight)[0] == 0
    assert admit(dlq, row, free_disk=tight + 1)[0] > 0
    assert admit(dlq, row, free_disk=None)[0] > 0


def test_a_whole_item_is_all_or_nothing_and_a_partial_one_is_sliced(dlq):
    """The difference the ``PARTIAL`` header makes, said as behaviour."""
    budget = 60 * MiB
    whole = admit(dlq, item("10-a.py", 100 * MiB, partial=False), budget=budget)
    assert whole == (0, whole[1]) and whole[1]
    sliced, why = admit(dlq, item("10-a.py", 100 * MiB), budget=budget)
    assert 0 < sliced <= budget and not why


def test_every_refusal_carries_a_sentence(dlq):
    """Nothing is ever turned down with an empty reason after the colon.

    ``fire()`` logs ``skip x: <reason>`` and the screen draws the same words on
    the cut line; a refusal with nothing on it is a night nobody can explain.
    """
    refusals = [
        admit(dlq, item("10-a.py", 0, partial=False)),
        admit(dlq, item("10-a.py", 10 * GiB, partial=False), budget=MiB),
        admit(dlq, item("10-a.py", 100 * MiB, part=100 * MiB)),
        admit(dlq, item("10-a.py", 100 * MiB), budget=MiB),
        admit(dlq, item("10-a.py", MiB), remaining_time=10),
        admit(dlq, item("10-a.py", 100 * MiB, partial=False), free_disk=0),
        admit(dlq, item("10-a.py", 10 * GiB, partial=False), rate=1000.0),
    ]
    for got, why in refusals:
        assert got == 0
        assert why.strip()


def test_a_whole_item_must_finish_with_time_still_to_go(dlq):
    """Not merely fit: finish, with half a minute of the firing left over.

    A whole item is all or nothing — there is no partial credit and nothing is
    struck off the queue — so one cut off at the stop time has spent every byte
    it moved for nothing, and will spend them again tomorrow. The margin is
    what keeps a projection that was right about the rate from being wrong
    about the outcome.
    """
    rate = float(MiB)
    remaining = 600.0
    fits = item("10-a.py", int(rate * (remaining - 30)), partial=False)
    over = item("10-a.py", int(rate * (remaining - 29)), partial=False)
    assert admit(dlq, fits, rate=rate, remaining_time=remaining)[0] == fits["cap"]
    got, why = admit(dlq, over, rate=rate, remaining_time=remaining)
    assert got == 0 and why


def test_a_nearly_finished_download_is_not_blocked_by_the_minimum(dlq):
    """The slice minimum stops nightly churn; it may not strand a last MiB."""
    cap = 100 * MiB
    row = item("10-a.py", cap, part=cap - 1024, slice_min=32 * MiB)
    got, why = admit(dlq, row, budget=GiB)
    assert got == 1024 and not why


def test_a_refusal_of_an_item_s_own_does_not_stop_the_queue_behind_it(dlq):
    """Only "out of time" is the firing's; every other reason is the item's.

    A download too big for what is left must not take the small one behind it
    down as well, or the queue would be only as long as its first unaffordable
    item — and the cut line, which is drawn from this, would fall in the wrong
    place and say so on the screen.
    """
    budget = 100 * MiB
    big = item("10-big.py", 500 * MiB, partial=False)
    small = item("20-small.py", 20 * MiB, partial=False)

    planned = dlq.runner.plan([big, small], {}, budget, FAST, 3600, False)
    got = {entry["name"]: entry["bytes"] for entry in planned}
    assert got == {"10-big.py": 0, "20-small.py": 20 * MiB}
    assert next(e for e in planned if e["name"] == "10-big.py")["reason"]


def test_a_whole_item_is_finished_once_and_never_offered_again(dlq):
    """A night is a row of firings, and one done in the first is not in the
    second.

    Offered again it would be projected to spend its cap once per firing, and
    the screen would draw a line for a night four times the size of the one
    the runner is going to work.
    """
    row = item("10-a.py", 50 * MiB, partial=False)
    night = dlq.runner.plan(
        [row], {}, GiB, FAST, 4 * dlq.runner.JOB_PERIOD, False
    )
    assert total(night) == 50 * MiB


def test_a_night_is_as_many_firings_as_it_has_periods(dlq):
    """One firing per :data:`expire_runner.JOB_PERIOD`, and no more.

    A projection that walked a firing more than the night has would promise a
    slice no job is ever going to run — and the item it promised it to sits
    above the cut line saying it downloads tonight.
    """
    rate = 10 * MiB
    period = dlq.runner.JOB_PERIOD
    nights = [
        total(
            dlq.runner.plan(
                [item("10-a.py", 100 * GiB)], {}, 100 * GiB, rate, n * period, False
            )
        )
        for n in (1, 2, 3)
    ]
    assert nights[0] > 0
    assert nights[1] == 2 * nights[0]
    assert nights[2] == 3 * nights[0]


def test_an_item_that_costs_exactly_what_is_left_still_fits(dlq):
    """The budget is what may be spent, not what may be approached."""
    budget = 100 * MiB
    got, why = admit(dlq, item("10-a.py", budget, partial=False), budget=budget)
    assert got == budget and not why
    over, said = admit(dlq, item("10-a.py", budget + 1, partial=False), budget=budget)
    assert over == 0 and said


def test_an_item_may_declare_a_slice_minimum_of_its_own(dlq):
    """The built-in minimum stops nightly churn on items that said nothing.

    An item that declared a smaller one has already answered that question for
    itself, and its figure is the one used — otherwise the header is a header
    nothing reads, and the item never gets the small slices it asked for.
    """
    small = 4 * MiB
    assert small < dlq.runner.SLICE_MIN_BYTES
    budget = 8 * MiB

    got, why = admit(dlq, item("10-a.py", GiB, slice_min=small), budget=budget)
    assert got and not why
    refused, said = admit(dlq, item("10-a.py", GiB), budget=budget)
    assert refused == 0 and said


def test_a_blind_budget_is_not_derated_twice(dlq):
    """The wire derate belongs to a budget measured on the wire.

    A blind budget is the items' own payload declarations added up, so charging
    the overhead against it would hand every partial item a slice short of the
    size it declared and send it back for a tail that was never missing.
    """
    budget = 100 * MiB
    row = item("10-a.py", 10 * GiB)
    counted = admit(dlq, row, budget=budget, flying=False)[0]
    flying = admit(dlq, row, budget=budget, flying=True)[0]
    assert flying == budget
    assert counted < flying


def test_a_blind_night_is_one_pass_with_no_clock(dlq):
    """``NO_DEADLINE`` seconds: the queue is worked until the queue is done."""
    items = [item(f"{10 + n:02d}-a{n}.py", 50 * MiB) for n in range(3)]
    planned = dlq.runner.plan(
        items, {}, GiB, FAST, dlq.runner.NO_DEADLINE, True
    )
    assert total(planned) == 150 * MiB
    # And nothing was refused for the clock.
    assert all(not entry["reason"] for entry in planned)


def test_the_night_is_a_row_of_slices_rather_than_one_long_run(dlq):
    """A firing gets nine minutes; a night is one of those per period.

    A partial item bigger than a firing therefore comes back for more, and the
    projection has to work the night the same way or it would promise a whole
    evening's throughput to a queue that only ever gets nine minutes at a time.
    """
    rate = 10 * MiB
    row = item("10-a.py", 10 * GiB)
    one = dlq.runner.plan([row], {}, 10 * GiB, rate, dlq.runner.FIRING_SECONDS, False)
    many = dlq.runner.plan([row], {}, 10 * GiB, rate, 4 * 3600, False)
    assert total(one) < total(many)
    # Working rate is half the measured one, and a firing stops 45s early.
    firing = dlq.runner.working_rate(rate) * (dlq.runner.FIRING_SECONDS - 45)
    assert total(one) <= firing


def test_the_rate_a_night_is_worked_at_is_half_what_was_measured(dlq):
    """One conversion, made in one place, and floored.

    Both ends hand over the figure they have — ``snapshot()["bps"]`` — so a
    screen that halved it once more, or not at all, would be drawing its line
    for a night at a different speed from the one the runner works.
    """
    assert dlq.runner.working_rate(10 * MiB) == 5 * MiB
    assert dlq.runner.working_rate(0) == dlq.runner.working_rate(1) == 100 * 1024
