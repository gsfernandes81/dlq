"""The six settings, the file they live in, and the reserve they move.

Three rules run through all of it and each has a way of failing that says
nothing at the time:

* **A stored value that fails its rule reads as the default.** This is read at
  the top of a firing nobody is watching, so a stray character typed into
  ``config.json`` must not be able to stop a night's downloads — or, worse,
  take the reserve out on its way past.
* **Reading a broken file is forgiving; writing over one is refused.**
  ``load_config`` answers a file it cannot parse with an empty dict, so a save
  on top of it would be a fresh file holding only the new key, with the
  destinations and every other setting gone under a line saying it worked.
* **The reserve is waived on the paid figure alone**, and only with the switch
  set to no. ``paid.left_bytes`` can only understate what is paid for, which is
  the one direction a reading cannot be wrong about in a way that spends money.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

MiB = 1024**2
MB = 1_000_000


def names(dlq):
    return list(dlq.runner.SETTINGS)


# --------------------------------------------------------------------------- #
# What is in force
# --------------------------------------------------------------------------- #


def test_a_stored_value_that_fails_its_rule_reads_as_the_default(dlq):
    """Every setting, with nonsense in the file: the built-in one is in force."""
    junk = ["", None, [], {}, "yes please", -1, 7.5, 10**9]
    for name in names(dlq):
        default = dlq.runner.SETTINGS[name]["default"]
        for value in junk:
            dlq.config({dlq.runner.SETTINGS[name]["key"]: value})
            assert dlq.runner.settings()[name] == default, (name, value)
            # And it is not silent: the command and the dump can name it.
            stored, problem, where = dlq.runner.setting_state(name)
            assert problem and where == "default" and stored == value


def test_whether_a_stored_value_is_in_force_is_keyed_on_the_key_being_there(dlq):
    """A stored ``null`` is a value somebody stored, and it is refused like one.

    Three places used to decide this for themselves, two on ``get(key) is
    None`` and one on ``key in config``, so a file holding ``null`` read as
    "set and refused" on the screen and as nothing at all from the command.
    """
    key = dlq.runner.SETTINGS["window"]["key"]
    dlq.config({})
    assert dlq.runner.setting_state("window") == (None, None, "default")
    dlq.config({key: None})
    stored, problem, where = dlq.runner.setting_state("window")
    assert stored is None and problem and where == "default"
    dlq.config({key: 45})
    assert dlq.runner.setting_state("window") == (45, None, "set")


def test_settings_are_read_at_the_moment_they_are_used(dlq):
    """Never cached: the screen sets them while a firing is in progress."""
    assert dlq.runner.settings()["window"] == 60
    dlq.config({"window_minutes": 45})
    assert dlq.runner.settings()["window"] == 45
    assert dlq.runner.window_seconds() == 45 * 60
    dlq.config({"window_minutes": 120})
    assert dlq.runner.window_seconds() == 2 * 60 * 60


def test_a_file_that_will_not_parse_leaves_every_setting_at_its_default(dlq):
    """Forgiving on the way in, and one line says why."""
    dlq.config("{not json at all")
    assert dlq.runner.load_config() == {}
    assert dlq.runner.settings() == {
        name: spec["default"] for name, spec in dlq.runner.SETTINGS.items()
    }
    assert dlq.runner.config_problem()


@pytest.mark.parametrize(
    ("text", "broken"),
    [
        ("", False),  # a shell redirect leaves this; there is nothing to lose
        ("   \n", False),
        ("{}", False),
        ('{"window_minutes": 45}', False),
        ("{oops", True),
        ("[1, 2]", True),  # valid JSON, and the same loss by another route
        ('"a string"', True),
        ("null", True),
    ],
)
def test_only_a_file_that_would_be_lost_is_a_problem(dlq, text, broken):
    dlq.config(text)
    assert bool(dlq.runner.config_problem()) is broken


def test_a_missing_file_is_not_a_problem(dlq):
    """Nothing has been set yet, which is what an empty config means anyway."""
    assert not (dlq.root / "config.json").exists()
    assert dlq.runner.config_problem() is None
    assert dlq.runner.load_config() == {}


# --------------------------------------------------------------------------- #
# Writing over one is refused
# --------------------------------------------------------------------------- #


def test_nothing_is_written_over_a_file_that_will_not_parse(dlq):
    """Both setters refuse, and the file is byte-for-byte what it was.

    The failure this prevents is silent and total: a save on top of an empty
    dict is a fresh file holding only the new key, and the destinations and the
    other five settings are gone under a line saying it worked.
    """
    text = '{"video_dir": "/somewhere", "window_minutes": 45,,,}'
    path = dlq.config(text)
    for worked, said in (
        dlq.sched.set_setting("window", "45"),
        dlq.sched.set_setting("auto", "off"),
        dlq.sched.set_dest("video", str(dlq.root)),
        dlq.sched.set_dest("video", "default"),
    ):
        assert not worked
        assert said == [dlq.runner.config_problem()]
    assert path.read_text() == text


def test_a_setting_that_takes_is_the_one_in_force_afterwards(dlq):
    """Set through the same function the screen sets through, and read back."""
    worked, said = dlq.sched.set_setting("window", "45 min")
    assert worked and said
    assert dlq.runner.settings()["window"] == 45
    assert dlq.runner.setting_state("window")[2] == "set"
    # Putting it back removes the key rather than writing today's default in,
    # where it would outlive any change of mind about what the default is.
    worked, said = dlq.sched.set_setting("window", "default")
    assert worked
    assert "window_minutes" not in json.loads((dlq.root / "config.json").read_text())
    assert dlq.runner.settings()["window"] == dlq.runner.SETTINGS["window"]["default"]


def test_a_refused_value_changes_nothing(dlq):
    dlq.config({"window_minutes": 45})
    worked, said = dlq.sched.set_setting("window", "7")
    assert not worked and said[0].strip()
    assert dlq.runner.settings()["window"] == 45


def test_setting_a_destination_leaves_the_settings_alone_and_the_other_way(dlq):
    """They share one file, and one save must not take the other's keys."""
    dlq.sched.set_setting("window", "45")
    worked, _ = dlq.sched.set_dest("video", str(dlq.root / "films"))
    assert worked
    assert dlq.runner.settings()["window"] == 45
    assert dlq.runner.dests()["video"] == dlq.root / "films"
    dlq.sched.set_setting("reserve", "250")
    assert dlq.runner.dests()["video"] == dlq.root / "films"
    assert dlq.runner.settings()["reserve"] == 250


