"""The downloader: one slice at a time, and always resumable afterwards.

``expire_dl`` is the only code here that touches a socket, so it is the only
thing checked against a server — one on the loopback interface, which is where
"offline" still means offline. What it is checked for is the three ideas the
module is built on: the *server* ends the transfer at the slice boundary, the
resume offset is whatever survived on the disk, and the item stops itself
before the runner would.

The first test is the one that matters most and reads like the least. **A file
smaller than one chunk must be fetched, not declined.** The runner hands a
partial item a slice of exactly what it still needs, so every such file asks
for less than a chunk on its first firing; a sub-chunk guard that refused it
would exit 75 with nothing moved, which the runner reads as "not tonight" — no
strike, no attempt spent — and the item would be offered the same too-small
slice again every firing, for ever, saying only that it was still queued.
"""

from __future__ import annotations

import hashlib
import json
import signal

import pytest

MiB = 1024**2


@pytest.fixture
def fetching(dlq, monkeypatch, tmp_path):
    """``EXPIRE_WORK`` and ``EXPIRE_OUT``, and a slice a test chooses."""
    work, out = tmp_path / "work", tmp_path / "out"
    work.mkdir()
    out.mkdir()
    monkeypatch.setenv("EXPIRE_WORK", str(work))
    monkeypatch.setenv("EXPIRE_OUT", str(out))
    monkeypatch.setenv("EXPIRE_RUN_ID", "run-1")
    monkeypatch.setenv("EXPIRE_STOP_EPOCH", "0")
    monkeypatch.setattr(dlq.dl, "_stop", False, raising=False)
    # ``fetch`` installs its own SIGINT and SIGTERM handlers, which is right
    # for an item and wrong for the process running the suite.
    kept = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}

    def slice_of(size: int) -> None:
        monkeypatch.setenv("EXPIRE_SLICE_BYTES", str(size))

    slice_of(64 * MiB)
    yield type(
        "Fetching",
        (),
        {"work": work, "out": out, "slice_of": staticmethod(slice_of)},
    )
    for number, handler in kept.items():
        signal.signal(number, handler)


def status(work) -> dict:
    return json.loads((work / ".status.json").read_text())


# --------------------------------------------------------------------------- #
# The one that reads like the least
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("size", [1, 1024, 15 * 1024])
def test_a_file_smaller_than_a_chunk_is_fetched_rather_than_declined(
    dlq, serving, fetching, size
):
    """A 15 KiB wheel is not a slow download; it is one that can never happen.

    The exemption is for the slice that is the whole of what is left, which is
    exactly what every sub-chunk file is handed on its first firing.
    """
    assert size < dlq.dl.CHUNK
    serving.payload = b"x" * size
    fetching.slice_of(size)
    assert dlq.dl.fetch(serving.url, "small.bin", total_hint=size) == dlq.dl.COMPLETE
    assert (fetching.out / "small.bin").read_bytes() == serving.payload
    assert serving.state_of_the_world["asked"], "it never even asked"


def test_a_small_slice_of_something_bigger_is_still_declined(
    dlq, serving, fetching
):
    """Below a chunk and not the end of the file: mostly connection setup.

    And it is declined *before* a request, which is the difference between the
    two cases — this one strikes nothing off the queue however often it is
    tried, and the one above finishes the item.
    """
    serving.payload = b"x" * (10 * MiB)
    fetching.slice_of(1024)
    answer = dlq.dl.fetch(serving.url, "big.bin", total_hint=10 * MiB)
    assert answer == dlq.dl.DECLINED
    assert serving.state_of_the_world.get("asked") is None
    assert status(fetching.work)["state"] == dlq.dl.DECLINED


# --------------------------------------------------------------------------- #
# Slices, and what survives between them
# --------------------------------------------------------------------------- #


def test_a_slice_takes_what_it_was_given_and_stops(dlq, serving, fetching):
    """The server ends the transfer at the boundary; nothing races an abort."""
    serving.payload = bytes(range(256)) * 2048  # 512 KiB
    fetching.slice_of(200 * 1024)
    answer = dlq.dl.fetch(serving.url, "part.bin", total_hint=len(serving.payload))
    assert answer == dlq.dl.PROGRESS
    part = fetching.work / "part.bin.part"
    assert part.stat().st_size == 200 * 1024
    assert part.read_bytes() == serving.payload[: 200 * 1024]
    assert serving.state_of_the_world["asked"][0] == "bytes=0-204799"


