"""The settings page stays where it was, and says what it changed.

The bug these pin (2026-09-02): a key pressed on the settings page changed the
setting and then *left* — back to the listing, one keypress into a page of six
settings — with the change's sentence clipped onto the row the legend keys sit
on. Two failures, so two halves.

The **cut rule** is pure and is checked pure: the sentence now lives in a said
area on the page, and a said area that costs a setting its row would have
traded the answer for the question. :func:`expire_ui.settings_body` is the one
place that decides it, so it is the one place this asks.

The **staying** is only true on a real screen, so it is checked on one: a pty,
a terminal emulator over it, and the keys somebody would actually press. What
that half is worth is that it reads the screen the way a person does — the
title bar, the hint row, the rows — rather than the return value of a function,
which is exactly what was right about the old code while the screen was wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from _pty import ENTER, drive, make_root, row_for  # noqa: F401  (ENTER: keys)

# The queue's modules are flat siblings at the checkout root, not a package —
# the items in queue/ import them by bare name and so does this. What is
# imported here is the checkout's own copy, and only for the two pure layout
# functions below; the screen half runs against a copy under a temporary root.
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import expire_ui  # noqa: E402  (the checkout has to be on the path first)

# --------------------------------------------------------------------------- #
# The cut rule
# --------------------------------------------------------------------------- #

#: What the scheduler row says when nothing has been asked. Handed in rather
#: than read, because ``termux-job-scheduler`` is not on this machine and a
#: layout rule must not depend on whether a phone answered.
JOB = [("nightly job", "not armed here; the nightly job is the phone's", "1;31")]

#: The sentence the bug report was written about: 67 columns, on a screen that
#: shows 38 of them. It is the longest thing this page ever has to say, which
#: is why it is the one the cut rule is measured against.
SAID = "auto: off — the nightly job fires and does nothing; run-now still works"

#: The two shapes in the report: the phone, and a small terminal window.
SIZES = [(32, 20), (40, 24)]


def _heads(body: list[tuple[int, str, str]]) -> list[str]:
    """Every row's key, name and value — the lines that are never given up."""
    return [text for _, text, tone in body if tone == "head"]


def _said(body: list[tuple[int, str, str]]) -> list[str]:
    """The said area, if this screen was tall enough to keep it."""
    return [text for _, text, tone in body if tone == expire_ui.SAID_TONE]


def _keys() -> list[str]:
    """Every letter the page answers to, settings and page rows alike."""
    return [chr(key) for key in (*expire_ui.SETTING_KEYS, *expire_ui.PAGE_KEYS)]


@pytest.mark.parametrize(("width", "height"), SIZES)
def test_a_said_area_costs_no_setting_its_row(width: int, height: int) -> None:
    """The sentence is drawn *beside* the settings, never instead of one."""
    plain = expire_ui.settings_body(width, height, JOB)
    told = expire_ui.settings_body(width, height, JOB, SAID)
    assert _heads(told) == _heads(plain)
    for letter in _keys():
        assert any(head.split()[0] == letter for head in _heads(told)), letter
    assert len(told) <= height - 6


@pytest.mark.parametrize(("width", "height"), SIZES)
def test_the_page_says_what_changed_at_both_sizes(width: int, height: int) -> None:
    """Both of the shapes in the report have room for the whole sentence."""
    told = _said(expire_ui.settings_body(width, height, JOB, SAID))
    assert told
    assert "".join(text.strip() for text in told).replace(" ", "") == SAID.replace(
        " ", ""
    )
    assert len(told) <= expire_ui.SAID_LINES