def test_a_destination_that_is_already_there_is_taken_as_it_is(dlq):
    """The ordinary case: somebody points the queue at a folder that exists."""
    there = dlq.root / "films"
    there.mkdir()
    worked, said = dlq.sched.set_dest("video", str(there))
    assert worked and said
    assert dlq.runner.dests()["video"] == there


def test_a_destination_is_created_one_level_and_never_a_tree(dlq):
    """A typo should not quietly build a folder nobody meant.

    Its parent missing is the signal that this *is* a typo rather than a new
    folder — and the cost of guessing wrong is a night's downloads delivered
    somewhere nobody will look for them.
    """
    one = dlq.root / "films"
    worked, _ = dlq.sched.set_dest("video", str(one))
    assert worked and one.is_dir()

    deep = dlq.root / "nope" / "deeper"
    worked, said = dlq.sched.set_dest("video", str(deep))
    assert not worked and said[-1].strip()
    assert not deep.exists() and not deep.parent.exists()
    # And the one that did take is still the one in force.
    assert dlq.runner.dests()["video"] == one


# --------------------------------------------------------------------------- #
# Typing one in
# --------------------------------------------------------------------------- #


@given(
    number=st.integers(0, 1440),
    unit=st.sampled_from(["", "m", " min", "mins", "minutes"]),
)
def test_a_window_reads_the_same_however_it_was_typed(dlq, number, unit):
    """The phone keyboard makes ``45m`` likelier than the bare number."""
    text = f"{number}{unit}"
    problem = dlq.runner.setting_problem("window", number)
    if problem:
        with pytest.raises(ValueError):
            dlq.runner.parse_setting("window", text)
    else:
        assert dlq.runner.parse_setting("window", text) == number


