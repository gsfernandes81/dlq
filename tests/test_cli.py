"""The one command: what a bare ``dlq`` does, and where each word goes.

Two decisions here are worth more than they look. **A bare ``dlq`` opens the
screen, and off a terminal it prints the status** — a pipe, a script, an ssh
command with no tty all used to print status, and curses in any of them is a
usage error where there used to be an answer. And **the default may never be an
action that does something**: the failure would be a bare command that arms the
job or spends data.

The other is that ``dlq <url>`` is routed before the verbs. No verb contains
``://``, so a URL can never be shadowed by one — and the queuer keeps its own
module and its own flags, with the dispatcher only the door.
"""

from __future__ import annotations

import json
import sys

import pytest

MiB = 1024**2


@pytest.fixture
def offline(dlq, monkeypatch):
    """Nothing that would reach the network, the scheduler or a real screen."""
    monkeypatch.setattr(dlq.runner, "portal_now", lambda: (None, "no credentials"))
    monkeypatch.setattr(dlq.sched, "api", lambda *a, **kw: (_ for _ in ()).throw(
        FileNotFoundError("termux-job-scheduler")
    ))
    return dlq


# --------------------------------------------------------------------------- #
# What a bare dlq does
# --------------------------------------------------------------------------- #


def test_a_bare_dlq_opens_the_screen_and_prints_status_off_a_terminal(dlq):
    assert dlq.sched.default_action(interactive=True) == "ui"
    assert dlq.sched.default_action(interactive=False) == "status"


def test_the_default_never_does_anything(dlq):
    """Both halves of it, whatever they come to be.

    A bare command that armed the job or spent data is the failure this exists
    to prevent, so it is the *set* of read-only actions that is pinned rather
    than the two names.
    """
    reads_only = {"status", "list", "ui", "names", "queue", "logs", "dump", "path"}
    for interactive in (True, False):
        assert dlq.sched.default_action(interactive) in reads_only


def test_every_action_is_reachable_and_every_reachable_one_is_named(dlq):
    """The usage block and the dispatcher are one list.

    An action in the usage that the dispatcher does not know falls through to
    the usage again; one the dispatcher knows and the usage does not is a
    feature nobody can find.
    """
    for name in dlq.sched.ACTIONS:
        assert dlq.sched._action([name]) == name
    for name in dlq.sched.HIDDEN:
        assert dlq.sched._action([name]) == name
    assert dlq.sched._action(["not-a-verb"]) is None
    assert not set(dlq.sched.ACTIONS) & set(dlq.sched.HIDDEN)
    assert [entry[0].split()[0] for entry in dlq.sched.HELP] == list(dlq.sched.ACTIONS)
    for _name, blurb in dlq.sched.HELP:
        assert blurb.strip()


def test_the_options_that_are_really_actions(dlq):
    """``--now`` was asked for as an option and reads as one; ``dlq`` has none."""
    for spelled, means in dlq.sched.ALIASES.items():
        assert dlq.sched._action([spelled]) == means
        assert means in dlq.sched.ACTIONS or means in dlq.sched.HIDDEN


def test_no_verb_could_ever_shadow_a_url(dlq):
    """Which is why a URL is routed before them and needs no flag."""
    for name in (*dlq.sched.ACTIONS, *dlq.sched.HIDDEN, *dlq.sched.ALIASES):
        assert "://" not in name


def test_a_url_goes_to_the_queuer_with_everything_after_it(dlq, monkeypatch):
    """The dispatch is only the door: the flags belong to ``dlq.py``."""
    seen = []
    monkeypatch.setattr(dlq.queuer, "main", lambda argv: seen.append(argv) or 0)
    argv = ["https://example.invalid/big.iso", "--name", "big.iso"]
    assert dlq.sched.main(list(argv)) == 0
    assert seen == [argv]


def test_an_unknown_word_prints_the_usage_and_fails(dlq, capsys):
    assert dlq.sched.main(["frobnicate"]) == 2
    said = capsys.readouterr().err
    assert "usage" in said
    for name in ("status", "list", "ui"):
        assert name in said


def test_an_action_that_takes_a_name_asks_for_one(dlq, capsys):
    for action in dlq.sched.NAMED:
        assert dlq.sched.main([action]) == 2
        assert "NAME" in capsys.readouterr().err


