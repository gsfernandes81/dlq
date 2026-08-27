#!/data/data/com.termux/files/usr/bin/python3
"""Find a video and queue it for the expiring-quota runner, at a measured size.

``EXPECT_BYTES`` is the cap the runner enforces against an item, and the queue
contract says plainly that "unknown" is not a valid answer for it. Guessing it
from a resolution is how an item ends up either refused every night for being
too big or killed by the interface watchdog for being bigger than it claimed.

So this asks yt-dlp. Paste a URL, it runs a metadata-only extraction (~0.1-0.5 MB
of internet data and no media), lists every format with the size yt-dlp reports
for it, and writes a queue item with that figure already in the header. The
number in the item is then a measurement with a stated overhead margin, not an
estimate.

It also searches. One flat search is a single request (~0.1 MB) that answers
with a title, a channel, a length and an approximate age per result; picking one
probes it exactly as a pasted URL would. The entry field takes either, and tells
them apart by looking, so there is one box rather than two.

Usage::

    ytq                      # search, or paste a URL into the same field
    ytq crust of rust        # straight to the results
    ytq <url>                # straight to the format list
    ytq --list <url>         # print the formats, write nothing
    ytq --now <url>          # open the format list ready to download now

The item it writes runs yt-dlp per firing (see :mod:`ytdl_item`); it never
resolves a media URL up front, because those are signed and expire in hours,
which is shorter than the queue takes to work through a large video.

Downloading now still writes that item first and then asks ``dlqd`` to run it,
rather than waiting for the window. Downloading without the queue would have
been less code and worse: this way an interrupted download resumes instead of
restarting, the nightly window finishes anything that stops early, and
``dlqd list`` knows about it like everything else. It is mobile data spent now,
though, which the nightly window is not.

That run is handed to a detached process rather than held in the foreground, so
choosing it does not end the session: the screen goes back to the results with
the download reporting its progress along the bottom.
"""

from __future__ import annotations

import argparse
import ast
import curses
import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ytdl_item  # noqa: E402  (sibling module, path fixed up above)
import contextlib

#: Where the queue lives when this module is not being run from inside it.
INSTALLED_ROOT = "~/or3/termux/expire"


def _root() -> Path:
    """The directory the runner works out of.

    Deliberately not just this file's directory. ``uv tool install`` without
    ``--editable`` copies these modules into a venv, and an item written next to
    that copy would sit in site-packages where the nightly runner never looks —
    queued, apparently fine, and never downloaded. A real queue root is the one
    holding the queue's own contract file; ``EXPIRE_HOME`` overrides for a
    checkout kept somewhere else.
    """
    override = os.environ.get("EXPIRE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve().parent
    if (here / "queue" / "README.md").is_file():
        return here
    return Path(INSTALLED_ROOT).expanduser().resolve()


HERE = _root()
QUEUE = HERE / "queue"
STAGING = QUEUE / ".staging"
DONE = HERE / "done"
FAILED = HERE / "failed"

#: The runner rejects an item whose interpreter is not on disk, and on Termux
#: /usr/bin/env is not. Written literally rather than from sys.executable so a
#: run under some other interpreter cannot emit an item that will not start.
SHEBANG = "#!/data/data/com.termux/files/usr/bin/python3"


def shebang_here() -> bool:
    """Whether the interpreter every item names actually exists on this machine.

    True on the phone and false everywhere else, which is the point:
    :data:`SHEBANG` is written literally rather than from ``sys.executable`` so
    that a run under some other interpreter cannot emit an item that will not
    start on the phone — and the flip side of that is that off Termux the
    runner's parser is *right* to refuse an item it would run there perfectly.

    So a check that asks the runner for a verdict asks this first and expects
    that one objection instead of none. Every other way of malforming an item
    stays a failure; what is not asserted is a fact about the machine the check
    happens to be running on.
    """
    return Path(SHEBANG[2:].strip()).exists()

#: Payload bytes are not wire bytes, and the item pays for one metadata
#: extraction per firing on top. Applied to the size yt-dlp reports.
OVERHEAD_EXACT = 1.03
OVERHEAD_APPROX = 1.12
OVERHEAD_FIXED = 4 * 1024 * 1024

#: Below the runner's 32 MiB default, because an extraction costs the item
#: ~0.1-0.5 MB whatever the slice is, and a 16 MiB slice still pays for itself.
SLICE_MIN_BYTES = 16 * 1024 * 1024

ITEM_RE = re.compile(r"^(\d{2,})-")

#: The highest priority a new item may be given, and the reason every one of
#: them is written with two digits.
#:
#: The runner takes its items in **file name** order, which is a string sort —
#: so ``100`` sorts before ``20`` and an item numbered past 99 does not go to
#: the back of the queue, it goes to the front. Zero-padded to two, string
#: order and number order are the same thing, and every screen that talks about
#: "lower runs first" is telling the truth. ``dlqd ui``'s reorder hands out
#: fresh two-digit keys when the room between two items runs out, which is also
#: what repairs a queue that already has three-digit ones in it.
MAX_PRIORITY = 99

#: The width the full-detail layouts need. Below it the format list drops the
#: format id and the codec detail, which are the two columns nobody chooses on,
#: and the confirm screen drops to the figures alone. Termux in portrait is
#: around 40 columns; the size and the label are what a choice is made from.
#: Matches ``expire_sched.WIDE``, so one terminal does not get two answers
#: about whether it is wide.
WIDE = 72

#: The key hints along the bottom of each screen, and the room they get. They
#: are drawn at x=1 and clipped at ``width - 1``, so a 40-column phone shows 38
#: columns of them — and a hint that does not fit is not a cosmetic problem,
#: it is the line saying how to get out of the screen, with the way out cut off.
HINT_WIDTH = 38
HINTS = {
    "entry": "⏎ go   esc quit",
    "results": "↑↓ pick  ⏎ quality  / new  q back",
    # Replaces the results hints while a download this session started is still
    # going: the key that stops it is worth more room than the key that starts
    # another search, and both do not fit.
    "running": "x stop  ↑↓ pick  ⏎ quality  q back",
    "pick": "↑↓ pick  ⏎ queue  n now  q back  ~ est",
    "pick-now": "↑↓ pick  ⏎ now PAID  t queue  q back",
    "queue": "⏎ queue  e edit  n now  q back",
    "now": "⏎ start PAID  e edit  t queue  q back",
    "watch": "x stop  q back",
}

#: And below 40 there are only 30 of them, so a second and shorter set rather
#: than a clipped first one — the same reason the listing has three shapes and
#: not one scaled table. What each of these drops is chosen on the rule above:
#: the word that must never be the one clipped off the end is the way out.
TIGHT = 40
TIGHT_WIDTH = 30
TIGHT_HINTS = {
    "entry": "⏎ go  esc quit",
    "results": "↑↓  ⏎ formats  / new  q back",
    "running": "x stop  ↑↓  ⏎ formats  q back",
    "pick": "↑↓  ⏎ queue  n now  q back",
    "pick-now": "↑↓  ⏎ now PAID  q back",
    "queue": "⏎ queue  e edit  n now  q back",
    "now": "⏎ start PAID  t queue  q back",
    "watch": "x stop  q back",
}


def hint(name: str, width: int) -> str:
    """The key hints for a screen, at whatever width there is for them."""
    return (TIGHT_HINTS if width < TIGHT else HINTS)[name]


#: How many results one search asks for. One request either way — paging costs
#: double, because ``ytsearch40`` re-fetches the first twenty to reach the rest,
#: so a better query is always cheaper than a second page.
SEARCH_RESULTS = 20

#: ``--flat-playlist`` answers a search in one round trip, but YouTube's flat
#: entries carry no upload date at all. This asks yt-dlp to parse the relative
#: string the search page does show ("4 months ago") into a timestamp. It is
#: approximate by construction, which is why :func:`age` always marks it.
APPROX_DATE_ARGS = ("--extractor-args", "youtubetab:approximate_date")

#: The free grant is ~763 MiB a day and the runner keeps 100 MB of it back, so
#: this is roughly what one night can spend. Used only to colour the size
#: column — green fits comfortably in a night, amber is most of one, red will
#: take several. That is the fact the list does not otherwise state, and it is
#: the one that decides whether a choice is a good idea on this connection.
NIGHT_BYTES = 650 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #


class ProbeError(RuntimeError):
    """yt-dlp could not describe the URL."""


def probe(url: str, timeout: int = 180) -> dict:
    """Metadata only — ``-J`` downloads no media."""
    argv = [
        *ytdl_item.ytdl_argv(),
        "-J",
        "--no-playlist",
        "--no-colors",
        "--no-warnings",
        url,
    ]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ProbeError("yt-dlp is not installed (pip install yt-dlp)")
    except subprocess.TimeoutExpired:
        raise ProbeError(f"yt-dlp did not answer within {timeout}s")
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        raise ProbeError(tail[-1] if tail else f"yt-dlp exited {done.returncode}")
    try:
        info = json.loads(done.stdout)
    except ValueError:
        raise ProbeError("yt-dlp returned something that is not JSON")
    if info.get("_type") == "playlist":
        raise ProbeError("that URL is a playlist; queue one video at a time")
    return info


# --------------------------------------------------------------------------- #
# Searching
# --------------------------------------------------------------------------- #


class Result:
    """One search hit: enough to choose on, and the URL to probe if chosen."""

    def __init__(
        self,
        title: str,
        channel: str,
        url: str,
        duration: int | None,
        timestamp: int | None,
        live: bool = False,
        key: str = "",
    ) -> None:
        self.title = title
        self.channel = channel
        self.url = url
        self.duration = duration
        self.timestamp = timestamp
        self.live = live
        #: What :func:`find_duplicate` recognises this by, so the list can mark
        #: what is already queued *before* anything is spent probing it.
        self.key = key


def looks_like_url(text: str) -> bool:
    """Whether the entry field holds a link rather than words to search for.

    One field for both, because on a phone the alternative is a mode key to
    remember. Deliberately generous about what counts as a link and strict
    about nothing: the cost of guessing wrong is one wasted extraction either
    way, and yt-dlp gives a clearer error about a bad URL than a search for it
    would.
    """
    text = text.strip()
    if not text or " " in text:
        return False
    return "://" in text or text.startswith(("www.", "youtu.be/", "youtube.com/"))


def search_argv(query: str, count: int = SEARCH_RESULTS) -> list[str]:
    """The command a search runs, kept separate so it can be checked.

    ``--flat-playlist`` is the whole cost argument: it answers from the search
    page alone, in one request, instead of extracting each result in turn — the
    difference between ~0.1 MB and twenty full extractions on a metered radio.
    The self-test pins this, because the shape that is expensive looks almost
    identical to the shape that is not.

    Nothing here disables the user config: ``~/.config/yt-dlp/config`` carries
    the JS runtime and the cookies without which YouTube answers with a
    fraction of what it has, or refuses.
    """
    return [
        *ytdl_item.ytdl_argv(),
        f"ytsearch{count}:{query}",
        "--flat-playlist",
        "-J",
        "--no-colors",
        "--no-warnings",
        *APPROX_DATE_ARGS,
    ]


def entries(info: dict) -> list[Result]:
    """The videos in a search answer, tolerating every field being absent.

    A search can also come back holding channels and playlists, and neither has
    a format to pick from — they are dropped here rather than at the moment
    someone selects one and gets an error about a playlist.
    """
    out: list[Result] = []
    for raw in info.get("entries") or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("_type") in ("playlist", "channel"):
            continue
        video_id = raw.get("id")
        url = raw.get("url") or raw.get("webpage_url")
        if not url and video_id:
            url = f"https://www.youtube.com/watch?v={video_id}"
        if not url:
            continue
        duration = raw.get("duration")
        timestamp = raw.get("timestamp") or raw.get("release_timestamp")
        out.append(
            Result(
                title=(raw.get("title") or "(untitled)").strip() or "(untitled)",
                channel=(raw.get("channel") or raw.get("uploader") or "").strip(),
                url=str(url),
                duration=int(duration) if isinstance(duration, (int, float)) else None,
                timestamp=int(timestamp)
                if isinstance(timestamp, (int, float))
                else None,
                live=raw.get("live_status") in ("is_live", "is_upcoming"),
                key=source_key(raw),
            )
        )
    return out


def search(query: str, count: int = SEARCH_RESULTS, timeout: int = 120) -> list[Result]:
    """Ask yt-dlp for *count* results. One request, no media, no per-video work."""
    try:
        done = subprocess.run(
            search_argv(query, count), capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise ProbeError("yt-dlp is not installed (pip install yt-dlp)")
    except subprocess.TimeoutExpired:
        raise ProbeError(f"yt-dlp did not answer within {timeout}s")
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        raise ProbeError(tail[-1] if tail else f"yt-dlp exited {done.returncode}")
    try:
        info = json.loads(done.stdout)
    except ValueError:
        raise ProbeError("yt-dlp returned something that is not JSON")
    if not isinstance(info, dict):
        raise ProbeError("yt-dlp returned something that is not a search answer")
    return entries(info)


def age(timestamp: int | None, now: float | None = None) -> str:
    """How long ago, roughly: ``<1d``, ``~3w``, ``~4mo``, ``~5y``, or ``?``.

    Relative rather than a date, and marked, because that is the precision we
    actually have. The timestamp comes from yt-dlp parsing YouTube's own
    rounded string ("4 months ago"), so ``2026-04-13`` would be a claim the
    number cannot support — the same reason the size column marks an estimate
    with ``~`` instead of printing it plain.

    ``?`` is a fact too: a search answer with no date must not be given one.
    """
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return "?"
    days = int(max(0.0, (time.time() if now is None else now) - timestamp) // 86400)
    if days < 1:
        return "<1d"
    if days < 14:
        return f"~{days}d"
    if days < 56:
        return f"~{days // 7}w"
    if days < 730:
        return f"~{days // 30}mo"
    return f"~{days // 365}y"


def clock(seconds: int | None, live: bool = False) -> str:
    """A length in the one spelling this repo uses, or what it is instead."""
    if live:
        return "live"
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "?"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


# --------------------------------------------------------------------------- #
# Formats
# --------------------------------------------------------------------------- #


class Choice:
    """One selectable download: a format string and what it will cost."""

    def __init__(
        self,
        kind: str,
        fmt: str,
        size: int,
        exact: bool,
        ext: str,
        label: str,
        detail: str,
        merge_ext: str | None = None,
    ) -> None:
        self.kind = kind
        self.fmt = fmt
        self.size = size
        self.exact = exact
        self.ext = ext
        self.label = label
        self.detail = detail
        self.merge_ext = merge_ext

    @property
    def expect_bytes(self) -> int:
        """The cap to declare: the measurement, plus a stated margin."""
        factor = OVERHEAD_EXACT if self.exact else OVERHEAD_APPROX
        return int(math.ceil(self.size * factor)) + OVERHEAD_FIXED


def _size_of(fmt: dict) -> tuple[int, bool]:
    """``(bytes, exact)``. Zero means yt-dlp would not say."""
    size = fmt.get("filesize")
    if isinstance(size, (int, float)) and size > 0:
        return int(size), True
    size = fmt.get("filesize_approx")
    if isinstance(size, (int, float)) and size > 0:
        return int(size), False
    return 0, False


def _family(ext: str) -> str:
    if ext in ("mp4", "m4a"):
        return "mp4"
    if ext in ("webm", "opus"):
        return "webm"
    return ext or "?"


def _video_label(fmt: dict) -> str:
    height = fmt.get("height")
    where = f"{height}p" if height else (fmt.get("format_note") or "video")
    fps = fmt.get("fps")
    if fps and fps > 30:
        where += f"{int(fps)}"
    return where


def _codec(name: str | None) -> str:
    if not name or name == "none":
        return "-"
    return name.split(".")[0]


def choices(info: dict) -> tuple[list[Choice], int]:
    """Selectable downloads, best first, plus a count of unsized formats.

    Formats yt-dlp will not put a size on are dropped rather than offered with
    a guessed cap, because that cap is the only thing standing between a
    mis-sized item and the runner's watchdog killing it every night.
    """
    formats = [
        f
        for f in (info.get("formats") or [])
        if f.get("ext") != "mhtml" and f.get("format_id")
    ]
    unsized = 0

    videos, audios, progressive = [], [], []
    for fmt in formats:
        # A missing codec means yt-dlp does not know, which is not the same as
        # the string "none" meaning the stream is absent. Only the explicit
        # "none" on both is a storyboard rather than something playable.
        vcodec, acodec = fmt.get("vcodec"), fmt.get("acodec")
        if vcodec == "none" and acodec == "none":
            continue
        size, exact = _size_of(fmt)
        if not size:
            unsized += 1
            continue
        entry = (fmt, size, exact)
        if acodec == "none" and vcodec != "none":
            videos.append(entry)
        elif vcodec == "none" and acodec != "none":
            audios.append(entry)
        else:
            progressive.append(entry)

    # One best audio per container family, so a merge does not have to transcode
    # or fall back to Matroska when it does not need to.
    best_audio: dict[str, tuple[dict, int, bool]] = {}
    for fmt, size, exact in audios:
        family = _family(fmt.get("ext") or "")
        current = best_audio.get(family)
        if current is None or (fmt.get("abr") or 0) > (current[0].get("abr") or 0):
            best_audio[family] = (fmt, size, exact)
    overall_audio = max(audios, key=lambda e: e[0].get("abr") or 0, default=None)

    out: list[Choice] = []

    for fmt, size, exact in progressive:
        out.append(
            Choice(
                "single",
                str(fmt["format_id"]),
                size,
                exact,
                fmt.get("ext") or "mp4",
                f"{_video_label(fmt)} {fmt.get('ext')}",
                f"one file, {_codec(fmt.get('vcodec'))}+{_codec(fmt.get('acodec'))}",
            )
        )

    for fmt, size, exact in videos:
        family = _family(fmt.get("ext") or "")
        pair = best_audio.get(family) or overall_audio
        if pair is None:
            continue
        afmt, asize, aexact = pair
        merge = (
            "mp4"
            if family == "mp4" and _family(afmt.get("ext") or "") == "mp4"
            else (
                "webm"
                if family == "webm" and _family(afmt.get("ext") or "") == "webm"
                else "mkv"
            )
        )
        out.append(
            Choice(
                "merge",
                f"{fmt['format_id']}+{afmt['format_id']}",
                size + asize,
                exact and aexact,
                merge,
                f"{_video_label(fmt)} {merge}",
                f"{_codec(fmt.get('vcodec'))} + "
                f"{_codec(afmt.get('acodec'))} {int(afmt.get('abr') or 0)}k, merged",
                merge_ext=merge,
            )
        )

    for fmt, size, exact in audios:
        out.append(
            Choice(
                "audio",
                str(fmt["format_id"]),
                size,
                exact,
                fmt.get("ext") or "m4a",
                f"audio {int(fmt.get('abr') or 0)}k {fmt.get('ext')}",
                f"{_codec(fmt.get('acodec'))}, no video",
            )
        )

    rank = {"single": 0, "merge": 0, "audio": 1}
    out.sort(key=lambda c: (rank[c.kind], -c.size))
    return out, unsized


# --------------------------------------------------------------------------- #
# Writing the item
# --------------------------------------------------------------------------- #


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB"):
        if abs(n) < 1024:
            return f"{n:,.0f} {unit}"
        n /= 1024
    return f"{n:,.2f} GiB"


def literal(value: str | None) -> str:
    """*value* as Python source: a quoted string, or ``None``.

    :func:`json.dumps` alone is right for the strings — it is what stops a
    title full of quotes from writing an item that will not parse — and wrong
    for ``None``, which it spells ``null``. That spelling *parses*, so every
    check that stops at "does this file compile" passes it, and the item dies
    with ``NameError`` on the night it was finally due to run.
    """
    return "None" if value is None else json.dumps(value)


def json_leaks(source: str) -> list[str]:
    """JSON spellings of Python values left in *source*, for the self-tests.

    ``null``, ``true`` and ``false`` are all valid Python *names*, so an item
    holding one parses, compiles and imports; it fails only when the line is
    reached, which is at the head of the queue on the night it was queued for.
    A check that the item compiles cannot see this, so this is the check.
    """
    return sorted(
        node.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name) and node.id in ("null", "true", "false")
    )


def slugify(title: str, limit: int = 42) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "video").lower()).strip("-")
    return slug[:limit].rstrip("-") or "video"


