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

import contextlib
import fcntl
import os
import pty
import select
import shutil
import struct
import sys
import termios
import time
from pathlib import Path

import pyte
import pytest

# The queue's modules are flat siblings at the checkout root, not a package —
# the items in queue/ import them by bare name and so does this.
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

#: The modules a queue root needs to run its own screen out of.
MODULES = (
    "dlq.py",
    "expire_dl.py",
    "expire_runner.py",
    "expire_sched.py",
    "expire_ui.py",
    "ytdl_item.py",
)


class Term(pyte.Screen):
    """pyte, plus the two sequences ncurses scrolls a region with.

    ``CSI S`` and ``CSI T`` are how ncurses reuses lines that only moved, and
    pyte answers neither: fed a real redraw without them its screen keeps text
    the terminal has already scrolled away, and the test reads a page that no
    terminal ever showed. Both are :meth:`index` and :meth:`reverse_index`,
    which pyte does have and which honour the margins ncurses just set.
    """

    def scroll_up(self, count: int | None = None) -> None:
        for _ in range(count or 1):
            self.index()

    def scroll_down(self, count: int | None = None) -> None:
        for _ in range(count or 1):
            self.reverse_index()


pyte.Stream.csi["S"] = "scroll_up"
pyte.Stream.csi["T"] = "scroll_down"


def _checkout(tmp_path: Path) -> Path:
    """A queue root of this checkout's own modules, with three things queued."""
    root = tmp_path / "expire"
    root.mkdir()
    for name in MODULES:
        shutil.copy(REPO / name, root / name)
    for sub in ("queue", "done", "failed", "work", "out", "logs"):
        (root / sub).mkdir()
    # The one file `dlq status` proves a queue root by, so the screen opens.
    shutil.copy(REPO / "queue" / "README.md", root / "queue" / "README.md")
    for name, text in ITEMS.items():
        (root / "queue" / name).write_text(text, encoding="utf-8")
    (root / "config.json").write_text('{\n  "auto": false\n}\n', encoding="utf-8")
    return root


def _env(root: Path, home: Path, cols: int, rows: int) -> dict[str, str]:
    """The child's environment: this checkout's siblings, and no portal.

    The siblings are named outright rather than left to be found, because
    ``EXPIRE_HOME`` moves the queue root and the "beside this one" answer moves
    with it — the copy under a temporary directory has no ytq beside it. They
    are the checkouts this suite itself imported, so the screen under test is
    running against the same code the test is reading.

    ``HOME`` is the temporary directory and the two credential variables are
    unset, which is the whole of what keeps this offline: ``zwana_quota``'s
    ``.env`` lives under ``HOME``, and with no credentials anywhere the portal
    is never called at all. The screen says so at the top and goes on working,
    which is the phone off the vessel's wifi.
    """
    env = dict(os.environ)
    env.update(
        {
            "EXPIRE_HOME": str(root),
            "YTQ_HOME": str(Path(expire_ui.ytq.__file__).resolve().parent),
            "ZWANA_HOME": str(expire_ui.sched._zwana_root()),
            "HOME": str(home),
            "TERM": "xterm-256color",
            "LINES": str(rows),
            "COLUMNS": str(cols),
            "ESCDELAY": "25",
        }
    )
    for name in ("zwana_username", "zwana_password"):
        env.pop(name, None)
        env.pop(name.upper(), None)
    return env


def _drive(
    root: Path, home: Path, cols: int, rows: int, keys: list[bytes]
) -> tuple[list[list[str]], bytes]:
    """Open the screen on a pty and press *keys*, one at a time.

    Returns the display after each keypress — the opening screen first — and
    the raw bytes the last one produced, which is where the receipts land once
    curses has been torn down.
    """
    screen = Term(cols, rows)
    stream = pyte.ByteStream(screen)
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child is replaced immediately
        os.chdir(root)
        os.execve(
            sys.executable,
            [sys.executable, str(root / "expire_sched.py"), "ui"],
            _env(root, home, cols, rows),
        )
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    raw: list[bytes] = []

    def settle(quiet: float, limit: float = 8.0) -> None:
        """Read until the screen has been still for *quiet* seconds."""
        end = time.time() + limit
        last = time.time()
        while time.time() < end and time.time() - last < quiet:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                continue
            try:
                data = os.read(fd, 65536)
            except OSError:
                return
            if not data:
                return
            raw.append(data)
            stream.feed(data)
            last = time.time()

    shots: list[list[str]] = []
    try:
        settle(1.0)
        shots.append([line.rstrip() for line in screen.display])
        for key in keys:
            raw.clear()
            os.write(fd, key)
            settle(0.7)
            shots.append([line.rstrip() for line in screen.display])
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, 9)
        os.waitpid(pid, 0)
        os.close(fd)
    return shots, b"".join(raw)


def _row(shot: list[str], letter: str) -> str:
    """The settings row *letter* sets, as the screen has it."""
    for line in shot:
        if line.split()[:1] == [letter]:
            return line
    return ""


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
    shots, tail = _drive(root, tmp_path, cols, rows, [b"s", b"a", b"q", b"q"])
    listing, settings, changed, back, _gone = shots

    assert "queue" in listing[0]
    assert "settings" in settings[0]

    # Still the settings page, with the hints it opened with.
    assert "settings" in changed[0]
    assert changed[-2].strip() == expire_ui.hint("settings", cols).strip()
    assert changed[-2] == settings[-2]

    # The switch flipped where it stands…
    assert _row(changed, "a") != _row(settings, "a")
    assert _row(changed, "a").split()[:2] == ["a", "auto"]
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