def test_a_name_that_matches_nothing_is_an_error_rather_than_a_guess(dlq, capsys):
    dlq.item("10-thing.py")
    assert dlq.sched.main(["path", "nothing-like-it"]) == 1
    assert "no download matches" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Running the queue on purpose
# --------------------------------------------------------------------------- #


def test_run_now_is_the_runner_forced_and_nothing_else(dlq):
    """``--force`` is what makes now mean now.

    Without it the runner answers "not yet: window opens 23:00Z", which is the
    right answer to a *firing* and no answer at all to somebody who has just
    typed run-now. It overrides the clock gate and nothing else.
    """
    argv = dlq.sched.queue_run_argv(blind=False)
    assert argv[1] == str(dlq.root / "expire_runner.py")
    assert "--force" in argv
    assert "--blind" not in argv
    blind = dlq.sched.queue_run_argv(blind=True)
    assert "--force" in blind and "--blind" in blind
    # One spelling, three callers: the difference is the question they ask.
    assert blind[:-1] == argv


def test_run_now_blind_says_the_number_and_asks_before_it_spends(
    dlq, monkeypatch, capsys
):
    """The figure is the runner's own :func:`blind_budget`, called rather than
    re-derived, so the number agreed to and the number spent are the same."""
    dlq.item("10-one.py", cap=200 * MiB)
    dlq.item("20-two.py", cap=50 * MiB)
    spawned = []
    monkeypatch.setattr(
        dlq.sched.subprocess, "run", lambda argv, **kw: spawned.append(argv)
    )
    monkeypatch.setattr(dlq.sched.sys.stdin, "isatty", lambda: False)

    assert dlq.sched.run_blind(assume_yes=False) == 2
    said = capsys.readouterr()
    assert dlq.ytq.human(250 * MiB) in said.out
    assert "mobile data" in said.out
    # Nobody was asked, so nothing was started — and that is an error rather
    # than a silence an unattended caller could read as a refusal.
    assert spawned == []
    assert "pass --yes" in said.err


def test_run_now_blind_on_an_empty_queue_starts_nothing(dlq, capsys):
    assert dlq.sched.run_blind(assume_yes=True) == 0
    assert "nothing queued" in capsys.readouterr().out


def test_downloading_one_item_now_asks_before_it_spends(dlq, monkeypatch, capsys):
    """Same bargain, one item: the remainder of its own declared cap."""
    dlq.item("10-one.py", cap=200 * MiB)
    monkeypatch.setattr(dlq.sched.sys.stdin, "isatty", lambda: False)
    row = dlq.sched.items()[0]
    assert dlq.sched.run_one(row, assume_yes=False) == 2
    said = capsys.readouterr()
    assert dlq.ytq.human(200 * MiB) in said.out
    assert "mobile data" in said.out


def test_an_item_that_cannot_run_is_refused_with_the_reason(dlq, capsys):
    (dlq.root / "queue" / "10-broken.py").write_text("# nothing declared\n")
    dlq.item("20-done.py", where="done/2026-09-01")
    rows = {row["name"]: row for row in dlq.sched.items()}
    assert dlq.sched.run_one(rows["10-broken.py"], assume_yes=True) == 1
    assert dlq.sched.run_one(rows["20-done.py"], assume_yes=True) == 1
    said = capsys.readouterr().err
    assert "not a runnable item" in said
    assert "not the queue" in said


def test_run_now_will_not_start_on_a_broken_checkout(dlq, capsys):
    (dlq.root / "queue" / "README.md").unlink()
    assert dlq.sched.main(["run-now"]) == 1
    assert "EXPIRE_HOME" in capsys.readouterr().err


def test_a_runner_the_scheduler_could_not_exec_is_reported_rather_than_fired(
    offline, capsys
):
    """The specific trap is ``#!/usr/bin/env python3``.

    It is the portable form everywhere else and it is wrong on Android, which
    has no ``/usr/bin`` at all: the job fires and dies with exit 126 before
    Python starts, so there is no heartbeat, no log and not even a lock file to
    say why. The queue then goes quiet for days.
    """
    runner = offline.root / "expire_runner.py"
    lines = runner.read_text().splitlines(keepends=True)
    # ``#!/usr/bin/env python3`` is the specific trap and it exists on this
    # machine, which is the whole point of it: it is portable everywhere except
    # the one platform this runs on. What the check actually asks is whether
    # the interpreter is on disk, so this is one that is not.
    lines[0] = "#!/no/such/prefix/bin/python3\n"
    runner.write_text("".join(lines))

    problem = offline.sched.shebang_problem()
    assert problem and "126" in problem
    assert offline.sched.main(["status"]) == 1
    assert "BROKEN" in capsys.readouterr().out
    # And the job is not armed over it either.
    worked, said = offline.sched.do_arm()
    assert not worked and said