#: The header that says what an item is a download *of*, so that queueing the
#: same video twice can be noticed before it is paid for twice.
SOURCE_RE = re.compile(r"^#\s*SOURCE\s*:\s*(.+?)\s*$")


def source_key(info: dict) -> str:
    """What this download *is*, as an extractor and an id.

    Not the URL: one video has many of them — ``youtu.be/x``,
    ``watch?v=x``, ``watch?v=x&list=…`` — and the one a search hands back is
    routinely not the one somebody pastes. yt-dlp's id is stable per extractor
    and both halves of ytq have it in hand already, the search from its flat
    entries and the probe from the full answer.

    Empty when there is no id to key on, which is not an error: it means this
    item can only be recognised again by its name, and :func:`find_duplicate`
    says as much when it matches one that way.
    """
    ident = info.get("id")
    if not ident:
        return ""
    who = (
        info.get("ie_key")
        or info.get("extractor_key")
        or info.get("extractor")
        or "video"
    )
    return f"{str(who).lower()}:{ident}"


def source_of(text: str) -> str:
    """The ``SOURCE`` an item declares, read from its header and nowhere else.

    Stops at the first line that is not a comment, the way the runner's own
    parser does: everything below is the item's docstring and its code, and a
    URL quoted in either is not a claim about what the item is.
    """
    for line in text.splitlines():
        if line.startswith("#!"):
            continue
        if not line.startswith("#"):
            break
        found = SOURCE_RE.match(line)
        if found:
            return found.group(1)
    return ""


def items() -> list[tuple[str, Path]]:
    """``(where, path)`` for every item the queue holds, in any state.

    Spelled here rather than taken from ``expire_sched._paths``, which does the
    same walk, because that module imports *this* one — the dependency only
    runs one way, and a front end that could not queue without the manager
    being importable would be a worse trade than one walk written twice.
    """
    found: list[tuple[str, Path]] = []
    for where, directory in (("queued", QUEUE), ("done", DONE), ("failed", FAILED)):
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if any(part.startswith(".") for part in path.relative_to(directory).parts):
                continue
            if ITEM_RE.match(path.name) and path.is_file():
                found.append((where, path))
    return found


