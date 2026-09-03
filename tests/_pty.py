"""A throwaway queue root, and the screen opened on a pty over it.

Two things the whole suite is built out of, kept here because both halves are
used from both sides of it: the tests that call the modules in this process
(through the ``dlq`` fixture in ``conftest``) and the tests that drive the real
``dlq ui`` under a terminal need the *same* queue root, or the screen under
test would not be the queue under test.

The root is a copy of this checkout's own modules with an empty queue tree
beside them. That is not a nicety either: :data:`expire_runner.ROOT` is the
directory the runner's own file sits in — an installed copy has to manage the
real queue, which is the reason it is spelled that way — so the only way to
root a runner somewhere else is to put a copy of it there. ``EXPIRE_HOME``
does the rest: it is what ``ytq._root`` answers with, and every path in
``expire_sched`` is spelled from that.

``HOME`` points at the temporary directory too, and that is the whole of what
keeps the suite offline: ``zwana_quota`` reads its credentials from
``$HOME/zwana-quota/.env`` and its session cookie from ``$HOME/.cache``, so
with neither there the portal is never called — it fails at the credentials,
before a socket is opened.
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

#: The checkout under test. Its modules are copied, never imported from here.
REPO = Path(__file__).resolve().parents[1]

#: The modules a queue root needs to run its own screen out of. ``ytdl_item``
#: is the shim pre-split items import; it is part of a queue root.
MODULES = (
    "dlq.py",
    "expire_dl.py",
    "expire_runner.py",
    "expire_sched.py",
    "expire_ui.py",
    "ytdl_item.py",
)

#: The directories the runner makes for itself. Made up front so a test that
#: only reads does not have to have written first.
SUBDIRS = ("queue", "done", "failed", "work", "out", "logs")


def sibling(name: str, env: str) -> Path:
    """A sibling checkout, resolved the way every module here resolves one.

    ``$YTQ_HOME`` / ``$ZWANA_HOME``, then a clone beside this one, then under
    ``~``. Spelled here rather than read off an imported module because the
    answer is wanted *before* anything is imported: it goes into the child's
    environment, and into this process's for the copy under the temporary
    root, which has no sibling beside it to be found.
    """
    override = os.environ.get(env)
    if override:
        return Path(override).expanduser().resolve()
    beside = REPO.parent / name
    if beside.is_dir():
        return beside.resolve()
    return Path.home() / name


YTQ = sibling("ytq", "YTQ_HOME")
ZWANA = sibling("zwana-quota", "ZWANA_HOME")


def make_root(where: Path, items: dict[str, str] | None = None) -> Path:
    """A queue root of this checkout's modules, with *items* queued in it."""
    root = where / "expire"
    root.mkdir(parents=True, exist_ok=True)
    for name in MODULES:
        shutil.copy(REPO / name, root / name)
    # The runner's shebang is Termux's absolute python, which is the right
    # answer on the phone — ``#!/usr/bin/env python3`` is the portable form
    # everywhere else and is the one thing Android cannot exec — and it is not
    # on this machine. A checkout whose runner the scheduler could not start is
    # a broken checkout, and this one is not being used to test that, so it is
    # written the way a checkout here would be. ``shebang_problem`` is checked
    # against a bad one on purpose, elsewhere.
    runner = root / "expire_runner.py"
    lines = runner.read_text().splitlines(keepends=True)
    lines[0] = f"#!{sys.executable}\n"
    runner.write_text("".join(lines))
    runner.chmod(0o755)
    for sub in SUBDIRS:
        (root / sub).mkdir(exist_ok=True)
    # The one file `root_problem` proves a queue root by, so the screen opens.
    shutil.copy(REPO / "queue" / "README.md", root / "queue" / "README.md")
    for name, text in (items or {}).items():
        (root / "queue" / name).write_text(text, encoding="utf-8")
    return root


def env(root: Path, home: Path, cols: int = 40, rows: int = 24) -> dict[str, str]:
    """The child's environment: this checkout's siblings, and no portal.

    The siblings are named outright rather than left to be found, because
    ``EXPIRE_HOME`` moves the queue root and the "beside this one" answer moves
    with it — the copy under a temporary directory has no ytq beside it.

    ``HOME`` is the temporary directory and the two credential variables are
    unset, which is what keeps this offline. The screen says the portal did not
    answer and goes on working, which is the phone off the vessel's wifi.
    """
    out = dict(os.environ)
    out.update(
        {
            "EXPIRE_HOME": str(root),
            "YTQ_HOME": str(YTQ),
            "ZWANA_HOME": str(ZWANA),
            "HOME": str(home),
            "TERM": "xterm-256color",
            "LINES": str(rows),
            "COLUMNS": str(cols),
            "ESCDELAY": "25",
        }
    )
    for name in ("zwana_username", "zwana_password"):
        out.pop(name, None)
        out.pop(name.upper(), None)
    return out


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

#: What an arrow key sends in application mode, which is what curses puts the
#: terminal into. The cursor-key form (``\x1bOA``), not the normal one.
UP = b"\x1bOA"
DOWN = b"\x1bOB"
ENTER = b"\r"


def drive(
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
            env(root, home, cols, rows),
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


def row_for(shot: list[str], letter: str) -> str:
    """The row whose first word is *letter* — a settings or destination key."""
    for line in shot:
        if line.split()[:1] == [letter]:
            return line
    return ""