# --------------------------------------------------------------------------- #
# The read-only answers
# --------------------------------------------------------------------------- #


def test_the_status_screen_prints_and_says_whether_anything_is_broken(
    offline, capsys
):
    offline.item("10-thing.py")
    assert offline.sched.main(["status"]) == 0
    said = capsys.readouterr().out
    assert "DOWNLOAD QUEUE" in said
    assert "10-thing" in said


def test_a_broken_checkout_stops_the_status_screen_rather_than_filling_it(
    offline, capsys
):
    """Nothing below it would be about the queue anybody means."""
    (offline.root / "queue" / "README.md").unlink()
    assert offline.sched.main(["status"]) == 1
    said = capsys.readouterr().out
    assert "BROKEN" in said
    assert "DOWNLOAD QUEUE" not in said


def test_the_listing_and_the_completions_show_the_same_downloads(dlq, capsys):
    dlq.item("10-thing.py", desc="a thing")
    dlq.item("20-other.py", where="failed")
    assert dlq.sched.main(["list"]) == 0
    listed = capsys.readouterr().out
    assert dlq.sched.main(["names"]) == 0
    names = capsys.readouterr().out
    for name in ("10-thing", "20-other"):
        assert name in listed and name in names
    # fish's own completion format: name, a tab, what it is.
    for line in names.strip().splitlines():
        assert line.count("\t") == 1


def test_the_raw_queue_view_is_the_files_and_nothing_else(dlq, capsys):
    dlq.item("10-thing.py")
    (dlq.root / "queue" / ".staging").mkdir()
    assert dlq.sched.main(["queue"]) == 0
    said = capsys.readouterr().out
    assert "10-thing.py" in said
    assert "staging" not in said
    assert "README" in said  # the raw view really is every file


def test_the_destinations_answer_names_every_kind_and_who_fills_it(dlq, capsys):
    """Three of them, because a film, a song and an installer do not belong in
    the same folder on a phone."""
    assert dlq.sched.main(["dest"]) == 0
    said = capsys.readouterr().out
    for kind in dlq.runner.DEST_KINDS:
        assert kind in said
        assert dlq.sched.FILLED_BY[kind].split(",")[0] in said
    assert "default" in said


def test_a_destination_that_is_not_one_is_refused_by_name(dlq, capsys):
    assert dlq.sched.main(["dest", "photos", "/tmp"]) == 2
    said = capsys.readouterr().err
    for kind in dlq.runner.DEST_KINDS:
        assert kind in said


def test_the_settings_answer_names_every_setting_and_what_it_is(dlq, capsys):
    dlq.config({"window_minutes": 45})
    assert dlq.sched.main(["settings"]) == 0
    said = capsys.readouterr().out
    for name, spec in dlq.runner.SETTINGS.items():
        assert name in said
        assert spec["label"] in said
    # Spelled the way the runner spells it everywhere, rather than the way
    # this test would have spelled it: a setting that reads 60 in one place and
    # 1h in another is two settings as far as anyone reading them is concerned.
    assert dlq.runner.spell_setting("window", 45) in said
    assert "set" in said and "default" in said


def test_a_setting_the_file_holds_and_the_runner_declines_is_named(dlq, capsys):
    """A list saying "default" all the way down with nothing explaining it is
    the version of this screen that gets it rewritten."""
    dlq.config({"window_minutes": 7})
    assert dlq.sched.main(["settings"]) == 0
    said = capsys.readouterr().out
    assert "7" in said
    assert "config.json" in said


