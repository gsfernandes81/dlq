"""A firing: what it admits, what it spawns, and how it is stopped.

The one rule this file exists for is that **the screen's line and the night's
spending are the same decision**. ``fire()`` calls
:func:`expire_runner.admit` rather than carrying its own copy of the
arithmetic, so a cut line that promises bytes cannot be followed by a runner
that refuses them. The way to check that from the outside is to make ``admit``
answer differently and watch the firing obey — which is what most of this file
does.

The other half is stopping one. A download runs in a session of its own so the
runner can kill its tree without killing itself, which means a signal to the
runner is *not* a signal to the download: what stops the download is the runner
unwinding past it. Only an interrupt does that, and that is checked here by
sending one to a real child and asking it which signal it got.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

import pytest

MiB = 1024**2


@pytest.fixture
def firing(dlq, monkeypatch):
    """A night that would download: a live reading, and nothing notified."""
    monkeypatch.setattr(dlq.runner, "read_portal", lambda: dlq.reading(free=600 * MiB))
    monkeypatch.setattr(dlq.runner, "notify", lambda *args: None)
    monkeypatch.setattr(dlq.runner, "portal_now", lambda: (dlq.reading(), ""))
    return dlq


@pytest.fixture
def spawned(dlq, monkeypatch):
    """Record every item ``fire()`` would actually run, and run none of them."""
    calls = []

    def run_item(item, cap, stop_by, doc, state):
        calls.append({"name": item["name"], "cap": cap, "stop_by": stop_by})
        return 0, cap, None

    monkeypatch.setattr(dlq.runner, "run_item", run_item)
    return calls


def three(dlq):
    for number, name in ((10, "one"), (20, "two"), (30, "three")):
        dlq.item(f"{number}-{name}.py", cap=50 * MiB)


# --------------------------------------------------------------------------- #
# fire() admits through admit()
# --------------------------------------------------------------------------- #


def test_a_firing_runs_nothing_admit_refuses(firing, spawned, monkeypatch):
    """The rule says no; the night spends nothing and the queue is untouched.

    Not one item, and not a byte: if ``fire()`` kept its own copy of the
    admission arithmetic this is the test it would fail, because the copy would
    go on admitting while the rule said not to.
    """
    three(firing)
    monkeypatch.setattr(firing.runner, "admit", lambda *args, **kw: (0, "not tonight"))

    assert firing.runner.fire(force=True) == 0
    assert spawned == []
    assert len(list((firing.root / "queue").glob("*.py"))) == 3
    assert firing.runner.load_state().get("items", {}) == {}
    # And the reason is in the log, against the item it belongs to.
    log = (firing.root / "logs" / "runner.log").read_text()
    assert "not tonight" in log
    assert "10-one.py" in log


def test_a_firing_spawns_exactly_what_admit_allows_with_the_cap_it_gave(
    firing, spawned, monkeypatch
):
    """One item admitted, one refused: the runner does as it is told, twice."""
    three(firing)
    allowed = 7 * MiB

    def admit(item, record, budget, rate, remaining, flying, free_disk=None):
        return (allowed, "") if item["name"] == "20-two.py" else (0, "not this one")

    monkeypatch.setattr(firing.runner, "admit", admit)
    assert firing.runner.fire(force=True) == 0
    assert [call["name"] for call in spawned] == ["20-two.py"]
    assert spawned[0]["cap"] == allowed


def test_out_of_time_stops_the_pass_rather_than_skipping_one_item(
    firing, spawned, monkeypatch
):
    """Everything behind it is out of time for the same reason.

    Recognised by the words :data:`expire_runner.NO_TIME` is spelled in, so
    ``fire()`` does not keep a second copy of the 45-second rule.
    """
    three(firing)

    def admit(item, record, budget, rate, remaining, flying, free_disk=None):
        if item["name"] == "10-one.py":
            return MiB, ""
        return 0, firing.runner.NO_TIME

    monkeypatch.setattr(firing.runner, "admit", admit)
    firing.runner.fire(force=True)
    assert [call["name"] for call in spawned] == ["10-one.py"]


def test_the_budget_a_firing_admits_against_is_the_one_the_gate_named(
    firing, spawned, monkeypatch
):
    """What ``snapshot`` shows and what ``fire`` spends are one figure."""
    three(firing)
    seen = []

    def admit(item, record, budget, rate, remaining, flying, free_disk=None):
        seen.append(budget)
        return 0, "no"

    monkeypatch.setattr(firing.runner, "admit", admit)
    firing.runner.fire(force=True)
    assert seen[0] == firing.runner.spendable_bytes(firing.reading(free=600 * MiB))


def test_a_blind_firing_admits_against_what_the_items_declared(
    dlq, spawned, monkeypatch
):
    """No portal, so no remainder, no floor and no expiring allowance."""
    three(dlq)
    monkeypatch.setattr(dlq.runner, "read_portal", lambda: None)
    monkeypatch.setattr(dlq.runner, "notify", lambda *args: None)
    seen = []

    def admit(item, record, budget, rate, remaining, flying, free_disk=None):
        seen.append((budget, flying, remaining))
        return 0, "no"

    monkeypatch.setattr(dlq.runner, "admit", admit)
    assert dlq.runner.fire(force=True, blind=True) == 0
    budget, flying, remaining = seen[0]
    assert budget == 150 * MiB
    assert flying is True
    # Nothing interrupts a blind download for the time.
    assert remaining == dlq.runner.NO_DEADLINE


def test_a_firing_the_gate_stops_writes_a_heartbeat_and_spends_nothing(
    firing, spawned
):
    """~96 firings a day are no-ops; the log is not where they belong."""
    three(firing)
    firing.config({"auto": False})
    assert firing.runner.fire() == 0
    assert spawned == []
    assert "automatic" in (firing.root / "heartbeat").read_text()


def test_a_fault_is_reported_rather_than_downloaded_through(dlq, spawned, monkeypatch):
    """No reading, so nothing starts — and the exit code says so."""
    dlq.item("10-one.py")
    monkeypatch.setattr(dlq.runner, "read_portal", lambda: None)
    monkeypatch.setattr(dlq.runner, "notify", lambda *args: None)
    assert dlq.runner.fire(force=True) == 1
    assert spawned == []


def test_a_malformed_item_is_skipped_and_the_rest_of_the_queue_runs(
    firing, spawned, monkeypatch
):
    """One bad file is one bad file, never the whole night."""
    three(firing)
    (firing.root / "queue" / "40-broken.py").write_text("nothing declared\n")
    (firing.root / "queue" / "50-binary.py").write_bytes(b"\x89PNG\r\n\x1a\n\xff")
    monkeypatch.setattr(firing.runner, "admit", lambda *a, **kw: (MiB, ""))
    firing.runner.fire(force=True)
    assert [call["name"] for call in spawned] == ["10-one.py", "20-two.py", "30-three.py"]
    log = (firing.root / "logs" / "runner.log").read_text()
    assert "40-broken.py" in log and "50-binary.py" in log


# --------------------------------------------------------------------------- #
# What happens to an item afterwards
# --------------------------------------------------------------------------- #


def test_a_finished_item_leaves_the_queue_by_rename(firing, monkeypatch):
    """So it cannot run twice, and where its file went is recorded.

    After delivery into a shared folder nothing can work out which file was
    ours by looking, so the record is the only answer ``dlq path`` has.
    """
    firing.item("10-one.py", cap=MiB, dest="file")
    landing = firing.root / "landing"
    landing.mkdir()
    firing.config({"file_dir": str(landing)})

    def run_item(item, cap, stop_by, doc, state):
        out = firing.root / "out" / item["name"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "thing.iso").write_bytes(b"x" * 1024)
        return 0, 1024, None

    monkeypatch.setattr(firing.runner, "run_item", run_item)
    monkeypatch.setattr(firing.runner, "admit", lambda *a, **kw: (MiB, ""))
    firing.runner.fire(force=True)

    assert not (firing.root / "queue" / "10-one.py").exists()
    assert list((firing.root / "done").rglob("10-one.py"))
    record = firing.runner.load_state()["items"]["10-one.py"]
    assert record["retired"] == "done"
    assert record["delivered"] == [str(landing / "thing.iso")]
    assert (landing / "thing.iso").is_file()


def test_a_delivery_that_cannot_land_leaves_the_file_where_it_was_paid_for(
    firing, monkeypatch
):
    """An unreachable destination must never cost the bytes."""
    firing.item("10-one.py", cap=MiB, dest=str(firing.root / "nope" / "deeper"))
    out = firing.root / "out" / "10-one.py"
    out.mkdir(parents=True)
    (out / "thing.iso").write_bytes(b"x" * 1024)
    firing.runner.archive({"path": firing.root / "queue" / "10-one.py",
                           "name": "10-one.py",
                           "dest": str(firing.root / "nope" / "deeper")}, {"items": {}})
    assert (out / "thing.iso").is_file()


def test_a_delivered_name_that_is_taken_is_never_overwritten(firing):
    """Downloads is full of other people's files, and two may share a name."""
    where = firing.root / "landing"
    where.mkdir()
    (where / "thing.iso").write_bytes(b"theirs")
    target = firing.runner.free_name(where, "thing.iso")
    assert target != where / "thing.iso"
    assert not target.exists()
    assert target.suffix == ".iso"


