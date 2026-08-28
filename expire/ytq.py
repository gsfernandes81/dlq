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

And it browses the subscription feed, which is the same list of the same things
and so is the same screen: ``subs`` in that one field asks YouTube for the
newest ``SUBS_RESULTS`` videos from the accounts you follow, bounded to one page
(~0.2 MB), marked with what the queue already holds, and picked from exactly as
a search result is. It is the one thing here that needs the *stored cookie* —
the jar the yt-dlp config already points at for the bot check — so whether there
is one is asked before a request is spent rather than after one comes back
empty. An empty feed is never reported as "nothing new": YouTube answers a
logged-out feed with no entries rather than with an error, so that answer means
the cookies have expired and the screen says so.

Usage::

    ytq                      # search, or paste a URL into the same field
    ytq crust of rust        # straight to the results
    ytq subs                 # straight to the subscription feed
    ytq <url>                # straight to the format list
    ytq --list <url>         # print the formats, write nothing
    ytq --list --subs        # print the feed, write nothing
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
import shlex
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
    # The feed's own pair. What changes is the middle key: a search is re-run
    # by retyping it, so `/` earns its place there; the feed has nothing to
    # retype and what it needs instead is a way to read it again.
    # ↑↓ is dropped from these two and nowhere else: it is the most guessable
    # key on a list and `m` is the one whose absence costs somebody the videos
    # they came here for.
    "subs": "⏎ quality  m more  r fresh  q back",
    "subs-running": "x stop  ⏎ quality  m more  q back",
    # And the same two with `m` spent. A key drawn in the hints that does
    # nothing when pressed is the shape somebody presses three times before
    # deciding the tool is broken — and this one used to look like the way to
    # the older videos it can no longer reach.
    "subs-end": "⏎ quality  r fresh  q back",
    "subs-end-running": "x stop  ⏎ quality  q back",
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
    "subs": "⏎ pick  m more  r new  q back",
    "subs-running": "x stop  ⏎ pick  m more  q back",
    "subs-end": "⏎ pick  r new  q back",
    "subs-end-running": "x stop  ⏎ pick  q back",
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

#: The subscription feed, as yt-dlp addresses it. A signed-in page: without
#: cookies YouTube answers it with nothing at all, which is why
#: :func:`cookie_state` is asked *before* a request is spent rather than after
#: one comes back empty.
SUBS_URL = "https://www.youtube.com/feed/subscriptions"

#: How much of the feed one look asks for. YouTube serves the feed a page at a
#: time and yt-dlp will follow every continuation it is offered, so this is a
#: bound and not a preference — see :func:`subs_argv`.
SUBS_RESULTS = 30

#: What ``m`` adds to that bound, and where it stops. The cap is not a
#: technical limit: it is there because every press costs more than the last
#: (see :func:`feed_cost`) and a list you can keep extending with one thumb is
#: a list somebody extends five times without meaning to.
SUBS_PAGE = 30
SUBS_MAX = 150

#: Roughly what one page of the feed costs on the wire. An estimate by analogy
#: with the search page and NOT measured on the vessel's link — the figure to
#: correct from ``docs/data-ledger.md`` when somebody does measure it. It is
#: printed rather than kept quiet because a key whose price is unknown to the
#: person pressing it is the one thing this whole tool exists to avoid.
SUBS_PAGE_MB = 0.2

#: A fetch of *count* videos is given this long. It scales because a timeout
#: that fires part way through has spent the bytes and kept none of them —
#: the worst outcome available here, and worse the deeper the ask.
SUBS_TIMEOUT_BASE = 120
SUBS_TIMEOUT_PER = 4

#: What the feed is called in the caches the session keeps, where a search is
#: keyed on the words that produced it. Not a query anybody can type, so it
#: cannot collide with one.
SUBS_KEY = ":subs"

#: Where the docs tell you to put the cookie jar, and the config that points at
#: it. Named here so that the screen saying the feed cannot be read sends
#: somebody to the same two files ``docs/ytq.md`` does; a screen that named a
#: different one of yt-dlp's several config paths would be sending them to a
#: file that works and that nobody else has ever edited.
COOKIE_SUGGESTION = "~/.config/yt-dlp/cookies.txt"
CONFIG_SUGGESTION = "~/.config/yt-dlp/config"

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


def ask(argv: list[str], timeout: int) -> dict:
    """Run a metadata-only yt-dlp and hand back the object it printed.

    One door, because the three things that ask — a probe, a search and the
    subscription feed — differ only in the argv they hand in, and every one of
    them fails in exactly the same four ways. Written out three times it was
    three places for "yt-dlp is not installed" to drift apart in.
    """
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
    if not isinstance(info, dict):
        raise ProbeError("yt-dlp returned something that is not a metadata object")
    return info


def probe(url: str, timeout: int = 180) -> dict:
    """Metadata only — ``-J`` downloads no media."""
    info = ask(
        [
            *ytdl_item.ytdl_argv(),
            "-J",
            "--no-playlist",
            "--no-colors",
            "--no-warnings",
            url,
        ],
        timeout,
    )
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
    return entries(ask(search_argv(query, count), timeout))


# --------------------------------------------------------------------------- #
# The subscription feed
# --------------------------------------------------------------------------- #
#
# The feed is the one screen here that cannot work on its own. A search and a
# probe are anonymous requests; the feed is a signed-in page, and what signs it
# in is the cookie jar the yt-dlp config already names for the bot check
# (docs/ytq.md, "Sign in to confirm you're not a bot"). So this half is mostly
# about that jar: finding out whether there is one *before* a request is spent,
# and never letting a logged-out answer be read as good news.


def tilde(path: Path | str) -> str:
    """A path with ``$HOME`` written as ``~`` — shorter, and how it is typed."""
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def config_paths() -> list[Path]:
    """The files yt-dlp reads its user config from, in its own order.

    Read rather than assumed, because the ``--cookies`` line is somebody's
    hand edit and the whole point of looking is to name the file it is missing
    from. Deliberately not exhaustive of yt-dlp's search (there is a portable
    config and a per-directory one) — those are not shapes this phone has, and
    a path listed here that nobody uses would be a path named in an error
    message that sends somebody to the wrong file.
    """
    home = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    return [
        xdg / "yt-dlp.conf",
        xdg / "yt-dlp" / "config",
        xdg / "yt-dlp" / "config.txt",
        home / "yt-dlp.conf",
        home / ".yt-dlp" / "config",
    ]


#: The two ways a config can say where the signed-in session comes from. Both
#: count: ``--cookies-from-browser`` is not a file this can stat, but it is a
#: declaration, and a tool that refused it would be refusing a working setup.
COOKIE_FLAGS = ("--cookies", "--cookies-from-browser")


def declared_cookies(paths: list[Path] | None = None) -> tuple[str, str, Path] | None:
    """``(flag, value, the config that said so)`` for the first one found.

    Parsed with :mod:`shlex` per line the way yt-dlp parses these files, so a
    quoted path with a space in it reads the same here as it does there, and a
    ``#`` comment holding the word ``--cookies`` is a comment in both. Reading
    it any more loosely would mean this screen and yt-dlp disagreeing about
    whether cookies are configured, which is a worse answer than not looking.
    """
    for path in config_paths() if paths is None else paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError:
                continue
            for index, token in enumerate(tokens):
                flag, _, inline = token.partition("=")
                if flag not in COOKIE_FLAGS:
                    continue
                value = inline or (
                    tokens[index + 1] if index + 1 < len(tokens) else ""
                )
                if value:
                    return flag, value, path
    return None