def test_a_config_that_will_not_parse_is_said_once_and_first(dlq, capsys):
    dlq.config("{oops")
    assert dlq.sched.main(["settings"]) == 0
    # Wrapped to the terminal, so it is compared the way it is drawn.
    said = " ".join(capsys.readouterr().out.split())
    assert " ".join(dlq.runner.config_problem().split()) in said
    assert "nothing can be set" in said


def test_a_setting_that_is_not_one_is_refused_with_the_list(dlq, capsys):
    assert dlq.sched.main(["settings", "colour", "blue"]) == 2
    said = capsys.readouterr().err
    for name in dlq.runner.SETTINGS:
        assert name in said
    # A name on its own is somebody halfway through changing it, not somebody
    # asking what it is — the bare command already answered that.
    assert dlq.sched.main(["settings", "window"]) == 2
    assert "VALUE" in capsys.readouterr().err


def test_a_value_the_shell_split_still_reads_as_one(dlq):
    """``dlq settings window 45 min`` is a sentence, and the shell hands it
    over in pieces."""
    assert dlq.sched.main(["settings", "window", "45", "min"]) == 0
    assert dlq.runner.settings()["window"] == 45


def test_the_log_says_so_when_there_is_none_yet(dlq, capsys):
    assert dlq.sched.main(["logs"]) == 0
    assert "not written yet" in capsys.readouterr().out


def test_the_command_names_itself_the_way_it_was_invoked(dlq, monkeypatch):
    """``dlq`` on PATH and ``expire_sched.py`` in the checkout — printing the
    other one sends people to the wrong place."""
    monkeypatch.setattr(dlq.sched.sys, "argv", ["dlq", "status"])
    assert dlq.sched._me() == "dlq"
    monkeypatch.setattr(dlq.sched.sys, "argv", ["expire_sched.py"])
    assert dlq.sched._me() == "expire_sched.py"
    # A front end drawing this module's screens still names this module's
    # commands, because that is whose commands they are.
    monkeypatch.setattr(dlq.sched.sys, "argv", ["expire_runner.py", "--status"])
    assert dlq.sched._me() in ("dlq", "expire_sched.py")


# --------------------------------------------------------------------------- #
# dlq <url>
# --------------------------------------------------------------------------- #


def test_a_server_that_answers_head_settles_both_questions_at_once(dlq, serving):
    """HEAD first: a length and an Accept-Ranges verdict is the whole answer,
    and it costs no payload at all."""
    serving.payload = b"x" * 4096
    assert dlq.queuer.probe(serving.url) == (4096, True)
    assert serving.state_of_the_world["head"]
    assert "asked" not in serving.state_of_the_world  # no GET was needed


def test_a_server_that_will_not_say_is_asked_for_one_byte(dlq, serving):
    """A GET for ``bytes=0-0``, closed unread, settles both at the cost of
    headers plus at most one byte of payload.

    Which is what makes the probe free enough to do before queueing: this runs
    on a phone whose whole point is not spending data it did not mean to.
    """
    serving.payload = b"y" * 8192
    serving.state_ranges = False
    serving.state_length = False
    assert dlq.queuer.probe(serving.url) == (8192, True)
    assert serving.state_of_the_world["asked"] == ["bytes=0-0"]


def test_a_server_that_refuses_head_is_still_sized(dlq, serving):
    """Plenty of them do; the ranged GET settles it."""
    serving.payload = b"z" * 2048
    serving.head_code = 405
    assert dlq.queuer.probe(serving.url) == (2048, True)


def test_a_server_that_ignores_range_says_so_in_the_answer(dlq, serving):
    """The whole file has to fit one night's slice or the item will fail, and
    that is a warning somebody has to be given at queue time."""
    serving.payload = b"w" * 3000
    serving.honour_range = False
    serving.state_ranges = False
    assert dlq.queuer.probe(serving.url) == (3000, False)


def test_what_head_said_stands_when_the_ranged_get_will_not_answer(dlq, serving):
    """Half an answer is still an answer.

    HEAD gave a length; the ranged GET was only ever there to settle the rest.
    Throwing the length away because the second request failed would send
    somebody to ``--expect-bytes`` for a file the server has already sized.
    """
    serving.payload = b"q" * 5000
    serving.state_ranges = False  # so the ranged GET is asked for at all
    serving.get_code = 500
    size, resumable = dlq.queuer.probe(serving.url)
    assert size == 5000
    assert resumable is None  # nothing was settled about resuming


