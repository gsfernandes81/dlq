"""When the window opens, when everything must stop, and in whose zone.

The grant is wiped at 00:00 **UTC** and the vessel changes timezone. So the
clock this file keeps is one clock: the same instant has to produce the same
window whether the phone thinks it is in Auckland or in Honolulu, which is why
the sweep below crosses the date line in both directions rather than testing
one zone and trusting the rest.

The other half is ``--blind``. It touches the clock in exactly one place — the
window is open now and does not close — because a run with no portal reading is
not spending the allowance that expires; a download of it cut short at midnight
would only have to buy the same bytes again.
"""

from __future__ import annotations

import datetime as dt
import math
import time

import pytest

#: A moment inside the ordinary window: 23:20 UTC on a Tuesday.
PINNED = dt.datetime(2026, 9, 1, 23, 20, tzinfo=dt.UTC).timestamp()

#: Zones either side of the date line, and one that is not a whole hour off.
#: The failure worth catching is a local ``.replace(hour=0)`` somewhere, which
#: passes in UTC and is a day out in Auckland.
ZONES = ["UTC", "Pacific/Auckland", "Pacific/Honolulu", "Asia/Kolkata", "Europe/Lisbon"]


@pytest.fixture
def at(dlq, monkeypatch):
    """Pin the clock. Every time in the runner is UTC epoch seconds."""

    def pin(when: float = PINNED):
        monkeypatch.setattr(dlq.runner, "now", lambda: when)
        return when

    return pin


@pytest.fixture
def zone(monkeypatch):
    """Move the phone to another timezone, and put it back afterwards."""

    def move(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield move
    time.tzset()


@pytest.mark.parametrize("where", ZONES)
def test_the_window_is_the_same_instant_in_every_zone(dlq, at, zone, where):
    """The vessel changes zone; the deadline does not move with it."""
    current = at()
    zone("UTC")
    baseline = dlq.runner.deadlines(None)
    zone(where)
    assert dlq.runner.deadlines(None) == baseline

    deadline, opens, stop = baseline
    # Midnight UTC of the following day, forty minutes away.
    assert deadline == dt.datetime(2026, 9, 2, tzinfo=dt.UTC).timestamp()
    assert deadline - current == 40 * 60
    assert opens == deadline - dlq.runner.window_seconds()
    assert stop == deadline - dlq.runner.STOP_MARGIN


def test_the_window_is_as_long_as_the_setting_says(dlq, at):
    at()
    _, opens, _ = dlq.runner.deadlines(None)
    dlq.config({"window_minutes": 120})
    _, wider, _ = dlq.runner.deadlines(None)
    assert opens - wider == 60 * 60


def test_the_portal_wins_when_it_takes_the_data_away_sooner(dlq, at):
    """The device clock drifts and the vessel changes zone; the portal knows.

    The two disagreeing means trusting the one that expires the grant first.
    """
    current = at()
    device, _, _ = dlq.runner.deadlines(None)
    sooner = dlq.runner.deadlines(dlq.reading(until=600))[0]
    later = dlq.runner.deadlines(dlq.reading(until=10 * 3600))[0]
    assert sooner == current + 600
    assert later == device


def test_a_blind_run_opens_now_and_never_closes(dlq, at):
    """The one place ``blind`` touches the clock, and the only one.

    Nothing may cut a blind download short for the time: the window belongs to
    an allowance it is not spending, and a run stopped at midnight would only
    buy the same bytes again.
    """
    current = at()
    deadline, opens, stop = dlq.runner.deadlines(None, blind=True)
    assert opens == current
    assert stop == dlq.runner.NO_DEADLINE
    # The deadline itself is left alone; it is still when the grant resets.
    assert deadline == dlq.runner.deadlines(None)[0]


def test_early_in_the_day_the_window_has_not_opened_yet(dlq, at):
    """Noon: the same deadline, and hours to wait for it."""
    current = at(dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC).timestamp())
    _, opens, stop = dlq.runner.deadlines(None)
    assert current < opens < stop


def test_no_stop_time_is_spelled_one_way(dlq):
    """``NO_DEADLINE`` is the one spelling, and it compares like a time.

    What has to ask about it is the handful of places that do arithmetic on a
    stop time: the ``timeout`` wrapper that is not put on, the slice that is not
    sized against the clock, and the reaper.
    """
    assert math.isinf(dlq.runner.NO_DEADLINE)
    assert dlq.runner.now() < dlq.runner.NO_DEADLINE


def test_no_deadline_means_no_timeout_wrapper_at_all(dlq, at):
    """Said three times at every spawn, and decided in one place.

    A made-up large number instead would be a deadline again — one nobody wrote
    down, that nobody could see coming, landing in the middle of a download.
    """
    wrapper, epoch, said = dlq.runner.spawn_plan(dlq.runner.NO_DEADLINE)
    assert wrapper == []
    # ``0`` is the contract's own spelling of "no deadline", and it is what an
    # item reads as +inf.
    assert epoch == "0"
    assert said.strip()

    current = at()
    wrapper, epoch, said = dlq.runner.spawn_plan(current + 300)
    assert wrapper[0] == "timeout" and "300" in wrapper
    assert int(epoch) == int(current + 300)


def test_a_stop_time_already_past_still_leaves_room_to_stop(dlq, at):
    """A ``timeout 0`` would kill the item before it opened its file."""
    current = at()
    wrapper, _, _ = dlq.runner.spawn_plan(current - 100)
    assert int(wrapper[-1]) >= 30


def test_the_item_reads_zero_as_no_deadline(dlq):
    """The other end of the same contract, in ``expire_dl``."""
    env = dlq.dl.Env()
    env.stop_epoch = 0
    assert env.deadline() == float("inf")
    env.stop_epoch = 1000.0
    assert env.deadline() == 1000.0 - dlq.dl.QUIT_MARGIN


def test_a_time_is_shown_with_its_zone_and_no_date(dlq):
    """Everything this file decides is within a day, and the zone is the fact.

    A screen quietly showing ship's time would agree with neither the window,
    the grant nor a single line of the log.
    """
    said = dlq.runner.clock(PINNED)
    assert said == "23:20Z"
    assert dlq.runner.stamp(PINNED).startswith("2026-09-01 23:20")