def test_three_failed_nights_set_an_item_aside(firing, monkeypatch):
    """And the attempt history is kept, because it is the evidence."""
    firing.item("10-one.py", cap=MiB)
    monkeypatch.setattr(firing.runner, "admit", lambda *a, **kw: (MiB, ""))
    monkeypatch.setattr(firing.runner, "run_item", lambda *a: (1, 0, None))

    for attempt in range(1, firing.runner.MAX_ATTEMPTS + 1):
        firing.runner.fire(force=True)
        record = firing.runner.load_state()["items"]["10-one.py"]
        assert record["attempts"] == attempt

    assert not (firing.root / "queue" / "10-one.py").exists()
    assert (firing.root / "failed" / "10-one.py").is_file()
    assert firing.runner.load_state()["items"]["10-one.py"]["retired"] == "failed"


def test_not_tonight_is_a_legitimate_answer_and_costs_no_attempt(firing, monkeypatch):
    """Exit 75 with progress: left queued, and its three nights intact."""
    firing.item("10-one.py", cap=100 * MiB)
    monkeypatch.setattr(firing.runner, "admit", lambda *a, **kw: (50 * MiB, ""))
    monkeypatch.setattr(
        firing.runner, "run_item", lambda *a: (firing.runner.EX_TEMPFAIL, 50 * MiB, None)
    )
    firing.runner.fire(force=True)
    assert (firing.root / "queue" / "10-one.py").is_file()
    assert firing.runner.load_state()["items"]["10-one.py"]["attempts"] == 0