def test_a_server_that_cannot_be_reached_is_an_error_and_not_a_guess(dlq):
    with pytest.raises(dlq.queuer.ProbeError):
        dlq.queuer.probe("http://127.0.0.1:1/nothing", timeout=2)


def test_a_probe_that_fails_is_survivable_with_a_declared_cap(dlq, monkeypatch, capsys):
    """The cap is a spending limit rather than a measurement, so a probe that
    could not run does not have to stop the queueing — but it is said."""
    monkeypatch.setattr(dlq.ytq, "SHEBANG", f"#!{sys.executable}")
    assert dlq.queuer.main(
        ["http://127.0.0.1:1/x.iso", "--expect-bytes", "1000000", "--name", "x.iso"]
    ) == 0
    assert "probe failed" in capsys.readouterr().err
    item = dlq.runner.parse_item(next((dlq.root / "queue").glob("*.py")))
    assert item["cap"] == 1_000_000

    # Without a cap there is nothing to queue against, and nothing is written.
    for path in (dlq.root / "queue").glob("*.py"):
        path.unlink()
    assert dlq.queuer.main(["http://127.0.0.1:1/x.iso"]) == 1
    assert list((dlq.root / "queue").glob("*.py")) == []

    # And --probe is a question rather than an ask, so it fails rather than
    # queueing something nobody sized.
    assert dlq.queuer.main(
        ["http://127.0.0.1:1/x.iso", "--expect-bytes", "1000000", "--probe"]
    ) == 1


@pytest.mark.parametrize("resumable", [True, False, None])
def test_a_probe_says_what_it_found_about_resuming(dlq, monkeypatch, capsys, resumable):
    """Three different answers, and "unknown" is not "no": the first means the
    item can be fetched in slices, the second that it cannot, and the third
    that the server did not say and the first firing will find out."""
    monkeypatch.setattr(
        dlq.queuer, "probe", lambda url, timeout=30: (100 * MiB, resumable)
    )
    assert dlq.queuer.main(["https://example.invalid/x.iso", "--probe"]) == 0
    said = capsys.readouterr().out
    assert "resume" in said.lower()
    assert dlq.ytq.human(100 * MiB) in said
    # The cap it *would* declare, since that is what the question is asked for.
    assert f"{dlq.queuer.expect_bytes(100 * MiB):,}" in said


