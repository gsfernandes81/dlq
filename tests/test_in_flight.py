"""What stops a download that is already running, and what it may not spend.

:func:`expire_runner.watch` is the only guard that acts while bytes are
actually crossing the radio. Everything else in this repo decides *before* a
download starts — the gate, the budget, ``admit`` — and can be re-decided next
firing; this one is the last thing between a runaway ``yt-dlp`` and the
reserve, and there is no second chance at it. It carries two watchdogs on
purpose, because either alone has a hole: the interface counters see every byte
immediately but cannot say whose they are, and the portal knows the
authoritative remainder but only says so every so often.

So each test here puts a download on a treadmill and asks whether it was
stopped: the clock, the interface counter and the portal are all handed in, and
what is asserted is whether ``kill_tree`` was reached — never the words it was
reached with. The pairs matter as much as the kills. A guard that stops
everything is not a guard, it is an outage, and a download killed for a floor
it never approached buys the same bytes again tomorrow night.
"""

from __future__ import annotations

import pytest

MiB = 1024**2
GiB = 1024**3
MB = 1_000_000

#: A slice far larger than any figure the floor tests move, so that the only
#: guard that can stop those runs is the one they are about.
ROOMY = 4 * GiB


def _slow(dlq) -> float:
    """A supervision tick long enough to be about the portal.

    The portal is asked on every one of them, and five minutes passes between
    two — which is what the dark timer is measured in. Spelled from the
    runner's own periods rather than as a number, so the tests that use it go
    on being about the timer if either period is ever changed.
    """
    return dlq.runner.PORTAL_POLL + 300


class Supervised:
    """A download under :func:`expire_runner.watch`, on a treadmill.

    Stands in for the child process *and* for everything ``watch`` reads about
    the world, because they have to move together: the interface counter
    advances exactly once per supervision tick, which is what makes "after
    three ticks it had moved this much" a thing a test can say.

    The clock advances by *step* per reading — one
    :data:`expire_runner.IFACE_POLL` unless a test wants coarser time — which
    is what keeps the supervisor's inner wait from spinning: one tick of the
    loop is one look at the counter, and no test here ever sleeps.
    """

    #: Somewhere far from any real epoch, so a stop time is obviously relative.
    START = 1_000_000.0

    #: What the interface counter already stood at when this download began.
    #: Not nought, because it never is — ``/proc`` counts since the phone
    #: booted, and a supervisor that forgot to subtract where it came in would
    #: charge tonight's item for the whole month.
    BASE = 7 * GiB

    def __init__(self, dlq, monkeypatch, moved, portal=None, step=None):
        self.dlq = dlq
        #: What this download has moved after each tick, one figure per tick.
        #: When they run out it has finished of its own accord.
        self.moved = list(moved)
        self.counted = 0
        self.clock = self.START
        self.step = dlq.runner.IFACE_POLL if step is None else step
        self.killed: list[str] = []
        self.polled = 0
        #: Supervision ticks the download actually lived through.
        self.ticks = 0
        self.returncode = None
        self._portal = portal
        monkeypatch.setattr(dlq.runner, "now", self._now)
        monkeypatch.setattr(dlq.runner, "iface_bytes", lambda: self.BASE + self.counted)
        monkeypatch.setattr(dlq.runner, "kill_tree", self._kill)
        monkeypatch.setattr(dlq.runner, "read_portal", self._read_portal)

    def _now(self) -> float:
        self.clock += self.step
        return self.clock

    def _kill(self, pgid, why):
        self.killed.append(why)

    def _read_portal(self):
        self.polled += 1
        return self._portal() if callable(self._portal) else self._portal

    # -- the child ---------------------------------------------------------- #

    def poll(self):
        if self.killed or not self.moved:
            self.returncode = 0
            return 0
        self.ticks += 1
        self.counted = self.moved.pop(0)
        return None

    def wait(self, timeout=None):
        return 0

    # -- and the supervision it gets ---------------------------------------- #

    def run(self, cap=100 * MiB, stop_by=None, doc=None) -> int:
        if stop_by is None:
            stop_by = self.dlq.runner.NO_DEADLINE
        return self.dlq.runner.watch(self, 424242, cap, stop_by, doc)

    @property
    def stopped(self) -> bool:
        return bool(self.killed)


def supervised(dlq, monkeypatch, moved, portal=None, step=None) -> Supervised:
    return Supervised(dlq, monkeypatch, moved, portal, step)


# --------------------------------------------------------------------------- #
# The interface cap: an item that runs away from its slice
# --------------------------------------------------------------------------- #