def test_an_item_that_was_never_offered_a_byte_collects_no_record(
    firing, spawned, monkeypatch
):
    """A row in ``state.json`` for having been walked past is a row that lies."""
    three(firing)
    monkeypatch.setattr(firing.runner, "admit", lambda *a, **kw: (0, "no"))
    firing.runner.fire(force=True)
    assert firing.runner.load_state().get("items", {}) == {}


# --------------------------------------------------------------------------- #
# The contract with the item
# --------------------------------------------------------------------------- #


def test_an_item_is_told_what_it_may_spend_and_where_to_put_it(dlq, monkeypatch):
    """The five things ``expire_dl`` reads out of its environment, plus the two
    the older contract used.

    This is the whole interface between the runner and a download: a name
    changed on one side of it is an item that quietly spends its default —
    which is no slice at all, or no deadline at all — and nothing says so.
    """
    said = dlq.root / "environment.json"
    item = dlq.script(
        "10-thing.py",
        "import json, os\n"
        "told = dict(os.environ)\n"
        'told["cwd"] = os.getcwd()\n'
        f"json.dump(told, open({str(said)!r}, 'w'))\n",
        cap=200 * MiB,
    )
    monkeypatch.setattr(
        dlq.runner, "watch", lambda child, *rest: child.wait(timeout=10) or 0
    )
    stop = dlq.runner.now() + 300
    dlq.runner.run_item(dlq.runner.parse_item(item), 50 * MiB, stop, None, {})

    told = json.loads(said.read_text())
    assert told["EXPIRE_SLICE_BYTES"] == str(50 * MiB)
    # Kept equal to the slice for items written against the older contract: a
    # slice is never larger than the cap they expect there.
    assert told["EXPIRE_BUDGET_BYTES"] == told["EXPIRE_SLICE_BYTES"]
    assert told["EXPIRE_TOTAL_BYTES"] == str(200 * MiB)
    assert int(told["EXPIRE_STOP_EPOCH"]) == int(stop)
    assert told["EXPIRE_WORK"] == str(dlq.root / "work" / "10-thing.py")
    assert told["EXPIRE_OUT"] == str(dlq.root / "out" / "10-thing.py")
    assert told["EXPIRE_RUN_ID"]
    # And it is run *in* its work directory, so a relative path it writes lands
    # where the runner will look for it.
    assert told["cwd"] == told["EXPIRE_WORK"]