class Duplicate(RuntimeError):
    """This download is already in the queue, or has already been made.

    Carried as an exception because :func:`write_item` is the one door every
    way of queueing goes through, and a door is the only place a rule like this
    can be enforced rather than remembered. Every screen catches it and says it
    in its own words.
    """

    def __init__(self, path: Path, where: str, how: str) -> None:
        self.path, self.where, self.how = path, where, how
        self.name = path.name
        super().__init__(f"{self.says()} — {self.stem}")

    @property
    def stem(self) -> str:
        return self.name[:-3] if self.name.endswith(".py") else self.name

    def says(self) -> str:
        """The verdict in one short line, fit for the narrowest screen."""
        same = "same name," if self.how == "name" else ""
        if self.where == "done":
            day = self.path.parent.name
            when = day if re.match(r"^\d{4}-\d{2}-\d{2}$", day) else ""
            # "done" rather than "downloaded" once the name qualifier is on the
            # front of it: the two together are two columns wider than the
            # narrowest screen, and this is the line that says why.
            got = f"{same} done {when}" if same else f"downloaded {when}"
            return got.strip() or "already downloaded"
        if self.where == "failed":
            return f"{same} tried and failed".strip() if same else "tried, and failed"
        return f"{same} already queued".strip()


def find_duplicate(key: str, slug: str) -> Duplicate | None:
    """The item this download would be a second copy of, if there is one.

    Two ways of being the same thing, and they are not equally strong. A
    matching ``SOURCE`` is the same video by id, wherever it was queued from
    and whatever it was called. A matching *name* is the fallback for items
    written before ``SOURCE`` existed, and for anything else that has no id: it
    is the same title, which is usually the same video and occasionally is not
    — so it is reported as what it is, and never silently.

    The queue is searched in the order the answer matters: still waiting, then
    already downloaded, then given up on.
    """
    tail = f"-{slugify(slug)}.py" if slug else ""
    named: Duplicate | None = None
    for where, path in items():
        if key:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:4096]
            except OSError:
                text = ""
            if text and source_of(text) == key:
                return Duplicate(path, where, "source")
        if tail and named is None and path.name.endswith(tail):
            named = Duplicate(path, where, "name")
    return named


def next_number() -> int:
    """Ten past the highest number ever used, leaving room to insert ahead.

    Files only. ``done/`` is a tree of day directories named ``2026-08-08``,
    which matches an item's number-and-dash exactly as well as an item does —
    counting one takes the next number to 2036 and every item after it with no
    sign that anything went wrong.

    Never past :data:`MAX_PRIORITY`, because the tenth item queued would
    otherwise be ``100``, which sorts *before* ``20``: a new download would go
    to the head of the queue rather than the tail, and the only symptom would
    be things running in the wrong order. At the cap new items share the last
    key and are ordered by their slugs; ``dlqd ui``'s reorder is what spreads
    them out again.
    """
    highest = 0
    for _, path in items():
        found = ITEM_RE.match(path.name)
        if found:
            highest = max(highest, int(found.group(1)))
    return min(highest + 10, MAX_PRIORITY) if highest else 10


def render(
    url: str,
    slug: str,
    choice: Choice,
    title: str,
    probed: str,
    dest: str = "video",
    key: str = "",
) -> str:
    """The item source. Strings go through :func:`json.dumps` so that a title
    full of quotes cannot produce a file that does not parse."""
    safe_title = title.replace("\\", "/").replace('"""', "'''").strip()
    desc = f"{safe_title} [{choice.label}] ({human(choice.size)} via yt-dlp)"
    margin = "3%" if choice.exact else "12%"
    sizing = textwrap.fill(
        f"Format {choice.fmt} — {choice.detail}. yt-dlp reported "
        f"{choice.size:,} bytes "
        f"{'exactly' if choice.exact else 'approximately'} when this was queued "
        f"on {probed}; EXPECT_BYTES is that figure plus a {margin} margin and "
        f"{human(OVERHEAD_FIXED)} for the per-firing metadata extractions, "
        f"retries and container overhead.",
        78,
    )
    # SOURCE is what this item is a download *of*, and it is written whether or
    # not anything reads it today: an item queued now is what tomorrow's
    # duplicate check has to recognise, and it cannot be added to a file that
    # has already been written.
    source = f"\n# SOURCE: {key}" if key else ""
    return f'''{SHEBANG}
# EXPIRE: v1
# EXPECT_BYTES: {choice.expect_bytes}
# PARTIAL: yes
# SLICE_MIN_BYTES: {SLICE_MIN_BYTES}
# DEST: {dest}{source}
# DESC: {desc[:160]}
"""{safe_title}

{url}

{sizing}

yt-dlp is invoked on every firing rather than resolving a media URL once,
because those URLs are signed and expire in about six hours — far less than the
queue may take to work through a video this size.
"""

import sys

sys.path.insert(0, {json.dumps(str(HERE))})
import ytdl_item  # noqa: E402

sys.exit(ytdl_item.run(
    url={json.dumps(url)},
    name={json.dumps(slug)},
    fmt={json.dumps(choice.fmt)},
    total_hint={choice.size},
    merge_ext={literal(choice.merge_ext)},
))
'''


def write_item(number: int, slug: str, source: str, again: bool = False) -> Path:
    """Stage, make executable, then rename into the queue.

    The runner scans the queue directory on a timer, so a file must never
    appear there until it is complete and executable.

    **This is where the duplicate check lives**, and it lives here because it
    is the one door: the search, a pasted URL, ``--now``, ``--from-json`` and
    ``dlq`` all end up on this line, so a check anywhere else would be a check
    each of them could be written around. It raises :class:`Duplicate` rather
    than deciding anything — what to say and whether to override is the
    screen's business, and *again* is that override coming back.
    """
    if not again:
        found = find_duplicate(source_of(source), slug)
        if found is not None:
            raise found
    STAGING.mkdir(parents=True, exist_ok=True)
    QUEUE.mkdir(parents=True, exist_ok=True)
    name = f"{number:02d}-{slug}.py"
    staged = STAGING / name
    # Written as UTF-8 because that is how the runner reads it. A title can
    # hold anything, and an item that decodes differently at midnight than it
    # did when it was queued is an item the runner refuses for no visible
    # reason.
    staged.write_text(source, encoding="utf-8")
    staged.chmod(0o755)
    final = QUEUE / name
    staged.replace(final)
    return final


def landing(dest: str) -> str:
    """Where a ``DEST`` value will actually put the file, for a message.

    Asked of the runner rather than worked out here, because the runner is what
    resolves it at delivery — and a line printed at queue time that disagrees
    with where the file turns up is worse than no line.
    """
    try:
        sys.path.insert(0, str(HERE))
        import expire_runner

        where = expire_runner.dest_of({"dest": dest})
    except Exception:  # noqa: BLE001 - a message, never a blocker
        return dest
    return str(where) if where else "out/"


def validate(path: Path) -> str | None:
    """Ask the runner's own parser whether it would admit this item."""
    try:
        sys.path.insert(0, str(HERE))
        import expire_runner

        item = expire_runner.parse_item(path)
    except Exception as exc:  # noqa: BLE001 - a check, never a blocker
        return f"could not check with the runner's parser: {exc!r}"
    return item.get("error")


# --------------------------------------------------------------------------- #
# Downloading one now, in the background
# --------------------------------------------------------------------------- #


def queue_busy() -> bool:
    """Whether something already holds the runner's lock.

    A nightly firing or another download-now owns the queue exclusively, and a
    second one would exit into a log nobody reads. Asking first turns that into
    a sentence on the screen. The answer can go stale between here and the
    spawn; the child takes the lock properly and would refuse anyway, so this
    is for the message, not for the safety.
    """
    try:
        handle = (HERE / "runner.lock").open("w")
    except OSError:
        # No lock file to collide over yet, which is not the same as busy.
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def now_argv(name: str) -> list[str]:
    """The command that downloads one queued item now.

    ``dlqd``'s own action, by path under the queue root rather than by console
    script, for the reason :func:`_root` exists: an installed copy in
    site-packages manages a queue that is not there. ``--yes`` because the
    confirm screen was the asking, and being asked twice teaches people to stop
    reading the question.
    """
    return [sys.executable, str(HERE / "expire_sched.py"), "now", name, "--yes"]


def start_now(name: str) -> tuple[subprocess.Popen, Path]:
    """Spawn the download detached, and say where it is writing.

    ``start_new_session`` on purpose, and the one place this disagrees with
    ``run_one``'s own choice: that avoids ``setsid`` so ctrl-c reaches the
    download through the terminal's process group. Here the screen goes back to
    the results and ytq later exits, and the download must not go with it.
    """
    logs = HERE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{time.strftime('%Y-%m-%d', time.gmtime())}-now-{name}.log"
    handle = log.open("a", encoding="utf-8")
    try:
        child = subprocess.Popen(
            now_argv(name),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(HERE),
        )
    finally:
        handle.close()
    return child, log