def test_the_resume_offset_is_whatever_is_on_the_disk(dlq, serving, fetching):
    """Never a counter in a metadata file: writes are append-only.

    So whatever survives is a valid prefix of the remote file however the
    process died, SIGKILL included — which is what a truncated part is made to
    prove here.
    """
    serving.payload = bytes(range(256)) * 2048
    part = fetching.work / "part.bin.part"
    part.write_bytes(serving.payload[: 100 * 1024])

    fetching.slice_of(100 * 1024)
    dlq.dl.fetch(serving.url, "part.bin", total_hint=len(serving.payload))
    assert serving.state_of_the_world["asked"][-1] == "bytes=102400-204799"
    assert part.read_bytes() == serving.payload[: 200 * 1024]

    # Half of it lost to a kill: the next slice starts from what is there.
    with part.open("r+b") as handle:
        handle.truncate(50 * 1024)
    dlq.dl.fetch(serving.url, "part.bin", total_hint=len(serving.payload))
    assert serving.state_of_the_world["asked"][-1].startswith("bytes=51200-")
    assert part.read_bytes() == serving.payload[: 150 * 1024]


def test_the_last_slice_delivers_the_file_and_takes_the_scratch_with_it(
    dlq, serving, fetching
):
    serving.payload = b"y" * (300 * 1024)
    fetching.slice_of(len(serving.payload))
    assert dlq.dl.fetch(serving.url, "done.bin", total_hint=0) == dlq.dl.COMPLETE
    assert (fetching.out / "done.bin").read_bytes() == serving.payload
    assert not (fetching.work / "done.bin.part").exists()
    assert not (fetching.work / "done.bin.part.meta.json").exists()
    report = status(fetching.work)
    assert report["state"] == dlq.dl.COMPLETE
    assert report["run_id"] == "run-1"
    assert report["part_bytes"] == len(serving.payload)


def test_the_size_a_server_states_beats_any_hint(dlq, serving, fetching):
    """``Content-Range`` carries the truth; the hint is only a starting guess."""
    serving.payload = b"z" * (300 * 1024)
    fetching.slice_of(100 * 1024)
    dlq.dl.fetch(serving.url, "sized.bin", total_hint=999)
    assert status(fetching.work)["total_bytes"] == len(serving.payload)


# --------------------------------------------------------------------------- #
# The ways it refuses
# --------------------------------------------------------------------------- #


def test_a_server_that_will_not_resume_is_refused_rather_than_looped_on(
    dlq, serving, fetching
):
    """Restart, restart, and then say so: the oscillation costs real data.

    Every attempt would download a slice, discover it cannot resume, throw it
    away and start over — spending money every other night and never
    converging.
    """
    serving.payload = b"w" * (400 * 1024)
    serving.honour_range = False
    part = fetching.work / "part.bin.part"
    part.write_bytes(b"w" * (10 * 1024))
    fetching.slice_of(100 * 1024)

    seen = [
        dlq.dl.fetch(serving.url, "part.bin", total_hint=len(serving.payload))
        for _ in range(3)
    ]
    # The first attempt throws the partial away and says nothing worse than
    # "not tonight"; once it is known that the server ignores Range and the
    # file is bigger than a slice, it is refused outright and stays refused.
    assert seen[0] == dlq.dl.DECLINED
    assert seen[1:] == [dlq.dl.FATAL, dlq.dl.FATAL]
    # A partial that could not be resumed is not left to be appended to.
    assert not part.exists()


def test_a_file_that_is_not_what_was_asked_for_is_never_delivered(
    dlq, serving, fetching
):
    """A sha256 mismatch is the runaway a human should see a strike for."""
    serving.payload = b"q" * (200 * 1024)
    fetching.slice_of(len(serving.payload))
    wrong = hashlib.sha256(b"something else").hexdigest()
    answer = dlq.dl.fetch(
        serving.url, "checked.bin", expect_sha256=wrong, total_hint=len(serving.payload)
    )
    assert answer == dlq.dl.FATAL
    assert not (fetching.out / "checked.bin").exists()
    assert not (fetching.work / "checked.bin.part").exists()


def test_the_right_file_passes_the_same_check(dlq, serving, fetching):
    serving.payload = b"q" * (200 * 1024)
    fetching.slice_of(len(serving.payload))
    right = hashlib.sha256(serving.payload).hexdigest()
    answer = dlq.dl.fetch(
        serving.url, "checked.bin", expect_sha256=right, total_hint=len(serving.payload)
    )
    assert answer == dlq.dl.COMPLETE
    assert (fetching.out / "checked.bin").read_bytes() == serving.payload


def test_a_server_that_cannot_be_reached_is_declined_and_says_so(dlq, fetching):
    """No traceback out of an item, and no strike either: it is not tonight."""
    answer = dlq.dl.fetch("http://127.0.0.1:1/gone.bin", "gone.bin", total_hint=MiB)
    assert answer == dlq.dl.DECLINED
    assert status(fetching.work)["state"] == dlq.dl.DECLINED