def test_hours_are_minutes(dlq):
    for text in ("2h", "2 hours", "2hr"):
        assert dlq.runner.parse_setting("window", text) == 120


@given(name=st.sampled_from(["window", "reserve", "paid-min"]), number=st.integers(0, 1440))
def test_a_figure_survives_being_spelled_and_read_back(dlq, name, number):
    """One spelling everywhere, and it is the one the parser takes.

    The screen prints ``spell_setting`` and the setter parses what somebody
    types; if those two disagreed, a value shown on the screen could not be
    typed back into it.
    """
    if dlq.runner.setting_problem(name, number):
        return
    spelled = dlq.runner.spell_setting(name, number)
    assert dlq.runner.parse_setting(name, spelled) == number


@pytest.mark.parametrize("word", ["on", "yes", "true", "1"])
def test_a_switch_takes_the_words_a_person_would_type(dlq, word):
    assert dlq.runner.parse_setting("auto", word) is True
    assert dlq.runner.parse_setting("auto", word.upper()) is True
    other = {"on": "off", "yes": "no", "true": "false", "1": "0"}[word]
    assert dlq.runner.parse_setting("auto", other) is False


def test_a_switch_is_never_a_number(dlq):
    """``True`` is an int in Python and would otherwise read as one minute."""
    assert dlq.runner.setting_problem("window", True)
    assert dlq.runner.setting_problem("auto", 1)
    with pytest.raises(ValueError):
        dlq.runner.parse_setting("auto", "45")


def test_the_window_is_a_multiple_of_the_scheduler_floor(dlq):
    """15 minutes is JobScheduler's own floor for a periodic job.

    A window that is not a multiple of one buys nothing beyond the nearest
    firing below it.
    """
    assert dlq.runner.SETTINGS["window"]["step"] == 15
    assert dlq.sched.PERIOD_MS == 15 * 60 * 1000
    assert not dlq.runner.setting_problem("window", 45)
    assert dlq.runner.setting_problem("window", 46)
    assert dlq.runner.setting_problem("window", 0)


# --------------------------------------------------------------------------- #
# The reserve, and what waives it
# --------------------------------------------------------------------------- #


def test_the_reserve_stands_unless_it_is_told_not_to(dlq):
    """Both halves of the waiver matter, and neither waives on its own."""
    with_paid = dlq.reading(free=200 * MiB, paid=500 * MB)
    without = dlq.reading(free=200 * MiB, paid=0)

    assert dlq.runner.reserve_waived(with_paid) is False  # the switch is on
    dlq.config({"reserve_when_paid": False})
    assert dlq.runner.reserve_waived(without) is False  # and no paid data
    assert dlq.runner.reserve_waived(with_paid) is True
    assert dlq.runner.floor_bytes(with_paid) == 0
    assert dlq.runner.floor_bytes(without) == dlq.runner.reserve_bytes()


def test_the_reserve_is_decimal_mb_because_that_is_how_data_is_sold(dlq):
    """The requirement was given in MB and a phone plan is sold in them.

    Read in MiB instead, a reserve set to 100 would quietly keep back 4.9%%
    more than the person asked for every night, out of an allowance that
    expires at midnight either way.
    """
    dlq.config({"reserve_mb": 250})
    assert dlq.runner.reserve_bytes() == 250_000_000
    dlq.config({})
    assert dlq.runner.reserve_bytes() == dlq.runner.SETTINGS["reserve"]["default"] * 1_000_000


def test_nothing_is_waived_on_a_reading_that_has_not_said(dlq):
    """A reading with no paid figure is not one that says there is paid data."""
    dlq.config({"reserve_when_paid": False})
    assert dlq.runner.reserve_waived(None) is False
    assert dlq.runner.reserve_waived({}) is False
    assert dlq.runner.reserve_waived({"paid": None}) is False
    assert dlq.runner.reserve_waived({"paid": {}}) is False
    assert dlq.runner.reserve_waived({"paid": {"left_bytes": None}}) is False