def now_progress(name: str) -> tuple[int, int] | None:
    """``(bytes on disk, total)`` from the item's own report, or ``None``.

    Read straight off ``work/<item>/.status.json``, which the download writes
    for the runner anyway — a local file, so watching a download costs nothing.
    Half-written or absent reads as "no report yet" rather than raising: this
    is drawn on a timer, and a screen must not die because it looked a
    millisecond early.
    """
    try:
        report = json.loads((HERE / "work" / name / ".status.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    have = report.get("part_bytes")
    if not isinstance(have, (int, float)):
        return None
    total = report.get("total_bytes")
    return int(have), int(total) if isinstance(total, (int, float)) else 0


def progress_line(name: str, report: tuple[int, int] | None, width: int) -> str:
    """The one line that says a background download is still going."""
    if report is None:
        body = "starting…"
    else:
        have, total = report
        body = human(have) + (f" / {human(total)}" if total else "")
    stem = name[:-3] if name.endswith(".py") else name
    return fit(f"↓ {stem}  {body}" if width >= WIDE else f"↓ {body}", width - 1)


class Running:
    """The background download this session started, if it started one."""

    def __init__(self) -> None:
        self.child: subprocess.Popen | None = None
        self.name = ""
        self.log: Path | None = None

    @property
    def alive(self) -> bool:
        return self.child is not None and self.child.poll() is None

    def start(self, name: str) -> None:
        self.child, self.log = start_now(name)
        self.name = name

    def stop(self) -> None:
        """Signal the whole group, which is what ctrl-c used to do.

        The part-file stays on disk and the item stays in the queue, so the
        nightly window carries on from where this stopped — the property that
        made ctrl-c cheap, kept now that there is no ctrl-c to press.
        """
        if self.child is None:
            return
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(self.child.pid), signal.SIGTERM)

    def line(self, width: int) -> str:
        return progress_line(self.name, now_progress(self.name), width)


# --------------------------------------------------------------------------- #
# Curses
# --------------------------------------------------------------------------- #


def _addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """Write clipped to the window; curses errors on the last cell."""
    height, width = win.getmaxyx()
    if not 0 <= y < height or x >= width:
        return
    with contextlib.suppress(curses.error):
        win.addnstr(y, x, text, max(0, width - x - 1), attr)


def fit(text: str, width: int) -> str:
    """*text* clipped to *width*, saying so when something was lost.

    Public because ``expire_sched`` lays out the same terminal and there is no
    second answer to be had about what a clipped string looks like.
    """
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def nights(size: int) -> int:
    """How many nightly windows a download this big needs, at the least.

    Rough by construction — the grant is shared with whatever else the queue is
    doing — but the distinction that matters is one night against several, and
    that one is robust.
    """
    return max(1, -(-size // NIGHT_BYTES))


def cost_band(size: int) -> str:
    """``fits``, ``night`` or ``nights``: which colour a size is worth.

    Separated from the drawing so it can be checked without a terminal, and
    named for the fact rather than for the colour — colour is one way of
    saying this and :func:`nights_note` is the other, because a terminal
    without colours must not be the terminal that loses the warning.
    """
    if size <= NIGHT_BYTES // 3:
        return "fits"
    return "night" if size <= NIGHT_BYTES else "nights"


def nights_note(size: int) -> str:
    """``(2 nights)`` when a download will not fit one window, else nothing."""
    count = nights(size)
    return f" ({count} nights)" if count > 1 else ""


def ink(win) -> dict[str, int]:
    """``name -> curses attribute``, with no colour at all if there is none.

    Termux sets ``TERM=xterm-256color``, but this also runs over ssh and in
    whatever a scheduled shell inherits, so every step is allowed to fail and
    leave the attribute at 0 — which is exactly "draw it plain".
    """
    wanted = {
        "fits": curses.COLOR_GREEN,
        "night": curses.COLOR_YELLOW,
        "nights": curses.COLOR_RED,
        "head": curses.COLOR_CYAN,
    }
    got = dict.fromkeys(wanted, 0)
    try:
        if not curses.has_colors():
            return got
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        return got
    for index, (name, colour) in enumerate(wanted.items(), start=1):
        try:
            curses.init_pair(index, colour, -1)
        except curses.error:
            continue
        got[name] = curses.color_pair(index)
    return got


def format_row(option: Choice, width: int) -> str:
    """One line of the format list, at whatever width there is for it.

    The format id and the codec detail go first when room runs out: they are
    the columns a choice is never made on. What is kept at every width is the
    size, because on a metered link it is the whole question.
    """
    mark = "~" if not option.exact else " "
    if width < WIDE:
        line = f" {human(option.size):>9}{mark} {option.label}"
    else:
        line = (
            f" {human(option.size):>10}{mark} "
            f"{option.label:<16} {option.fmt:<12} {option.detail}"
        )
    # The nights note is appended before the clip, not after, so that on a
    # terminal narrow enough to lose something it is the codec detail that
    # goes and not the warning that this will take a week.
    note = nights_note(option.size)
    return (line[: max(0, width - len(note))] + note)[:width]


def result_row(result: Result, width: int, queued: bool = False) -> list[str]:
    """One search hit, as the one or two lines there is room for.

    Four facts and a 40-column phone do not share a line without mutilating the
    title, and the title is the one a choice is actually made from — so below
    :data:`WIDE` the title gets a line of its own and everything else sits
    under it.

    The tail is composed before the channel is fitted into what is left, rather
    than clipping the finished line: clipping would drop the length and the age,
    which are two of the four things this screen exists to say. The channel is
    the one that can afford to lose its end.
    """
    when = age(result.timestamp)
    long = clock(result.duration, result.live)
    channel = result.channel or "?"
    mark = "✓" if queued else " "

    if width >= WIDE:
        tail = f"{fit(channel, 16):<16}  {when:>5}  {long:>7}"
        # mark, a space, the title, two spaces, the tail — and one column in
        # hand, because a line drawn into the last cell is a wrapped line.
        room = max(8, width - 5 - len(tail))
        return [f"{mark} {fit(result.title, room):<{room}}  {tail}"[: width - 1]]

    # Two columns for the mark whether or not there is one, so that queueing a
    # result does not shift its title one to the right of its neighbours'.
    prefix = "✓ " if queued else "  "
    tail = f"{when} · {long}"
    room = max(3, width - 7 - len(tail))
    return [
        (prefix + fit(result.title, max(4, width - 1 - len(prefix))))[: width - 1],
        # Packed left and not padded into a column: the age and the length are
        # different lengths on every row, so a column here would put the dots
        # in a different place on each line and read as a ragged table rather
        # than as the sentence it is.
        f"   {fit(channel, room)} · {tail}"[: width - 1],
    ]


def _height_of(label: str) -> int | None:
    """The resolution a format label leads with, if it leads with one."""
    found = re.match(r"(\d+)p", label)
    return int(found.group(1)) if found else None


def preferred_index(options: list[Choice], remembered: dict | None) -> int:
    """Where the cursor opens: the last format chosen, or the top of the list.

    Tiers rather than one comparison, because the exact format a video offers
    varies with the video — the useful memory is "1080p, merged", not the
    string. Falling through to ``0`` is today's behaviour, so a memory that
    matches nothing changes nothing.

    Worth noting this is also the safer default: row 0 is always the largest
    file in the list, and opening there is how a tired thumb queues four
    gigabytes.
    """
    if not remembered:
        return 0
    label = remembered.get("label") or ""
    kind = remembered.get("kind") or ""
    height = _height_of(label)
    tiers = (
        lambda o: o.label == label and o.kind == kind,
        lambda o: (
            height is not None and _height_of(o.label) == height and o.kind == kind
        ),
        lambda o: height is not None and _height_of(o.label) == height,
        lambda o: o.kind == kind,
    )
    for wants in tiers:
        for index, option in enumerate(options):
            if wants(option):
                return index
    return 0


def recalled_format() -> dict | None:
    """The format chosen last time, from the queue's own config file."""
    try:
        sys.path.insert(0, str(HERE))
        import expire_runner

        remembered = expire_runner.load_config().get("ytq_last_format")
    except Exception:  # noqa: BLE001 - a convenience, never a blocker
        return None
    return remembered if isinstance(remembered, dict) else None


def remember_format(choice: Choice) -> None:
    """Record the choice, at the moment an item is written and not before.

    Kept in the queue's ``config.json`` beside the destinations rather than in
    a file of its own: it is the same kind of setting, it already has an atomic
    writer, and a second place to look for preferences is how two of them end
    up disagreeing.
    """
    try:
        sys.path.insert(0, str(HERE))
        import expire_runner

        config = expire_runner.load_config()
        config["ytq_last_format"] = {"label": choice.label, "kind": choice.kind}
        expire_runner.save_config(config)
    except Exception:  # noqa: BLE001 - a convenience, never a blocker
        pass


def text_input(win, y: int, x: int, initial: str = "", width: int = 60) -> str | None:
    """A one-line editor. ``None`` if the user backed out with Esc."""
    buffer = list(initial)
    curses.curs_set(1)
    try:
        while True:
            shown = "".join(buffer)[-width:]
            _addstr(win, y, x, shown + " " * (width - len(shown)), curses.A_UNDERLINE)
            win.move(y, min(x + len(shown), win.getmaxyx()[1] - 1))
            win.refresh()
            key = win.getch()
            if key in (curses.KEY_ENTER, 10, 13):
                return "".join(buffer).strip()
            if key == 27:
                return None
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if buffer:
                    buffer.pop()
            elif key == 21:  # ctrl-U
                buffer.clear()
            elif 32 <= key < 127:
                buffer.append(chr(key))
    finally:
        curses.curs_set(0)


def spinner_while(win, message: str, work) -> tuple[object, Exception | None]:
    """Run *work* in a thread, keeping the screen alive while it goes."""
    result: list = [None, None]

    def target():
        try:
            result[0] = work()
        except Exception as exc:  # noqa: BLE001 - reported to the user
            result[1] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    win.nodelay(True)
    frames = "|/-\\"
    tick = 0
    try:
        while thread.is_alive():
            win.erase()
            _addstr(win, 1, 2, f"{frames[tick % 4]} {message}", curses.A_BOLD)
            _addstr(
                win,
                3,
                2,
                "metadata only, no media"
                if win.getmaxyx()[1] < WIDE
                else "metadata only, no media is downloaded",
            )
            win.refresh()
            tick += 1
            time.sleep(0.12)
            if win.getch() == 27:
                break
    finally:
        win.nodelay(False)
    thread.join(timeout=1)
    return result[0], result[1]


def pick(
    win,
    info: dict,
    options: list[Choice],
    unsized: int,
    paint: dict | None = None,
    start: int = 0,
    recalled: bool = False,
    now_default: bool = False,
) -> tuple[Choice, bool] | None:
    """The format list.

    Returns the chosen download and whether it was chosen to run *now*, or
    ``None`` to go back. ``⏎`` takes *now_default* (which ``--now`` sets), and
    ``n``/``t`` say so explicitly from either mode — so the answer to "is this
    going to cost me" is never more than one key away from the size it costs.
    """
    title = info.get("title") or "(untitled)"
    duration = info.get("duration")
    paint = paint if paint is not None else dict.fromkeys(("fits", "head"), 0)
    top = 0
    cursor = max(0, min(start, len(options) - 1))

    while True:
        win.erase()
        height, width = win.getmaxyx()
        narrow = width < WIDE
        _addstr(
            win,
            0,
            0,
            f" {title} ".ljust(width - 1)[: width - 1],
            curses.A_REVERSE | curses.A_BOLD | paint.get("head", 0),
        )
        length = f"{int(duration) // 60}m{int(duration) % 60:02d}s" if duration else "?"
        meta = f"{length}  ·  {len(options)} formats"
        if not narrow:
            meta = f"{info.get('extractor_key', '?')}  ·  {meta}"
        if unsized:
            meta += (
                f"  ·  {unsized} unsized"
                if narrow
                else (f"  ·  {unsized} without a size, hidden")
            )
        # Only where there is room for it. On a phone the cursor sitting part
        # way down the list already says it opened somewhere chosen, and saying
        # so twice is what makes a 40-column screen feel busy.
        if recalled and not narrow:
            meta += "  ·  last used"
        _addstr(win, 1, 1, meta, curses.A_DIM)

        listed = max(1, height - 6)
        cursor = max(0, min(cursor, len(options) - 1))
        top = max(min(top, cursor), cursor - listed + 1, 0)

        for row in range(listed):
            index = top + row
            if index >= len(options):
                break
            option = options[index]
            chosen = index == cursor
            line = format_row(option, width)
            # The size is coloured by what it will cost, so the shape of the
            # list answers "which of these can this connection actually have"
            # before any of them is read. Reversed on the cursor line, where a
            # colour on a reversed cell is unreadable on some terminals.
            attr = curses.A_REVERSE if chosen else paint.get(cost_band(option.size), 0)
            _addstr(win, 3 + row, 0, line.ljust(width - 1), attr)

        if narrow:
            keys = hint("pick-now" if now_default else "pick", width)
        elif now_default:
            keys = (
                "↑↓ choose   enter download it now   "
                "t queue for tonight instead   q back"
            )
        else:
            keys = (
                "↑↓ choose   enter queue it   n download it now   q back   ~ = estimate"
            )
        _addstr(win, height - 2, 1, keys, curses.A_DIM)
        win.refresh()

        key = win.getch()
        if key in (ord("q"), 27):
            return None
        if key in (curses.KEY_UP, ord("k")):
            cursor -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor += 1
        elif key == curses.KEY_NPAGE:
            cursor += listed
        elif key == curses.KEY_PPAGE:
            cursor -= listed
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = len(options) - 1
        elif key == ord("n"):
            return options[cursor], True
        elif key == ord("t"):
            return options[cursor], False
        elif key in (curses.KEY_ENTER, 10, 13):
            return options[cursor], now_default


def confirm(
    win,
    url: str,
    info: dict,
    choice: Choice,
    now: bool = False,
    paint: dict | None = None,
    dest: str = "video",
) -> tuple[int, str, bool] | None:
    """Let the priority, the file name and free-or-paid be settled.

    Returns the priority, the slug and whether to download now. ``n`` and ``t``
    switch between the two modes here as well as on the format list, because
    this is the screen showing the number that decides it.
    """
    number = next_number()
    slug = slugify(info.get("title") or "")
    paint = paint if paint is not None else dict.fromkeys(("fits", "head"), 0)
    # Resolved once: it needs the runner's config, and this redraws on
    # every keystroke.
    where = landing(dest)
    field = 0

    while True:
        win.erase()
        height, width = win.getmaxyx()
        narrow = width < WIDE
        # The field values sit right of their labels, and "file name" is nine
        # columns, so this is as far left as the column can go.
        gutter = 11 if narrow else 14
        # The header is the screen's identity, and the fact it has to carry is
        # not "now versus later" but "paid versus free". Said in words because
        # a terminal without colours must not be the one that loses it.
        _addstr(
            win,
            0,
            0,
            (" download NOW — paid " if now else " queue tonight — free ").ljust(
                width - 1
            ),
            curses.A_REVERSE | curses.A_BOLD | paint.get("head", 0),
        )
        _addstr(
            win,
            2,
            2,
            f"{choice.label}  {choice.fmt}"
            if narrow
            else f"{choice.label}   format {choice.fmt}   {choice.detail}",
        )
        _addstr(
            win,
            3,
            2,
            f"yt-dlp says {human(choice.size)}{'' if choice.exact else ' (estimated)'}",
        )
        # The cap is the number this screen exists to show, so it is bold and
        # coloured by how many nights it will take — the same scale the format
        # list used, so a red row stays red here.
        cost = paint.get(cost_band(choice.expect_bytes), 0)
        _addstr(
            win,
            4,
            2,
            f"cap {human(choice.expect_bytes)}"
            if narrow
            else f"EXPECT_BYTES {choice.expect_bytes:,} "
            f"({human(choice.expect_bytes)}) — the cap the runner "
            f"holds it to",
            curses.A_BOLD | cost,
        )
        _addstr(win, 6, 2, "priority", curses.A_DIM)
        _addstr(win, 7, 2, "file name", curses.A_DIM)
        if narrow:
            # Only the saved file, and only its name: the queue path is the
            # same information twice and the leading directories are the part
            # nobody needs at 40 columns.
            _addstr(win, 9, 2, f"→ {slug}.{choice.ext}")
        else:
            _addstr(win, 9, 2, f"→ queue/{number:02d}-{slug}.py")
            _addstr(win, 10, 2, fit(f"→ {where}/{slug}.{choice.ext}", width - 3))
        if now:
            # The second of the three places this says paid, and the only one
            # carrying the number. Bold and cost-banded where there is colour;
            # the sentence stands on its own where there is not.
            _addstr(
                win,
                11,
                2,
                fit(
                    f"this spends {human(choice.expect_bytes)} of PAID data", width - 3
                ),
                curses.A_BOLD | cost,
            )
            note = (
                "starts on enter and runs in the background; dlqd list "
                "shows it, x stops it"
            )
            for offset, wrapped in enumerate(textwrap.wrap(note, max(20, width - 4))):
                _addstr(win, 13 + offset, 2, wrapped, curses.A_DIM)
        _addstr(
            win,
            height - 2,
            1,
            hint("now" if now else "queue", width)
            if narrow
            else "tab switch field   e edit   "
            + (
                "enter start it now   t queue for tonight"
                if now
                else "enter write it   n download it now"
            )
            + "   q back",
            curses.A_DIM,
        )

        _addstr(win, 6, gutter, f"{number:02d}", curses.A_REVERSE if field == 0 else 0)
        # Clipped with an ellipsis rather than by the window edge: a long slug
        # cut off at the last column looks like the whole file name, and this
        # is the screen where the file name is being decided.
        _addstr(
            win,
            7,
            gutter,
            fit(slug, max(8, width - gutter - 2)),
            curses.A_REVERSE if field == 1 else 0,
        )
        win.refresh()

        key = win.getch()
        if key in (ord("q"), 27):
            return None
        if key in (9, curses.KEY_DOWN, curses.KEY_UP):
            field = 1 - field
        elif key == ord("n"):
            now = True
        elif key == ord("t"):
            now = False
        elif key in (curses.KEY_ENTER, 10, 13):
            return number, slug, now
        elif key in (ord("e"), ord("i")):
            # The editor must not be told it has more room than the window
            # does, or the field it underlines runs off the right of a phone.
            room = max(8, width - gutter - 2)
            if field == 0:
                typed = text_input(win, 6, gutter, f"{number:02d}", min(8, room))
                if typed and typed.isdigit():
                    number = max(0, min(99999, int(typed)))
            else:
                typed = text_input(win, 7, gutter, slug, min(48, room))
                if typed:
                    slug = slugify(typed)


def already_queued(hits: list[Result]) -> set[int]:
    """Which of these are already queued, downloaded or given up on.

    Asked before anything is probed, because a duplicate noticed here has cost
    nothing and one noticed afterwards has cost an extraction — which on this
    connection is the entire point of noticing it.
    """
    known = set()
    for _, path in items():
        try:
            key = source_of(path.read_text(encoding="utf-8", errors="replace")[:4096])
        except OSError:
            continue
        if key:
            known.add(key)
    return {index for index, hit in enumerate(hits) if hit.key and hit.key in known}


def duplicate_screen(win, paint: dict, dup: Duplicate) -> bool:
    """Say this has been queued before. ``True`` if it is to be queued anyway.

    A screen of its own rather than a line on the confirmation, for the same
    reason the confirmation exists at all: this is a moment where data gets
    spent twice, and a warning sharing a screen with nine other facts is a
    warning that gets skimmed past.

    The override is written on the screen rather than left to be known. A key
    that is live on one screen and silent on another is how somebody presses it
    three times and concludes the tool is broken.
    """
    win.erase()
    height, width = win.getmaxyx()
    titles = {
        "queued": " already in the queue ",
        "done": " already downloaded ",
        "failed": " already tried ",
    }
    _addstr(
        win,
        0,
        0,
        titles.get(dup.where, " already queued ").ljust(width - 1),
        curses.A_REVERSE | curses.A_BOLD | paint.get("night", 0),
    )
    row = 2
    for text in (dup.says(), f"as {dup.stem}"):
        for wrapped in textwrap.wrap(text, max(12, width - 4)) or [""]:
            _addstr(win, row, 2, wrapped, curses.A_BOLD if row == 2 else 0)
            row += 1
    row += 1
    if dup.how == "name":
        # Weaker evidence, said as such: the id is what proves two items are
        # the same video, and an item queued before SOURCE existed has none.
        for wrapped in textwrap.wrap(
            "matched by name, not by id — it may be a different video with "
            "the same title",
            max(12, width - 4),
        ):
            _addstr(win, row, 2, wrapped, curses.A_DIM)
            row += 1
        row += 1
    _addstr(win, min(row, height - 4), 2, "a  queue it again anyway")
    _addstr(win, height - 2, 1, "a  again   any other key: back", curses.A_DIM)
    win.refresh()
    return win.getch() == ord("a")


def message(win, lines: list[str]) -> None:
    """A full-screen notice, wrapped to whatever width there is.

    Wrapped rather than clipped: these are the sentences that explain why
    nothing was queued, and half of one is no explanation at all.
    """
    win.erase()
    width = max(20, win.getmaxyx()[1] - 4)
    row = 1
    for index, line in enumerate(lines):
        for wrapped in textwrap.wrap(line, width) or [""]:
            _addstr(win, row, 2, wrapped, curses.A_BOLD if index == 0 else 0)
            row += 1
        row += 1
    _addstr(win, row, 2, "any key to continue", curses.A_DIM)
    win.refresh()
    win.getch()


def entry(win, paint: dict, initial: str = "") -> str | None:
    """The one field that takes either words to search for or a URL.

    One field rather than two, because the alternative on a phone is a mode key
    to remember; :func:`looks_like_url` tells them apart by looking. What is
    printed under it is the cost of each answer, standing there before anything
    is spent rather than in a dialog afterwards.
    """
    win.erase()
    height, width = win.getmaxyx()
    narrow = width < WIDE
    _addstr(
        win,
        1,
        2,
        "search, or paste a URL" if narrow else "search youtube, or paste a URL",
        curses.A_BOLD | paint.get("head", 0),
    )
    if width < 40:
        _addstr(win, 6, 2, "search ~0.1 MB · a URL ~0.3 MB", curses.A_DIM)
    else:
        _addstr(win, 6, 2, "words → youtube      ~0.1 MB", curses.A_DIM)
        _addstr(win, 7, 2, "a URL → the formats  ~0.1-0.5 MB", curses.A_DIM)
    _addstr(
        win,
        height - 2,
        1,
        hint("entry", width) if narrow else "enter go   esc quit",
        curses.A_DIM,
    )
    return text_input(win, 3, 2, initial, max(12, width - 4))


def results(
    win,
    query: str,
    hits: list[Result],
    paint: dict,
    queued: set[int],
    running: Running,
) -> int | str | None:
    """The search results. An index, ``"/"`` to search again, or ``None`` back.

    Below :data:`WIDE` each result takes two lines, so ten of them are ten
    titles a thumb can read rather than twenty truncated columns. The cursor
    reverses both lines of the one it is on; ``✓`` is the only other mark, and
    only on what this session has already queued.
    """
    top = 0
    cursor = 0

    while True:
        win.erase()
        height, width = win.getmaxyx()
        narrow = width < WIDE
        tall = 1 if not narrow else 2
        _addstr(
            win,
            0,
            0,
            fit(f" search: {query} ", width - 1).ljust(width - 1),
            curses.A_REVERSE | curses.A_BOLD | paint.get("head", 0),
        )
        meta = f"{len(hits)} results  ·  ~ approx dates"
        if not narrow:
            meta = f"{len(hits)} results  ·  youtube  ·  ~ dates are approximate"
        _addstr(win, 1, 1, meta, curses.A_DIM)

        listed = max(1, (height - 6) // tall)
        cursor = max(0, min(cursor, len(hits) - 1))
        top = max(min(top, cursor), cursor - listed + 1, 0)

        for row in range(listed):
            index = top + row
            if index >= len(hits):
                break
            attr = curses.A_REVERSE if index == cursor else 0
            for offset, line in enumerate(
                result_row(hits[index], width, index in queued)
            ):
                _addstr(win, 3 + row * tall + offset, 0, line.ljust(width - 1), attr)

        if running.alive:
            _addstr(win, height - 3, 1, running.line(width), curses.A_BOLD)
        _addstr(
            win,
            height - 2,
            1,
            hint("running" if running.alive else "results", width)
            if narrow
            else (
                "↑↓ choose   enter see the formats   / search again   q back"
                + ("   x stop the download" if running.alive else "")
            ),
            curses.A_DIM,
        )
        win.refresh()

        # Blocking unless there is a download to watch, so an idle screen costs
        # no wakeups at all and a live one redraws twice a second.
        win.timeout(500 if running.alive else -1)
        try:
            key = win.getch()
        finally:
            win.timeout(-1)

        if key == -1:
            continue
        if key in (ord("q"), 27):
            return None
        if key == ord("/"):
            return "/"
        if key == ord("x"):
            running.stop()
        elif key in (curses.KEY_UP, ord("k")):
            cursor -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor += 1
        elif key == curses.KEY_NPAGE:
            cursor += listed
        elif key == curses.KEY_PPAGE:
            cursor -= listed
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = len(hits) - 1
        elif key in (curses.KEY_ENTER, 10, 13):
            return cursor


def watch(win, paint: dict, running: Running) -> None:
    """Stay with a background download when there is no list to go back to.

    The URL path has no results screen behind it, and a download that vanishes
    the moment it starts is worse than one that never went to the background at
    all. ``q`` leaves it running.
    """
    while True:
        win.erase()
        height, width = win.getmaxyx()
        _addstr(
            win,
            0,
            0,
            " downloading now — paid ".ljust(width - 1),
            curses.A_REVERSE | curses.A_BOLD | paint.get("head", 0),
        )
        if running.alive:
            _addstr(win, 2, 2, running.line(width), curses.A_BOLD)
        else:
            _addstr(
                win,
                2,
                2,
                "no longer running — dlqd list says how it went"
                if width >= WIDE
                else "no longer running",
                curses.A_BOLD,
            )
        _addstr(
            win,
            height - 2,
            1,
            hint("watch", width)
            if width < WIDE
            else "x stop it   q leave it running and go back",
            curses.A_DIM,
        )
        win.refresh()
        win.timeout(500 if running.alive else -1)
        try:
            key = win.getch()
        finally:
            win.timeout(-1)
        if key == ord("x"):
            running.stop()
        elif key in (ord("q"), 27):
            return


def app(
    win,
    first: str | None,
    preloaded: dict | None = None,
    now: bool = False,
    dest: str = "video",
) -> list[str]:
    """The whole flow: find it, choose a quality, queue it or start it.

    Returns the receipts to print once curses has been torn down — a list,
    because a session can queue several items now rather than ending at the
    first one.

    Written as an explicit screen name and a loop rather than nested calls, so
    that "where does q go from here" is one table in one place. Both caches are
    the point of that shape: backing out of a video and into another costs
    nothing, and coming back to the first one costs nothing either, which on
    this link is the difference between browsing and rationing.
    """
    curses.curs_set(0)
    win.keypad(True)
    paint = ink(win)

    receipts: list[str] = []
    running = Running()
    searched: dict[str, list[Result]] = {}
    probed: dict[str, tuple[dict, list[Choice], int]] = {}
    marks: dict[str, set[int]] = {}
    #: Videos this session has been told to queue again despite already having
    #: them. Per target, so agreeing to one is not agreeing to the next.
    agreed: set[str] = set()

    typed = first or ""
    query = ""
    hits: list[Result] = []
    target = ""
    chosen_index = -1
    came_from = "entry"
    screen = "entry"

    # A saved dump stands in for whichever call would have fetched it, so both
    # halves of this can be worked on without spending anything.
    if preloaded is not None:
        if preloaded.get("entries") is not None:
            query = preloaded.get("title") or "(saved search)"
            hits = searched[query] = entries(preloaded)
            screen = "results"
        else:
            target = preloaded.get("webpage_url") or preloaded.get("url") or first or ""
            probed[target] = (preloaded, *choices(preloaded))
            screen = "formats"
    elif first and looks_like_url(first):
        target, screen = first, "formats"
    elif first:
        query, screen = first, "search"

    while True:
        if screen == "entry":
            text = entry(win, paint, typed)
            typed = ""
            if not text:
                return receipts
            if looks_like_url(text):
                target, came_from, screen = text, "entry", "formats"
            else:
                query, screen = text, "search"

        elif screen == "search":
            if query in searched:
                hits = searched[query]
            else:
                room = max(12, win.getmaxyx()[1] - 20)
                found, failure = spinner_while(
                    win,
                    f"searching for {query[:room]}…",
                    # Bound rather than closed over: this is inside the screen
                    # loop, and a lambda that reads the variable later would
                    # read whatever the next screen put there.
                    lambda words=query: search(words),
                )
                if failure is not None:
                    message(win, ["that search did not come back", str(failure)])
                    typed, screen = query, "entry"
                    continue
                if found is None:
                    return receipts
                hits = searched[query] = found
            if not hits:
                message(win, ["nothing found for that", "try different words"])
                typed, screen = query, "entry"
                continue
            screen = "results"

        elif screen == "results":
            # Marked with what this session queued *and* what the queue
            # already holds, which are the same fact to whoever is reading it.
            picked = results(
                win,
                query,
                hits,
                paint,
                marks.setdefault(query, set()) | already_queued(hits),
                running,
            )
            if picked is None:
                typed, screen = "", "entry"
            elif isinstance(picked, str):
                typed, screen = query, "entry"
            else:
                chosen_index = picked
                target, came_from, screen = hits[picked].url, "results", "formats"

        elif screen == "formats":
            if target not in probed:
                room = max(12, win.getmaxyx()[1] - 22)
                info, failure = spinner_while(
                    win,
                    f"asking yt-dlp about {target[:room]}…",
                    lambda page=target: probe(page),
                )
                if failure is not None:
                    message(win, ["could not read that URL", str(failure)])
                    typed, screen = "", came_from
                    continue
                if info is None:
                    return receipts
                probed[target] = (info, *choices(info))
            info, options, unsized = probed[target]
            if not options:
                message(
                    win,
                    [
                        "no format has a size yt-dlp will state",
                        "nothing can be queued without one — see the queue "
                        "contract on EXPECT_BYTES.",
                        "if this is a plain file URL, expire_dl handles those "
                        "better anyway: it slices by Range.",
                    ],
                )
                typed, screen = "", came_from
                continue

            # Before a format is chosen rather than after: the probe is spent
            # either way, but nothing else needs to be.
            if target not in agreed:
                clash = find_duplicate(
                    source_key(info), slugify(info.get("title") or "")
                )
                if clash is not None:
                    if not duplicate_screen(win, paint, clash):
                        typed, screen = "", came_from
                        continue
                    agreed.add(target)

            remembered = recalled_format()
            start = preferred_index(options, remembered)
            # Backing out of the confirmation returns to the format list rather
            # than any further: re-probing would spend another extraction's
            # worth of data to show what is already in hand.
            decided = None
            while decided is None:
                chosen = pick(win, info, options, unsized, paint, start, start > 0, now)
                if chosen is None:
                    break
                choice, run_now = chosen
                start = options.index(choice)
                decided = confirm(win, target, info, choice, run_now, paint, dest)
            if decided is None:
                typed, screen = "", came_from
                continue
            number, slug, run_now = decided

            item = render(
                target,
                slug,
                choice,
                info.get("title") or slug,
                time.strftime("%Y-%m-%d", time.gmtime()),
                dest,
                source_key(info),
            )
            try:
                path = write_item(number, slug, item, again=target in agreed)
            except Duplicate as clash:
                # Reachable when the name typed on the confirmation collides
                # with something the id did not, and it is the check that makes
                # the door a door: no route to here can skip it.
                if not duplicate_screen(win, paint, clash):
                    typed, screen = "", came_from
                    continue
                agreed.add(target)
                path = write_item(number, slug, item, again=True)
            problem = validate(path)
            if problem:
                message(
                    win,
                    [
                        "the runner would reject this item",
                        problem,
                        f"written anyway at {path}",
                    ],
                )
            remember_format(choice)
            receipt = (
                f"queued {path.name} — {human(choice.size)} "
                f"({choice.label}), cap {human(choice.expect_bytes)}"
            )
            if run_now:
                receipt = _start_or_say_why(win, running, path, choice) or receipt
            receipts.append(receipt)

            if came_from == "results":
                marks.setdefault(query, set()).add(chosen_index)
                screen = "results"
            elif run_now and running.child is not None:
                # Nothing behind this screen to go back to, and something did
                # start — so stay with it rather than exiting into silence.
                # Guarded on the child, because a download that was refused for
                # a busy queue has already said so and has nothing to watch.
                watch(win, paint, running)
                return receipts
            else:
                return receipts


def _start_or_say_why(win, running: Running, path: Path, choice: Choice) -> str | None:
    """Start the background download, or explain what is in the way.

    Either way the item is already written and queued, so the worst outcome is
    that it waits for the nightly window — which is the free one anyway.
    """
    if running.alive:
        message(
            win,
            [
                "one download is already running",
                f"{running.name} has the queue until it finishes",
                f"{path.name} is queued and will follow",
            ],
        )
        return None
    if queue_busy():
        message(
            win,
            [
                "the queue is busy",
                "a nightly firing or another download holds it",
                f"{path.name} is queued and will run at the window",
            ],
        )
        return None
    # Said here rather than discovered in a log nobody opens. The queue root
    # holds the modules as well as the queue — the self-test pins that — so
    # this failing means the anchor is wrong, and a download that quietly does
    # nothing is exactly the failure `_root` exists to prevent.
    if not (HERE / "expire_sched.py").is_file():
        message(
            win,
            [
                "the queue manager is not where the queue is",
                f"expected it beside the queue at {HERE}",
                f"{path.name} is queued and will run at the window",
            ],
        )
        return None
    running.start(path.name)
    return (
        f"downloading {path.name} now — {human(choice.size)} "
        f"({choice.label}); dlqd list shows it"
    )


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #


def load_json(path: str) -> dict:
    """A saved ``yt-dlp -J`` dump, so a re-pick costs no data."""
    info = json.loads(Path(path).read_text())
    if not isinstance(info, dict):
        raise ProbeError(f"{path} is not a yt-dlp metadata object")
    return info


def list_results(hits: list[Result], width: int) -> int:
    """A saved search, printed. The same rows, without a terminal to curse."""
    for result in hits:
        for line in result_row(result, width):
            print(line)
    return 0


def list_formats(url: str, dump: str | None = None) -> int:
    """The same information the TUI shows, for a terminal that cannot curse."""
    try:
        info = load_json(dump) if dump else probe(url)
    except (ProbeError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    # A saved search dump is a playlist envelope, and its rows are the ones the
    # results screen draws — not formats, which it has none of.
    if info.get("entries") is not None:
        return list_results(entries(info), width)
    options, unsized = choices(info)
    title = f"{info.get('title')}"
    if width >= WIDE:
        title += f"  [{info.get('extractor_key')}]"
    print(title[:width])
    for option in options:
        line = format_row(option, width)
        if width >= WIDE:
            line += f"  cap {human(option.expect_bytes)}"
        print(line[:width])
    if unsized:
        print(f"  ({unsized} hidden: yt-dlp states no size for them)"[:width])
    return 0


def _self_test() -> int:
    """Check the parts that decide what gets written, without a network."""
    import contextlib
    import io
    import tempfile

    # Several checks below point the queue at a temporary directory rather than
    # at the real one, which is the whole reason they are safe to run on the
    # phone while the nightly job is armed.
    global QUEUE, STAGING, DONE, FAILED

    passed = failed = 0

    def check(label: str, got, want) -> None:
        nonlocal passed, failed
        if got == want:
            passed += 1
        else:
            failed += 1
            print(f"FAIL {label}: got {got!r}, want {want!r}")

    def at_most(label: str, got: int, limit: int) -> None:
        """The same shape ``quota_widget`` uses for its own width checks."""
        nonlocal passed, failed
        if got <= limit:
            passed += 1
        else:
            failed += 1
            print(f"FAIL {label}: {got} exceeds {limit}")

    info = {
        "title": 'A "Talk": part 1/2 & more \\ things',
        "extractor_key": "Youtube",
        "duration": 61,
        "formats": [
            {
                "format_id": "137",
                "ext": "mp4",
                "vcodec": "avc1.64",
                "acodec": "none",
                "height": 1080,
                "fps": 60,
                "filesize": 500_000_000,
            },
            {
                "format_id": "248",
                "ext": "webm",
                "vcodec": "vp9",
                "acodec": "none",
                "height": 1080,
                "filesize_approx": 400_000_000,
            },
            {
                "format_id": "18",
                "ext": "mp4",
                "vcodec": "avc1.42",
                "acodec": "mp4a",
                "height": 360,
                "filesize": 40_000_000,
            },
            {
                "format_id": "140",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a",
                "abr": 129,
                "filesize": 10_000_000,
            },
            {
                "format_id": "251",
                "ext": "webm",
                "vcodec": "none",
                "acodec": "opus",
                "abr": 141,
                "filesize": 12_000_000,
            },
            {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none"},
            {
                "format_id": "701",
                "ext": "mp4",
                "vcodec": "av01",
                "acodec": "none",
                "height": 2160,
            },
            {
                "format_id": "direct",
                "ext": "mp4",
                "vcodec": None,
                "acodec": None,
                "filesize": 7_000_000,
            },
        ],
    }
    options, unsized = choices(info)
    by_fmt = {option.fmt: option for option in options}

    check("unsized formats are dropped, not guessed at", unsized, 1)
    check("storyboards are not offered", "sb0" in by_fmt, False)
    check(
        "video-only pairs with same-container audio",
        "137+140" in by_fmt and "248+251" in by_fmt,
        True,
    )
    check(
        "merge container avoids a needless remux",
        (by_fmt["137+140"].merge_ext, by_fmt["248+251"].merge_ext),
        ("mp4", "webm"),
    )
    check("paired size is the sum", by_fmt["137+140"].size, 510_000_000)
    check(
        "an approximate part makes the pair approximate", by_fmt["248+251"].exact, False
    )
    check(
        "unknown codecs are playable, not storyboards", by_fmt["direct"].kind, "single"
    )
    check("progressive needs no merge", by_fmt["18"].merge_ext, None)
    check("largest video first", options[0].fmt, "137+140")
    check(
        "audio-only sorts below video",
        [o.kind for o in options][-2:],
        ["audio", "audio"],
    )

    exact = by_fmt["137+140"]
    check(
        "exact sizes take the smaller margin",
        exact.expect_bytes,
        int(510_000_000 * 1.03) + OVERHEAD_FIXED,
    )
    check("the cap is above the measurement", exact.expect_bytes > exact.size, True)
    approx = by_fmt["248+251"]
    check(
        "estimates take the larger margin",
        approx.expect_bytes,
        math.ceil(412_000_000 * OVERHEAD_APPROX) + OVERHEAD_FIXED,
    )

    # An archived item lives in done/<date>/, and a date is a number and a dash
    # — the same shape as a priority. Counting the directory takes the next
    # number to 2036 and leaves it there.
    with tempfile.TemporaryDirectory() as raw:
        keep = (QUEUE, DONE, FAILED)
        QUEUE, DONE, FAILED = (Path(raw) / part for part in ("queue", "done", "failed"))
        try:
            (DONE / "2026-08-08").mkdir(parents=True)
            (DONE / "2026-08-08" / "50-a.py").write_text("")
            QUEUE.mkdir()
            check("the day directory is not an item number", next_number(), 60)
            # Past 99 the key gains a digit, and a string sort puts "100"
            # in front of "20": the newest item would run first. The cap is
            # what keeps "lower runs first" true, and two items sharing the
            # last key is a far smaller wrong than a queue in reverse.
            (QUEUE / "95-b.py").write_text("")
            check("a new item never sorts ahead of the queue", next_number(), 99)
            check(
                "which is what two digits buys",
                sorted([f"{next_number():02d}-new.py", "20-old.py"]),
                ["20-old.py", "99-new.py"],
            )
        finally:
            QUEUE, DONE, FAILED = keep

    # Cost banding. The number a phone user is really choosing on is not the
    # size but how many nights it will take, and this is the only place that
    # says so — in colour where there is colour, in words where there is not.
    check("a small download fits a night", cost_band(50 * 1024**2), "fits")
    check("most of a grant is a whole night", cost_band(600 * 1024**2), "night")
    check("more than a grant spans nights", cost_band(2 * 1024**3), "nights")
    check("and the count says how many", nights(2 * 1024**3), 4)
    check("one night is not worth remarking on", nights_note(1024**2), "")
    check("several is", nights_note(2 * 1024**3), " (4 nights)")

    # The format list has to fit the terminal, for the same reason the quota
    # widget's face has to fit the tile: a line wider than the screen is not an
    # error, it is a row of wrapped fragments with the size scrolled off the
    # end. 32 is below anything real and is here as a floor, not a target.
    for width in (32, 40, 48, 64, 80, 120):
        at_most(
            f"format row at {width}",
            max(len(format_row(option, width)) for option in options),
            width,
        )
    # The hints are the line that says how to leave the screen. Clipped, they
    # take the way out with them, and a curses screen with no visible way out
    # is the worst thing this can do on a phone.
    for name, line in HINTS.items():
        at_most(f"the {name} hints fit a phone", len(line), HINT_WIDTH)
    # And the tight set fits the floor, where the room is 30 rather than 38.
    # This is the check that would have caught `q back` falling off the end of
    # the format list at 32 columns.
    for name, line in TIGHT_HINTS.items():
        at_most(f"the {name} hints fit the floor", len(line), TIGHT_WIDTH)
    check("both sets cover the same screens", set(TIGHT_HINTS), set(HINTS))
    # Whichever key it is — the entry screen leaves on esc, everything below it
    # on q — the hint that survives the floor has to name one of them.
    for name in HINTS:
        tight = hint(name, 32)
        check(
            f"the {name} hints still say how to leave",
            "q " in tight or "esc " in tight,
            True,
        )

    # -- searching ---------------------------------------------------------- #

    # The one check that stands between a search and twenty extractions. The
    # cheap shape and the expensive one differ by a single flag, and the
    # expensive one is not an error — it works, it just costs twenty times as
    # much on a link where that is the whole question.
    built = search_argv("crust of rust", 20)
    check(
        "a search asks for the stated number", "ytsearch20:crust of rust" in built, True
    )
    check("a search stays flat", "--flat-playlist" in built, True)
    check("a search asks for the approximate date", APPROX_DATE_ARGS[1] in built, True)
    # The config holds the JS runtime and the cookies; without them YouTube
    # answers with a fraction of what it has, or refuses outright.
    check(
        "a search does not bypass the yt-dlp config", "--ignore-config" in built, False
    )

    envelope = {
        "_type": "playlist",
        "title": "ytsearch20:rust",
        "entries": [
            {
                "id": "aaaaaaaaaaa",
                "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
                "title": "Crust of Rust: Lifetime Annotations",
                "channel": "Jon Gjengset",
                "duration": 5434,
                "timestamp": 1_600_000_000,
            },
            # No channel, no duration, no date, and only an id to build a URL
            # from: every one of these is a field yt-dlp is allowed to omit.
            {"id": "bbbbbbbbbbb", "title": "Bare"},
            # A search answers with channels and playlists too, and neither has
            # a format to pick from.
            {"_type": "playlist", "id": "PL1", "title": "A playlist"},
            {"_type": "url", "title": "no id, no url"},
            "not even a dict",
        ],
    }
    hits = entries(envelope)
    check("channels and playlists are not offered as videos", len(hits), 2)
    check("a hit keeps its channel", hits[0].channel, "Jon Gjengset")
    check(
        "a hit with only an id still gets a URL",
        hits[1].url,
        "https://www.youtube.com/watch?v=bbbbbbbbbbb",
    )
    check("a hit missing everything does not raise", hits[1].channel, "")
    check("an empty answer is empty, not an error", entries({}), [])

    # Age is approximate by construction — yt-dlp parses it out of YouTube's
    # own rounded "4 months ago" — so it is always marked, and a missing date
    # is never given one.
    now = 2_000_000_000
    day = 86400
    check("no date says so", age(None), "?")
    check("and a zero one does too", age(0), "?")
    check("hours old reads as under a day", age(now - 3600, now), "<1d")
    check("days are days", age(now - 5 * day, now), "~5d")
    check("a fortnight is weeks", age(now - 20 * day, now), "~2w")
    check("two months is months", age(now - 60 * day, now), "~2mo")
    check("years are years", age(now - 900 * day, now), "~2y")
    check("a length is the one spelling used elsewhere", clock(5434), "90m34s")
    check("a live stream says so instead", clock(None, live=True), "live")
    check("an unknown length is not zero", clock(None), "?")

    # One field for words and links both, so there is no mode key to remember.
    for text, wanted in (
        ("https://youtu.be/x", True),
        ("www.youtube.com/watch?v=x", True),
        ("youtube.com/watch?v=x", True),
        ("crust of rust", False),
        ("rust", False),
        ("", False),
        # A title can hold a colon or a dot without being a link.
        ("rust: the good parts", False),
        ("node.js streams", False),
    ):
        check(
            f"{text!r} is a link" if wanted else f"{text!r} is words",
            looks_like_url(text),
            wanted,
        )

    # The results row carries four facts, and the two that must survive a
    # 32-column phone are the length and the age: a title alone cannot be
    # chosen between, and a channel can afford to lose its end.
    # Dated off the real clock, because the row renderer asks :func:`age` for
    # the answer and :func:`age` asks the clock — a pinned timestamp here would
    # be dated in the future and read as "<1d".
    long_channel = Result(
        title="A very long title that will not fit any of these widths at all",
        channel="An Extremely Long Channel Name Indeed",
        url="https://x/y",
        duration=5434,
        timestamp=int(time.time()) - 900 * day,
    )
    for width in (32, 40, 48, 64, 80, 120):
        at_most(
            f"result row at {width}",
            max(len(line) for line in result_row(long_channel, width)),
            width,
        )
        at_most(
            f"a queued result row at {width}",
            max(len(line) for line in result_row(long_channel, width, True)),
            width,
        )
        drawn = " ".join(result_row(long_channel, width))
        check(f"the length survives {width} columns", "90m34s" in drawn, True)
        check(f"the age survives {width} columns", "~2y" in drawn, True)

    # -- remembering the last format ---------------------------------------- #

    check(
        "no memory opens at the top, as it always did",
        preferred_index(options, None),
        0,
    )
    check(
        "the exact format chosen last is where the cursor opens",
        options[preferred_index(options, {"label": "1080p webm", "kind": "merge"})].fmt,
        "248+251",
    )
    check(
        "a resolution that is offered differently is still found",
        _height_of(
            options[
                preferred_index(options, {"label": "360p mkv", "kind": "merge"})
            ].label
        ),
        360,
    )
    check(
        "asking for audio lands on audio",
        options[
            preferred_index(options, {"label": "audio 999k flac", "kind": "audio"})
        ].kind,
        "audio",
    )
    check(
        "a resolution nothing offers falls back to the top",
        preferred_index(options, {"label": "4320p mp4", "kind": "merge"}),
        0,
    )

    # The round trip, against a config file that is not the real one. Pointed
    # somewhere temporary for the same reason the queue is above: this runs on
    # the phone, and a self-test that rewrites a live setting is a self-test
    # that changes the thing it was checking.
    with tempfile.TemporaryDirectory() as raw:
        sys.path.insert(0, str(HERE))
        import expire_runner

        kept = expire_runner.CONFIG_FILE
        expire_runner.CONFIG_FILE = Path(raw) / "config.json"
        try:
            check("nothing remembered yet is not an error", recalled_format(), None)
            remember_format(by_fmt["248+251"])
            check(
                "the format chosen comes back next time",
                recalled_format(),
                {"label": "1080p webm", "kind": "merge"},
            )
            check(
                "and it is what the cursor opens on",
                options[preferred_index(options, recalled_format())].fmt,
                "248+251",
            )
        finally:
            expire_runner.CONFIG_FILE = kept

    # -- the background download -------------------------------------------- #

    # It runs dlqd's own action, by path under the queue root: a console script
    # would be the copy in site-packages, which manages a queue that is not
    # there. --yes because the confirm screen was the asking.
    spawn = now_argv("60-clip.py")
    check("a now-run is dlqd's own action", spawn[2:], ["now", "60-clip.py", "--yes"])
    check(
        "a now-run points at the queue root, not at an installed copy",
        spawn[1],
        str(HERE / "expire_sched.py"),
    )
    check(
        "a missing progress report reads as no report, not as a crash",
        now_progress("60-nothing-is-here.py"),
        None,
    )
    check(
        "a download with no report yet still draws a line",
        progress_line("60-clip.py", None, 40),
        "↓ starting…",
    )
    for width in (32, 40, 48, 64, 80, 120):
        at_most(
            f"the progress line at {width}",
            len(
                progress_line(
                    "60-a-fairly-long-item-name.py", (12345678, 500_000_000), width
                )
            ),
            width - 1,
        )

    check("slug is a filename", slugify(info["title"]), "a-talk-part-1-2-more-things")
    check("an empty title still names something", slugify(""), "video")
    check("slug does not end in a dash", slugify("hi -- ").endswith("-"), False)

    # Wherever this module is installed, the queue it writes into has to be the
    # one the runner reads — a copy in site-packages has neither.
    check(
        "the queue root is where the runner lives",
        (HERE / "expire_runner.py").is_file(),
        True,
    )
    # And where the queue manager lives, because a download-now spawns it from
    # there by path. Wrong, and the spawn fails into a log instead of on the
    # screen — the same silent shape, one directory along.
    check(
        "and where the queue manager lives",
        (HERE / "expire_sched.py").is_file(),
        True,
    )

    # --now writes an item and then asks expire_sched to run it, and
    # expire_sched imports this module at load. Only one of the two may do it
    # while loading or the pair is circular, and the failure would land at the
    # moment someone is waiting for a download rather than here.
    import expire_sched

    check("the queue manager imports back cleanly", expire_sched.ROOT, HERE)

    # --list writes nothing, so there would be nothing for --now to run.
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            main(["--list", "--now", "https://x/y"])
        code = None
    except SystemExit as exc:
        code = exc.code
    check("--list and --now together are refused", code, 2)

    #: A line that looks like the header but is not one: the parser stops at
    #: the first line that is not a comment, so nothing below the header block
    #: can claim to say what an item is.
    SOURCE_CASE = "\n".join(("#!/x", "# EXPIRE: v1", "", "d = 1  # SOURCE: no:no", ""))

    # The written item has to survive the runner's own admission check.
    with tempfile.TemporaryDirectory() as raw:
        keep = (QUEUE, STAGING)
        keep_env = os.environ.get("EXPIRE_HOME")
        os.environ["EXPIRE_HOME"] = raw
        try:
            check("EXPIRE_HOME moves the queue root", _root(), Path(raw).resolve())
        finally:
            if keep_env is None:
                del os.environ["EXPIRE_HOME"]
            else:
                os.environ["EXPIRE_HOME"] = keep_env
        QUEUE = Path(raw) / "queue"
        STAGING = QUEUE / ".staging"
        url_used = 'https://x/y?a=1&b="2"'
        try:
            source = render(url_used, "clip", exact, info["title"], "2026-08-01")
            path = write_item(40, "clip", source)
            # Off the phone the one thing the runner may object to is the
            # interpreter, which is Termux's and is not here; anything else it
            # says is still a failure. See :func:`shebang_here`.
            admits = (
                None
                if shebang_here()
                else f"shebang interpreter not found: {SHEBANG[2:].strip()!r}"
            )
            check("the runner would admit it", validate(path), admits)
            check("it is executable", os.access(path, os.X_OK), True)
            check(
                "shebang is the one Termux has",
                path.read_text().split("\n")[0],
                SHEBANG,
            )
            check("nothing is left staged", list(STAGING.iterdir()), [])
            # A title full of quotes and backslashes must not be able to write
            # a file that does not parse, or one whose URL was mangled.
            tree = ast.parse(source, str(path))
            check(
                "the title survived quoting",
                '"Talk"' in (ast.get_docstring(tree) or ""),
                True,
            )
            check("the url survived quoting", 'https://x/y?a=1&b="2"' in source, True)
            check(
                "the item imports from the queue root",
                f'sys.path.insert(0, "{HERE}")' in source,
                True,
            )
            # A progressive or audio-only format needs no merge and so passes
            # merge_ext=None. Rendered with json.dumps that is `null`, which
            # Python parses happily as a name it does not have — an item that
            # compiles, validates, waits its turn, and then dies.
            check(
                "a format that needs no merge still renders Python",
                json_leaks(render(url_used, "clip", by_fmt["18"], "t", "2026-08-01")),
                [],
            )
            check("and so does one that does", json_leaks(source), [])

            # ---------------------------------------------------- duplicates
            # The queue exists to spend metered data once. Queueing the same
            # video twice spends it twice, and the second time is invisible:
            # two items with different numbers and the same content, both
            # downloading, neither obviously wrong.
            key = source_key({"ie_key": "Youtube", "id": "HoVsWE1_JUk"})
            check("a video is known by extractor and id", key, "youtube:HoVsWE1_JUk")
            check(
                "however its URL was written",
                source_key({"extractor_key": "Youtube", "id": "HoVsWE1_JUk"}),
                key,
            )
            check("something with no id has no key", source_key({"title": "x"}), "")
            keyed = render(
                url_used, "clip", exact, info["title"], "2026-08-01", "video", key
            )
            check("an item carries what it is a download of", source_of(keyed), key)
            check("and one that has no key says nothing", source_of(source), "")
            check(
                "the header is a header, not any line that looks like one",
                source_of(SOURCE_CASE),
                "",
            )

            DONE.mkdir(parents=True, exist_ok=True)
            FAILED.mkdir(parents=True, exist_ok=True)
            (QUEUE / "50-keyed.py").write_text(keyed)
            found = find_duplicate(key, "anything-else")
            check(
                "the same video is found by its id", found and found.name, "50-keyed.py"
            )
            check("and reported as the strong match it is", found.how, "source")
            check(
                "a different video is not",
                find_duplicate("youtube:zzz", "nope"),
                None,
            )
            # The fallback, for items written before SOURCE existed: same name,
            # which is the same title, which is usually but not always the same
            # video — so it is said as the weaker thing it is.
            (DONE / "2026-08-01").mkdir(parents=True, exist_ok=True)
            (DONE / "2026-08-01" / "20-old-talk.py").write_text(source)
            named = find_duplicate("youtube:zzz", "old-talk")
            check(
                "an item with no id is found by name",
                named and named.name,
                "20-old-talk.py",
            )
            check("and said to be the weaker match", named.how, "name")
            check("with where it went", named.says(), "same name, done 2026-08-01")
            check(
                "an id match beats a name match",
                find_duplicate(key, "old-talk").how,
                "source",
            )

            # The door: this is the line every way of queueing goes through —
            # the search, a pasted URL, --now, --from-json and dlq — so a check
            # anywhere else is one each of them could be written around.
            raised = None
            try:
                write_item(60, "clip", keyed)
            except Duplicate as exc:
                raised = exc
            check(
                "the door refuses a second copy",
                raised and raised.name,
                "50-keyed.py",
            )
            check("nothing was written", (QUEUE / "60-clip.py").exists(), False)
            check("and nothing was left staged", list(STAGING.iterdir()), [])
            again = write_item(60, "clip", keyed, again=True)
            check("saying so anyway writes it", again.name, "60-clip.py")
            check(
                "which is then itself a duplicate",
                find_duplicate(key, "x").where,
                "queued",
            )
            # Every verdict has to fit the floor unwrapped: this is the line
            # that says why nothing is being queued, on the screen that has
            # nothing else on it.
            for where in ("queued", "done", "failed"):
                for how in ("source", "name"):
                    said = Duplicate(DONE / "2026-08-01" / "20-x.py", where, how).says()
                    check(
                        f"the {how} verdict for {where} fits a phone",
                        len(said) <= TIGHT_WIDTH,
                        True,
                    )
            check(
                "and the way past it is written on the screen",
                len("a  queue it again anyway") <= TIGHT_WIDTH,
                True,
            )
        finally:
            QUEUE, STAGING = keep

    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              ytq                       search, or paste a URL, then pick
              ytq crust of rust         search youtube (~0.1 MB)
              ytq URL                   probe (~0.1-0.5 MB), pick, queue
              ytq --now URL             pick, then start it in the background
              ytq --list URL            print formats and caps, write nothing
              ytq --list --from-json F  reprint a saved dump; costs no data

            plain file URLs queue with dlq instead; the queue itself is
            dlqd — bare for the screen, or dlqd (status|list|arm|logs).
            docs: ~/or3/docs/ytq.md and ~/or3/docs/download-queue.md"""),
    )
    parser.add_argument(
        "terms",
        nargs="*",
        metavar="URL-OR-WORDS",
        help="a page to download from, or words to search youtube for",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the formats and exit, writing nothing",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="start it in the background instead of waiting for the nightly "
        "window; the same as pressing n on the format list",
    )
    parser.add_argument(
        "--dest",
        metavar="DIR",
        help="put this one somewhere other than the configured video directory "
        "(dlqd dest sets that)",
    )
    parser.add_argument(
        "--from-json",
        metavar="FILE",
        help="use a saved 'yt-dlp -J' dump or search instead of asking again; "
        "costs no data",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="check the search, format and item logic, no network",
    )
    args = parser.parse_args(argv)
    first = " ".join(args.terms).strip()

    if args.self_test:
        return _self_test()

    if args.list:
        if args.now:
            parser.error("--list writes nothing, so there is nothing for --now to run")
        if not (first or args.from_json):
            parser.error("--list needs a URL or --from-json")
        if first and not looks_like_url(first):
            parser.error("--list prints one video's formats, so it needs a URL")
        return list_formats(first, args.from_json)

    preloaded = None
    if args.from_json:
        try:
            preloaded = load_json(args.from_json)
        except (ProbeError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if not sys.stdout.isatty():
        print("ytq needs a terminal; use --list <url> otherwise", file=sys.stderr)
        return 2

    os.environ.setdefault("ESCDELAY", "25")
    dest = str(Path(args.dest).expanduser()) if args.dest else "video"
    # A list, because a session can queue several items now. The download-now
    # case is already running by the time this returns: it was handed to a
    # detached `dlqd now`, so there is nothing left here to wait for.
    receipts = curses.wrapper(app, first or None, preloaded, args.now, dest)
    if not receipts:
        print("nothing queued")
        return 0
    for line in receipts:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