def test_the_rate_is_learned_only_from_transfers_big_enough_to_mean_something(
    firing, monkeypatch
):
    """A 200 kB item that spent most of its life in DNS would poison it."""
    firing.item("10-thing.py", cap=500 * MiB)
    monkeypatch.setattr(firing.runner, "admit", lambda *a, **kw: (100 * MiB, ""))

    monkeypatch.setattr(
        firing.runner, "run_item", lambda *a: (firing.runner.EX_TEMPFAIL, 1024, None)
    )
    firing.runner.fire(force=True)
    assert "ewma_bps" not in firing.runner.load_state()

    monkeypatch.setattr(
        firing.runner, "run_item", lambda *a: (firing.runner.EX_TEMPFAIL, 100 * MiB, None)
    )
    firing.runner.fire(force=True)
    learned = firing.runner.load_state()["ewma_bps"]
    assert learned > 0
    # It is an average, so one slow night does not throw the whole figure away.
    slower = learned
    monkeypatch.setattr(
        firing.runner, "run_item", lambda *a: (firing.runner.EX_TEMPFAIL, 5 * MiB, None)
    )
    firing.runner.fire(force=True)
    assert 0 < firing.runner.load_state()["ewma_bps"] < slower


def test_an_item_that_says_not_tonight_while_moving_nothing_runs_out_of_nights(
    firing, monkeypatch
):
    """"Not tonight" is a legitimate answer, but an item that says it every
    night while moving nothing would never retire.

    Only counted when it was actually given room and time to make progress,
    which is why the clock is wound on here rather than the slice shrunk.
    """
    firing.item("10-thing.py", cap=500 * MiB)
    monkeypatch.setattr(firing.runner, "admit", lambda *a, **kw: (100 * MiB, ""))
    monkeypatch.setattr(
        firing.runner, "run_item", lambda *a: (firing.runner.EX_TEMPFAIL, 0, None)
    )

    # Every reading of the clock is two hundred seconds later than the last, so
    # the item is credited with having had time rather than being cut off.
    clock = [firing.runner.now()]

    def creeping():
        clock[0] += 200
        return clock[0]

    monkeypatch.setattr(firing.runner, "now", creeping)

    for stall in range(1, firing.runner.MAX_STALLS):
        firing.runner.fire(force=True)
        record = firing.runner.load_state()["items"]["10-thing.py"]
        assert record["stalls"] == stall
        assert record["attempts"] == 0

    firing.runner.fire(force=True)
    record = firing.runner.load_state()["items"]["10-thing.py"]
    assert record["stalls"] == 0
    assert record["attempts"] == 1