def written(mtime: float, now: float | None = None) -> str:
    """How long ago a file was last written, in words.

    Cookies expire, and every symptom of that looks like something else — a
    short feed, an empty one, a video that lists one 320p format. This is the
    fact that turns "the feed is empty" into "the cookies are three weeks old",
    which is the sentence somebody can act on.
    """
    days = int(max(0.0, (time.time() if now is None else now) - mtime) // 86400)
    if days < 1:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def cookie_state(paths: list[Path] | None = None) -> tuple[str, str]:
    """``(state, what to say about it)``: can the feed be asked for at all?

    ``file`` and ``browser`` are a go; ``missing`` and ``none`` are not. Asked
    *before* the request, on the same rule as :func:`already_queued`: a feed
    request with no session behind it is not a small failure, it is a whole
    page of YouTube bought in order to be told to sign in — and then answered
    with an empty list rather than an error, so the money buys no explanation
    either.

    The detail is a phrase and not a sentence, because both callers put it in
    the middle of one.
    """
    declared = declared_cookies(paths)
    if declared is None:
        looked = [
            path for path in (config_paths() if paths is None else paths)
            if path.is_file()
        ]
        if looked:
            return "none", "no --cookies line in " + ", ".join(
                tilde(path) for path in looked
            )
        # Named from the constant rather than by indexing the list: the list
        # is where yt-dlp looks and this is where the docs say to write, and
        # only one of those is a sentence to put in front of somebody.
        return "none", f"there is no yt-dlp config at {CONFIG_SUGGESTION}"
    flag, value, config = declared
    if flag == "--cookies-from-browser":
        return "browser", f"--cookies-from-browser {value}, from {tilde(config)}"
    jar = Path(value).expanduser()
    try:
        stat = jar.stat()
    except OSError:
        return "missing", f"{tilde(jar)} is named by {tilde(config)} but is not there"
    if stat.st_size == 0:
        return "missing", f"{tilde(jar)} is empty"
    return "file", f"{tilde(jar)}, written {written(stat.st_mtime)}"


#: Why the feed alone needs anything. Its own entry rather than part of
#: :func:`cookie_fix`, because the two screens that do not need it each say it
#: better in their own words — the refusal has yt-dlp's line and the empty feed
#: has a paragraph about what an empty feed is — and a notice has to fit a
#: phone whole, which is checked.
COOKIE_WHY = "the feed is a signed-in page — no cookies, no answer."


def cookie_fix(detail: str) -> list[str]:
    """What was found, and how to renew it.

    One spelling, appended to every screen that cannot read the feed — a
    refusal, an absent jar and an empty answer are three symptoms with one
    remedy, and three wordings of it would be three chances for two of them to
    go stale.

    Short because it has to fit a phone whole: a fix the screen had to
    truncate is not a fix. The mode and the "never in the repo" stay in it at
    that price — the jar holds live google session tokens, and yt-dlp rewrites
    it in place as they refresh.
    """
    return [
        detail,
        f"export cookies.txt from the browser, netscape format, mode 600 and "
        f"never in the repo: {COOKIE_SUGGESTION}, named by --cookies in "
        f"{CONFIG_SUGGESTION}.",
        "docs: ~/or3/docs/ytq.md",
    ]


def cookie_advice(state: str, detail: str) -> list[str]:
    """Why the feed cannot be read, and the one thing that fixes it.

    A screen rather than a line, and the fix written on it, for the reason
    :func:`duplicate_screen` gives: this is a dead end, and a dead end that
    does not say the way out is one somebody concludes is a broken tool.
    """
    return [
        {
            "none": "no cookies are configured",
            "missing": "the cookie jar is not there",
        }.get(state, "the subscription feed cannot be read"),
        COOKIE_WHY,
        *cookie_fix(detail),
    ]


def empty_feed_advice(detail: str) -> list[str]:
    """What an empty feed means, which is never "nothing new".

    YouTube answers a logged-out feed with an empty tab rather than with an
    error, so the honest-looking reading of that answer — you are up to date —
    is the one reading it cannot have. Saying it would hide the only fix there
    is behind a screen that looks like good news, on a tool somebody opens
    once a day.
    """
    return [
        "the feed came back empty",
        "a signed-out session, not an empty subscription list: youtube "
        "answers a logged-out feed with no entries rather than an error, so "
        "this is what expired cookies look like.",
        *cookie_fix(detail),
    ]


def subs_argv(count: int = SUBS_RESULTS) -> list[str]:
    """The command one look at the feed runs, kept separate so it can be checked.

    ``--playlist-end`` is the whole cost argument, and it is the same shape of
    trap :func:`search_argv` documents: without it this works perfectly and
    follows every continuation YouTube will serve, which for a few years of
    subscriptions is hundreds of pages on a metered radio. Bounded, yt-dlp
    stops pulling once it has *count*, so one look is one request.

    ``--flat-playlist`` is the other half: the feed is a list of videos and
    extracting each one is what the format screen is for, later, on the one
    that gets picked.

    Nothing here disables the user config, and here that is not a nicety — the
    ``--cookies`` line in it is the only reason this page answers at all.
    """
    return [
        *ytdl_item.ytdl_argv(),
        SUBS_URL,
        "--flat-playlist",
        "--playlist-end",
        str(count),
        "-J",
        "--no-colors",
        "--no-warnings",
        *APPROX_DATE_ARGS,
    ]


def subscriptions(count: int = SUBS_RESULTS, timeout: int | None = None) -> list[Result]:
    """The newest *count* videos from the subscription feed, newest first.

    Longer than a search's timeout because the tab page is bigger and the
    connection is the vessel's, not a datacentre's — and longer again the more
    is asked for, because giving up part way through has spent every byte
    walked so far and kept nothing.
    """
    if timeout is None:
        timeout = SUBS_TIMEOUT_BASE + SUBS_TIMEOUT_PER * count
    return entries(ask(subs_argv(count), timeout))


def feed_cost(count: int) -> str:
    """Roughly what reading *count* videos off the feed costs, in words.

    The **total**, never the increment, and that is the whole point of the
    function. YouTube's continuations are sequential: there is no asking for
    videos 31 to 60 without walking 1 to 30 to reach them, so going deeper
    re-buys everything already on screen. A key labelled "+30" while spending
    the lot is a bill nobody agreed to.

    Marked ``~`` for the same reason every size on the format list is: the
    per-page figure is an estimate, not a measurement.
    """
    return f"~{count / SUBS_RESULTS * SUBS_PAGE_MB:.1f} MB"


def feed_meta(
    count: int, when: str, more: int | None, at_cap: bool, width: int
) -> str:
    """The feed's second line: how much is on it, how old, and what more costs.

    Pure and width-aware rather than composed at the point of drawing, because
    the thing it carries is a **price** and a price clipped off the end of a
    line is worse than no price at all — which is exactly what happened the
    first time this was written inline: ``m 60 for ~0.4 …`` on a 40-column
    phone, the figure lost and the key still offered.

    It replaces "~ dates are approximate", which every row already says for
    itself with a ``~`` beside its age. This does not say itself anywhere.
    """
    if more:
        # No spaces inside the figure at the floor: this line is read, not
        # parsed, and the columns are better spent on the number than on the
        # gap in front of MB.
        deeper = (
            f"m {more} for {feed_cost(more)}"
            if width >= WIDE
            else f"m {more} {feed_cost(more).replace(' ', '')}"
        )
    else:
        deeper = "at the cap" if at_cap else "the whole feed"
    parts = [f"{count} videos"]
    # The age is the first thing to go, and it is the right one: it is a
    # comfort, where the other two are the answer and the price.
    if when and width >= TIGHT:
        parts.append(f"read {when}" if width >= WIDE else when)
    parts.append(deeper)
    return ("  ·  " if width >= WIDE else " · ").join(parts)


def next_page(got: int, asked: int) -> tuple[int | None, bool]:
    """``(the total a deeper look would ask for, whether the cap stopped it)``.

    ``(None, False)`` is the end of the feed: YouTube handed back fewer than
    it was asked for, so there is nothing further back to reach and asking
    again would buy the same bytes to learn the same thing. That is the case
    worth getting right — a ``m`` that stays live at the bottom of the feed
    spends real quota on nothing, every press, and looks exactly like one that
    is working.

    ``(None, True)`` is the cap, which is a different sentence: there IS more
    and this will not spend it. The screen says which of the two it is.
    """
    if got < asked:
        return None, False
    if asked >= SUBS_MAX:
        return None, True
    return min(SUBS_MAX, asked + SUBS_PAGE), False


#: The words the one entry field takes for the feed. ``:ytsubs`` is yt-dlp's
#: own spelling and is here so that what somebody already knows works.
FEED_WORDS = ("subs", ":subs", "subscriptions", ":subscriptions", ":ytsubs")


def looks_like_feed(text: str) -> bool:
    """Whether the entry field is being asked for the subscription feed.

    A third answer out of the same box, on :func:`looks_like_url`'s rule: the
    alternative on a phone is a mode key to remember, and the field is already
    telling two things apart by looking. ``subs`` is read as the feed rather
    than as words to search for, which is a guess — and the cheap one, since
    being wrong costs one press of ``/`` and being wrong the other way costs a
    search nobody wanted.

    Asked *before* :func:`looks_like_url`, because the feed's own URL is a URL
    too and probing it gets the playlist refusal instead of the feed.
    """
    text = text.strip().lower()
    return text in FEED_WORDS or "/feed/subscriptions" in text


def freshness(fetched: float | None, now: float | None = None) -> str:
    """How old the listing on screen is, or ``""`` when it was never fetched.

    Said for the feed and not for a search. A search is a question you re-ask
    by retyping it; a feed is a thing that changes under you, and it is cached
    for the session precisely so that backing out of a video costs nothing —
    which means the screen has to say how old what it is showing is, or the
    cache is just a quiet lie about the time.
    """
    if not fetched:
        return ""
    seconds = max(0.0, (time.time() if now is None else now) - fetched)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    return f"{int(seconds // 3600)}h ago"


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


#: Extractors this can say anything useful about. The claim :func:`withheld`
#: makes is specific to YouTube — it always has adaptive streams, so their
#: total absence is evidence — and it is simply false of the many sites that
#: legitimately serve one progressive file. Named rather than assumed, so a
#: plain .mp4 URL is never accused of a bot check.
ADAPTIVE_ALWAYS = ("youtube",)


def withheld(info: dict) -> bool:
    """Whether YouTube answered with a fraction of the formats it really has.

    The signature of a failed bot check: with no PO token the adaptive streams
    are never sent, and what comes back is the legacy progressive format and
    the storyboards. A YouTube video always has adaptive streams, so none at
    all is not a video that only exists in 360p — it is an extraction that was
    refused politely.

    Read off the raw formats and deliberately **not** off :func:`choices`,
    which drops what it cannot size. Those are the two ways to end up looking
    at a single 360p row and they have completely different fixes, so the one
    thing this must not do is conflate them: `choices` reports the second as
    its ``unsized`` count and this reports the first.
    """
    who = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    if not any(name in who for name in ADAPTIVE_ALWAYS):
        return False
    adaptive = playable = 0
    for fmt in info.get("formats") or []:
        vcodec, acodec = fmt.get("vcodec"), fmt.get("acodec")
        if vcodec == "none" and acodec == "none":
            continue  # storyboards, which arrive either way
        playable += 1
        # Exactly one side absent is an adaptive stream; neither absent is the
        # progressive one that survives a refusal.
        if (vcodec == "none") != (acodec == "none"):
            adaptive += 1
    return playable > 0 and adaptive == 0


#: A ``uv tool`` venv carries this at its root; an ordinary venv does not.
#: Chosen over a path substring because it is uv's own bookkeeping rather than
#: a guess about where uv keeps things — ``UV_TOOL_DIR`` moves that — and
#: because it *lists the requirements*, so the ``--with`` packages a reinstall
#: has to re-assert are read off the machine instead of being remembered by
#: whoever last wrote the upgrade line.
#:
#: Deliberately not ``yt-dlp --version``, whose banner says ``(pip)`` under a
#: uv tool as well (measured 2026-08-28): the one thing in that output that
#: looks like an answer to this question and is not.
UV_RECEIPT = "uv-receipt.toml"


def uv_receipt(python: str) -> Path | None:
    """The receipt of the uv tool venv *python* belongs to, if it is one."""
    if not python:
        return None
    receipt = Path(python).parent.parent / UV_RECEIPT
    return receipt if receipt.is_file() else None


def uv_requirements(receipt: Path) -> list[str]:
    """The packages a uv tool was installed with, in uv's own order.

    Read rather than remembered, because the failure this exists to prevent is
    a reinstall that silently drops a ``--with`` package — and a hard-coded
    list is that same failure with extra steps. Scoped to the ``requirements``
    block: ``entrypoints`` below it carries the tool's own name again.
    """
    try:
        text = receipt.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    start = text.find("requirements")
    opened = text.find("[", start) if start >= 0 else -1
    closed = text.find("]", opened) if opened >= 0 else -1
    if closed < 0:
        return []
    return re.findall(r'name\s*=\s*"([^"]+)"', text[opened:closed])


def shebang_of(binary: str) -> str:
    """The interpreter a console script names, or ``""`` if it names none."""
    try:
        with open(binary, "rb") as handle:
            first = handle.readline(4096).decode("utf-8", "replace")
    except OSError:
        return ""
    return first[2:].strip().split()[0] if first.startswith("#!") else ""


def install_of(command: str) -> tuple[str, str]:
    """``(how it was installed, the interpreter behind it)``.

    ``uv-tool``, ``pip`` or ``absent``. The question matters because the two
    have different upgrade commands and neither works on the other, and the
    obvious place to look — the tool's own version banner — cannot answer it.
    """
    binary = shutil.which(command)
    if not binary:
        return "absent", ""
    python = shebang_of(binary)
    return ("uv-tool" if uv_receipt(python) else "pip"), python


def short_python(python: str) -> str:
    """The interpreter as somebody would type it, if that means the same file.

    A Termux shebang is 45 characters — longer than this screen's whole fold —
    and a path broken mid-word is a path retyped wrong. The basename is what
    fits and what a person types, so it is used *only* when ``which`` resolves
    it back to the same file, and the full path stands when it does not.
    """
    if not python:
        return "python3"
    name = Path(python).name
    # Compared as written and never resolved: a venv's `python3` is a symlink
    # to the base interpreter, so `resolve()` makes every venv look like the
    # system python — and `python3 -m pip` under the system python installs
    # into a different place entirely, which is the whole failure this line
    # is meant to avoid.
    return name if shutil.which(name) == python else python


def upgrade_command(command: str) -> str:
    """The line that actually upgrades *command* on this machine.

    ``uv tool upgrade`` is deliberately not what this emits: a tool installed
    with any version constraint answers "Nothing to upgrade" and does nothing
    at all, silently (measured 2026-08-28). ``install --force`` moves it
    whatever the original requirement said — and re-asserts the ``--with``
    packages, which a plain reinstall drops, trading one silent failure for
    the other one.
    """
    kind, python = install_of(command)
    if kind == "absent":
        return f"{command} is not on PATH"
    receipt = uv_receipt(python)
    if receipt is not None:
        withs = "".join(
            f" --with {name}" for name in uv_requirements(receipt) if name != command
        )
        return f"uv tool install {command}{withs} --force"
    return f"{short_python(python)} -m pip install -U {command}"


def checkout_root() -> Path | None:
    """The repository this is running out of, found by its own ``.git``.

    Walked rather than counted from the queue root's depth. Two directories up
    is right on the phone and is ``/`` the moment anything else is true — and
    ``git -C / pull`` in a fix somebody is about to type is worse than
    offering no path at all. ``.exists`` rather than ``.is_dir`` because a
    worktree's ``.git`` is a file.
    """
    for folder in (HERE, *HERE.parents):
        if (folder / ".git").exists():
            return folder
    return None


def own_upgrade() -> str:
    """How to update *this* tool, which is a different question again.

    An editable install runs out of the checkout, so what updates it is a pull
    and not a reinstall. Told apart by where this file actually is: under the
    uv tools directory is a copy that has to be reinstalled, anywhere else is
    the checkout it was installed from.
    """
    venv = Path(sys.executable).parent.parent
    here = Path(__file__).resolve().parent
    copied = (venv / UV_RECEIPT).is_file() and str(here).startswith(str(venv.resolve()))
    if copied:
        return "uv tool install or3-expire-queue --force"
    root = checkout_root()
    return f"git -C {tilde(root)} pull" if root else "reinstall ytq from the checkout"


def tool_version(command: str, timeout: int = 20) -> str:
    """What ``<command> --version`` says. Free: no network, no URL."""
    binary = shutil.which(command)
    if not binary:
        return ""
    try:
        done = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or "").strip().splitlines()[0].strip() if done.stdout else ""