def test_the_waiver_never_asks_the_free_figure(dlq):
    """``free.left_bytes`` can only overstate; ``paid.left_bytes`` understates.

    "There is paid data" is the one direction the reading cannot be wrong about
    in a way that spends the reserve, so it is the only reading it asks.
    """
    dlq.config({"reserve_when_paid": False})
    plenty_free = dlq.reading(free=700 * MiB, paid=0)
    assert plenty_free["free"]["left_bytes"] > 0
    assert plenty_free["paid"]["left_bytes"] == 0
    assert dlq.runner.reserve_waived(plenty_free) is False


@pytest.mark.parametrize(
    ("minimum", "paid", "waived"),
    [
        (0, 0, False),  # nought means "any paid data at all", not "none needed"
        (0, 1, True),
        (0, 500 * MB, True),
        (200, 199 * MB, False),
        (200, 200 * MB, True),
        (200, 500 * MB, True),
    ],
)
def test_paid_min_is_how_much_paid_data_the_waiver_wants(dlq, minimum, paid, waived):
    """A figure above nought keeps the reserve for longer, never for less."""
    dlq.config({"reserve_when_paid": False, "paid_min_mb": minimum})
    assert dlq.runner.reserve_waived(dlq.reading(paid=paid)) is waived


def test_paid_min_does_nothing_on_its_own(dlq):
    """It qualifies ``reserve-when-paid``; with the switch on it is inert."""
    dlq.config({"paid_min_mb": 0})
    assert dlq.runner.reserve_waived(dlq.reading(paid=10 * MB)) is False


def test_the_budget_is_the_smaller_of_the_two_limits(dlq):
    """The floor is exact; the free figure is discounted before it is spent."""
    doc = dlq.reading(free=600 * MiB, paid=0)
    spendable = dlq.runner.spendable_bytes(doc)
    floor = doc["today"]["remainder_bytes"] - dlq.runner.floor_bytes(doc)
    assert spendable <= floor
    assert spendable < doc["free"]["left_bytes"]
    # And a bigger reserve leaves less to spend.
    dlq.config({"reserve_mb": 400})
    assert dlq.runner.spendable_bytes(doc) < spendable
    # It never goes negative: a reading below the floor is nothing to spend.
    dlq.config({"reserve_mb": 100_000})
    assert dlq.runner.spendable_bytes(doc) == 0


def _discount(dlq, doc) -> int:
    """How much of the expiring allowance the budget declines to spend."""
    return doc["free"]["left_bytes"] - dlq.runner.spendable_bytes(doc)


def test_the_expiring_allowance_is_never_spent_to_the_letter(dlq):
    """On a night where the free figure is the limit rather than the floor.

    ``free.left_bytes`` can only *overstate* — the portal lags live traffic and
    the carry-in it is worked out from is a lower bound — so what is spent is
    always less than what it says. The discount has two halves and the larger
    of them is the one taken: a proportion, which is what protects a big
    reading, and a fixed floor, which is what protects a small one where three
    per cent of nearly nothing would protect nothing at all.

    Each reading here carries paid data behind it, which is what puts the floor
    out of the way and leaves this figure as the limit under test.
    """
    behind = 4 * 1024 * MiB
    big = dlq.reading(free=600 * MiB, paid=behind)
    small = dlq.reading(free=20 * MiB, paid=behind)

    # Both are limited by the free figure rather than by the reserve.
    for doc in (big, small):
        assert dlq.runner.spendable_bytes(doc) < doc["free"]["left_bytes"]
        assert dlq.runner.spendable_bytes(doc) < doc["today"]["remainder_bytes"]

    # A proportion of the big one is more than the floor, and it is what is
    # taken; taking only the floor would spend three per cent of an evening's
    # allowance that the reading never really had.
    assert _discount(dlq, big) >= dlq.runner.FREE_HAIRCUT_FRACTION * (600 * MiB)
    assert _discount(dlq, big) > dlq.runner.FREE_HAIRCUT_FLOOR
    # A proportion of the small one is less than the floor, and the floor wins.
    assert dlq.runner.FREE_HAIRCUT_FRACTION * (20 * MiB) < dlq.runner.FREE_HAIRCUT_FLOOR
    assert _discount(dlq, small) >= dlq.runner.FREE_HAIRCUT_FLOOR