def test_a_download_is_stopped_when_it_runs_away_from_its_slice(dlq, monkeypatch):
    """The item was told what it may spend; the counters say whether it did.

    This is the watchdog that does not need the portal, and so the only one a
    blind run has. What it guards against is an item that ignores its slice —
    a resumable download that restarts from zero, a redirect to something
    enormous — spending the SIM for as long as the firing lasts. Twice the
    slice is a runaway by any reading of it; the margin below is for the wire,
    and the wire does not cost as much as the payload.
    """
    cap = 100 * MiB
    run = supervised(dlq, monkeypatch, [2 * cap])
    run.run(cap=cap)
    assert run.stopped


def test_the_wire_costs_more_than_the_payload_and_that_is_not_a_runaway(
    dlq, monkeypatch
):
    """Headers, retries and TLS all cross the interface and none are payload.

    An item handed a 100 MiB slice and counted at 100 MiB on the wire has done
    exactly as it was told, and one counted a little over has too. Killing
    either would strand a nearly finished download for the overhead of
    fetching it — and it would then be fetched again tomorrow.
    """
    cap = 100 * MiB
    for counted in (cap, int(cap * 1.1)):
        run = supervised(dlq, monkeypatch, [counted])
        run.run(cap=cap)
        assert not run.stopped, counted


# --------------------------------------------------------------------------- #
# The floor, in flight
# --------------------------------------------------------------------------- #


def headroom(dlq, doc) -> int:
    """What the reading has above the floor this download may not cross."""
    return (
        doc["today"]["remainder_bytes"]
        - dlq.runner.floor_bytes(doc)
        - dlq.runner.FLOOR_MARGIN
    )


def test_a_download_is_stopped_before_the_floor_rather_than_after_it(
    dlq, monkeypatch
):
    """Between portal polls the remainder is projected down by the counters.

    The projection over-counts on purpose — the interface sees other apps'
    bytes too — so the floor is approached pessimistically. Crossing it stops
    the download *that tick*, without waiting for the portal to confirm what
    has already happened.
    """
    doc = dlq.reading(free=700 * MiB)
    room = headroom(dlq, doc)
    assert room > 2 * MiB

    # Exactly the headroom, because a floor is a floor and not a figure the
    # night may come to rest on: the reserve is what has to *survive*.
    for counted in (room, room + MiB):
        crossed = supervised(dlq, monkeypatch, [counted], portal=doc)
        crossed.run(cap=ROOMY, doc=doc)
        assert crossed.stopped, counted


def test_a_download_that_stays_above_the_floor_is_left_alone(dlq, monkeypatch):
    """The other half, and the one that costs money to get wrong.

    A download stopped for a floor it never reached is a download that has to
    buy the same bytes again tomorrow night, out of an allowance that will have
    expired.
    """
    doc = dlq.reading(free=700 * MiB)
    room = headroom(dlq, doc)
    run = supervised(dlq, monkeypatch, [room - 1], portal=doc)
    assert run.run(cap=ROOMY, doc=doc) == room - 1
    assert not run.stopped


def test_the_floor_is_re_asked_of_every_fresh_reading(dlq, monkeypatch):
    """``reserve-when-paid`` turns on a figure that moves while a download runs.

    Both runs below are handed the *same* remainder at the same poll — 120 MB,
    under the 100 MB reserve plus its margin — and differ only in what the
    reading says that 120 MB is made of. With the reserve in force the run
    stops; with paid data behind it and the reserve told to stand aside for
    paid data, the same figure is money the person has already bought and the
    download goes on.

    A floor worked out once before the first byte would enforce the answer to a
    question nobody asked tonight, in whichever direction it was wrong.
    """
    comfortable = dlq.reading(free=700 * MiB)
    ticks = [0] * 6

    kept = supervised(
        dlq, monkeypatch, ticks, portal=dlq.reading(free=120 * MB, paid=0)
    )
    kept.run(cap=ROOMY, doc=comfortable)
    assert kept.polled and kept.stopped

    dlq.config({"reserve_when_paid": False})
    waived = supervised(
        dlq, monkeypatch, ticks, portal=dlq.reading(free=0, paid=120 * MB)
    )
    waived.run(cap=ROOMY, doc=comfortable)
    assert waived.polled and not waived.stopped