def withheld_note(width: int) -> str:
    """The one line the format list carries while the answer is a refusal.

    Three widths, measured like the hints are, because the first version was
    one string of 39 columns drawn into 38 and a phone rendered "youtube
    withheld the res" — a warning clipped mid-word, on a screen whose whole
    job at that moment is to be believed.

    Short on purpose. The notice has already said the whole of it once this
    session; what this has left to do is stop somebody queueing 360p while
    believing it is the best on offer, and "bot check" does that in two words.
    At the floor it is those two words and nothing else.

    No symbol in front of it. ``⚠`` is ambiguous-width and routinely rendered
    as a double-width emoji, which would put the clip back; and this repo's
    own rule is that the status is the word, never the decoration.
    """
    if width >= WIDE:
        return "bot check — youtube sent one format, not all of them"
    return "bot check — one format only" if width >= TIGHT else "bot check"


def withheld_advice(
    detail: str,
    version: str | None = None,
    upgrade: str | None = None,
    mine: str | None = None,
) -> list[str]:
    """What a thin answer means, and what to do — in order of likelihood.

    **The version goes first, and that is a correction.** This notice used to
    lead with the cookies and the JS runtime, and on 2026-08-28 it sent
    somebody down both of those with a correct config and a six-week-old
    yt-dlp. YouTube breaks yt-dlp faster than anything else this repo depends
    on, so "how old is it" is at once the likeliest answer and the cheapest to
    ask — ``--version`` needs no URL and no network.

    The upgrade line is composed from how the thing is *actually* installed
    (:func:`upgrade_command`) rather than guessed, because a uv tool and a pip
    install take different commands and neither works on the other. Same again
    for ``ytq`` (:func:`own_upgrade`), where an editable install is updated by
    a pull and not by a reinstall at all.

    Everything is injectable so the wording can be measured on a machine with
    no yt-dlp on it.
    """
    if version is None:
        version = tool_version("yt-dlp") or "unknown"
    if upgrade is None:
        upgrade = upgrade_command("yt-dlp")
    if mine is None:
        mine = own_upgrade()
    return [
        "youtube sent one format, not all of them",
        f"yt-dlp {version} — usually just stale. upgrade:",
        upgrade,
        f"then the cookies: {detail}",
        "and yt-dlp -v must show a JS runtime and yt_dlp_ejs.",
        # One entry, because the worst case — both of these being uv
        # commands — is a row over the phone's budget with two. The em-dash
        # is spaced so the path cannot read as part of the command.
        f"ytq itself: {mine} — docs: ~/or3/docs/ytq.md",
    ]


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


#: The two destinations ytq queues into, and the one it uses when nothing is
#: said. ``dlq`` fills the third (``file``); the runner owns the full list.
VIDEO_DEST = "video"
AUDIO_DEST = "audio"


def dest_for(choice: Choice, asked: str = VIDEO_DEST) -> str:
    """Which destination an item declares: a kind, or the path asked for.

    An audio-only pick is not a video and does not belong in the folder videos
    go to — the same argument ``DEST_KINDS`` already makes about a film and an
    installer, one step further in. A phone makes it sharper than a desktop
    would: the music player and the video player look in different places, and
    a song delivered among the films is one nothing will offer to play.

    Decided from :attr:`Choice.kind`, which is the row that was actually
    chosen, and deliberately not from the file extension — at queue time there
    is no file yet to have one, and the runner resolves this at delivery
    precisely so that changing the setting moves what is already queued.

    ``--dest`` names a directory and wins over both, which is the runner's own
    rule about an absolute path in the header, kept rather than re-decided.
    """
    if asked != VIDEO_DEST:
        return asked
    return AUDIO_DEST if choice.kind == "audio" else VIDEO_DEST


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


def wrapped(text: str, width: int) -> list[str]:
    """*text* broken to *width*, but never inside a word.

    ``break_on_hyphens`` off, which is the whole reason this is a function.
    What these screens wrap ends in a path, a flag or a file stem —
    ``~/.config/yt-dlp/cookies.txt``, ``--cookies``,
    ``10-crust-of-rust-subtyping`` — and the default splits all three at a
    hyphen. A name wrapped mid-word is a name somebody retypes wrong, on the
    screens whose entire job is telling them what to type.

    Everything here that wraps prose goes through it, which is also what stops
    the name being shadowed by the three loop variables it replaced.
    """
    return textwrap.wrap(text, width, break_on_hyphens=False) or [""]


#: One step of a scrolling title, and how many steps it holds still at each
#: end of a lap before moving again. Slow on purpose: this is a list somebody
#: is reading, not an animation, and a title that snaps past faster than it
#: can be read has cost a wakeup to say nothing.
MARQUEE_MS = 300
MARQUEE_HOLD = 5

#: What separates the end of a scrolling title from its own beginning, so that
#: the lap is visible as a lap. Without it a wrapped title reads as one long
#: string with a nonsense join in the middle of it.
MARQUEE_GAP = "   ·   "


def marquee(text: str, width: int, tick: int) -> str:
    """*width* characters of *text*, scrolled to step *tick*.

    Text that fits is returned whole and unmoved, which is what makes this
    safe to call on every row: the motion is a property of the title being too
    long, never of the row being selected.

    A lap holds at the start for :data:`MARQUEE_HOLD` steps before it moves —
    the beginning of a title is the part a choice is usually made on, and a
    line already sliding when the eye arrives is one that has to be waited out
    for a whole lap to read.

    Deliberately *not* :func:`fit`: no ellipsis. The ellipsis is how a clipped
    line says something was lost, and a line that is visibly moving has
    already said it.
    """
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    loop = text + MARQUEE_GAP
    phase = max(0, tick) % (len(loop) + MARQUEE_HOLD)
    offset = 0 if phase < MARQUEE_HOLD else phase - MARQUEE_HOLD
    return (loop + loop)[offset : offset + width]