def test_an_item_never_tracebacks_out(dlq, monkeypatch):
    """Whatever happens, the runner gets an exit code it understands."""

    def boom(*args, **kw):
        raise RuntimeError("something nobody thought of")

    monkeypatch.setattr(dlq.dl, "fetch", boom)
    assert dlq.dl.run("http://127.0.0.1:1/x", "x.bin") == dlq.dl.EXIT[dlq.dl.FATAL]
    assert set(dlq.dl.EXIT) == {
        dlq.dl.COMPLETE,
        dlq.dl.PROGRESS,
        dlq.dl.DECLINED,
        dlq.dl.FATAL,
    }
    # "Not tonight" and "nothing to do" are the same answer to the runner; only
    # a fault is a strike.
    assert dlq.dl.EXIT[dlq.dl.COMPLETE] == 0
    assert dlq.dl.EXIT[dlq.dl.PROGRESS] == dlq.dl.EXIT[dlq.dl.DECLINED] == 75
    assert dlq.dl.EXIT[dlq.dl.FATAL] not in (0, 75)


def test_a_deadline_stops_a_slice_and_leaves_it_resumable(
    dlq, serving, fetching, monkeypatch
):
    """The item stops itself before the runner would, and keeps what it has."""
    serving.payload = b"r" * (2 * MiB)
    fetching.slice_of(2 * MiB)
    # A stop time that has already passed by the margin: the first check inside
    # the read loop ends the slice.
    monkeypatch.setenv("EXPIRE_STOP_EPOCH", str(int(dlq.runner.now()) - 60))
    answer = dlq.dl.fetch(serving.url, "part.bin", total_hint=len(serving.payload))
    assert answer == dlq.dl.DECLINED
    assert not (fetching.out / "part.bin").exists()
    # Whatever it did take is a valid prefix and can be resumed from.
    part = fetching.work / "part.bin.part"
    got = part.read_bytes() if part.exists() else b""
    assert serving.payload.startswith(got)


def test_a_finished_part_from_last_night_is_delivered_without_a_request(
    dlq, serving, fetching
):
    """Last night moved the final bytes but died before verifying."""
    serving.payload = b"s" * (100 * 1024)
    part = fetching.work / "late.bin.part"
    part.write_bytes(serving.payload)
    (fetching.work / "late.bin.part.meta.json").write_text(
        json.dumps({"total": len(serving.payload)})
    )
    fetching.slice_of(1024)
    assert dlq.dl.fetch(serving.url, "late.bin") == dlq.dl.COMPLETE
    assert serving.state_of_the_world.get("asked") is None
    assert (fetching.out / "late.bin").read_bytes() == serving.payload


def test_progress_is_reported_under_the_run_id_it_was_given(dlq, serving, fetching):
    """So the runner can never mistake last night's report for tonight's."""
    serving.payload = b"t" * (300 * 1024)
    fetching.slice_of(100 * 1024)
    dlq.dl.fetch(serving.url, "part.bin", total_hint=len(serving.payload))
    assert dlq.runner.read_status(fetching.work, "run-1", 10 * MiB)["spent"] == 100 * 1024
    assert dlq.runner.read_status(fetching.work, "another-run", 10 * MiB) is None


def test_a_claim_is_clamped_to_what_crossed_the_interface(dlq, tmp_path):
    """The count is the item's claim, and an under-report would flatter it."""
    work = tmp_path / "claiming"
    work.mkdir()
    (work / ".status.json").write_text(
        json.dumps({"run_id": "r", "payload_bytes_this_slice": 10 * MiB})
    )
    assert dlq.runner.read_status(work, "r", 4 * MiB)["spent"] == 4 * MiB
    assert dlq.runner.read_status(work, "r", 40 * MiB)["spent"] == 10 * MiB
    (work / ".status.json").write_text(
        json.dumps({"run_id": "r", "payload_bytes_this_slice": -5})
    )
    assert dlq.runner.read_status(work, "r", 40 * MiB)["spent"] == 0


def test_a_file_it_has_no_time_to_verify_keeps_until_the_next_firing(
    dlq, serving, fetching, monkeypatch
):
    """Hashing is not interruptible-and-resumable, so it refuses to start it
    without room to finish — and says "not tonight" rather than delivering an
    unverified file or throwing a verified one away."""
    serving.payload = b"v" * (300 * 1024)
    part = fetching.work / "checked.bin.part"
    part.write_bytes(serving.payload)
    (fetching.work / "checked.bin.part.meta.json").write_text(
        json.dumps({"total": len(serving.payload)})
    )
    # A stop time a second away: there is no room to hash anything.
    monkeypatch.setenv("EXPIRE_STOP_EPOCH", str(int(dlq.runner.now()) + 1))
    right = hashlib.sha256(serving.payload).hexdigest()
    answer = dlq.dl.fetch(serving.url, "checked.bin", expect_sha256=right)

    assert answer == dlq.dl.PROGRESS
    assert not (fetching.out / "checked.bin").exists()
    assert part.read_bytes() == serving.payload