def test_the_status_screen_draws_from_the_runners_own_facts(dlq, monkeypatch, capsys):
    """``expire_runner --status`` draws through ``expire_sched``, so the screen
    exists once however it is reached."""
    dlq.item("10-thing.py")
    monkeypatch.setattr(dlq.runner, "portal_now", lambda: (dlq.reading(), ""))
    assert dlq.runner.report() == 0
    said = capsys.readouterr().out
    assert "DOWNLOAD QUEUE" in said
    assert "10-thing" in said


# --------------------------------------------------------------------------- #
# Stopping one
# --------------------------------------------------------------------------- #


#: A child that says which signal reached it and then goes quietly. Both are
#: trapped, so the file it leaves behind is the evidence of which one was sent.
TRAP = """\
import signal, sys, time
from pathlib import Path

where = Path(sys.argv[1])


def caught(number, frame):
    where.write_text(signal.Signals(number).name)
    raise SystemExit(0)


signal.signal(signal.SIGINT, caught)
signal.signal(signal.SIGTERM, caught)
Path(sys.argv[2]).write_text("up")
while True:
    time.sleep(0.05)
"""


def _wait_for(path, limit=8.0):
    end = time.time() + limit
    while time.time() < end:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.02)
    return ""


def test_stopping_a_run_from_the_screen_is_an_interrupt(dlq, tmp_path, monkeypatch):
    """SIGINT, never SIGTERM — and it is checked by asking the child.

    The download is not in the runner's process group, so a signal to the
    runner is not a signal to the download: what stops it is the runner
    *unwinding*, and only an interrupt does that. SIGTERM would kill the runner
    where it stands and leave yt-dlp spending data with nothing watching it.
    """
    script = tmp_path / "trap.py"
    script.write_text(TRAP)
    got, up = tmp_path / "signal.txt", tmp_path / "up.txt"
    monkeypatch.setattr(
        dlq.sched,
        "queue_run_argv",
        lambda blind: [sys.executable, str(script), str(got), str(up)],
    )

    running = dlq.ui.Firing()
    running.start(blind=False)
    assert _wait_for(up) == "up"
    assert running.alive
    running.stop()
    assert _wait_for(got) == "SIGINT"
    running.child.wait(timeout=5)


def test_the_runner_kills_the_item_tree_on_its_way_past(dlq, monkeypatch):
    """A supervisor unwinding takes the download with it.

    Without this, stopping a run with no deadline would leave a download
    nothing is watching and nothing will stop, spending mobile data until the
    phone is rebooted.
    """
    marker = dlq.root / "up.txt"
    item = dlq.script(
        "10-one.py",
        "import time\n"
        f"open({str(marker)!r}, 'w').write('up')\n"
        "while True: time.sleep(0.05)\n",
    )
    parsed = dlq.runner.parse_item(item)
    state = {}

    def watch(child, pgid, cap, stop_by, doc):
        assert _wait_for(marker) == "up"
        raise KeyboardInterrupt

    monkeypatch.setattr(dlq.runner, "watch", watch)
    monkeypatch.setattr(dlq.runner, "kill_tree", _record_kill(dlq))

    with pytest.raises(KeyboardInterrupt):
        dlq.runner.run_item(parsed, MiB, dlq.runner.NO_DEADLINE, None, state)

    killed = dlq.runner.kill_tree.calls
    assert killed, "the item's process group was left running"
    # The group it killed is the item's own session, not the runner's.
    assert killed[0] != os.getpgrp()
    # And nothing is left in state.json claiming a run is in progress.
    assert "active_pgid" not in state and "active_stop_by" not in state
    os.killpg(killed[0], signal.SIGKILL)


def _record_kill(dlq):
    calls = []

    def kill_tree(pgid, why):
        calls.append(pgid)

    kill_tree.calls = calls
    return kill_tree