def title_room(result: Result, width: int) -> int:
    """The columns a result's title gets on the row that draws it.

    One function, because two things have to agree about it and they are in
    different places: :func:`result_row`, which draws the title, and
    :func:`results`, which decides whether to keep waking up to scroll it.
    Written twice they drift, and the failure is silent both ways — a title
    that moves for no reason, or an idle screen paying a wakeup every 300ms
    for ever.
    """
    if width >= WIDE:
        tail = (
            f"{fit(result.channel or '?', 16):<16}  "
            f"{age(result.timestamp):>5}  {clock(result.duration, result.live):>7}"
        )
        return max(8, width - 5 - len(tail))
    # Two columns for the mark, one in hand at the right — the same arithmetic
    # the narrow branch of `result_row` lays out with.
    return max(4, width - 1 - 2)


def scrolls(result: Result, width: int) -> bool:
    """Whether this title is longer than the room it has, and so has to move."""
    return len(result.title) > title_room(result, width)


def viewport(cursor: int, top: int, listed: int, count: int) -> tuple[int, int]:
    """Clamp *cursor* into the list and slide *top* until the cursor is on screen.

    Extracted from the two listing screens that had it written out identically,
    and then given a second job: a position **restored** from a previous visit
    arrives with a ``top`` that may be nonsense — the list has grown under it,
    or shrunk, or it was saved at a different terminal size. This is what makes
    that safe. Handing back a cursor the screen does not draw would be worse
    than forgetting the position, because every key would still work and
    nothing would appear to move.

    The window is held inside the list as well as around the cursor — the
    ``count - listed`` term — which the two screens did not need while the
    only thing moving ``top`` was the cursor one row at a time. A place
    restored onto a list that has since shrunk reaches it immediately: without
    it, a saved cursor of 90 landing in a list of 60 puts the last row alone
    at the top of an otherwise empty screen.
    """
    cursor = max(0, min(cursor, count - 1))
    top = max(min(top, cursor, max(0, count - listed)), cursor - listed + 1, 0)
    return cursor, top


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


