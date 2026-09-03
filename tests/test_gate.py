"""Whether a firing downloads, and the order it decides that in.

:func:`expire_runner.gate` is one decision with two readers — the firing, which
acts on it, and the status screen, which reports it — and **the order is part
of the answer**. An empty queue is reported as an empty queue even when the
portal is also down, because that is the one that would have to be fixed first;
the switch is asked ahead of all of them, because it is the answer to "why did
nothing happen tonight" on every night it is off.

So each test here puts *two* things wrong at once and pins which of them is
reported. A gate that answered truthfully but in the wrong order would pass a
test that only ever broke one thing.
"""

from __future__ import annotations

import pytest

MiB = 1024**2

#: A queue with something in it. Only its length is read by the gate.
QUEUED = [{"name": "10-a.py"}]


def gate(dlq, **changes):
    """:func:`expire_runner.gate` with a night that would otherwise download."""
    call = {
        "items": QUEUED,
        "doc": None,
        "window_open": 1_000.0,
        "stop_by": 9_000.0,
        "current": 5_000.0,
        "force": False,
        "blind": False,
        "auto": True,
    }
    call.update(changes)
    if call["doc"] == "live":
        call["doc"] = dlq.reading()
    return dlq.runner.gate(
        call["items"],
        call["doc"],
        call["window_open"],
        call["stop_by"],
        call["current"],
        call["force"],
        call["blind"],
        call["auto"],
    )


def verdict(dlq, **changes):
    return gate(dlq, **changes)[0]


def test_every_verdict_says_something_and_is_one_of_the_named_ones(dlq):
    """The screen keys its words off :data:`GATE_STATES`; nothing may fall out."""
    seen = set()
    for auto in (True, False):
        for force in (True, False):
            for blind in (True, False):
                for items in ([], QUEUED):
                    docs = (
                        None,
                        "live",
                        dlq.reading(age=600, live=False),
                        dlq.reading(free=0, paid=0),
                    )
                    for doc in docs:
                        for current in (0.0, 5_000.0, 9_500.0):
                            answer, detail = gate(
                                dlq,
                                auto=auto,
                                force=force,
                                blind=blind,
                                items=items,
                                doc=doc,
                                current=current,
                            )
                            assert answer in dlq.runner.GATE_STATES
                            assert detail.strip()
                            seen.add(answer)
    # Every one of them is reachable from the gate's own arguments — a state
    # nothing can produce is a screen with words for a night that never comes.
    assert seen == set(dlq.runner.GATE_STATES)


def test_the_switch_is_asked_before_anything_else(dlq):
    """Off outranks an empty queue, a closed window and a missing portal.

    Whichever of those is also true, the answer is the switch: someone who
    turned downloading off and forgot is told the truth about the wrong thing
    by any of the others.
    """
    assert verdict(dlq, auto=False) == "off"
    assert verdict(dlq, auto=False, items=[]) == "off"
    assert verdict(dlq, auto=False, current=0.0) == "off"
    assert verdict(dlq, auto=False, doc="live") == "off"
    # And with the switch on, each of those is the answer again.
    assert verdict(dlq, items=[]) == "empty"
    assert verdict(dlq, current=0.0) == "early"
    assert verdict(dlq) == "no-portal"


def test_an_empty_queue_outranks_the_clock_and_the_portal(dlq):
    assert verdict(dlq, items=[], current=0.0) == "empty"
    assert verdict(dlq, items=[], doc=None) == "empty"
    assert verdict(dlq, items=[], current=9_500.0) == "empty"


def test_force_steps_over_the_two_gates_about_when_and_no_others(dlq):
    """``run-now --force``: the clock and the switch, and nothing about money.

    The reserve, the per-item caps and the portal reading answer to nobody's
    ``--force``, and neither does an empty queue.
    """
    assert verdict(dlq, current=0.0) == "early"
    assert verdict(dlq, current=0.0, force=True) == "no-portal"
    assert verdict(dlq, auto=False, force=True) == "no-portal"
    # Past the stop time is not a schedule it may step over: the grant is gone.
    assert verdict(dlq, current=9_500.0, force=True) == "late"
    assert verdict(dlq, items=[], force=True) == "empty"
    assert verdict(dlq, doc="live", force=True) == "go"


def test_blind_steps_over_the_portal_and_says_nothing_about_the_switch(dlq):
    """It turns "no reading, so nothing starts" into "this is mobile data".

    A missing portal is not a change of mind about the schedule, so ``--blind``
    reaches neither the switch nor the clock nor the empty queue.
    """
    assert verdict(dlq, blind=True) == "blind"
    assert verdict(dlq, blind=True, doc=dlq.reading(age=600, live=False)) == "blind"
    assert verdict(dlq, blind=True, auto=False) == "off"
    assert verdict(dlq, blind=True, current=0.0) == "early"
    assert verdict(dlq, blind=True, items=[]) == "empty"


