"""Every line fits the terminal it was laid out for.

Termux in portrait is about 40 columns and the queue's rule is that every line
of every screen fits down to 32. That is not cosmetic: a line wider than the
screen wraps, and the wrapping is what pushes the answer off the top of it.

So this is a *property* rather than a set of expected strings — nothing here
knows what any line says, only that it fits, that it is there, and that what
must never be given up has not been. The three widths are the phone, a small
terminal window and a desktop; between them are swept rather than sampled,
because the failures are at the width where a layout changes shape.

A long checkout path makes some of these fail, and that is the path rather than
a regression: the root is on the wide status screen, spelled with ``~``.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

MiB = 1024**2

#: The phone, a small window, a desktop.
WIDTHS = [32, 40, 80]

#: A long name, a long description and a long destination — the three cells a
#: layout gives up room in, each one longer than any width tested.
LONG = "a-really-quite-long-name-for-a-download-that-somebody-queued"


def plain(pairs):
    return [text for text, _ in pairs]


def fits(line: str, width: int) -> bool:
    """Whether *line* is inside *width*, by the rule the wrapping works to.

    A single word longer than the terminal — which on a phone is most absolute
    paths — is left to overhang rather than broken, deliberately: a terminal
    wrapping a path is visibly a wrapped path, whereas a break inserted into
    one reads as a path with a space in it, and these are the lines somebody is
    about to retype.
    """
    return len(line) <= width or " " not in line.strip()


def paint(text, code):
    """``quota_widget.Paint`` with colour off: what a pipe would get."""
    return text


def stocked(dlq):
    """One of everything: queued, failed, done, rejected, and one downloading."""
    dlq.item(f"10-{LONG}.py", cap=6 * 1024 * MiB, desc=f"{LONG} {LONG}")
    dlq.item("20-small.py", cap=MiB, desc="a short one")
    dlq.item("30-failed.py", where="failed", desc="this one failed")
    dlq.item("40-done.py", where="done/2026-09-01", desc="this one finished")
    (dlq.root / "queue" / "50-broken.py").write_text("# nothing declared here\n")
    work = dlq.root / "work" / f"10-{LONG}.py"
    work.mkdir(parents=True, exist_ok=True)
    (work / "part.bin").write_bytes(b"x" * 4096)
    dlq.state({"30-failed.py": {"attempts": 2}, "20-small.py": {"attempts": 1}})
    return dlq.sched.items()


def facts_for(dlq, **rest):
    return dlq.facts(portal=dlq.reading(free=400 * MiB), force=True, **rest)


# --------------------------------------------------------------------------- #
# The two printed screens
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("width", WIDTHS)
def test_the_listing_fits(dlq, width):
    rows = stocked(dlq)
    lines = plain(dlq.sched.compose_list(rows, width, paint))
    assert lines
    for line in lines:
        assert fits(line, width), line
    # Every download is on it, under a heading that counts it.
    drawn = "\n".join(lines)
    for row in rows:
        assert dlq.ui._slug_of(row["name"])[:12] in drawn


@pytest.mark.parametrize("width", WIDTHS)
def test_the_status_screen_fits(dlq, width):
    stocked(dlq)
    job = [("job", "armed, fires every 15m", "32")]
    lines = plain(dlq.sched.compose_status(facts_for(dlq), width, paint, job=job))
    for line in lines:
        assert fits(line, width), line


@pytest.mark.parametrize("width", WIDTHS)
def test_the_status_screen_leads_with_the_verdict(dlq, width):
    """The first line is the title bar; the second is what happens next.

    Everything under it is the working — the money, the queue, the machinery —
    and this is the whole answer to the question the screen was opened to ask.
    """
    stocked(dlq)
    facts = facts_for(dlq)
    lines = plain(dlq.sched.compose_status(facts, width, paint))
    headline = dlq.sched.VERDICTS[facts["verdict"]][0]
    assert lines[1].strip() == headline.strip()[: len(lines[1].strip())]


@pytest.mark.parametrize("verdict", ["go", "early", "late", "empty", "off", "spent",
                                     "blind", "no-portal", "stale"])
def test_every_verdict_has_words_on_both_screens(dlq, verdict):
    """A verdict the gate grew and a table did not would draw a blank where the
    answer goes."""
    assert verdict in dlq.sched.VERDICTS
    assert verdict in dlq.ui.TONIGHT_SHORT
    assert dlq.sched.VERDICTS[verdict][0].strip()
    assert dlq.ui.TONIGHT_SHORT[verdict].strip()
    # Short enough for the narrowest phone unwrapped: this line is the answer.
    assert len(dlq.sched.VERDICTS[verdict][0]) <= 32
    assert set(dlq.sched.VERDICTS) - {"downloading"} == set(dlq.runner.GATE_STATES)


# --------------------------------------------------------------------------- #
# The screen with a cursor on it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("width", WIDTHS)
def test_the_listing_with_a_cursor_fits_a_curses_window(dlq, width):
    """One column in hand at the right: curses treats a write into the last
    cell of a line as an error, so the whole screen is laid out one narrower."""
    rows = stocked(dlq)
    facts = facts_for(dlq)
    order = [row["name"] for row in rows if row["where"] == "queued"]
    planned = dlq.ui.tonight_plan(order, facts)
    cut = dlq.ui.cut_index(order, facts, width, planned)
    entries = dlq.ui.compose_rows(rows, width, "", cut, planned)
    for _, lines in entries:
        for line in lines:
            assert len(line) <= width - 1, line


@pytest.mark.parametrize("width", WIDTHS)
def test_the_two_header_lines_fit(dlq, width):
    stocked(dlq)
    for facts in (None, facts_for(dlq), facts_for(dlq, blind=True)):
        lines = dlq.ui.tonight_lines(facts, width)
        assert len(lines) == 2
        for line in lines:
            assert len(line) <= width - 2, line


@pytest.mark.parametrize("width", WIDTHS)
def test_a_download_in_flight_outranks_every_verdict(dlq, width):
    """What is being spent now is the answer to "what is happening"; the
    window it is being spent inside is not."""
    stocked(dlq)
    live = dlq.ui.tonight_lines(facts_for(dlq), width, live="20-small.py")
    assert "small" in live[0]
    assert len(live[0]) <= width - 2


@pytest.mark.parametrize("width", WIDTHS)
def test_one_item_and_its_facts_fit(dlq, width):
    rows = stocked(dlq)
    for row in rows:
        for downloading in (False, True):
            lines = dlq.ui.item_lines(row, width, (1, 3), downloading, None)
            assert lines
            for line in lines:
                assert len(line) <= max(12, width - 4), line


@pytest.mark.parametrize("width", WIDTHS)
def test_the_settings_page_fits_and_keeps_every_row(dlq, width):
    job = [("nightly job", "armed, fires every 15m", "32")]
    for height in range(12, 30):
        body = dlq.ui.settings_body(width, height, job, "something happened just now")
        heads = [text for _, text, tone in body if tone == "head"]
        assert len(heads) == len(dlq.runner.SETTINGS) + len(dlq.ui.PAGE_KEYS)
        for indent, text, _ in body:
            assert indent + len(text) <= width - 1, text


@pytest.mark.parametrize("width", WIDTHS)
def test_the_key_hints_fit_and_always_say_the_way_out(dlq, width):
    """The hints are the line that must never be the one clipped.

    And every screen but the two confirms names the way off it: a confirm is
    answered by *any* key, which its own hint says instead.
    """
    for name in dlq.ui.HINTS:
        said = dlq.ui.hint(name, width)
        assert said.strip()
        assert len(said) <= width - 2, (name, said)
        if not name.startswith("confirm"):
            assert "q" in said or "esc" in said, (name, said)
    assert set(dlq.ui.TIGHT_HINTS) == set(dlq.ui.HINTS)


@pytest.mark.parametrize("width", WIDTHS)
def test_the_legend_names_the_keys_the_listing_answers_to(dlq, width):
    """One spelling at every width — at 28 characters it already fits the
    narrowest phone, so there is nothing for a tight version to give up."""
    assert len(dlq.ui.LEGEND_KEYS) <= 32 - 2
    for letter in ("n", "s", "l"):
        assert f"{letter} " in dlq.ui.LEGEND_KEYS


# --------------------------------------------------------------------------- #
# Swept rather than sampled
# --------------------------------------------------------------------------- #


@given(width=st.integers(32, 120))
def test_nothing_is_laid_out_wider_than_the_terminal_at_any_width(dlq, width):
    """Between the three shapes are the widths where a layout changes shape.

    From 32 up, because that is the rule: below it even the title bar and the
    clock beside it do not fit together, and nothing can help a line that is
    one word wider than the terminal.
    """
    rows = stocked(dlq)
    facts = facts_for(dlq)
    for line in plain(dlq.sched.compose_list(rows, width, paint)):
        assert fits(line, width)
    for line in plain(dlq.sched.compose_status(facts, width, paint)):
        assert fits(line, width)
    for _, lines in dlq.ui.compose_rows(rows, width):
        for line in lines:
            assert len(line) <= max(1, width - 1)


@given(
    text=st.text(min_size=0, max_size=200),
    width=st.integers(1, 100),
)
def test_wrapping_always_gives_back_at_least_one_line(dlq, text, width):
    """The lines this draws are reasons and paths, and a reason with its tail
    cut off is usually the half that said what to do about it."""
    lines = dlq.sched._wrap(text, width)
    assert lines
    # Long words are left whole on purpose: a break inserted into a path reads
    # as a path with a space in it, and these are lines someone retypes.
    for line in lines:
        assert len(line) <= max(8, width) or " " not in line.strip()


def test_a_bar_is_drawn_in_characters_a_phone_cannot_widen(dlq):
    """``=`` against ``·``, never block glyphs: they are East-Asian *ambiguous*
    width, and a terminal that renders them double leaves the bar a column
    wider than it was measured at."""
    for width in range(12, 60):
        bar = dlq.ui.progress_bar(3, 4, width)
        assert len(bar) <= width
        assert set(bar) <= set("[]=· 0123456789%")
    # No total means no fraction to draw: a full-looking bar over an unknown
    # size is the one reading worse than none.
    assert dlq.ui.progress_bar(3, 0, 40) == ""
    assert dlq.ui.progress_bar(3, 4, 11) == ""
    assert dlq.ui.progress_bar(4, 4, 40).endswith("100%")
    assert dlq.ui.progress_bar(0, 4, 40).endswith("0%")


def test_a_size_nobody_has_stated_is_marked_as_the_bound_it_is(dlq):
    """Until a server states one the only figure is the declared cap, which is
    deliberately larger than the file: printed as a size it makes a finished
    download look 70% done."""
    assert "≤" in dlq.sched._of(1, 0, 100)
    assert "≤" not in dlq.sched._of(1, 100, 200)
    assert dlq.sched._of(1, 100, 200, compact=True).count("/") == 1