def test_a_reading_that_lands_on_the_floor_stops_the_download(dlq, monkeypatch):
    """The reserve is what has to survive the night, not what it may end on.

    And it is stopped on the reading that says so rather than on the next look
    at the counters: the projection would catch it a tick later, and a tick is
    fifteen more seconds of spending against a figure already known to be out.
    """
    comfortable = dlq.reading(free=700 * MiB)
    on_it = dlq.runner.reserve_bytes() + dlq.runner.FLOOR_MARGIN
    run = supervised(
        dlq,
        monkeypatch,
        [0] * 6,
        portal=dlq.reading(free=on_it),
        step=_slow(dlq),
    )
    run.run(cap=ROOMY, doc=comfortable)
    assert run.polled and run.stopped
    assert run.ticks == 1


def test_a_blind_run_has_no_floor_to_enforce_and_never_asks_for_one(
    dlq, monkeypatch
):
    """With no reading there is no remainder, and nothing at the other end.

    Asking anyway would stall every gap on a connection timeout and log the
    same failure once a minute. What the blind run keeps is the byte cap, which
    is why the counter below stays inside the slice.
    """
    cap = 100 * MiB
    run = supervised(dlq, monkeypatch, [cap // 8 * n for n in range(1, 9)])
    run.run(cap=cap, doc=None)
    assert not run.stopped
    assert run.polled == 0


# --------------------------------------------------------------------------- #
# The clock
# --------------------------------------------------------------------------- #


def test_the_stop_time_stops_the_download(dlq, monkeypatch):
    """Past it the grant this run was spending has already reset.

    Whatever is still crossing the interface after that is being paid for out
    of the phone's own plan, which is the one thing the whole window exists to
    avoid.
    """
    run = supervised(dlq, monkeypatch, [0] * 6)
    run.run(stop_by=Supervised.START + 60)
    assert run.stopped


def test_nothing_interrupts_a_download_that_has_no_stop_time(dlq, monkeypatch):
    """A blind run works the queue until the queue is done.

    A run cut short for a midnight that means nothing to it would only buy the
    same bytes again — which is why :data:`expire_runner.NO_DEADLINE` reaches
    all the way down here rather than being turned into a large number
    somewhere above.
    """
    run = supervised(dlq, monkeypatch, [0] * 20)
    run.run(stop_by=dlq.runner.NO_DEADLINE)
    assert not run.stopped


# --------------------------------------------------------------------------- #
# The portal going quiet
# --------------------------------------------------------------------------- #


def test_a_portal_that_goes_dark_for_five_minutes_stops_the_download(
    dlq, monkeypatch
):
    """Half the guard is gone, and the run is spending against a figure nobody
    can confirm any more."""
    run = supervised(dlq, monkeypatch, [0] * 3, portal=None, step=_slow(dlq))
    run.run(cap=ROOMY, doc=dlq.reading(free=700 * MiB))
    assert run.polled and run.stopped


def test_a_portal_quiet_for_a_moment_is_not_a_portal_gone_dark(dlq, monkeypatch):
    """Below five minutes the run carries on against the reading it has.

    The remainder it was given is still the remainder — the portal lags live
    traffic anyway — and the interface watchdog and the projection are both
    still doing their work. Stopping here would cost the night for a link that
    dropped a request.
    """
    run = supervised(dlq, monkeypatch, [0] * 6, portal=None)
    run.run(cap=ROOMY, doc=dlq.reading(free=700 * MiB))
    assert run.polled
    assert not run.stopped


def test_a_portal_that_answers_again_is_not_a_dark_portal(dlq, monkeypatch):
    """One failed poll on a flaky link is not five minutes of silence.

    The timer is how long it has been dark *for*, so an answer clears it. A
    run stopped by a dropped request would be stopped most nights, and each of
    those is bytes bought twice — so the alternating link below, which never
    goes quiet for five minutes together, is left to finish.
    """
    good = dlq.reading(free=700 * MiB)
    answers = []

    def flaky():
        answers.append(None if len(answers) % 2 == 0 else good)
        return answers[-1]

    run = supervised(dlq, monkeypatch, [0] * 8, portal=flaky, step=_slow(dlq))
    run.run(cap=ROOMY, doc=good)
    assert answers.count(None) > 2
    assert not run.stopped


# --------------------------------------------------------------------------- #
# What it hands back
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("counted", [0, MiB, 40 * MiB])
def test_what_it_reports_is_what_crossed_the_interface(dlq, monkeypatch, counted):
    """Not what the item claims — that is a claim, and it is clamped to this.

    The ledger a night is reconstructed from, and the figure the remaining
    budget is reduced by, so an under-count here is a budget that outlives the
    data it was spending.
    """
    run = supervised(dlq, monkeypatch, [counted])
    assert run.run(cap=100 * MiB) == counted