def test_a_portal_that_answers_beats_a_guess(dlq):
    """A blind run that finds the portal up is an ordinary run.

    Which is the whole reason ``blind`` never appears past the reading: the
    floor is intact, and the screen must not claim otherwise.
    """
    assert verdict(dlq, blind=True, doc="live") == "go"


def test_a_reading_too_old_to_gate_on_is_stale_rather_than_missing(dlq):
    """Two different faults with two different things to do about them."""
    assert verdict(dlq, doc=None) == "no-portal"
    assert verdict(dlq, doc=dlq.reading(age=600, live=False)) == "stale"
    assert verdict(dlq, doc=dlq.reading(age=121)) == "stale"
    assert verdict(dlq, doc=dlq.reading(age=119)) == "go"


def test_a_reading_with_nothing_left_in_it_is_spent(dlq):
    """The window opened and there was nothing to spend — a different night."""
    empty = dlq.reading(free=0, paid=0)
    assert verdict(dlq, doc=empty) == "spent"
    assert dlq.runner.spendable_bytes(empty) == 0


def test_the_two_faults_are_the_ones_a_person_could_fix(dlq):
    """Nothing is wrong with a queue that is merely waiting.

    ``no-portal`` and ``stale`` are a runner stopped by something; ``off`` is a
    runner doing as it was told, and ``blind`` is the same missing portal
    already answered for by a human.
    """
    assert set(dlq.runner.GATE_FAULTS) == {"no-portal", "stale"}
    assert set(dlq.runner.GATE_GO) == {"go", "blind"}
    assert not set(dlq.runner.GATE_FAULTS) & set(dlq.runner.GATE_GO)
    assert "off" not in dlq.runner.GATE_FAULTS


@pytest.mark.parametrize(
    ("age", "live", "usable"),
    [(0.0, True, True), (119.0, True, True), (121.0, True, False), (0.0, False, False)],
)
def test_usable_is_the_one_question_both_portal_gates_ask(dlq, age, live, usable):
    """Live, and recent enough to spend against. ``None`` is neither."""
    assert dlq.runner.usable(dlq.reading(age=age, live=live)) is usable
    assert dlq.runner.usable(None) is False


def test_the_screen_and_the_firing_ask_the_same_gate(dlq):
    """``snapshot``'s verdict is ``gate``'s, on the same figures.

    A screen saying "waiting for the window" on a night the runner refused for
    some other reason is a wrong answer that looks exactly like the right one.
    """
    dlq.item("10-a.py")
    facts = dlq.facts(portal=dlq.reading())
    again = dlq.runner.gate(
        [{"name": "10-a.py"}],
        facts["portal"],
        facts["window_open"],
        facts["stop_by"],
        facts["now"],
        False,
        False,
        dlq.runner.auto_enabled(),
    )
    assert (facts["verdict"], facts["detail"]) == again


def test_a_night_that_downloads_nothing_has_no_working_time(dlq):
    """How a projection says "no time" without knowing what a verdict means."""
    dlq.item("10-a.py")
    dlq.config({"auto": False})
    assert dlq.facts(portal=dlq.reading())["night_seconds"] == 0.0
    # A blind night has no stop time at all — nothing may cut it short.
    blind = dlq.facts(blind=True, force=True)
    assert blind["verdict"] == "blind"
    assert blind["night_seconds"] == dlq.runner.NO_DEADLINE


def test_asking_to_fly_blind_and_flying_blind_are_two_different_things(dlq):
    """A portal that answers is always preferred to a guess.

    So ``--blind`` on a night the portal is up changes nothing, and the screen
    must not say it did — the floor is intact and the run is spending the
    expiring grant, which is the opposite of what a blind run means. The other
    way round is just as wrong: a night with no reading that nobody asked
    about is a fault that stops, not a blind run that goes ahead on the SIM.
    """
    dlq.item("10-a.py")
    asked = dlq.facts(portal=dlq.reading(), blind=True, force=True)
    unasked = dlq.facts(portal=None, blind=False, force=True)
    flying = dlq.facts(portal=None, blind=True, force=True)

    assert asked["blind"] is False and asked["verdict"] == "go"
    assert unasked["blind"] is False and unasked["verdict"] not in dlq.runner.GATE_GO
    assert flying["blind"] is True and flying["verdict"] == "blind"


def test_a_blind_snapshot_spends_what_the_items_declared(dlq):
    """The figure ``run-now --blind`` says out loud is the runner's own.

    Called rather than re-derived by the front end, so the number agreed to and
    the number spent cannot drift apart.
    """
    dlq.item("10-a.py", cap=200 * MiB)
    dlq.item("20-b.py", cap=50 * MiB)
    dlq.state({"10-a.py": {"part_bytes": 50 * MiB}})
    facts = dlq.facts(blind=True, force=True)
    assert facts["spendable"] == 200 * MiB
    assert facts["spendable"] == dlq.runner.blind_budget(
        dlq.runner.queued_items()[0], dlq.runner.load_state()
    )
