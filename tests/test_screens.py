"""The listing, driven on a real terminal.

The front ends *are* their screens: a page that leaves when it should stay, a
cut line drawn over a row, a move that says it happened and renames nothing —
none of those show in a return value. So these open the real ``dlq ui`` on a
pty, read it back through a terminal emulator, and press the keys somebody
would press.

Everything asserted is *structure*: which screen is up, according to the title
bar; that a row for a download exists; that the keys are on the key row; that
after a move the file on disk has a smaller number than its neighbour. Nothing
here knows what any line says, because the wording is the part that improves.

They are marked ``tui`` and are most of the suite's wall clock; the mutation
run skips them for that reason, and the code they drive is fenced out of it.
"""

from __future__ import annotations

import pytest

from _pty import DOWN, ENTER, drive, make_root

pytestmark = pytest.mark.tui

#: Three downloads, headers only, ten apart. The slugs are distinct at their
#: first letter so a row can be found on a 40-column screen.
ITEMS = {
    "10-alpha.py": "# EXPIRE: v1\n# EXPECT_BYTES: 6023000000\n# PARTIAL: yes\n",
    "20-bravo.py": "# EXPIRE: v1\n# EXPECT_BYTES: 529530000\n# PARTIAL: yes\n",
    "30-charlie.py": "# EXPIRE: v1\n# EXPECT_BYTES: 100000000\n# PARTIAL: yes\n",
}


def order_on_disk(root):
    """The queue as the runner would work it: file names, sorted."""
    return [
        path.name.split("-", 1)[1].removesuffix(".py")
        for path in sorted((root / "queue").glob("*.py"))
    ]


def row_with(shot, text):
    """The line the download *text* is drawn on, or ``""``."""
    return next((line for line in shot if text in line), "")


def test_the_listing_opens_on_the_queue_with_its_keys_on_it(pty_root):
    """A bare ``dlq`` lands here, and everything the old queue screen did is
    behind the three keys on the legend row."""
    root = make_root(pty_root.root.parent, ITEMS)
    shots, _ = drive(root, pty_root.home, 40, 24, [b"q"])
    listing = shots[0]

    assert "queue" in listing[0]
    for slug in ("alpha", "bravo", "charlie"):
        assert row_with(listing, slug)
    # The three keys that act on the whole queue, and the hints under them.
    legend = listing[-3]
    for letter in ("n", "s", "l"):
        assert f"{letter} " in legend
    assert listing[-2].strip()


def test_the_cut_line_is_drawn_through_the_queued_group(pty_root):
    """With no portal there is nothing to spend, so the line goes to the top of
    the group and says why — which is the only place that answer can be."""
    root = make_root(pty_root.root.parent, ITEMS)
    # After a keypress rather than on the opening screen: the reading is taken
    # off the screen's own thread, so the listing opens saying it is asking and
    # fills the answer in when it lands.
    shots, _ = drive(root, pty_root.home, 40, 24, [DOWN, b"q"])
    listing = shots[1]

    ruled = [line for line in listing if "─" in line]
    assert ruled, listing
    # It is drawn *inside* the queued group: above the first download and below
    # the heading that counts them.
    where = listing.index(ruled[0])
    assert where < listing.index(row_with(listing, "alpha"))
    assert "queued" in "\n".join(listing[:where])


def test_moving_a_download_renames_it_and_says_where_it_went(pty_root):
    """``m``, ↓, ⏎ — and the file on disk has a smaller number afterwards.

    The whole of what a move means is where the item is in the run order, and
    the run order is the file names: a screen that reordered its own list and
    renamed nothing would look exactly like this one and download in the order
    it always did.
    """
    root = make_root(pty_root.root.parent, ITEMS)
    assert order_on_disk(root) == ["alpha", "bravo", "charlie"]

    shots, tail = drive(
        root, pty_root.home, 40, 24, [b"m", DOWN, ENTER, b"q"]
    )
    _, held, moved, dropped, _gone = shots

    # Held: the title bar says so, and it names what is in the air.
    assert "moving" in held[0]
    assert "alpha" in held[0]
    # Dropped: back on the listing, and the queue is in the new order.
    assert "queue" in dropped[0]
    assert order_on_disk(root) == ["bravo", "alpha", "charlie"]
    # And the session says what it changed, once curses is down.
    assert b"alpha" in tail


def test_a_move_that_is_thought_better_of_costs_nothing(pty_root):
    """esc leaves it where it was, and nothing was renamed to find that out."""
    root = make_root(pty_root.root.parent, ITEMS)
    before = order_on_disk(root)
    shots, _ = drive(root, pty_root.home, 40, 24, [b"m", DOWN, b"\x1b", b"q"])
    assert "moving" in shots[1][0]
    assert "queue" in shots[3][0]
    assert order_on_disk(root) == before


def test_enter_opens_the_download_and_q_comes_back(pty_root):
    """The list picks and the item screen acts: every key that changes a
    download is on a screen showing that download and nothing else."""
    root = make_root(pty_root.root.parent, ITEMS)
    shots, _ = drive(root, pty_root.home, 40, 24, [ENTER, b"q", b"q"])
    listing, item, back, _gone = shots

    assert "queue" in listing[0]
    # The item screen names the download it is about in its title bar.
    assert "alpha" in item[0]
    assert "queue" not in item[0]
    # Its actions are spelled out in words, one key each.
    keys = [line.strip()[0] for line in item[2:-4] if line.strip()[1:2] == " "]
    for letter in ("d", "l"):
        assert letter in keys
    assert "queue" in back[0]


def test_the_settings_page_is_a_key_away_from_the_listing(pty_root):
    """``s`` opens it; ``q`` comes back to the listing it was opened from."""
    root = make_root(pty_root.root.parent, ITEMS)
    shots, _ = drive(root, pty_root.home, 40, 24, [b"s", b"q", b"q"])
    listing, settings, back, _gone = shots
    assert "settings" in settings[0]
    assert "queue" in listing[0] and "queue" in back[0]
    # Every setting and both page rows answer to a key, and each is named.
    letters = [line.strip()[0] for line in settings if line.strip()[1:3] == "  "]
    for letter in "wrpman":
        assert letter in letters