def test_a_stale_reading_is_discounted_by_how_stale_it_is(dlq):
    """On a night where the expiring grant is the limit rather than the floor.

    A cached reading ages, and what it does not know about is data burned since
    it was taken, so the older it is the less of it may be spent.
    """
    fresh = dlq.reading(free=600 * MiB, paid=4 * 1024 * MiB, age=0)
    old = dlq.reading(free=600 * MiB, paid=4 * 1024 * MiB, age=90)
    assert dlq.runner.spendable_bytes(old) < dlq.runner.spendable_bytes(fresh)
    # And it is a rate over the time rather than a token taken off for being
    # old: a minute and a half of a mobile link is megabytes, and a reading
    # that discounted bytes for it would be a reading nothing was discounted.
    lost = dlq.runner.spendable_bytes(fresh) - dlq.runner.spendable_bytes(old)
    assert lost > MiB


# --------------------------------------------------------------------------- #
# One spec, three front ends
# --------------------------------------------------------------------------- #


def test_every_setting_is_reachable_and_has_a_sentence(dlq):
    """A setting added to the runner and nowhere else goes unreachable.

    A short zip is not an error — it just leaves the last setting with no key —
    and ``SETTING_SAYS`` would raise on the one line whose whole job is to say
    what just happened.
    """
    assert set(dlq.sched.SETTING_SAYS) == set(dlq.runner.SETTINGS)
    assert len(dlq.ui.SETTING_KEYS) == len(dlq.runner.SETTINGS)
    assert len(set(dlq.ui.SETTING_KEYS)) == len(dlq.ui.SETTING_KEYS)
    for name in dlq.runner.SETTINGS:
        said = dlq.sched._setting_said(name, dlq.runner.settings()[name])
        assert said.startswith(f"{name}:") and len(said) > len(name) + 2


def test_the_keys_that_set_are_never_the_keys_that_leave(dlq):
    """``q`` and ``x`` are the way out and the way to stop a download."""
    taken = {chr(key) for key in (*dlq.ui.SETTING_KEYS, *dlq.ui.PAGE_KEYS)}
    assert not taken & {"q", "x"}
    assert len(taken) == len(dlq.ui.SETTING_KEYS) + len(dlq.ui.PAGE_KEYS)


def test_every_destination_kind_is_reachable_and_named(dlq):
    assert len(dlq.ui.DEST_KEYS) == len(dlq.runner.DEST_KINDS)
    assert set(dlq.sched.FILLED_BY) == set(dlq.runner.DEST_KINDS)


# --------------------------------------------------------------------------- #
# The one notification a setting can silence
# --------------------------------------------------------------------------- #


def test_notify_blocked_covers_the_blocked_firing_and_no_other(dlq, monkeypatch):
    """Off leaves the log line and ``dlq status`` exactly as they were.

    A blocked firing repeats every ~15 minutes on a phone off the vessel's
    wifi, which is how a person learns to ignore notifications. A malformed
    item and an item that has run out of nights each happen once and still need
    somebody.
    """
    posted = []
    monkeypatch.setattr(dlq.runner, "notify", lambda title, body: posted.append(title))

    dlq.runner.say_blocked()
    assert len(posted) == 1

    dlq.config({"notify_blocked": False})
    dlq.runner.say_blocked()
    assert len(posted) == 1

    # The item that ran out of nights still says so, with the switch off.
    item = {"name": "10-a.py", "path": dlq.item("10-a.py")}
    dlq.runner.give_up(item, {"items": {}})
    assert len(posted) == 2