@pytest.mark.parametrize("width", [32, 40, 80])
def test_the_sentence_is_given_up_before_any_row(width: int) -> None:
    """Shorter and shorter: the said area goes, and every row is still there.

    Walked rather than sampled, because the failure is a height at which the
    rule inverts — one row of a six-setting page quietly traded for a message
    about a change already made — and one height cannot stand for the rest.
    """
    dropped = 0
    for height in range(12, 26):
        plain = expire_ui.settings_body(width, height, JOB)
        told = expire_ui.settings_body(width, height, JOB, SAID)
        assert _heads(told) == _heads(plain)
        if not _said(told):
            dropped += 1
            # Nothing is bought by dropping it that the rows do not get.
            assert [line for line in told if line[2] != ""] == [
                line for line in plain if line[2] != ""
            ]
    assert dropped, "no screen short enough to make the trade"


# --------------------------------------------------------------------------- #
# The screen itself
# --------------------------------------------------------------------------- #

#: Three queued downloads, headers only: the listing has to have something on
#: it for the page under test to be reached the way a person reaches it.
ITEMS = {
    "10-ubuntu-24-04.py": (
        "# EXPIRE: v1\n# EXPECT_BYTES: 6023000000\n# PARTIAL: yes\n"
        "# DESC: Ubuntu 24.04 desktop ISO\n"
    ),
    "15-big-iso.py": (
        "# EXPIRE: v1\n# EXPECT_BYTES: 8589934592\n# DESC: a very large image\n"
    ),
    "20-some-talk.py": (
        "# EXPIRE: v1\n# EXPECT_BYTES: 529530000\n"
        "# DESC: a talk nobody has watched yet\n"
    ),
}


def _checkout(tmp_path: Path) -> Path:
    """A queue root of this checkout's own modules, with three things queued.

    ``auto`` is stored off, so the switch the screen flips has somewhere to
    flip to and the listing behind it is not waiting on a portal to say so.
    """
    root = make_root(tmp_path, ITEMS)
    (root / "config.json").write_text('{\n  "auto": false\n}\n', encoding="utf-8")
    return root


@pytest.mark.tui
@pytest.mark.parametrize(("cols", "rows"), [(40, 20), (80, 24)])
def test_changing_a_setting_stays_on_the_settings_page(
    tmp_path: Path, cols: int, rows: int
) -> None:
    """``s`` then ``a``: the page is still the settings page, and it says so.

    Every assertion here is on the shape of the screen rather than on any
    sentence in it — which row, which keys, which page — because the wording of
    a setting is the sort of thing that improves and the fix is not about any
    of it. The one comparison against text reads the hint out of
    :func:`expire_ui.hint`, so a hint reworded stays a hint and only a hint
    *mangled* fails.
    """
    root = _checkout(tmp_path)
    shots, tail = drive(root, tmp_path, cols, rows, [b"s", b"a", b"q", b"q"])
    listing, settings, changed, back, _gone = shots

    assert "queue" in listing[0]
    assert "settings" in settings[0]

    # Still the settings page, with the hints it opened with.
    assert "settings" in changed[0]
    assert changed[-2].strip() == expire_ui.hint("settings", cols).strip()
    assert changed[-2] == settings[-2]

    # The switch flipped where it stands…
    assert row_for(changed, "a") != row_for(settings, "a")
    assert row_for(changed, "a").split()[:2] == ["a", "auto"]
    # …and the page said so, in the said area rather than over the hints: the
    # word is on the page twice now, its own row and the sentence under them.
    body = changed[1:-3]
    assert sum("auto" in line for line in body) >= 2
    # The foot's flash row is left empty — the sentence is too long for it, and
    # that row is where it used to be clipped over the legend keys.
    assert changed[-3].strip() == ""

    # q leaves, and the listing is the listing again — legend keys and all.
    assert "queue" in back[0]
    assert back[-3].strip() == expire_ui.LEGEND_KEYS
    for letter in ("n", "s", "l"):
        assert f"{letter} " in back[-3]
    assert back[-2].strip() == expire_ui.hint("list", cols).strip()

    # The receipt survives the screen: q on the listing tears curses down and
    # prints what the session changed.
    assert b"auto" in tail