def test_a_remote_file_that_changed_under_us_is_never_appended_to(
    dlq, serving, fetching
):
    """Appending would corrupt silently, and the corruption is only found — if
    ever — by a hash at the end of a file that has already been paid for."""
    serving.payload = b"a" * (300 * 1024)
    fetching.slice_of(100 * 1024)
    assert dlq.dl.fetch(serving.url, "part.bin") == dlq.dl.PROGRESS
    part = fetching.work / "part.bin.part"
    assert part.stat().st_size == 100 * 1024

    serving.payload = b"b" * (300 * 1024)
    serving.etag = '"second"'
    assert dlq.dl.fetch(serving.url, "part.bin") == dlq.dl.DECLINED
    assert not part.exists()
    # And the next firing starts from nothing rather than from half of each.
    assert dlq.dl.fetch(serving.url, "part.bin") == dlq.dl.PROGRESS
    assert part.read_bytes() == serving.payload[: 100 * 1024]


# --------------------------------------------------------------------------- #
# What the runner reads back, and what a manual run gets
# --------------------------------------------------------------------------- #


def test_progress_is_written_often_enough_and_never_torn(dlq, fetching):
    """Rewritten atomically every few seconds, so a killed item still leaves a
    nearly-current figure — and never a half-written one for the runner to
    read.

    The throttle is why it can be called on every chunk: a 64 KiB read is not
    worth a write, and a slice of many gigabytes is not worth a million of
    them.
    """
    env = dlq.dl.Env()
    status = dlq.dl.Status(env, total=1000, part_bytes=0)

    status.slice_bytes = 10
    status.write(force=True)
    first = json.loads(status.path.read_text())
    assert first["part_bytes"] == 0 and first["run_id"] == "run-1"

    # A moment later and a few bytes on: not worth a write.
    status.slice_bytes = 20
    status.write()
    assert json.loads(status.path.read_text())["payload_bytes_this_slice"] == 10

    # Enough bytes, and it is worth one whenever it happens.
    status.slice_bytes = 10 + dlq.dl.STATUS_EVERY_BYTES
    status.write()
    assert json.loads(status.path.read_text())["payload_bytes_this_slice"] > 10

    # Forced always writes, which is what the end of a slice does.
    status.slice_bytes = 3
    status.state = dlq.dl.COMPLETE
    status.write(force=True)
    said = json.loads(status.path.read_text())
    assert said["payload_bytes_this_slice"] == 3
    assert said["state"] == dlq.dl.COMPLETE


def test_a_run_by_hand_outside_the_scheduler_is_safe(dlq, monkeypatch):
    """The defaults are what a person gets when they run an item themselves:
    here, now, with no deadline and no slice — which declines rather than
    downloading something nobody bounded."""
    for name in (
        "EXPIRE_WORK",
        "EXPIRE_OUT",
        "EXPIRE_SLICE_BYTES",
        "EXPIRE_BUDGET_BYTES",
        "EXPIRE_STOP_EPOCH",
        "EXPIRE_RUN_ID",
        "EXPIRE_TOTAL_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)
    env = dlq.dl.Env()
    assert env.slice == 0
    assert env.total_hint == 0
    assert env.deadline() == float("inf")
    assert env.run_id  # something to tell one run from another
    assert str(env.work) == "." and str(env.out) == "."


def test_the_sidecar_holds_the_validators_and_never_the_position(dlq, tmp_path):
    """The resume offset is always the size on disk. What the sidecar carries
    is what a *server* said — the ETag, the date, the total — so losing it
    costs a re-check and never a re-download."""
    part = tmp_path / "thing.iso.part"
    assert dlq.dl._load_meta(part) == {}  # nothing written yet is not an error
    dlq.dl._save_meta(part, {"etag": '"a"', "total": 1234, "restarts": 1})
    assert dlq.dl._load_meta(part) == {"etag": '"a"', "total": 1234, "restarts": 1}
    assert "position" not in dlq.dl._load_meta(part)

    # A sidecar that will not parse is nothing rather than a crash: it is
    # written beside a file that is worth more than it is.
    dlq.dl._meta_path(part).write_text("{ truncated")
    assert dlq.dl._load_meta(part) == {}