def test_what_is_recorded_while_an_item_runs_is_whether_it_can_be_reaped(
    dlq, monkeypatch
):
    """A blind run has no stop time, and says so rather than inventing one.

    "Past its stop time" is the reaper's whole test, so a very large number
    here would leave it quietly waiting for a clock that never comes round. The
    lock is what says the run is an orphan instead.
    """
    seen = []

    def watch(child, pgid, cap, stop_by, doc):
        child.wait(timeout=10)
        seen.append(json.loads((dlq.root / "state.json").read_text()))
        return 0

    monkeypatch.setattr(dlq.runner, "watch", watch)
    item = dlq.script("10-one.py", "pass\n")
    parsed = dlq.runner.parse_item(item)

    dlq.runner.run_item(parsed, MiB, dlq.runner.NO_DEADLINE, None, {})
    assert seen[-1]["active_pgid"]
    assert seen[-1]["active_stop_by"] is None

    stop = dlq.runner.now() + 300
    dlq.runner.run_item(parsed, MiB, stop, None, {})
    assert seen[-1]["active_stop_by"] == stop

    # And nothing is left claiming a run is in progress once one is not.
    after = json.loads((dlq.root / "state.json").read_text())
    assert "active_pgid" not in after and "active_stop_by" not in after


def test_the_reaper_leaves_a_running_firing_alone(dlq, monkeypatch):
    """What is recorded belongs to the runner holding the lock, which is alive.

    Only the caller that *holds* the lock may treat a recorded group as an
    orphan; the one that failed to take it must not signal a live download.
    """
    killed = []
    monkeypatch.setattr(dlq.runner.os, "killpg", lambda pgid, sig: killed.append(sig))
    # The escalation waits two seconds between signals for a process that is
    # really there; here nothing is, and the wait is the test's own wall clock.
    monkeypatch.setattr(dlq.runner.time, "sleep", lambda seconds: None)
    state = {"active_pgid": 424242, "active_stop_by": None}
    dlq.runner.reap(dict(state), orphaned=False)
    assert killed == []
    dlq.runner.reap(dict(state), orphaned=True)
    assert killed[:1] == [signal.SIGTERM]


def test_the_reaper_waits_for_a_stop_time_that_has_not_come(dlq, monkeypatch):
    killed = []
    monkeypatch.setattr(dlq.runner.os, "killpg", lambda pgid, sig: killed.append(sig))
    monkeypatch.setattr(dlq.runner.time, "sleep", lambda seconds: None)
    later = dlq.runner.now() + 3600
    dlq.runner.reap({"active_pgid": 424242, "active_stop_by": later})
    assert killed == []
    dlq.runner.reap({"active_pgid": 424242, "active_stop_by": dlq.runner.now() - 1})
    assert killed


def test_only_one_firing_at_a_time(dlq, monkeypatch):
    """The lock is what stops two runners racing into the same .part file."""
    monkeypatch.setattr(dlq.runner, "fire", lambda **kw: 0)
    handle = (dlq.root / "runner.lock").open("w")
    import fcntl

    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert dlq.runner.main([]) == 0
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def test_an_item_leads_its_own_session_and_that_is_what_is_recorded(
    dlq, monkeypatch
):
    """The recorded group is the item's, never the runner's own.

    Spawning ``setsid`` as a program instead of asking Python for a new session
    leaves a window in which the child's group is still the runner's — and
    recording that means every later kill signals the runner and leaves the
    download running.
    """
    said = dlq.root / "pgid.txt"
    item = dlq.script(
        "10-one.py", f"import os\nopen({str(said)!r}, 'w').write(str(os.getpgrp()))\n"
    )
    seen = {}

    def watch(child, pgid, cap, stop_by, doc):
        child.wait(timeout=10)
        seen["pgid"] = pgid
        return 0

    monkeypatch.setattr(dlq.runner, "watch", watch)
    dlq.runner.run_item(
        dlq.runner.parse_item(item), MiB, dlq.runner.NO_DEADLINE, None, {}
    )
    assert int(said.read_text()) == seen["pgid"]
    assert seen["pgid"] != os.getpgrp()