def result_row(
    result: Result, width: int, queued: bool = False, tick: int | None = None
) -> list[str]:
    """One search hit, as the one or two lines there is room for.

    Four facts and a 40-column phone do not share a line without mutilating the
    title, and the title is the one a choice is actually made from — so below
    :data:`WIDE` the title gets a line of its own and everything else sits
    under it.

    The tail is composed before the channel is fitted into what is left, rather
    than clipping the finished line: clipping would drop the length and the age,
    which are two of the four things this screen exists to say. The channel is
    the one that can afford to lose its end.

    *tick* is the scroll step for a title too long for its room, and ``None``
    is "do not move" — which is every row but the one under the cursor, and
    every caller with no clock at all (:func:`list_results`, printing to a
    pipe). Only one row moves because a list where every line slides is a list
    nothing can be read off, and the cursor is the row a choice is being made
    on.
    """
    when = age(result.timestamp)
    long = clock(result.duration, result.live)
    channel = result.channel or "?"
    mark = "✓" if queued else " "
    room = title_room(result, width)
    title = fit(result.title, room) if tick is None else marquee(
        result.title, room, tick
    )

    if width >= WIDE:
        tail = f"{fit(channel, 16):<16}  {when:>5}  {long:>7}"
        # mark, a space, the title, two spaces, the tail — and one column in
        # hand, because a line drawn into the last cell is a wrapped line.
        return [f"{mark} {title:<{room}}  {tail}"[: width - 1]]

    # Two columns for the mark whether or not there is one, so that queueing a
    # result does not shift its title one to the right of its neighbours'.
    prefix = "✓ " if queued else "  "
    tail = f"{when} · {long}"
    # The channel's room, not the title's — a different line and a different
    # sum, named apart so the two cannot be confused for one another.
    channel_room = max(3, width - 7 - len(tail))
    return [
        (prefix + title)[: width - 1],
        # Packed left and not padded into a column: the age and the length are
        # different lengths on every row, so a column here would put the dots
        # in a different place on each line and read as a ragged table rather
        # than as the sentence it is.
        f"   {fit(channel, channel_room)} · {tail}"[: width - 1],
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
        meta = f"{length}  ·  {len(options)} format{'' if len(options) == 1 else 's'}"
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
        _addstr(win, 1, 1, fit(meta, width - 2), curses.A_DIM)
        # Its own line and not another `·` clause on the meta, because it is
        # not a detail about this list — it is the reason the list is wrong.
        # Bold and cost-red where there is colour, and saying it in words
        # where there is not, on the rule the size column already follows.
        if withheld(info):
            _addstr(
                win,
                2,
                1,
                fit(withheld_note(width), width - 2),
                curses.A_BOLD | paint.get("nights", 0),
            )

        listed = max(1, height - 6)
        cursor, top = viewport(cursor, top, listed, len(options))

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


#: The confirmation's two field labels, drawn at x=2 with their values in a
#: column to the right of them.
CONFIRM_LABELS = ("priority", "file name")

#: Where that column starts on a phone. **Derived, not typed**: it was 11,
#: reasoned as "file name is nine columns", which is true and one short —
#: nine columns starting at 2 end at 10, so 11 is flush against the label and
#: the screen rendered `file namecrust-of-rust-subtyping`. Computed from the
#: labels themselves so that arithmetic cannot be got wrong again, and so that
#: a third label is a wider gutter rather than another one touching.
CONFIRM_GUTTER = 2 + max(len(label) for label in CONFIRM_LABELS) + 1


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
        # As far left as the column can go on a phone, and roomier where there
        # is room. Both clear the longest label, which a check pins.
        gutter = CONFIRM_GUTTER if narrow else 14
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
        _addstr(win, 6, 2, CONFIRM_LABELS[0], curses.A_DIM)
        _addstr(win, 7, 2, CONFIRM_LABELS[1], curses.A_DIM)
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
            for offset, piece in enumerate(wrapped(note, max(20, width - 4))):
                _addstr(win, 13 + offset, 2, piece, curses.A_DIM)
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
        # Through `wrapped`, not textwrap: the second of these is `as
        # 10-crust-of-rust-subtyping`, and the default breaks it at a hyphen.
        for piece in wrapped(text, max(12, width - 4)):
            _addstr(win, row, 2, piece, curses.A_BOLD if row == 2 else 0)
            row += 1
    row += 1
    if dup.how == "name":
        # Weaker evidence, said as such: the id is what proves two items are
        # the same video, and an item queued before SOURCE existed has none.
        for piece in wrapped(
            "matched by name, not by id — it may be a different video with "
            "the same title",
            max(12, width - 4),
        ):
            _addstr(win, row, 2, piece, curses.A_DIM)
            row += 1
        row += 1
    _addstr(win, min(row, height - 4), 2, "a  queue it again anyway")
    _addstr(win, height - 2, 1, "a  again   any other key: back", curses.A_DIM)
    win.refresh()
    return win.getch() == ord("a")


def message_body(lines: list[str], width: int, rows: int) -> list[str]:
    """*lines* wrapped and blank-separated, bounded to *rows* of them.

    Pure, so the one thing that must never happen on this screen can be
    checked without a terminal. :func:`message` draws its own way out under
    the body; a body allowed to run past the bottom takes that line with it
    and leaves a full-screen notice with no visible key to leave it — which is
    the same failure as a hint clipped at the floor, arrived at from the other
    direction, and it turned up here the moment a notice grew long enough to
    matter.

    Overflow is said and not dropped quietly, because the line that goes first
    is the last one and on these screens the last line is the fix.
    """
    out: list[str] = []
    for index, line in enumerate(lines):
        if index:
            out.append("")
        out.extend(wrapped(line, width))
    if len(out) <= rows:
        return out
    keep = max(0, rows - 1)
    return out[:keep] + [fit(f"…{len(out) - keep} more — see ~/or3/docs/ytq.md", width)]


def message(win, lines: list[str]) -> None:
    """A full-screen notice, wrapped to whatever width there is.

    Wrapped rather than clipped: these are the sentences that explain why
    nothing was queued, and half of one is no explanation at all.
    """
    win.erase()
    height, columns = win.getmaxyx()
    width = max(20, columns - 4)
    body = message_body(lines, width, max(1, height - 4))
    # The whole first entry is the verdict, however many lines it wrapped to.
    head = len(wrapped(lines[0], width)) if lines else 0
    for row, text in enumerate(body, start=1):
        _addstr(win, row, 2, text, curses.A_BOLD if row <= head else 0)
    _addstr(win, height - 2, 1, "any key to continue", curses.A_DIM)
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
    # Three answers now, and the third is written here or it does not exist:
    # `subs` is a word typed into a field, with nothing on any screen to
    # discover it from. The costs stand under it for the same reason they
    # always did — before anything is spent, rather than in a dialog after.
    if width < 40:
        _addstr(win, 6, 2, "search ~0.1 MB · a URL ~0.3 MB", curses.A_DIM)
        _addstr(win, 7, 2, "subs → your feed ~0.2 MB", curses.A_DIM)
    else:
        _addstr(win, 6, 2, "words → youtube      ~0.1 MB", curses.A_DIM)
        _addstr(win, 7, 2, "a URL → the formats  ~0.1-0.5 MB", curses.A_DIM)
        _addstr(win, 8, 2, "subs  → your feed    ~0.2 MB", curses.A_DIM)
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
    feed: bool = False,
    fetched: float | None = None,
    more: int | None = None,
    at_cap: bool = False,
    place: tuple[int, int] = (0, 0),
) -> tuple[int | str | None, tuple[int, int]]:
    """A list of videos, and where it was left. An index, ``"/"``, ``"r"`` or
    ``"m"``, or ``None`` to go back — paired with the place to hand back in.

    Below :data:`WIDE` each result takes two lines, so ten of them are ten
    titles a thumb can read rather than twenty truncated columns. The cursor
    reverses both lines of the one it is on; ``✓`` is the only other mark, and
    only on what this session has already queued.

    One screen for the search results and the subscription feed, because they
    are the same list of the same things and everything below them — the
    duplicate marks, the format screen, the confirmation — takes a
    :class:`Result` and does not care where it came from. *feed* changes the
    four things that are genuinely different: what the banner calls it, that
    the listing has an age worth saying (*fetched*), that the middle key reads
    it again instead of searching again, and that there is more of it to be
    bought (*more*, the total a deeper look would ask for, with *at_cap*
    telling the two ways of having none apart — see :func:`next_page`).

    The title under the cursor scrolls when it does not fit. The screen only
    wakes up to do that while there is actually something scrolling, so a list
    of short titles still blocks on the keyboard and costs nothing at all,
    which is the property this loop was written for in the first place.

    *place* is handed back with the answer rather than kept here, because
    every way out of this screen comes back to it: queueing a video redraws
    the list, and ``m`` and ``r`` fetch and redraw it. Starting at the top
    each time turned "find three things to download in one search" — which is
    what this screen is *for* — into three scrolls back to where you were, and
    made ``m`` useless past the first page.

    ``←`` and ``→`` jump a screenful, the same as page up and page down and
    for the same reason those are here: with ``m`` the list runs to
    :data:`SUBS_MAX`, and on a phone the page keys are two taps into an
    extra-keys row that the arrows are already on. Neither pair is in the
    hints — there is no room at 38 columns and less at 30 — so both are in
    ``docs/ytq.md`` instead.
    """
    cursor, top = place
    #: The scroll step for the cursor's title, and the cursor it belongs to.
    #: Reset on every move, so a title just arrived at starts from its
    #: beginning rather than half way through somebody else's lap.
    tick = 0
    ticking_for = -1

    while True:
        win.erase()
        height, width = win.getmaxyx()
        narrow = width < WIDE
        tall = 1 if not narrow else 2
        _addstr(
            win,
            0,
            0,
            fit(" subscriptions " if feed else f" search: {query} ", width - 1).ljust(
                width - 1
            ),
            curses.A_REVERSE | curses.A_BOLD | paint.get("head", 0),
        )
        if feed:
            # What `m` costs stands on the screen before it is pressed, which
            # is the entry screen's rule applied to the one key here that
            # spends.
            meta = feed_meta(
                len(hits), freshness(fetched), more, at_cap, width
            )
        else:
            meta = f"{len(hits)} results  ·  ~ approx dates"
            if not narrow:
                meta = f"{len(hits)} results  ·  youtube  ·  ~ dates are approximate"
        _addstr(win, 1, 1, fit(meta, width - 2), curses.A_DIM)

        listed = max(1, (height - 6) // tall)
        cursor, top = viewport(cursor, top, listed, len(hits))
        if cursor != ticking_for:
            tick, ticking_for = 0, cursor

        for row in range(listed):
            index = top + row
            if index >= len(hits):
                break
            attr = curses.A_REVERSE if index == cursor else 0
            for offset, line in enumerate(
                result_row(
                    hits[index], width, index in queued, tick if index == cursor else None
                )
            ):
                _addstr(win, 3 + row * tall + offset, 0, line.ljust(width - 1), attr)

        if running.alive:
            _addstr(win, height - 3, 1, running.line(width), curses.A_BOLD)
        if narrow:
            if feed:
                # `-end` when there is nothing further back to buy, so the
                # hints and the meta line agree about whether `m` is a key.
                name = "subs" if more else "subs-end"
                name += "-running" if running.alive else ""
            else:
                name = "running" if running.alive else "results"
            keys = hint(name, width)
        else:
            keys = "↑↓ choose   enter see the formats"
            if feed:
                if more:
                    keys += f"   m {more - len(hits)} more"
                keys += "   r read it again"
            else:
                keys += "   / search again"
            keys += "   q back"
            if running.alive:
                keys += "   x stop the download"
        _addstr(win, height - 2, 1, keys, curses.A_DIM)
        win.refresh()

        # Blocking unless there is something to redraw for, so an idle screen
        # costs no wakeups at all: a live download redraws twice a second, a
        # title too long for its room a little faster than that, and a screen
        # with neither waits on the keyboard the way it always did.
        moving = bool(hits) and scrolls(hits[cursor], width)
        waits = [500] if running.alive else []
        if moving:
            waits.append(MARQUEE_MS)
        win.timeout(min(waits) if waits else -1)
        try:
            key = win.getch()
        finally:
            win.timeout(-1)

        if key == -1:
            # A timeout and not a keypress: the only thing a step of the clock
            # advances is the scroll.
            tick += 1
            continue
        # Every way out of here carries the place with it, so that whatever
        # brings the list back — a queued video, a deeper look, a re-read —
        # brings it back where it was left.
        here = (cursor, top)
        if key in (ord("q"), 27):
            return None, here
        if key == ord("/"):
            return "/", here
        # Live on the feed alone, and named in its hints. A key that is silent
        # on one list and does something on the other is the shape somebody
        # presses three times before concluding the tool is broken, so the
        # feed's own hint carries it and the search's does not.
        if feed and key == ord("r"):
            return "r", here
        # Guarded on there actually being more, so that at the bottom of the
        # feed this is a key that does nothing rather than one that buys the
        # same listing again — and the meta line above says which it is.
        if feed and more and key == ord("m"):
            return "m", here
        if key == ord("x"):
            running.stop()
        elif key in (curses.KEY_UP, ord("k")):
            cursor -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor += 1
        # The arrows alias the page keys rather than replacing them: `m` can
        # make this list five times longer than it was, and on a phone the
        # page keys are two taps into an extra-keys row the arrows sit on.
        elif key in (curses.KEY_NPAGE, curses.KEY_RIGHT):
            cursor += listed
        elif key in (curses.KEY_PPAGE, curses.KEY_LEFT):
            cursor -= listed
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = len(hits) - 1
        elif key in (curses.KEY_ENTER, 10, 13):
            return cursor, here


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
    #: When each cached listing was fetched, so the feed can say how old what
    #: is on screen is. Keyed the same as :data:`searched`.
    stamps: dict[str, float] = {}
    #: And where each was left. Keyed the same again, so backing out of one
    #: search into another and returning lands where you were in both — and so
    #: that a listing never seen before starts at the top, which is the only
    #: time starting at the top is right.
    places: dict[str, tuple[int, int]] = {}
    probed: dict[str, tuple[dict, list[Choice], int]] = {}
    marks: dict[str, set[int]] = {}
    #: Videos this session has been told to queue again despite already having
    #: them. Per target, so agreeing to one is not agreeing to the next.
    agreed: set[str] = set()
    #: Whether the bot check has already been explained. Once a session and
    #: not once per video: the fix is a config edit that cannot be made from
    #: here, so saying it again on the next video is a keypress charged for
    #: nothing. The format screen goes on marking every list it applies to.
    warned = False

    typed = first or ""
    query = ""
    hits: list[Result] = []
    target = ""
    chosen_index = -1
    came_from = "entry"
    screen = "entry"
    #: Set by ``r`` on the feed and cleared by the fetch it asks for. A flag
    #: rather than dropping the cache entry, so a look that does not come back
    #: leaves the listing that was on screen still on screen.
    refetch = False
    #: How deep the feed has been asked for, which ``m`` raises a page at a
    #: time. Kept here rather than derived from ``len(hits)``: a feed that
    #: handed back 28 of the 30 asked for has been asked for 30, and it is the
    #: gap between those two numbers that says there is no more to come.
    #:
    #: Two of them, and that is the point: ``subs_want`` is what the next
    #: fetch will ask for and ``subs_asked`` is what the listing on screen was
    #: actually answered at. One variable said the deeper number the moment
    #: ``m`` was pressed, so a deeper fetch that FAILED left thirty videos on
    #: screen beside a claim that sixty had been asked for — which
    #: :func:`next_page` reads as the end of the feed, and the screen then
    #: says "the whole feed" over a third of it with `m` switched off.
    subs_asked = subs_want = SUBS_RESULTS

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
    elif first and looks_like_feed(first):
        # Before the URL test, because the feed's own URL passes that one too.
        screen = "subs"
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
            if looks_like_feed(text):
                screen = "subs"
            elif looks_like_url(text):
                target, came_from, screen = text, "entry", "formats"
            else:
                query, screen = text, "search"

        elif screen == "subs":
            # Asked before the spinner and before anything is spent: a feed
            # request with no session behind it does not fail, it comes back
            # empty, so the page is bought and buys no explanation with it.
            state, detail = cookie_state()
            if state in ("none", "missing"):
                message(win, cookie_advice(state, detail))
                typed, screen = "", "entry"
                continue
            if SUBS_KEY in searched and not refetch:
                hits = searched[SUBS_KEY]
            else:
                found, failure = spinner_while(
                    win,
                    f"reading your subscriptions — {subs_want}, "
                    f"{feed_cost(subs_want)}…",
                    lambda count=subs_want: subscriptions(count),
                )
                if failure is not None:
                    message(
                        win,
                        ["the subscription feed did not come back", str(failure)]
                        # The fix and not a second verdict: yt-dlp has already
                        # said what went wrong on the line above, and expired
                        # cookies are what it usually means.
                        + cookie_fix(detail),
                    )
                    # Back to the depth the listing on screen really is: a
                    # deeper ask that did not come back is not a deeper ask.
                    subs_want = subs_asked
                    typed, screen = "", "entry"
                    continue
                if found is None:
                    return receipts
                hits = searched[SUBS_KEY] = found
                stamps[SUBS_KEY] = time.time()
                # Committed here and nowhere else — the depth the answer on
                # screen was actually answered at.
                subs_asked = subs_want
                refetch = False
            if not hits:
                message(win, empty_feed_advice(detail))
                typed, screen = "", "entry"
                continue
            query, screen = SUBS_KEY, "results"

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
                stamps[query] = time.time()
            if not hits:
                message(win, ["nothing found for that", "try different words"])
                typed, screen = query, "entry"
                continue
            screen = "results"

        elif screen == "results":
            # Marked with what this session queued *and* what the queue
            # already holds, which are the same fact to whoever is reading it.
            feed = query == SUBS_KEY
            more, at_cap = (
                next_page(len(hits), subs_asked) if feed else (None, False)
            )
            picked, places[query] = results(
                win,
                query,
                hits,
                paint,
                marks.setdefault(query, set()) | already_queued(hits),
                running,
                feed=feed,
                fetched=stamps.get(query),
                more=more,
                at_cap=at_cap,
                place=places.get(query, (0, 0)),
            )
            if picked is None:
                typed, screen = "", "entry"
            elif picked == "r":
                refetch, screen = True, "subs"
            elif picked == "m":
                # The whole listing is bought again, not the extra thirty —
                # `feed_cost` is the total for that reason, and the screen
                # that offered this key said the total.
                subs_want, refetch, screen = more, True, "subs"
            elif isinstance(picked, str):
                # `/` — the search prefills the field with the words that got
                # here, and the feed has none to prefill it with.
                typed, screen = ("" if feed else query), "entry"
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
            # Before the list rather than after a choice has been made from
            # it, because the whole trouble with a withheld extraction is that
            # what comes back looks exactly like an answer.
            if withheld(info) and not warned:
                message(win, withheld_advice(cookie_state()[1]))
                warned = True
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
                # The resolved destination and not the one asked for: this
                # screen prints the folder the file lands in, and a line that
                # disagrees with where it turns up is worse than no line.
                decided = confirm(
                    win, target, info, choice, run_now, paint, dest_for(choice, dest)
                )
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
                dest_for(choice, dest),
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


def list_feed(count: int = SUBS_RESULTS) -> int:
    """The subscription feed, printed. The same rows, without a terminal.

    Here for the same reason ``dlqd``'s read-only screens are: this is the one
    thing on the feed side that something with no terminal can still ask for —
    a pipe, a script, an ssh session with no tty — and the alternative is that
    the only way to see the feed at all is a curses app.
    """
    def refuse(lines: list[str]) -> int:
        """The same words the screen uses, one to a line, on stderr.

        The verdict first and prefixed, the working under it, which is the
        shape ``dlqd status`` prints in — and the reason it is not one long
        line is that these end in a path somebody has to read.
        """
        head, *rest = lines
        print(f"error: {head}", file=sys.stderr)
        for line in rest:
            print(f"  {line}", file=sys.stderr)
        return 1

    state, detail = cookie_state()
    if state in ("none", "missing"):
        return refuse(cookie_advice(state, detail))
    try:
        hits = subscriptions(count)
    except ProbeError as exc:
        return refuse([str(exc), *cookie_fix(detail)])
    if not hits:
        return refuse(empty_feed_advice(detail))
    return list_results(hits, shutil.get_terminal_size(fallback=(80, 24)).columns)


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
    # The same verdict the format screen shows, for the path with no screen.
    # On stderr so that a pipe reading the table is not handed a row that is
    # not one, and so it survives being redirected away from.
    if withheld(info):
        for line in withheld_advice(cookie_state()[1]):
            print(f"  {line}", file=sys.stderr)
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

    # -- the confirmation's two columns never touch -------------------------- #

    # `file name` is nine columns and starts at x=2, so it ends at 10 and a
    # gutter of 11 drew `file namecrust-of-rust-subtyping` — a label and a
    # value welded into one word on the screen that decides the file name.
    for gutter, where in ((CONFIRM_GUTTER, "a phone"), (14, "a wide terminal")):
        for label in CONFIRM_LABELS:
            check(
                f"{label!r} clears its value on {where}",
                gutter > 2 + len(label),
                True,
            )
    # Derived rather than typed, so a third label widens the column instead of
    # touching it — which is the only reason the number above can be trusted.
    check(
        "the gutter is computed from the labels",
        CONFIRM_GUTTER,
        2 + max(len(label) for label in CONFIRM_LABELS) + 1,
    )
    # And the value still has somewhere to go at the floor.
    at_most("the labels leave room for a value at 32", CONFIRM_GUTTER + 8, 32)

    # -- an audio-only pick is not a video ---------------------------------- #

    sound = {
        "title": "X", "extractor_key": "Youtube", "duration": 60,
        "formats": [
            {"format_id": "137", "ext": "mp4", "vcodec": "avc1", "acodec": "none",
             "height": 1080, "filesize": 300_000_000},
            {"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a",
             "height": 360, "filesize": 40_000_000},
            {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a",
             "abr": 129, "filesize": 10_000_000},
        ],
    }
    picks = {option.kind: option for option in choices(sound)[0]}
    check("every kind of pick is offered", set(picks), {"merge", "single", "audio"})
    # The music player and the video player look in different folders on a
    # phone, so a song delivered among the films is one nothing offers to play.
    check("an audio-only pick goes to audio", dest_for(picks["audio"]), AUDIO_DEST)
    for kind in ("merge", "single"):
        check(f"a {kind} pick still goes to video", dest_for(picks[kind]), VIDEO_DEST)
    # `--dest` names a directory and wins over both, which is the runner's own
    # rule about an absolute path in the header rather than a second one.
    for kind in ("merge", "single", "audio"):
        check(
            f"and --dest overrides it for a {kind} pick",
            dest_for(picks[kind], "/music"),
            "/music",
        )
    # Every kind ytq can emit has to be one the runner will resolve, or the
    # item is delivered nowhere and stays in out/ with nothing raising.
    try:
        sys.path.insert(0, str(HERE))
        import expire_runner as _runner

        for kind in (VIDEO_DEST, AUDIO_DEST):
            check(
                f"the runner knows the {kind} destination",
                kind in _runner.DEST_KINDS,
                True,
            )
            check(
                f"and resolves {kind} to a real path",
                isinstance(_runner.dest_of({"dest": kind}), Path),
                True,
            )
    except ImportError:  # pragma: no cover - the sibling is always there
        print("SKIP the runner is not importable, so its kinds were not checked")

    # -- a withheld extraction is not a 360p video --------------------------- #

    # The two ways to end up looking at one 360p row, which have nothing in
    # common but the symptom: youtube refused and sent the legacy stream, or
    # yt-dlp described every format and put a size on none of them. Telling
    # them apart is the whole job here — `withheld` reports the first and
    # `choices`'s unsized count reports the second, and neither may answer for
    # the other.
    refused = {
        "title": "X", "extractor_key": "Youtube",
        "formats": [
            {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none"},
            {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42", "acodec": "mp4a.40",
             "height": 360, "filesize": 40_000_000},
        ],
    }
    sizeless = {
        "title": "X", "extractor_key": "Youtube",
        "formats": [
            {"format_id": "137", "ext": "mp4", "vcodec": "avc1", "acodec": "none",
             "height": 1080},
            {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"},
            {"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a",
             "height": 360, "filesize": 40_000_000},
        ],
    }
    check("a refused extraction is recognised", withheld(refused), True)
    check("and shows the one row it was sent", len(choices(refused)[0]), 1)
    check("with nothing hidden to explain it", choices(refused)[1], 0)
    check("formats with no size are NOT a refusal", withheld(sizeless), False)
    check("they are the hidden count instead", choices(sizeless)[1], 2)
    # Both land on a single 360p row, which is why the symptom cannot be the
    # thing that decides.
    check("both look the same on screen", len(choices(sizeless)[0]), 1)

    # A site that only ever serves one progressive file is not being refused,
    # and accusing it of a bot check would send somebody to edit a config over
    # a video that is working perfectly.
    for who in ("generic", "Vimeo", "TwitterBroadcast", ""):
        other = dict(refused, extractor_key=who)
        check(f"{who or 'an unnamed extractor'} is not accused", withheld(other), False)
    check(
        "a youtube answer with adaptive streams is fine",
        withheld({"extractor_key": "Youtube", "formats": [
            {"format_id": "137", "vcodec": "avc1", "acodec": "none", "ext": "mp4",
             "filesize": 1},
            {"format_id": "18", "vcodec": "avc1", "acodec": "mp4a", "ext": "mp4",
             "filesize": 1}]}),
        False,
    )
    check("and one with no formats at all is not accused either",
          withheld({"extractor_key": "Youtube", "formats": []}), False)
    check("nor is one holding only storyboards",
          withheld({"extractor_key": "Youtube", "formats": [
              {"format_id": "sb0", "ext": "mhtml", "vcodec": "none",
               "acodec": "none"}]}), False)

    # Measured at the same three widths the hints are, and for the same
    # reason: this one was 39 columns drawn into 38, and a phone showed
    # "youtube withheld the res".
    for span, room in ((80, 78), (40, HINT_WIDTH), (32, TIGHT_WIDTH)):
        line = withheld_note(span)
        at_most(f"the bot-check line fits {span}", len(line), room)
        check(f"and still says what it is at {span}", "bot check" in line, True)
    # No symbol in front of it: `⚠` is ambiguous-width and often drawn as a
    # double-width emoji, which is the clip again.
    check(
        "the bot-check line is plain words",
        any(ord(ch) > 0x2500 for ch in withheld_note(40)),
        False,
    )

    # The notice has to fit the phone whole, like every other one here: it is
    # the only place the fix is written down at the moment it is needed.
    # Injected rather than read off this machine: the wording is measured on
    # a phone, and the gate that measures it runs where there may be no yt-dlp
    # at all. The real values come from the machine at the point of use.
    advice = withheld_advice(
        f"{COOKIE_SUGGESTION}, written 23 days ago",
        version="2026.7.4",
        upgrade="python3.14 -m pip install -U yt-dlp",
        mine="git -C ~/or3 pull",
    )
    # And the same notice at its LONGEST, which is the measurement that
    # matters: every string on it is composed from the machine, so the pretty
    # case fitting says nothing about the phone in front of somebody. Both
    # tools installed as uv tools with a jar that is missing is the widest
    # this can get, and it was a row over budget when only the tidy case was
    # being measured.
    for worst_upgrade in (
        "uv tool install yt-dlp --with yt-dlp-ejs --force",
        "/data/data/com.termux/files/usr/bin/python3.14 -m pip install -U yt-dlp",
    ):
        # Both strings `own_upgrade` can actually return when it is not a
        # `git -C` line — driven from the code rather than invented, or this
        # measures a notice the tool never shows.
        for worst_mine in (
            "uv tool install or3-expire-queue --force",
            "reinstall ytq from the checkout",
        ):
            longest = withheld_advice(
                f"{COOKIE_SUGGESTION} is named by {CONFIG_SUGGESTION} but is not there",
                version="2026.08.19",
                upgrade=worst_upgrade,
                mine=worst_mine,
            )
            at_most(
                "the notice fits a phone at its longest",
                len(message_body(longest, 40 - 4, 999)),
                24 - 4,
            )
    body = message_body(advice, 40 - 4, 24 - 4)
    check("the bot-check notice fits a phone whole", len(body),
          len(message_body(advice, 40 - 4, 999)))
    for row in body:
        at_most("and every line of it fits", len(row), 40 - 4)
    # It names a command, and a command broken across two lines is one that
    # gets retyped wrong — the same rule the paths follow.
    # A command broken across two lines is a command retyped wrong, which is
    # why this one is bare rather than piped through grep.
    check("the check command survives whole",
          any("yt-dlp -v" in row for row in body), True)
    # The version leads, and that is the correction this notice exists in:
    # it used to lead with the cookies and the runtime, and sent somebody down
    # both with a correct config and a six-week-old yt-dlp.
    check("the notice leads with what happened", "youtube sent one" in body[0], True)
    check("and the version is the first fix offered",
          any("2026.7.4" in row for row in body[:6]), True)
    check("the cookies come after it",
          body.index(next(r for r in body if "cookies" in r))
          > body.index(next(r for r in body if "2026.7.4" in r)), True)
    check("and ytq's own upgrade is on it",
          any("git -C ~/or3 pull" in row for row in body), True)
    # A command is only useful if it survives the fold intact.
    check("the upgrade command is one unbroken row",
          "python3.14 -m pip install -U yt-dlp" in body, True)

    # -- how a thing was installed decides how it is upgraded ---------------- #

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # A uv tool venv and an ordinary one differ by uv's own receipt, which
        # is what this reads — a path substring would be wrong the moment
        # UV_TOOL_DIR moves it, and yt-dlp's own banner says "(pip)" for both.
        for name in ("uvtool", "plain"):
            (root / name / "bin").mkdir(parents=True)
            (root / name / "bin" / "python3").write_text("#!/bin/sh\n")
        (root / "uvtool" / UV_RECEIPT).write_text(
            '[tool]\nrequirements = [\n'
            '    { name = "yt-dlp" },\n    { name = "yt-dlp-ejs" },\n]\n'
            'entrypoints = [\n    { name = "yt-dlp", from = "yt-dlp" },\n]\n',
            encoding="utf-8",
        )
        check("a uv tool venv is recognised by its receipt",
              uv_receipt(str(root / "uvtool" / "bin" / "python3")) is not None, True)
        check("and an ordinary venv is not",
              uv_receipt(str(root / "plain" / "bin" / "python3")), None)
        # Read from the requirements block and not the entrypoints below it,
        # which names the tool a second time.
        got = uv_requirements(root / "uvtool" / UV_RECEIPT)
        check("the --with packages are read off the machine", got, ["yt-dlp", "yt-dlp-ejs"])

        script = root / "yt-dlp"
        script.write_text(f"#!{root / 'uvtool' / 'bin' / 'python3'}\n")
        script.chmod(0o755)
        keep = os.environ.get("PATH", "")
        try:
            os.environ["PATH"] = str(root)
            kind, python = install_of("yt-dlp")
            check("a uv tool install is told apart", kind, "uv-tool")
            line = upgrade_command("yt-dlp")
            # `uv tool upgrade` respects the original requirement and answers
            # "Nothing to upgrade" on a pinned tool, silently doing nothing.
            check("and is upgraded by a forced install", "--force" in line, True)
            check("never by uv tool upgrade", "tool upgrade" in line, False)
            # A reinstall that drops --with trades this bug for the other one.
            check("re-asserting the with packages", "--with yt-dlp-ejs" in line, True)
            check("and not naming the tool as its own extra",
                  "--with yt-dlp " in line, False)

            script.write_text(f"#!{root / 'plain' / 'bin' / 'python3'}\n")
            script.chmod(0o755)
            check("a plain install is told apart", install_of("yt-dlp")[0], "pip")
            check("and is upgraded with pip",
                  upgrade_command("yt-dlp"),
                  f"{root / 'plain' / 'bin' / 'python3'} -m pip install -U yt-dlp")
            os.environ["PATH"] = str(root / "nothing")
            check("and one that is not there says so",
                  "not on PATH" in upgrade_command("yt-dlp"), True)
        finally:
            os.environ["PATH"] = keep

    # `resolve()` would say a venv's python3 IS the system python3, because it
    # is a symlink to it — and `python3 -m pip` under the system python
    # installs somewhere else entirely.
    check("a venv interpreter is never shortened to the system one",
          short_python("/somewhere/venv/bin/python3"), "/somewhere/venv/bin/python3")
    check("and no interpreter at all still gives something runnable",
          short_python(""), "python3")

    # `git -C / pull` is what counting directories produces off the phone, and
    # a wrong path in a command somebody is about to type is worse than none.
    root = checkout_root()
    check("the checkout is found by its .git", root is not None, True)
    if root is not None:
        check("and is a real repository", (root / ".git").exists(), True)
    check("what updates this tool is never the filesystem root",
          own_upgrade().strip().endswith((" / pull", "-C / pull")), False)

    # The notice is measured whole at 40x24, and below that it truncates — so
    # what has to survive the cut is the fix most likely to be the answer.
    # That is the whole reason the version leads.
    squeezed = message_body(advice, 28, 16)
    check("a narrow screen still gets the verdict", "youtube sent one" in squeezed[0], True)
    check("and still gets the version", any("2026.7.4" in r for r in squeezed), True)
    # Wrapped at a space is fine and unavoidable at 28 columns; what must
    # never happen is a token split down the middle, which is a command
    # retyped wrong. Checked on the tokens rather than the string.
    command = ["python3.14", "-m", "pip", "install", "-U", "yt-dlp"]
    check("and the upgrade command with it, unbroken",
          all(word in " ".join(squeezed).split() for word in command), True)
    check("and is told what it dropped", "more" in squeezed[-1], True)

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

    # -- the subscription feed ---------------------------------------------- #

    # The same check as the one above it, against the same trap in a worse
    # shape: without --playlist-end this works perfectly and follows every
    # continuation YouTube will serve, which for years of subscriptions is
    # hundreds of pages bought on a metered radio to show thirty rows.
    fed = subs_argv(30)
    check("the feed is the subscriptions feed", SUBS_URL in fed, True)
    check("the feed stays flat", "--flat-playlist" in fed, True)
    check(
        "the feed is bounded to what is asked for",
        "--playlist-end" in fed and fed[fed.index("--playlist-end") + 1] == "30",
        True,
    )
    check("the feed asks for the approximate date", APPROX_DATE_ARGS[1] in fed, True)
    # The config is where the cookies are, and the feed is the one request here
    # that does not answer at all without them.
    check("the feed does not bypass the yt-dlp config", "--ignore-config" in fed, False)
    # It is a playlist on purpose; --no-playlist would ask for one video.
    check("the feed is not asked for as a single video", "--no-playlist" in fed, False)

    # -- going further back ------------------------------------------------- #

    check("a deeper look is bounded to what it asked for",
          subs_argv(90)[subs_argv(90).index("--playlist-end") + 1], "90")

    # The end of the feed is the case that costs real quota to get wrong: an
    # `m` still live at the bottom buys the same listing again on every press
    # and looks exactly like one that is working.
    check("a short answer is the end of the feed", next_page(28, 30), (None, False))
    check("a full answer means there is more", next_page(30, 30), (60, False))
    check("and the cap is a different sentence", next_page(150, 150), (None, True))
    check("which the last page reaches exactly", next_page(120, 120), (150, False))
    check("and never overshoots", next_page(SUBS_MAX - 1, SUBS_MAX - 1)[0], SUBS_MAX)

    # The figure is the TOTAL, because youtube's continuations are sequential
    # and there is no reaching 31-60 without walking 1-30 again. A key priced
    # at the increment while spending the lot is the bill nobody agreed to.
    check("one page is priced at one page", feed_cost(30), "~0.2 MB")
    check("and two pages at two", feed_cost(60), "~0.4 MB")
    check("the deepest look says what it costs", feed_cost(SUBS_MAX), "~1.0 MB")

    # The price is the reason this line exists, so it may never be the part
    # that gets clipped off the end of it — which is what the first inline
    # version did on a 40-column phone: `m 60 for ~0.4 …`, figure gone, key
    # still offered. Measured at the two floors the hints are measured at.
    for span, room in ((40, HINT_WIDTH), (32, TIGHT_WIDTH)):
        for count, when, more, at_cap in (
            (30, "just now", 60, False),
            (120, "12m ago", SUBS_MAX, False),
            (SUBS_MAX, "2h ago", None, True),
            (65, "just now", None, False),
            (30, "", 60, False),
        ):
            line = feed_meta(count, when, more, at_cap, span)
            at_most(f"the feed's meta fits {span} at {count}/{more}", len(line), room)
            if more:
                check(
                    f"and keeps the price whole at {span}",
                    feed_cost(more).replace(" ", "") in line.replace(" ", ""),
                    True,
                )
            else:
                check(
                    f"and says why m is gone at {span}",
                    ("cap" if at_cap else "whole feed") in line,
                    True,
                )
    check("the wide meta spells the price out", "for ~0.4 MB" in feed_meta(30, "just now", 60, False, 80), True)
    # The hints and the meta have to agree about whether `m` is a key: one of
    # them saying "more" while the other says "the whole feed" is the state
    # that gets pressed three times.
    for name in ("subs", "subs-running", "subs-end", "subs-end-running"):
        check(f"{name} is a screen both hint sets know", name in HINTS and name in TIGHT_HINTS, True)
        check(
            f"and only {name} without -end offers m",
            "m " in hint(name, 40),
            not name.startswith("subs-end"),
        )

    # -- keeping your place -------------------------------------------------- #

    # A place is saved on the way out and handed back on the way in, and what
    # comes back may not fit any more: `m` grows the list under it, `r` can
    # shrink it, and the terminal may have been resized in between. Every one
    # of those has to land on a row the screen actually draws — a cursor off
    # the end would leave every key working and nothing appearing to move.
    check("a place that still fits is left alone", viewport(17, 12, 9, 60), (17, 12))
    check("a cursor past the end comes back to the last row", viewport(90, 80, 9, 60), (59, 51))
    check("and brings a full screen with it", viewport(90, 90, 9, 60), (59, 51))
    check("an empty list does not go negative", viewport(4, 2, 9, 0), (0, 0))
    check("nor does a list of one", viewport(7, 3, 9, 1), (0, 0))
    # The top saved with it is a hint, not a fact: this is what stops a
    # restored cursor being scrolled off the screen it was restored onto.
    check("a stale top is slid down onto the cursor", viewport(40, 0, 9, 60), (40, 32))
    check("and up onto it", viewport(3, 55, 9, 60), (3, 3))
    check("a taller terminal shows more above the cursor", viewport(40, 0, 20, 60), (40, 21))
    for cursor, top, listed, count in (
        (17, 12, 9, 60), (90, 80, 9, 60), (0, 0, 1, 1), (40, 0, 9, 60), (3, 55, 9, 60),
        (59, 0, 4, 60), (0, 40, 9, 60),
    ):
        landed, window = viewport(cursor, top, listed, count)
        check(
            f"the cursor is drawn from {cursor}/{top} at {listed}x{count}",
            window <= landed < window + listed,
            True,
        )

    # -- long titles scroll rather than being cut off ------------------------ #

    check("a title that fits does not move", marquee("short", 20, 7), "short")
    # Long enough to overrun even a wide terminal, which is the whole set this
    # is checked across: `scrolls` is a fact about the title and the room, not
    # about the screen being a phone.
    long = (
        "A very long video title that will not fit a phone held in portrait, "
        "nor a terminal much wider than one"
    )
    check("one that does not fit is cut to the room it has", len(marquee(long, 20, 0)), 20)
    # The beginning is the part a choice is usually made on, so a lap holds
    # there before it moves — a line already sliding when the eye arrives has
    # to be waited out for a whole lap to read.
    for step in range(MARQUEE_HOLD + 1):
        check(f"it holds at its beginning for step {step}", marquee(long, 20, step), marquee(long, 20, 0))
    check("then starts moving", marquee(long, 20, MARQUEE_HOLD + 1) != marquee(long, 20, 0), True)
    check("by exactly one column a step", marquee(long, 20, MARQUEE_HOLD + 1), (long + MARQUEE_GAP)[1:21])
    lap = len(long) + len(MARQUEE_GAP) + MARQUEE_HOLD
    check("and comes back round to where it began", marquee(long, 20, lap), marquee(long, 20, 0))
    check("a gap makes the lap visible", MARQUEE_GAP.strip() != "", True)
    check("no width is not a crash", marquee(long, 0, 3), "")

    # `result_row` draws the title and `results` decides whether to keep waking
    # up to scroll it. They read the room from one function so they cannot
    # disagree — the failure is silent either way: a row that moves for no
    # reason, or an idle screen paying a wakeup every 300ms for ever.
    tall = Result(long, "Chan", "https://y/x", 61, 1_600_000_000)
    short = Result("Brief", "Chan", "https://y/x", 61, 1_600_000_000)
    for span in (32, 40, 72, 100):
        check(f"a long title has to scroll at {span}", scrolls(tall, span), True)
        check(f"a short one does not at {span}", scrolls(short, span), False)
        # Standing still is what every row but the cursor's does, and what
        # `list_results` does printing into a pipe with no clock at all.
        still = result_row(tall, span, False, None)
        for row in still:
            at_most(f"a still row fits {span}", len(row), span - 1)
        for step in (0, MARQUEE_HOLD, MARQUEE_HOLD + 9, lap + 3):
            for row in result_row(tall, span, True, step):
                at_most(f"and so does a scrolling one at {span}", len(row), span - 1)
    # The mark must not shift the title, moving or still — a queued row that
    # slid one column right of its neighbours would read as a broken list.
    check(
        "queueing a row does not shift its scrolling title",
        len(result_row(tall, 40, True, 9)[0]) == len(result_row(tall, 40, False, 9)[0]),
        True,
    )

    for word in ("subs", ":subs", "subscriptions", ":ytsubs", SUBS_URL):
        check(f"{word!r} opens the feed", looks_like_feed(word), True)
    for word in ("crust of rust", "subscription boxes", "https://youtu.be/aaa"):
        check(f"{word!r} does not open the feed", looks_like_feed(word), False)
    # The feed's URL passes the URL test too, so the order the entry screen
    # asks in is the whole of what stops it being probed as a video and
    # refused as a playlist.
    check("the feed URL is a URL as well", looks_like_url(SUBS_URL), True)

    # An empty answer is the one that has to be read right: it is not an error
    # and it is not an empty subscription list, and calling it either is how a
    # tool opened once a day says nothing is wrong for a fortnight.
    check(
        "an empty feed is never reported as being up to date",
        any("signed-out" in line for line in empty_feed_advice("x")),
        True,
    )
    for lines in (
        cookie_advice("none", "detail"),
        cookie_advice("missing", "detail"),
        empty_feed_advice("detail"),
    ):
        check("every dead end names the jar", any(COOKIE_SUGGESTION in x for x in lines), True)
        check("and repeats what was found", "detail" in lines, True)

    # The two paths the screens name have to be two of the ones actually read,
    # or every dead end here sends somebody to a file nothing consults.
    check(
        "the config the screens name is one yt-dlp reads",
        Path(CONFIG_SUGGESTION).expanduser() in config_paths(),
        True,
    )

    # `message` pins its own "any key to continue" to the bottom row and
    # bounds the body above it — but a notice that has to be truncated to fit
    # is a fix somebody cannot read, so every notice this tool can raise is
    # measured against the smallest screen it is read on. 40x24 is Termux in
    # portrait; the body gets height - 4 of those rows.
    PHONE_ROWS, PHONE_WRAP = 24 - 4, 40 - 4
    for name, lines in (
        ("no cookies", cookie_advice("none", "there is no yt-dlp config at " + CONFIG_SUGGESTION)),
        ("a missing jar", cookie_advice("missing", f"{COOKIE_SUGGESTION} is named by {CONFIG_SUGGESTION} but is not there")),
        ("a refusal", ["the subscription feed did not come back",
                       "Sign in to confirm you are not a bot",
                       *cookie_fix(f"{COOKIE_SUGGESTION}, written 12 days ago")]),
        ("an empty feed", empty_feed_advice(f"{COOKIE_SUGGESTION}, written 12 days ago")),
    ):
        body = message_body(lines, PHONE_WRAP, PHONE_ROWS)
        check(f"the {name} notice fits a phone whole", len(body), len(message_body(lines, PHONE_WRAP, 999)))
        for row in body:
            at_most(f"and every line of the {name} notice fits it", len(row), PHONE_WRAP)
        # Every one of these ends in a path or a flag, and a path wrapped
        # across two lines is a path somebody retypes wrong.
        for token in (COOKIE_SUGGESTION, CONFIG_SUGGESTION, "--cookies"):
            if any(token in line for line in lines):
                check(
                    f"the {name} notice keeps {token} in one piece",
                    any(token in row for row in body),
                    True,
                )
    # And when one does not fit, the truncation says so rather than simply
    # ending — an explanation that stops mid-sentence reads as a crash.
    squeezed = message_body(["a", "b", "c", "d", "e", "f"], 20, 4)
    check("a body too long for the screen is bounded", len(squeezed), 4)
    check("and says what was dropped", "more" in squeezed[-1], True)

    check("a listing read a moment ago says so", freshness(1000, 1030), "just now")
    check("one read a while ago says how long", freshness(1000, 1000 + 3600), "60m ago")
    check("and a much older one drops to hours", freshness(1000, 1000 + 4 * 3600), "4h ago")
    check("a listing never fetched says nothing", freshness(None), "")
    check("cookies written today say today", written(1000, 1000), "today")
    check("cookies from three weeks ago say so", written(0, 21 * 86400), "21 days ago")

    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "config"
        jar = Path(tmp) / "cookies.txt"
        absent = Path(tmp) / "no-such-config"
        # Parsed the way yt-dlp parses it, driven on a real file: a `#` comment
        # naming the flag is a comment in both, or this screen and yt-dlp
        # disagree about whether cookies are configured.
        conf.write_text(
            "# once had --cookies /old/jar.txt here\n"
            "--js-runtimes node\n"
            f"--cookies {jar}\n",
            encoding="utf-8",
        )
        check("the --cookies line is found", declared_cookies([conf])[1], str(jar))
        check("a commented-out one is not", declared_cookies([absent]), None)
        check("nor is a config that is not there", cookie_state([absent])[0], "none")

        check("a named jar that is not on disk is not a go", cookie_state([conf])[0], "missing")
        jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        check("one that is, is", cookie_state([conf])[0], "file")
        # The age is the fact that turns "the feed is empty" into something
        # somebody can act on, so it is on the screen and not only in a log.
        check("and says when it was written", "today" in cookie_state([conf])[1], True)
        jar.write_text("", encoding="utf-8")
        check("an empty jar is no jar", cookie_state([conf])[0], "missing")

        conf.write_text("--cookies-from-browser chrome\n", encoding="utf-8")
        check(
            "a browser is a declaration too",
            cookie_state([conf])[0],
            "browser",
        )
        conf.write_text(f"--cookies={jar}\n", encoding="utf-8")
        check(
            "and the flag=value spelling is the same flag",
            declared_cookies([conf])[1],
            str(jar),
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
              ytq subs                  browse your subscription feed
              ytq --list --subs         print the feed, write nothing
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
        "--subs",
        action="store_true",
        help="open the subscription feed instead of searching; needs the "
        "cookies the yt-dlp config already points at",
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
        help="put this one somewhere other than the configured video or audio "
        "directory (dlqd dest sets those)",
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

    if args.subs and first:
        parser.error("--subs is the whole request; it takes no words or URL")

    if args.list:
        if args.now:
            parser.error("--list writes nothing, so there is nothing for --now to run")
        if args.subs:
            if args.from_json:
                parser.error("--subs asks youtube; --from-json reads a saved dump")
            return list_feed()
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

    if args.subs:
        # `--subs` is the flag spelling of the word the entry field already
        # takes, handed to `app` down that one road rather than as a second
        # way in. One door: whatever routes `subs` routes this.
        first = ":subs"

    if not sys.stdout.isatty():
        print(
            "ytq needs a terminal; use --list <url> or --list --subs otherwise",
            file=sys.stderr,
        )
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