def test_the_cap_is_the_measurement_plus_the_margin_ytq_uses(dlq):
    """Payload bytes are not wire bytes, and the item pays for retries."""
    size = 100 * MiB
    cap = dlq.queuer.expect_bytes(size)
    assert cap > size
    assert cap == int(-(-size * dlq.ytq.OVERHEAD_EXACT // 1)) + dlq.ytq.OVERHEAD_FIXED
    # It never shrinks with the file: a bigger file has a bigger margin.
    assert dlq.queuer.expect_bytes(2 * size) > cap


@pytest.mark.parametrize(
    ("headers", "answer"),
    [
        ({"Content-Length": "50", "Accept-Ranges": "bytes"}, (50, True)),
        ({"Content-Length": "50", "Accept-Ranges": "none"}, (50, False)),
        ({"Content-Length": "50"}, (50, None)),  # the server has not said
        ({"Accept-Ranges": "bytes"}, (0, True)),
        ({"Content-Length": "about fifty"}, (0, None)),
        ({}, (0, None)),
    ],
)
def test_what_a_head_response_is_read_as(dlq, headers, answer):
    """``None`` is "the server has not said", which is not "no"."""
    assert dlq.queuer._from_head(headers) == answer


@pytest.mark.parametrize(
    ("code", "headers", "answer"),
    [
        (206, {"Content-Range": "bytes 0-0/900"}, (900, True)),
        (206, {"Content-Range": "bytes 0-0/*"}, (0, True)),
        (206, {}, (0, True)),
        (200, {"Content-Length": "900"}, (900, False)),
        (200, {}, (0, False)),
        (416, {}, (0, None)),
    ],
)
def test_what_a_ranged_response_is_read_as(dlq, code, headers, answer):
    """A 206 proves the server honours Range and names the total after the
    slash; a 200 proves it does not, and its length is the whole file."""
    assert dlq.queuer._from_ranged(code, headers) == answer


@pytest.fixture
def probed(dlq, monkeypatch):
    """A server that states a size and honours Range, without a server."""

    def probe(url, timeout=30):
        return 100 * MiB, True

    monkeypatch.setattr(dlq.queuer, "probe", probe)
    # The shebang an item is written with is Termux's absolute python, spelled
    # literally so a run under some other interpreter cannot emit an item that
    # will not start on the phone. Which means it does not start *here*, and
    # the runner would rightly refuse it — so for the checks below it is the
    # interpreter this machine has.
    monkeypatch.setattr(dlq.ytq, "SHEBANG", f"#!{sys.executable}")
    return dlq


def test_a_url_becomes_an_item_the_runner_would_admit(probed, capsys):
    """Written through ``ytq.write_item``, which is the one door, and checked
    with the runner's own parser rather than by eye."""
    assert probed.queuer.main(["https://example.invalid/big.iso"]) == 0
    queued = list((probed.root / "queue").glob("*.py"))
    assert len(queued) == 1
    assert probed.ytq.validate(queued[0]) is None
    item = probed.runner.parse_item(queued[0])
    assert "error" not in item
    # The cap is the measurement plus a margin, never below it.
    assert item["cap"] > 100 * MiB
    assert item["partial"] is True
    assert item["dest"] == "file"
    said = capsys.readouterr().out
    assert queued[0].name.removesuffix(".py") in said
    assert "lands in" in said
    # This server resumes, so there is nothing to warn about — the warning is
    # for the server that cannot be fetched in slices at all.
    assert "warning" not in said


def test_nothing_is_queued_twice(probed, capsys):
    """Items record ``# SOURCE:`` and ``write_item`` is the one door that
    refuses a duplicate; ``dlq`` goes through it too."""
    url = "https://example.invalid/big.iso"
    assert probed.queuer.main([url]) == 0
    assert probed.queuer.main([url]) == 1
    assert len(list((probed.root / "queue").glob("*.py"))) == 1
    said = capsys.readouterr().err
    assert "--again" in said
    # Matched by its URL, which is the same file — so nothing is said about it
    # possibly being another one.
    assert "another file" not in said
    # And meaning it anyway is one flag.
    assert probed.queuer.main([url, "--again"]) == 0
    assert len(list((probed.root / "queue").glob("*.py"))) == 2


def test_a_dry_run_writes_nothing(probed, capsys):
    assert probed.queuer.main(["https://example.invalid/x.iso", "--dry-run"]) == 0
    assert list((probed.root / "queue").glob("*.py")) == []
    assert "EXPECT_BYTES" in capsys.readouterr().out


def test_a_server_that_states_no_size_needs_a_cap_to_be_given(dlq, monkeypatch, capsys):
    """"unknown" is not a valid ``EXPECT_BYTES``: the cap is the most you are
    willing to let it cost."""
    monkeypatch.setattr(dlq.queuer, "probe", lambda url, timeout=30: (0, None))
    monkeypatch.setattr(dlq.ytq, "SHEBANG", f"#!{sys.executable}")
    assert dlq.queuer.main(["https://example.invalid/feed"]) == 1
    assert "--expect-bytes" in capsys.readouterr().err
    assert list((dlq.root / "queue").glob("*.py")) == []

    assert dlq.queuer.main(["https://example.invalid/feed", "--expect-bytes",
                            str(50 * MiB)]) == 0
    item = dlq.runner.parse_item(next((dlq.root / "queue").glob("*.py")))
    assert item["cap"] == 50 * MiB


def test_a_cap_below_the_measured_size_is_refused(probed, capsys):
    assert probed.queuer.main(
        ["https://example.invalid/big.iso", "--expect-bytes", "1000"]
    ) == 1
    assert "below the measured" in capsys.readouterr().err


def test_a_name_is_taken_from_the_url_and_made_safe(dlq):
    name_from_url = dlq.queuer.name_from_url
    assert name_from_url("https://x.invalid/a/b/big.iso") == "big.iso"
    assert name_from_url("https://x.invalid/a/b/%20big%20file.iso") == "big-file.iso"
    assert name_from_url("https://x.invalid/") == "download"
    assert "/" not in name_from_url("https://x.invalid/a/../b")


def test_the_item_it_writes_is_valid_python_that_names_its_source(probed):
    probed.queuer.main(["https://example.invalid/big.iso"])
    source = next((probed.root / "queue").glob("*.py")).read_text()
    compile(source, "item", "exec")
    assert "# SOURCE: url:https://example.invalid/big.iso" in source
    assert "expire_dl" in source


def test_a_probe_prints_what_it_found_and_writes_nothing(probed, capsys):
    assert probed.queuer.main(["https://example.invalid/big.iso", "--probe"]) == 0
    said = capsys.readouterr().out
    assert "resume" in said
    assert list((probed.root / "queue").glob("*.py")) == []


def test_the_number_a_new_item_gets_leaves_room_in_front_of_it(probed):
    """Ten past the highest, and never past the two digits the runner sorts by."""
    for number in range(1, 4):
        probed.queuer.main([f"https://example.invalid/file{number}.iso"])
    numbers = sorted(
        int(path.name.split("-", 1)[0]) for path in (probed.root / "queue").glob("*.py")
    )
    assert numbers == [10, 20, 30]
    assert all(number <= probed.ytq.MAX_PRIORITY for number in numbers)


def test_what_each_flag_puts_in_the_item(probed):
    """The four that change the item rather than the asking.

    Checked by reading the item back with the runner's own parser, because the
    item is the only thing that outlives the command.
    """
    assert probed.queuer.main(
        [
            "https://example.invalid/x",
            "--name",
            "chosen.iso",
            "--number",
            "42",
            "--sha256",
            "a" * 64,
            "--dest",
            str(probed.root / "elsewhere"),
        ]
    ) == 0
    path = next((probed.root / "queue").glob("*.py"))
    assert path.name.startswith("42-")
    item = probed.runner.parse_item(path)
    assert item["dest"] == str(probed.root / "elsewhere")
    source = path.read_text()
    assert "chosen.iso" in source
    assert "a" * 64 in source
    # And it is the runner that resolves where that goes, at delivery.
    assert probed.runner.dest_of(item) == probed.root / "elsewhere"


def test_a_server_that_ignores_range_is_a_warning_at_queue_time(dlq, monkeypatch, capsys):
    """The whole file has to fit one night's slice or the item fails every
    night — and the night it fails is not the night to find out."""
    monkeypatch.setattr(dlq.ytq, "SHEBANG", f"#!{sys.executable}")
    monkeypatch.setattr(
        dlq.queuer, "probe", lambda url, timeout=30: (500 * MiB, False)
    )
    assert dlq.queuer.main(["https://example.invalid/big.iso"]) == 0
    said = capsys.readouterr().out
    assert "warning" in said
    assert "Range" in said


def test_an_item_the_runner_would_refuse_is_written_and_said(probed, monkeypatch, capsys):
    """Written anyway, because the bytes of a queue item are cheap and the
    fault may be one line — but the exit code and the line say so, rather than
    leaving somebody to find it out on the night it does not run."""
    monkeypatch.setattr(probed.ytq, "validate", lambda path: "no 'EXPIRE: v1' header")
    assert probed.queuer.main(["https://example.invalid/x.iso"]) == 1
    said = capsys.readouterr().out
    assert "would reject" in said
    assert "written anyway" in said
    assert list((probed.root / "queue").glob("*.py"))


def test_a_duplicate_matched_by_name_says_it_may_be_another_file(probed, capsys):
    """The two matches are not the same fact: a URL already queued is the same
    file, and a slug already queued may be a different one."""
    assert probed.queuer.main(["https://example.invalid/big.iso"]) == 0
    capsys.readouterr()
    # The same name from a different URL: matched by the slug, not the source.
    assert probed.queuer.main(["https://elsewhere.invalid/big.iso"]) == 1
    said = capsys.readouterr().err
    assert "another file" in said
    assert "--again" in said


def test_a_state_file_that_will_not_parse_is_not_a_crash(dlq):
    """It is read on every listing, and a listing that raises is every screen."""
    (dlq.root / "state.json").write_text("{ not json")
    dlq.item("10-thing.py")
    assert [row["name"] for row in dlq.sched.items()] == ["10-thing.py"]
    assert dlq.runner.load_state() == {}
    (dlq.root / "state.json").write_text(json.dumps(["a", "list"]))
    assert dlq.sched._state_items() == {}
