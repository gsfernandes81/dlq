#!/usr/bin/env python3
"""Pretty one-screen data-quota readout for a Termux home-screen widget.

Wraps :mod:`zwana_quota` and adds the three things a widget needs that a CLI
does not:

* **Local time.** The portal thinks and reports in UTC; the daily allowance
  lands at 00:00 UTC (observed 00:02). Shown here in the device's own zone.
* **A cache.** A widget can be tapped or refreshed repeatedly, so a fresh answer
  is reused for ``--max-age`` seconds. The portal is the vessel's own captive
  portal and is not metered, so this is about latency and not hammering the
  endpoint rather than about quota — hence a short window.
* **Graceful offline.** If the portal cannot be reached, the last known figures
  are drawn with their age instead of an error page.

Colour is emitted only when stdout is a terminal, so piping this into a widget
that renders plain text gives clean output with no escape-code litter.

Usage::

    python3 quota_widget.py              # the tile: data left, big, plus the reset
    python3 quota_widget.py --full       # the detailed box: bar, top-up, reserve
    python3 quota_widget.py --refresh    # ignore the cache
    python3 quota_widget.py --plain      # never colour
    python3 quota_widget.py --line       # single line, for a status bar
    python3 quota_widget.py --json       # the derived figures, for other scripts

The pipeline is three separable steps, so a caller can stop at whichever one it
needs: :func:`gather` reads the portal, :func:`derive` turns that into the
documented figures, and :func:`render` draws them. ``--json`` prints the middle
step verbatim.

A note for anything gating a download on this: ``free.left_bytes`` is an **upper
bound**, never a floor. Every term that can be wrong — unobserved carried-over
paid data, the portal's accounting lag, a stale cached reading — pushes it the
same way, so treat it as the most free data you could possibly have left.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import zwana_quota as z  # noqa: E402

CACHE = Path.home() / ".cache" / "zwana" / "widget.json"
LOCK = Path.home() / ".cache" / "zwana" / "refresh.lock"
DEFAULT_MAX_AGE = 45

#: A background refresh that has not finished in this long is assumed dead.
LOCK_TIMEOUT = 120
INNER = 30  # --full frame width, corner to corner
INDENT = 3  # left margin of the text inside the frame
#: Tile face width. Measured with ``--probe``, not guessed: the ruler's first
#: row breaks after 35 columns and the wrapped rows are 35 too. A wide, short
#: banner rather than the tall card this face used to be, which is what fixes
#: the layout in :func:`compose_tile`.
TILE = 35

#: Rows the tile shows, which is one more than the face uses. The probe's
#: numbering is readable to five, and at this width its ruler wraps onto a
#: second row rather than the third it needed at 28 columns — so the height is
#: one more than the last number, not the two the ruler used to add.
TILE_LINES = 6

#: Where the face sits in the tile. One column in and one row down, which
#: centres the face vertically in the row it has spare and keeps the figure off
#: the rounded corner the tall tile used to spend a whole blank row avoiding.
#: Both are only defaults: ``--margin`` and ``--top`` override them, and the
#: margin gives way anyway when a wide figure needs the columns.
TILE_MARGIN = 1
TILE_TOP = 1

# The portal's cron tops the allowance up at midnight UTC.
RESET_HOUR_UTC = 0


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


#: ``CreditHistory.TopUpMethod`` for the nightly cron grant. Those entries cost
#: 0 credits and have no ``Allocator``; everything else is a paid draw.
FREE_TOPUP_METHOD = 4


def gather(previous: dict | None = None) -> dict:
    """Hit the portal and return everything the box needs, as plain JSON.

    Three calls. ``UserProvider/GetActive`` is the important one — it carries
    ``Remainder``, the only exact statement of how much data is actually left,
    and ``Allocated``, which reconciles to the byte with the sum of the whole
    allocation history.
    """
    if not z.load_session():
        z.log_in(z.DEFAULT_ENV)
    balance = z.fetch("Balance/GetForCurrentUser", z.DEFAULT_ENV)
    if not isinstance(balance, dict) or not isinstance(
        balance.get("Balance"), (int, float)
    ):
        raise z.PortalError("balance response has no numeric Balance field")

    active = z.fetch("UserProvider/GetActive", z.DEFAULT_ENV)
    if not isinstance(active, dict) or not isinstance(
        active.get("Remainder"), (int, float)
    ):
        raise z.PortalError("active provider response has no numeric Remainder")
    remainder = int(active["Remainder"])

    per_credit = z.BYTES_PER_CREDIT
    grant = 0
    drawn_today = 0
    try:
        history = z.fetch("Allocation/GetHistoryForCurrentUser", z.DEFAULT_ENV)
    except z.PortalError:
        history = None

    today = dt.datetime.now(dt.UTC).date().isoformat()
    if history is None and previous and previous.get("pool_day") == today:
        # Losing the history would otherwise drive the grant to zero, and a
        # zero grant reads as "no free data left" — wrong, and wrong in the
        # dangerous direction. Yesterday's figures are still the right shape.
        grant = int(previous.get("grant") or 0)
        drawn_today = int(previous.get("drawn_today") or 0)

    if isinstance(history, list):
        for entry in sorted(history, key=lambda e: str(e.get("Date", ""))):
            provider = (entry.get("UserProvider") or {}).get("Provider") or {}
            unit_cost = provider.get("UnitCost")
            if (
                provider.get("IsByteType")
                and isinstance(unit_cost, (int, float))
                and unit_cost > 0
            ):
                per_credit = round(1 / unit_cost)

            amount = entry.get("Allocation")
            if not isinstance(amount, (int, float)):
                continue
            credit = entry.get("CreditHistory") or {}
            free = credit.get("TopUpMethod") == FREE_TOPUP_METHOD
            if free:
                # The nightly grant has been identical every day of the retained
                # history, but read it rather than hard-code it.
                grant = int(amount)
            # Timestamps are UTC and unzoned; a date prefix compare is enough.
            if str(entry.get("Date", "")).startswith(today) and not free:
                drawn_today += int(amount)

    return {
        "ts": time.time(),
        "credits": float(balance["Balance"]),
        "per_credit": per_credit,
        "remainder": remainder,
        "allocated": int(active.get("Allocated") or 0),
        "grant": grant,
        "drawn_today": drawn_today,
        "online": bool(balance.get("Online")),
        "profile": balance.get("CronProfileName") or "",
        **day_pool(remainder, grant, drawn_today, previous),
    }


def day_pool(
    remainder: int, grant: int, drawn_today: int, previous: dict | None
) -> dict:
    """Work out today's total pool, the 100% the bar is drawn against.

    The nightly grant is spent first and expires at the reset; paid allocations
    carry forward and are spent last. So today's pool is the grant plus what has
    been drawn today plus whatever paid data survived midnight — and that last
    term is the awkward one, because nothing in the API states it.

    Two things pin it down without guessing. ``Remainder`` can never exceed the
    pool, so it is a hard floor. And the pool only ever grows during a day, so
    the largest floor seen so far today still holds — carried forward in the
    cache. On a day that starts empty, which is every day the previous one was
    drawn to zero, the first term is already exact and these do nothing.
    """
    today = dt.datetime.now(dt.UTC).date().isoformat()
    now = time.time()
    floor = grant + drawn_today
    first_ts = now
    if previous and previous.get("pool_day") == today:
        floor = max(floor, int(previous.get("pool") or 0))
        first_ts = float(previous.get("pool_first_ts") or now)
    return {"pool_day": today, "pool": max(floor, remainder), "pool_first_ts": first_ts}


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #


def derive(data: dict, age: float, live: bool) -> dict:
    """Turn a raw reading into the documented figures, as plain JSON.

    This is the intermediate other scripts should consume (``--json``): the
    portal's own fields plus everything worked out from them, with the
    reasoning and the error direction stated in the payload rather than left
    for the caller to rediscover.
    """
    now_utc = dt.datetime.now(dt.UTC)
    reset_utc = next_reset(now_utc)
    left = reset_utc - now_utc

    grant = int(data["grant"])
    drawn = int(data["drawn_today"])
    remainder = int(data["remainder"])
    pool = max(1, int(data["pool"]))

    # Paid data that survived midnight. Nothing in the API states it, but the
    # pool high-water mark bounds it from below: any pool seen above the
    # grant plus the day's draws can only have come from carried paid data.
    carry_in = max(0, pool - grant - drawn)
    paid_pool = carry_in + drawn

    # The grant is spent first and paid data last, so free left is whatever
    # the remainder holds in excess of the paid pool, and never more than the
    # grant itself.
    free_left = max(0, min(grant, remainder - paid_pool))
    paid_left = remainder - free_left

    # How long after the reset the first reading of the day landed. Usage in
    # that window is invisible, which is what makes free_left an upper bound.
    first_gap = max(
        0.0,
        data.get("pool_first_ts", data["ts"])
        - (reset_utc - dt.timedelta(days=1)).timestamp(),
    )
    observed_from_reset = first_gap <= 300

    return {
        "schema": 1,
        "generated": now_utc.isoformat(timespec="seconds"),
        "reading": {
            "taken": dt.datetime.fromtimestamp(data["ts"], dt.UTC).isoformat(
                timespec="seconds"
            ),
            "age_seconds": round(age, 1),
            "live": live,
            "online": bool(data["online"]),
        },
        "free": {
            "grant_bytes": grant,
            "left_bytes": free_left,
            "used_bytes": max(0, grant - free_left),
            "expires": reset_utc.isoformat(timespec="seconds"),
        },
        "paid": {
            "left_bytes": paid_left,
            "drawn_today_bytes": drawn,
            "carry_in_bytes": carry_in,
        },
        "today": {
            "pool_bytes": pool,
            "remainder_bytes": remainder,
            "used_bytes": max(0, pool - remainder),
        },
        "reserve": {
            "credits": data["credits"],
            "currency": "USD",
            "bytes_per_credit": data["per_credit"],
            "bytes": int(data["credits"] * data["per_credit"]),
        },
        "reset": {
            "utc": reset_utc.isoformat(timespec="seconds"),
            "local": reset_utc.astimezone().isoformat(timespec="seconds"),
            "seconds_until": int(left.total_seconds()),
        },
        "totals": {"allocated_bytes": int(data["allocated"])},
        "accuracy": {
            # Every term that can be wrong pushes the same way, so callers can
            # treat free.left_bytes as a ceiling and never a floor.
            "free_left_bias": "upper_bound"
            if not observed_from_reset
            else "exact_if_online",
            "free_left_is_upper_bound": not observed_from_reset,
            "first_reading_after_reset_seconds": int(first_gap),
            "carry_in_observed": carry_in > 0,
            "notes": [
                "free.left_bytes = remainder - (carry_in + drawn_today), capped at the grant",
                "carry_in is a lower bound, so free.left_bytes is an upper bound",
                "portal accounting lags live traffic, which also overstates what is left",
                "a stale reading (see reading.age_seconds) overstates it further",
            ],
        },
    }


def cached(max_age: float) -> dict | None:
    """Return a cached reading if it is younger than *max_age* seconds."""
    try:
        data = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "ts" not in data:
        return None
    return data if time.time() - data["ts"] <= max_age else None


def stale() -> dict | None:
    """Return the last reading of any age, for use when the portal is down."""
    return cached(float("inf"))


def store(data: dict) -> None:
    """Persist a reading, readable only by this user."""
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.parent.chmod(0o700)
    os.close(os.open(CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    CACHE.write_text(json.dumps(data))


def spawn_refresh() -> None:
    """Start a detached cache refresh, unless one is already running.

    This is what lets a caller in a render path — a status line runs on every
    frame — show a number immediately and still converge on a current one. The
    lock is an exclusive create, so two callers racing cannot both win; a lock
    older than :data:`LOCK_TIMEOUT` is treated as abandoned, because otherwise
    one killed refresher would freeze the figure forever.
    """
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - LOCK.stat().st_mtime > LOCK_TIMEOUT:
            LOCK.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)

    # Detached, with its own session and no shared pipes: a caller that reaps
    # its children would otherwise wait on this one's EOF, which is exactly the
    # block this whole path exists to avoid.
    script = (
        f"{json.dumps(sys.executable)} "
        f"{json.dumps(str(Path(__file__).resolve()))} --refresh-only; "
        f"rm -f {json.dumps(str(LOCK))}"
    )
    try:
        subprocess.Popen(
            ["sh", "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        LOCK.unlink(missing_ok=True)


def current(max_age: float = DEFAULT_MAX_AGE, force: bool = False) -> tuple[dict, bool]:
    """Return ``(reading, live)``, applying the caching policy.

    The one entry point callers should use. Three cases:

    * **force** — a blocking live read, for a caller that wants the truth now
      and can afford a second for it (the home-screen widget on tap).
    * **cache within max_age** — returned as is, no network.
    * **cache too old** — the old reading is returned immediately and a
      detached refresh is started, so the next call is current. Only when there
      is no cached reading at all does this block, to bootstrap one.

    Raises :class:`zwana_quota.PortalError` only when it had to go to the
    network and could not.
    """
    if not force:
        fresh = cached(max_age)
        if fresh is not None:
            return fresh, False
        old = stale()
        if old is not None:
            spawn_refresh()
            return old, False

    data = gather(stale())
    store(data)
    return data, True


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def size(value: float) -> str:
    """Compact byte count: no decimals below a GiB, two above."""
    for unit in ("B", "KiB", "MiB"):
        if abs(value) < 1024:
            return f"{value:,.0f} {unit}"
        value /= 1024
    return f"{value:,.2f} GiB"


def money(credits: float) -> str:
    """A credit is a dollar. Whole dollars lose the pointless ``.00``."""
    return f"(${credits:,.0f})" if credits == int(credits) else f"(${credits:,.2f})"


def since(seconds: float) -> str:
    """Human age of a cached reading."""
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.0f}h ago"


def next_reset(now_utc: dt.datetime) -> dt.datetime:
    """The next allowance top-up, as an aware UTC datetime."""
    today = now_utc.replace(hour=RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    return today if today > now_utc else today + dt.timedelta(days=1)


def countdown(delta: dt.timedelta) -> str:
    """``8h 49m`` / ``47m`` — always fits in seven characters."""
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def day_label(reset_local: dt.datetime) -> str:
    """``today`` / ``tmrw`` for the local calendar day the reset falls on."""
    today = dt.datetime.now().astimezone().date()
    days = (reset_local.date() - today).days
    return {0: "today", 1: "tmrw"}.get(days, reset_local.strftime("%a"))


class Paint:
    """ANSI colours, silently disabled when the output is not a terminal."""

    def __init__(self, enabled: bool) -> None:
        self.on = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text


# --------------------------------------------------------------------------- #
# The box
# --------------------------------------------------------------------------- #
#
# Alignment note, measured with --probe against the real widget: the launcher's
# font does NOT render every glyph one cell wide. Taking an ASCII cell as 1.00:
#
#     -  =  ASCII      1.00      ·  U+00B7 middot   1.00
#     ─  U+2500 rule   1.22      ●  U+25CF bullet   1.02
#     ▓ ░ █ blocks     1.18-1.25 ▪  U+25AA square   0.58
#
# So a right-hand border built from rules or blocks lands somewhere different on
# every row — the more of them a row holds, the further right it drifts.
#
# The fix is structural rather than cosmetic: nothing but ASCII inside the
# frame, no per-row right border to drift, and the frame reduced to four corner
# marks joined by ordinary spaces. Only those four glyphs are non-ASCII, they
# sit at the ends of two lines that are otherwise identical, so any width error
# is invisible. It also stops fighting the tile: One UI draws its own rounded
# rectangle, and corner marks echo that instead of boxing a box.


def grade(share: float) -> str:
    """Colour for a fraction remaining: comfortable, thin, nearly gone."""
    return "1;32" if share >= 0.25 else "1;33" if share >= 0.08 else "1;31"


# --------------------------------------------------------------------------- #
# The tile face: one big number
# --------------------------------------------------------------------------- #
#
# A 35 x 6 tile has no room for the full box, so the face states one thing and
# states it large: how much data is left. Everything else the box carries — the
# bar, the top-up, the reserve — moves to --full and --json.
#
# Five rows tall, in a tile of six, and that single fact fixes the rest of the
# layout. The tall tile could afford a blank row above the figure for the corner
# radius to clip and could stack the small print beneath it; one spare row can
# do neither, so the figure is the full height of the face, the small print
# stands in a column beside it, and the spare row goes above the whole thing as
# :data:`TILE_TOP` — which centres the face rather than only protecting it.
#
# Digits are three columns and the point is one, with one column between glyphs,
# so 1.68 is thirteen columns and a five-character 12.34 is seventeen.
# :func:`face_value` drops decimals until the figure leaves :data:`FACE_SPINE`
# columns for that column of small print, rather than overflowing the tile or
# squeezing the print out of it.

GLYPH_ROWS = 5
GLYPH_GAP = 1

#: Blank columns between the figure and the small print beside it.
FACE_GAP = 3

#: Columns the small print is guaranteed, taken out of the figure's budget. It
#: is the width of ``+762 MiB 05:30``, the longest spelling of the top-up row
#: that still carries both facts — because a figure allowed to grow into this
#: space would push the reset off the right-hand edge, which is the wide tile's
#: version of the tall one's reset clipped off the bottom, and just as silent.
FACE_SPINE = 14

#: Drawn in ASCII only, for the reason in the alignment note above: '#' is one
#: cell in the launcher's font and the block glyphs that would look denser are
#: not, so a figure built from them would sit at a different width every row.
GLYPHS = {
    "0": ("###", "# #", "# #", "# #", "###"),
    "1": (" # ", "## ", " # ", " # ", "###"),
    "2": ("###", "  #", "###", "#  ", "###"),
    "3": ("###", "  #", " ##", "  #", "###"),
    "4": ("# #", "# #", "###", "  #", "  #"),
    "5": ("###", "#  ", "###", "  #", "###"),
    "6": ("###", "#  ", "###", "# #", "###"),
    "7": ("###", "  #", "  #", "  #", "  #"),
    "8": ("###", "# #", "###", "# #", "###"),
    "9": ("###", "# #", "###", "  #", "###"),
    ".": (" ", " ", " ", " ", "#"),
}


def glyph_width(text: str) -> int:
    """Columns :func:`big` will need for *text*, gaps included."""
    if not text:
        return 0
    return sum(len(GLYPHS[c][0]) for c in text if c in GLYPHS) + GLYPH_GAP * (
        len(text) - 1
    )


def big(text: str) -> list[str]:
    """Render *text* as :data:`GLYPH_ROWS` rows of ASCII art."""
    parts = [GLYPHS[c] for c in text if c in GLYPHS]
    gap = " " * GLYPH_GAP
    return [gap.join(part[row] for part in parts) for row in range(GLYPH_ROWS)]


def face_value(value: int, budget: int) -> tuple[str, str]:
    """Split a byte count into the digits to draw and the unit to label them.

    Precision is whatever fits: ``1.68 GiB`` at two decimals, but a value that
    rounds up into an extra digit (``10.00``) loses one rather than overflowing
    the tile. Only the last candidate can exceed *budget*, and only on a tile
    too narrow for four digits at all.
    """
    gib = value / 1024**3
    if gib >= 1:
        unit, options = "GiB", [f"{gib:.2f}", f"{gib:.1f}", f"{gib:.0f}"]
    elif value >= 1024**2:
        mib = value / 1024**2
        unit = "MiB"
        options = [f"{mib:.1f}", f"{mib:.0f}"] if mib < 10 else [f"{mib:.0f}"]
    else:
        unit, options = "KiB", [f"{value / 1024:.0f}"]
    for text in options:
        if glyph_width(text) <= budget:
            return text, unit
    return options[-1], unit


def compose_tile(doc: dict, paint: Paint, width: int) -> list[tuple[str, str]]:
    """Build the tile face as ``(plain, styled)`` rows.

    Five rows exactly, which is the whole tile and is also exactly the figure's
    height: the figure on the left, and beside it the unit, the share of today's
    pool, tonight's top-up and the paid part. The first line of the small print
    is blank in the ordinary case, and is where a stale or offline reading gets
    to say so — of the five rows it is the only one with anything to give.
    """
    remainder = doc["today"]["remainder_bytes"]
    pool = max(1, doc["today"]["pool_bytes"])
    colour = grade(remainder / pool)

    text, unit = face_value(remainder, width - FACE_GAP - FACE_SPINE)
    art = big(text)
    # Every row of the figure is the same width, so the small print aligns to
    # the figure rather than to the tile: the face reads as one object however
    # many digits the value has.
    block = max(len(row) for row in art)
    room = width - block - FACE_GAP

    def fit(*options: str) -> str:
        """The first spelling of a line that fits the room the figure left.

        Each ladder ends in something a handful of columns wide, so the last
        rung always fits and the slice below never actually cuts anything; it
        is there so that a width this function cannot satisfy still cannot
        overflow the tile.
        """
        for option in options:
            if len(option) <= room:
                return option
        return options[-1][:room]

    # A stale or offline reading overstates what is left, so it is not something
    # the face gets to drop. It goes on the one line that is otherwise empty,
    # which also puts it where nothing normally is.
    reading = doc["reading"]
    tail = (
        since(reading["age_seconds"])
        if not reading["live"] and reading["age_seconds"] >= 90
        else ""
        if reading["online"]
        else "offline"
    )

    # The share states the figure against today's pool, so the number has a
    # scale attached rather than floating free.
    share = f"{remainder / pool * 100:.0f}%"
    stamp = dt.datetime.fromisoformat(doc["reset"]["local"]).strftime("%H:%M")
    grant = size(doc["free"]["grant_bytes"])

    # What is left is two different things wearing one number: tonight's grant,
    # which expires at the reset, and paid data, which does not. Only the second
    # cost money, so the face names it rather than leaving the figure to be read
    # as free. Nothing to say when none of it is paid.
    paid = size(doc["paid"]["left_bytes"]) if doc["paid"]["left_bytes"] > 0 else ""

    small = [
        fit(tail),
        fit(f"{unit} left", unit),
        fit(f"{share} of {size(pool)}", share),
        # The top-up amount gives way before the clock time does: when it goes,
        # the row still says the one thing that cannot be worked out from the
        # rest of the face.
        fit(f"+{grant} {stamp}", f"reset {stamp}", stamp),
        fit(f"{paid} paid", paid) if paid else "",
    ]

    gap = " " * FACE_GAP
    rows: list[tuple[str, str]] = []
    for line, note in zip(art, small, strict=False):
        if note:
            rows.append(
                (f"{line}{gap}{note}", f"{paint(line, colour)}{gap}{paint(note, '90')}")
            )
        else:
            # Trailing spaces are dropped rather than padded out: some widgets
            # draw them as a stray highlighted band.
            rows.append((line.rstrip(), paint(line.rstrip(), colour)))
    return rows


def render_tile(doc: dict, paint: Paint, width: int = TILE, margin: int = 0) -> str:
    """Draw the narrow tile face as one string.

    *margin* nudges the whole face right, and is a request rather than an
    instruction: it gives way to whatever the widest row needs, because a face
    shifted into a wrap costs a row and the tile has none to give. A five-glyph
    figure therefore sits where it fits and everything else takes the nudge.
    """
    rows = compose_tile(doc, paint, width)
    widest = max(len(plain) for plain, _ in rows)
    lead = " " * max(0, min(margin, width - 1 - widest))
    return "\n".join(f"{lead}{styled}" if plain else "" for plain, styled in rows)


# --------------------------------------------------------------------------- #
# The Quick Settings tile: two strings, a state and a level
# --------------------------------------------------------------------------- #
#
# A Quick Settings tile is not a small widget, and the face above does not
# shrink into one. Android gives it a label, a subtitle, an icon and a state
# (ACTIVE draws it lit, INACTIVE dim), and nothing else — no rows to lay out, no
# monospace to align, and no way to redraw it except by running something. So
# this composition answers a different question: of everything the box says,
# which two phrases survive being the only two?
#
# The label is the figure, because that is what the tile is for. The subtitle is
# a ladder, and what it keeps at each rung is the rule worth pinning: the reset
# time, which is the one fact on the face that cannot be worked out from the
# rest of it — except when the reading is stale or the portal is offline, which
# means the figure overstates what is left, and then saying so outranks
# everything, exactly as it does on the tile face.
#
# The widths are character budgets against a *proportional* font, so they are
# approximations rather than the measured cell counts :data:`TILE` is. That is
# why they are conservative, why every ladder ends in something very short, and
# why ``--qs-width`` exists: set the tile to a ruler once (``--qs-width 99 99``
# with a known string) and read off where the launcher clips it.

#: Characters the tile's label and subtitle get. One UI clips both without a
#: word, and a clipped subtitle is usually the reset time that got clipped.
QS_LABEL = 10
QS_STATUS = 16

#: Budgets for a tile whose size is the *user's* choice rather than the
#: system's. One UI 8.5 lets a tile be dragged to a different size in the
#: panel, and nothing tells the app which size was picked — there is no
#: callback, no configuration change, nothing to read. So the size is a setting
#: here too: pick the preset matching the tile you dragged out, or measure your
#: own with ``--qs-probe`` and set ``--qs-width``.
#:
#: ``small`` has a status of zero, which means *this tile has no subtitle*
#: rather than "fit the subtitle into nothing". It is the one size that cannot
#: carry the reset time, and that is a real loss rather than a tidier layout:
#: what survives is the figure and the level the icon draws. Choosing it is
#: choosing that, which is why it is named and documented rather than reached
#: by setting the width to something small and seeing what happens.
QS_SIZES = {
    "small": (5, 0),
    "medium": (QS_LABEL, QS_STATUS),
    "large": (12, 34),
}

#: What the fourth line can say: a word for the icon to follow, since an icon is
#: the one part of the tile that no amount of text budget buys back.
QS_LEVELS = {"1;32": "ok", "1;33": "low", "1;31": "critical"}

#: The tile with no reading behind it at all. Four lines like any other answer,
#: because the caller splits on newlines and a short answer would leave the
#: label of a *previous* run standing beside this run's subtitle.
#:
#: The state is still one of the two ordinary ones, deliberately. Android has a
#: third, ``UNAVAILABLE``, which greys the tile out and stops it being tapped —
#: and a tap is how this one recovers, so a portal that was down when the tile
#: last ran would leave no way to ask it again. The level says ``unknown``
#: instead, where it costs an icon rather than the way out.
QS_UNKNOWN = ("quota ?", "no reading", "inactive", "unknown")


def qs_size(value: int, room: int) -> str:
    """The remainder, in as much precision as *room* characters allow.

    The first spelling is :func:`size`'s, so an unclipped tile reads exactly
    like the widget and the status line do; the rungs below it give up decimals
    and then the space before the unit, which is the last thing that can go
    before the figure itself is wrong.

    Every branch's last rung also drops the thousands separator, because that
    rung is the one the smallest tile lands on and a comma is a whole character
    spent on punctuation. The self-test found this the honest way: at 1,023
    bytes there was no rung under seven characters at all.
    """
    gib = value / 1024**3
    if gib >= 1:
        options = [
            f"{gib:,.2f} GiB",
            f"{gib:,.1f} GiB",
            f"{gib:,.0f} GiB",
            f"{gib:.0f}G",
        ]
    elif value >= 1024**2:
        mib = value / 1024**2
        options = [f"{mib:,.0f} MiB", f"{mib:.0f}M"]
    elif value >= 1024:
        options = [f"{value / 1024:,.0f} KiB", f"{value / 1024:.0f}K"]
    else:
        options = [f"{value:,.0f} B", f"{value:.0f}B"]
    for text in options:
        if len(text) <= room:
            return text
    return options[-1]


def compose_qs(
    doc: dict, label_width: int = QS_LABEL, status_width: int = QS_STATUS
) -> tuple[str, str, str, str]:
    """Build the tile as ``(label, subtitle, state, level)``.

    *state* is what Android does with the tile itself: ``active`` while there is
    free data left to spend, ``inactive`` once the figure is only paid data, so
    the tile answers "is it free right now" without being read at all.
    """
    remainder = doc["today"]["remainder_bytes"]
    pool = max(1, doc["today"]["pool_bytes"])
    reading = doc["reading"]

    label = qs_size(remainder, label_width)
    share = f"{remainder / pool * 100:.0f}%"
    stamp = dt.datetime.fromisoformat(doc["reset"]["local"]).strftime("%H:%M")
    grant = qs_size(doc["free"]["grant_bytes"], 8)

    # Same rule as the face, and the same reason: a reading that overstates what
    # is left has to say so. Here it costs the share rather than a spare row.
    mark = (
        since(reading["age_seconds"])
        if not reading["live"] and reading["age_seconds"] >= 90
        else ""
        if reading["online"]
        else "offline"
    )

    # Separators are ASCII on purpose. Nothing here is aligned, so the reason is
    # not the widget's font drift but the pipe: this text crosses a shell, a
    # plugin and Tasker's own variable handling before anything draws it.
    #
    # The ladder is climbed as well as descended: a tile dragged wider gets the
    # top rung, which spells the pool out rather than leaving the extra room
    # blank. A wide tile saying as little as a narrow one is the same waste as a
    # narrow one clipped, just quieter about it.
    if mark:
        ladder = (
            f"{mark}, {share} of {size(pool)}, {stamp}",
            f"{mark}, {share}, {stamp}",
            f"{mark}, {stamp}",
            mark,
        )
    else:
        ladder = (
            f"{share} of {size(pool)}, +{grant} {stamp}",
            f"{share}, +{grant} at {stamp}",
            f"{share}, +{grant} {stamp}",
            f"{share}, reset {stamp}",
            f"{share}, {stamp}",
            stamp,
        )
    # Zero is "this tile has no subtitle", not "fit one into nothing": the small
    # size genuinely has nowhere to put it, and half a phrase would be worse
    # than none. Every other width falls back to the shortest rung.
    if status_width <= 0:
        status = ""
    else:
        status = next(
            (text for text in ladder if len(text) <= status_width), ladder[-1]
        )

    state = "active" if doc["free"]["left_bytes"] > 0 else "inactive"
    return label, status, state, QS_LEVELS[grade(remainder / pool)]


def render_qs(
    doc: dict, label_width: int = QS_LABEL, status_width: int = QS_STATUS
) -> str:
    """The four lines Tasker reads, in the order it splits them into.

    Order is the interface: Tasker addresses these as ``%stdout1``..``%stdout4``
    after a Variable Split, so reordering them silently relabels the tile rather
    than failing. The self-test pins it.
    """
    return "\n".join(compose_qs(doc, label_width, status_width))


def qs_probe(label_width: int, status_width: int) -> str:
    """A tile made of rulers, for reading the real widths off the real panel.

    The same ruler the widget's :func:`probe` uses, because the same question is
    being asked: not "does this fit" but "how much fits". Put it on the tile,
    drag the tile to the size you want, and read the last mark still visible —
    that is the budget, and it is the number ``--qs-width`` wants. Guessing it
    from the tile's apparent size cannot work: the font is proportional, so the
    answer differs between a label of digits and a label of letters, which is
    why the ruler is made of both.

    Deliberately longer than any tile can show. A ruler that fits tells you
    nothing except that it fits.
    """
    rule = "----+----1----+----2----+----3----+----4----+----5"
    return "\n".join(
        (
            rule[: max(label_width * 2, 20)],
            rule[: max(status_width * 2, 30)],
            "active",
            "ok",
        )
    )


def compose(doc: dict, paint: Paint, width: int) -> list[tuple[str, str]]:
    """Build the interior as ``(plain, styled)`` rows, ASCII only."""
    reset_local = dt.datetime.fromisoformat(doc["reset"]["local"])
    left = dt.timedelta(seconds=doc["reset"]["seconds_until"])

    pool = max(1, doc["today"]["pool_bytes"])
    remainder = doc["today"]["remainder_bytes"]
    grant = max(1, doc["free"]["grant_bytes"])

    # The face of the widget deliberately shows measured figures only. Free
    # left is derived and carries a one-sided error, so it lives in --json
    # where its caveats travel with it -- it is deliberately not read here.
    colour = bar_colour = grade(remainder / pool)

    rows: list[tuple[str, str]] = []

    def row(plain: str = "", styled: str | None = None) -> None:
        rows.append((plain, styled if styled is not None else plain))

    pad = " " * INDENT
    inner = width - 2 * INDENT

    head = "ZWANA DATA"
    # Flag only the states worth flagging: a reading seconds old is simply
    # current, and an online session is the unremarkable case. A status dot was
    # the obvious thing here, but every candidate glyph is outside ASCII and so
    # risks the width problem this layout exists to avoid.
    reading = doc["reading"]
    tail = (
        since(reading["age_seconds"])
        if not reading["live"] and reading["age_seconds"] >= 90
        else ""
        if reading["online"]
        else "offline"
    )
    gap = max(1, inner - len(head) - len(tail))
    row(
        f"{pad}{head}{' ' * gap}{tail}".rstrip(),
        f"{pad}{paint(head, '1;36')}"
        + (f"{' ' * gap}{paint(tail, '90')}" if tail else ""),
    )
    row()

    # The headline is what is actually left to spend, measured, not the money
    # behind it — and it is stated against today's pool so the number has a
    # scale attached rather than floating free.
    big = size(remainder)
    tag = f"left of {size(pool)}"
    row(f"{pad}{big} {tag}", f"{pad}{paint(big, colour)} {paint(tag, '90')}")

    # The bar spans today's whole pool: the nightly grant plus anything drawn
    # from the reserve since. Filled is what has gone.
    used = max(0, pool - remainder)
    pct = f"{used / pool * 100:3.0f}%"
    track = max(4, inner - len(pct) - 4)
    filled = min(track, max(0, round(used / pool * track)))
    # The empty track is middots: measured at exactly one cell in the widget
    # font, and they recede behind the filled run in a way dashes do not.
    bar = "=" * filled + "·" * (track - filled)
    row(
        f"{pad}[{bar}] {pct}",
        f"{pad}{paint('[', '90')}{paint(bar[:filled], bar_colour.split(';')[-1])}"
        f"{paint(bar[filled:], '90')}{paint(']', '90')} {paint(pct, '90')}",
    )

    row()

    stamp = reset_local.strftime("%H:%M")
    # "today" is the common case and says nothing; name the day only when the
    # reset has slipped past midnight local and the bare time would mislead.
    label = day_label(reset_local)
    when = stamp if label == "today" else f"{stamp} {label}"
    topup = f"+{size(grant)}"
    # The named day can push the row past the frame; the countdown restates
    # what the clock time already says, so it is the part that gives way.
    tail = countdown(left)
    if INDENT + len(f"{topup} at {when} · {tail}") > width:
        tail = ""
    row(
        f"{pad}{topup} at {when}" + (f" · {tail}" if tail else ""),
        f"{pad}{paint(topup, '1;37')}{paint(' at ', '90')}{paint(when, '37')}"
        + (f"{paint(' · ', '90')}{paint(tail, '37')}" if tail else ""),
    )

    reserve = f"reserve {size(doc['reserve']['bytes'])} "
    cash = money(doc["reserve"]["credits"])
    row(f"{pad}{reserve}{cash}", f"{pad}{paint(reserve, '90')}{paint(cash, '90')}")

    return rows


def render(
    doc: dict, paint: Paint, width: int = INNER, frame: str = "corners", margin: int = 0
) -> str:
    """Draw the whole widget as one string."""
    rows = compose(doc, paint, width)
    lead = " " * margin
    edge = lambda s: paint(s, "90")  # noqa: E731
    out: list[str] = []

    # A blank row stays genuinely blank: prefixing it with the margin would
    # leave a line of trailing spaces, which some widgets render as a stray
    # highlighted band.
    place = lambda styled: f"{lead}{styled}" if styled else ""  # noqa: E731

    if frame == "corners":
        span = " " * (width - 4)
        out.append(lead + edge("╭─") + span + edge("─╮"))
        out.append("")
        out.extend(place(styled) for _, styled in rows)
        out.append("")
        out.append(lead + edge("╰─") + span + edge("─╯"))
    elif frame == "box":
        # Only safe where every glyph really is one cell, i.e. a real terminal.
        out.append(lead + edge("╭" + "─" * width + "╮"))
        for plain, styled in rows:
            out.append(
                f"{lead}{edge('│')}{styled}"
                f"{' ' * max(0, width - len(plain))}{edge('│')}"
            )
        out.append(lead + edge("╰" + "─" * width + "╯"))
    else:
        out.extend(place(styled) for _, styled in rows)

    return "\n".join(out)


def render_line(doc: dict, paint: Paint) -> str:
    """One-line variant, for a status bar or a very small widget."""
    left = countdown(dt.timedelta(seconds=doc["reset"]["seconds_until"]))
    stamp = dt.datetime.fromisoformat(doc["reset"]["local"]).strftime("%H:%M")
    reading = doc["reading"]
    mark = (
        ""
        if reading["live"] or reading["age_seconds"] < 90
        else f" ({since(reading['age_seconds'])})"
    )
    return (
        f"{size(doc['today']['remainder_bytes'])} left of "
        f"{size(doc['today']['pool_bytes'])} · "
        f"+{size(doc['free']['grant_bytes'])} at {stamp} in {left}{mark}"
    )


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #


PROBE_LOG = Path.home() / ".cache" / "zwana" / "probe.txt"

#: Ten characters each, nine tests to a page. Nine because the ruler is longer
#: than the tile and wraps to three rows of its own, which leaves nine of the
#: twelve readable rows — a tenth test would be drawn half off the bottom.
#: Whichever lines end in the same column are genuinely one cell wide in the
#: launcher's font; the ones that overhang are the glyphs that cannot be used
#: for alignment, and a line of boxes is a glyph it has no font for at all.
GLYPH_PAGES = {
    "ascii": [
        ("dash", "-"),
        ("hash", "#"),
        ("box rule", "─"),
        ("blk dark", "▓"),
        ("blk lite", "░"),
        ("blk full", "█"),
        ("bullet", "●"),
        ("middot", "·"),
        ("square", "▪"),
    ],
    # Nerd Font icons live in the private use area, so they render only if some
    # font on the device claims those codepoints. ~/.termux/font.ttf does
    # (SauceCodePro Nerd Font Mono) and the terminal is fine; nothing in
    # /system/fonts does, which is what a launcher widget draws with.
    #
    # Screenshotted in the tile: the first five come out as tofu boxes, and the
    # next three — the codepoints Samsung's SECCJK-Regular-Extra happens to use
    # for its own private-use glyphs — come out as unrelated CJK characters at
    # double width. So the answer is not "no icon" but "wrong icon, wrong
    # width", which is why the face is ASCII and this page stays as evidence.
    "nerd": [
        ("nf clock", ""),
        ("nf db", ""),
        ("nf dl", ""),
        ("nf hdd", ""),
        ("pl sep", ""),
        ("nf git", ""),
        ("md prog", ""),
        # The last two are controls. If these do not render, the page is
        # telling you about the tile rather than about the fonts.
        ("blk full", "█"),
        ("ascii #", "#"),
    ],
}


def probe(page: str = "ascii") -> str:
    """Print a ruler the launcher cannot lie about, and log the environment.

    One screenshot of this answers three questions at once: how many columns
    the tile shows, how many rows, and which glyphs are one cell wide (which
    test lines end flush with the ASCII ones).

    Reading the first two off the picture needs one correction each. The ruler
    is longer than any tile, so it wraps, and the column count is the width of
    its *first* visual row — the ``01`` prefix included — which the wrapped rows
    then confirm. Those extra rows are real rows, so the height is two more than
    the last test number you can read whole, not the number itself.

    The environment, argv and parent process are written to :data:`PROBE_LOG`
    so it can also be read back over the shell — that is what reveals whether
    the launcher hands us a pty or any geometry at all.
    """
    import shutil

    tests = GLYPH_PAGES[page]
    lines = ["01 ----+----1----+----2----+----3----+----4----+----5----+----6"]
    for number, (name, glyph) in enumerate(tests, start=2):
        lines.append(f"{number:02d} {glyph * 10} {name}")
    # Numbering runs past the tile on purpose: the last one visible is the row
    # count, and it is the only way to learn the tile grew.
    for number in range(len(tests) + 2, 25):
        lines.append(f"{number:02d}")

    try:
        parent = (
            Path(f"/proc/{os.getppid()}/cmdline")
            .read_bytes()
            .replace(b"\0", b" ")
            .decode()
        )
    except OSError:
        parent = "?"

    report = {
        "argv": sys.argv,
        "isatty": {"stdout": sys.stdout.isatty(), "stdin": sys.stdin.isatty()},
        "terminal_size": list(shutil.get_terminal_size(fallback=(-1, -1))),
        "ppid": os.getppid(),
        "parent_cmdline": parent,
        "cwd": os.getcwd(),
        "env": dict(sorted(os.environ.items())),
    }
    PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
    PROBE_LOG.write_text(json.dumps(report, indent=2))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #


#: The face's whole budget, and the reason this self-test exists: all five rows
#: the tile shows, inside its 35 columns. A face one row taller loses a line to
#: the bottom edge, and one column too wide wraps, which costs a row and so
#: loses a line as well. Neither is an error — the tile just draws less — so
#: they are checked here rather than found on the home screen, days later, by
#: noticing something that is not there.
TILE_ROWS = 5


def _fake(
    remainder: int, *, grant: int = 800_000_000, drawn: int = 0, live: bool = True
) -> dict:
    """A derived document for a given remainder, with no portal involved."""
    now = time.time()
    reading = {
        "ts": now,
        "credits": 18.0,
        "per_credit": 419_430_400,
        "remainder": remainder,
        "allocated": 0,
        "grant": grant,
        "drawn_today": drawn,
        "online": True,
        "profile": "",
        "pool_day": dt.datetime.now(dt.UTC).date().isoformat(),
        "pool": max(remainder, grant + drawn),
        "pool_first_ts": now,
    }
    return derive(reading, 0.0, live)


def _self_test() -> int:
    """Check the face fits the tile at every figure it can ever draw.

    The magnitudes are the ones that change the layout rather than a spread for
    its own sake: the unit thresholds, the values that gain a digit, and the
    ones :func:`face_value` has to drop a decimal for. The stale and offline
    readings are here because a reading that overstates what is left must say so
    on a face that has one line spare for it.
    """
    passed = failed = 0

    def check(label: str, got, want) -> None:
        nonlocal passed, failed
        if got == want:
            passed += 1
        else:
            failed += 1
            print(f"FAIL {label}: got {got!r}, want {want!r}")

    def at_most(label: str, got: int, limit: int) -> None:
        nonlocal passed, failed
        if got <= limit:
            passed += 1
        else:
            failed += 1
            print(f"FAIL {label}: {got} exceeds {limit}")

    plain = Paint(False)

    for key, glyph in sorted(GLYPHS.items()):
        check(f"{key!r} is {GLYPH_ROWS} rows", len(glyph), GLYPH_ROWS)
        check(f"{key!r} rows are one width", len({len(row) for row in glyph}), 1)
    # A promised width the renderer does not honour would quietly break the
    # centring and the right-aligned rows that hang off it.
    for text in ("0", "8.8", "1.68", "999", "10.5"):
        check(
            f"width of {text!r}", glyph_width(text), max(len(row) for row in big(text))
        )

    values = [
        0,
        1,
        1023,
        900 * 1024,
        5 * 1024**2,
        174_000_000,
        999 * 1024**2,
        1_800_000_000,
        10_700_000_000,
        99 * 1024**3,
        999 * 1024**3,
    ]
    for value in values:
        doc = _fake(value)
        stamp = dt.datetime.fromisoformat(doc["reset"]["local"]).strftime("%H:%M")
        for margin in (0, TILE_MARGIN, 2):
            rows = render_tile(doc, plain, width=TILE, margin=margin).split("\n")
            check(f"{value} margin {margin} rows", len(rows), TILE_ROWS)
            at_most(
                f"{value} margin {margin} width", max(len(row) for row in rows), TILE
            )
        # The figure and the small print share one row, so a wide figure now
        # crowds the print sideways where it used to push it off the bottom.
        # The reset is what must survive that: it is the only thing on the face
        # that cannot be worked out from the rest of it.
        check(
            f"{value} keeps the reset",
            stamp in [row for row, _ in compose_tile(doc, plain, TILE)][3],
            True,
        )

    # Where the face sits in the tile. The row the face does not use is spent
    # above it, which centres it; grow the offset past that one spare row and
    # the bottom line goes over the edge, as silently as everything else here.
    at_most("the face and its offset fit the tile", TILE_TOP + TILE_ROWS, TILE_LINES)
    placed = _fake(174_000_000)
    check(
        "the default margin is drawn",
        render_tile(placed, plain, margin=TILE_MARGIN).split("\n")[0],
        " " * TILE_MARGIN + render_tile(placed, plain, margin=0).split("\n")[0],
    )
    # Both offsets are defaults rather than fixed, and an explicit zero has to
    # survive being one: resolving them with `or` would quietly undo it.
    check("--margin unset is unset", parse_args([]).margin, None)
    check("--margin 0 stays 0", parse_args(["--margin", "0"]).margin, 0)
    check("--top unset is unset", parse_args([]).top, None)
    check("--top 0 stays 0", parse_args(["--top", "0"]).top, 0)

    # The two readings that overstate what is left. The tail is not decoration:
    # without it the face states a figure it cannot support, so check it is
    # drawn and not merely that the row it lives on still fits.
    stale_doc = _fake(1_800_000_000, live=False)
    stale_doc["reading"]["age_seconds"] = 7200
    offline_doc = _fake(1_800_000_000)
    offline_doc["reading"]["online"] = False
    for name, doc, mark in (
        ("stale", stale_doc, "2h ago"),
        ("offline", offline_doc, "offline"),
    ):
        rows = render_tile(doc, plain, width=TILE, margin=2).split("\n")
        check(f"{name} rows", len(rows), TILE_ROWS)
        at_most(f"{name} width", max(len(row) for row in rows), TILE)
        check(f"{name} is said on the face", mark in rows[0], True)
    # The converse, and the reason the top line can be the tail's: a current
    # reading leaves it to the figure, with nothing on it to read.
    check(
        "a live reading says nothing",
        any(
            c.isalnum()
            for c in [
                row for row, _ in compose_tile(_fake(1_800_000_000), plain, TILE)
            ][0]
        ),
        False,
    )

    # The paid line names what a credit bought, so it has to say so only when
    # some of the remainder is paid, and has to fit at the widest it can get:
    # a full reserve carried in, which is the longest string the face can draw.
    paid = _fake(1_800_000_000)
    check(
        "paid line named",
        any("paid" in row for row, _ in compose_tile(paid, plain, TILE)),
        True,
    )
    free_only = _fake(700_000_000)
    check(
        "no paid line when all free",
        any("paid" in row for row, _ in compose_tile(free_only, plain, TILE)),
        False,
    )
    check(
        "paid line reads as bytes",
        [row for row, _ in compose_tile(paid, plain, TILE)][4].endswith("954 MiB paid"),
        True,
    )
    for value in (5 * 1024**2 + 1, 999 * 1024**3, 9_999 * 1024**3):
        rows = render_tile(_fake(value, grant=1), plain, width=TILE, margin=2).split(
            "\n"
        )
        check(f"paid {value} rows", len(rows), TILE_ROWS)
        at_most(f"paid {value} width", max(len(row) for row in rows), TILE)

    # The Quick Settings tile. Nothing here can be seen going wrong: Android
    # clips the two strings without a word, and Tasker addresses them by
    # position, so a line too long, a line too many or two lines swapped all
    # draw a plausible tile that is not the one meant.
    for value in values:
        doc = _fake(value)
        lines = render_qs(doc).split("\n")
        stamp = dt.datetime.fromisoformat(doc["reset"]["local"]).strftime("%H:%M")
        check(f"qs {value} lines", len(lines), 4)
        at_most(f"qs {value} label", len(lines[0]), QS_LABEL)
        at_most(f"qs {value} status", len(lines[1]), QS_STATUS)
        check(f"qs {value} says something on every line", all(lines), True)
        # The reset survives the ladder, for the same reason it survives the
        # face: it is the only thing here that cannot be inferred from the rest.
        check(f"qs {value} keeps the reset", stamp in lines[1], True)
        check(f"qs {value} state", lines[2] in ("active", "inactive"), True)
        check(f"qs {value} level", lines[3] in QS_LEVELS.values(), True)

    # A narrower tile than the default drops the share and then the top-up, and
    # is still holding the reset when there is room for nothing else.
    tight = _fake(1_800_000_000)
    tight_stamp = dt.datetime.fromisoformat(tight["reset"]["local"]).strftime("%H:%M")
    for width in (QS_STATUS, 12, 10, 5):
        _, status, _, _ = compose_qs(tight, QS_LABEL, width)
        check(f"qs status {width} keeps the reset", tight_stamp in status, True)
    check(
        "qs status 5 is the reset alone",
        compose_qs(tight, QS_LABEL, 5)[1],
        tight_stamp,
    )

    # ...unless the reading overstates what is left, when saying so outranks it.
    for name, doc, mark in (
        ("stale", stale_doc, "2h ago"),
        ("offline", offline_doc, "offline"),
    ):
        for width in (QS_STATUS, 8):
            _, status, _, _ = compose_qs(doc, QS_LABEL, width)
            check(f"qs {name} {width} says so", mark in status, True)
            at_most(f"qs {name} {width} status", len(status), max(width, len(mark)))

    # The state is the tile's own answer to "is this still free", so it has to
    # move when the free grant runs out and not when the figure merely gets low.
    check("qs active while free", compose_qs(_fake(700_000_000))[2], "active")
    check(
        "qs inactive once paid only",
        compose_qs(_fake(1_800_000_000, grant=800_000_000, drawn=1_800_000_000))[2],
        "inactive",
    )
    # Every colour the face can grade to needs a word here, or the icon lookup
    # raises on the one day the figure is lowest.
    check(
        "every grade has a level",
        {grade(share) for share in (1.0, 0.3, 0.24, 0.1, 0.07, 0.0)} <= set(QS_LEVELS),
        True,
    )
    # Every named size, at every figure. A tile the user dragged to a size we
    # have a preset for must not be the way anyone finds out the preset is wrong.
    for name, (label_w, status_w) in sorted(QS_SIZES.items()):
        for value in values:
            doc = _fake(value)
            label, status, state, level = compose_qs(doc, label_w, status_w)
            at_most(f"qs {name} {value} label", len(label), label_w)
            at_most(f"qs {name} {value} status", len(status), max(status_w, 0))
            check(f"qs {name} {value} has a label", bool(label.strip()), True)
            check(f"qs {name} {value} state", state in ("active", "inactive"), True)
            check(f"qs {name} {value} level", level in QS_LEVELS.values(), True)
            # The small tile is the only one allowed to lose the reset, and it
            # loses it wholesale rather than by halves.
            stamp = dt.datetime.fromisoformat(doc["reset"]["local"]).strftime("%H:%M")
            if status_w > 0:
                check(f"qs {name} {value} keeps the reset", stamp in status, True)
            else:
                check(f"qs {name} {value} has no subtitle", status, "")

    # The wide tile is the reason the ladder is climbed as well as descended:
    # given the room, it says more rather than leaving it blank.
    wide = _fake(1_800_000_000)
    small_status = compose_qs(wide, *QS_SIZES["medium"])[1]
    large_status = compose_qs(wide, *QS_SIZES["large"])[1]
    check("qs large says more than medium", len(large_status) > len(small_status), True)
    check("qs large names the pool", " of " in large_status, True)
    # ...and a stale reading still displaces it at every size, which is the one
    # thing that must not be what the extra room is spent on.
    for name, (label_w, status_w) in sorted(QS_SIZES.items()):
        if status_w <= 0:
            continue
        check(
            f"qs {name} stale says so",
            "2h ago" in compose_qs(stale_doc, label_w, status_w)[1],
            True,
        )

    # The ruler is a measuring tool, so the one thing it must never do is fit:
    # a ruler that fits reads as a width that was fine.
    for name, (label_w, status_w) in sorted(QS_SIZES.items()):
        rows = qs_probe(label_w, status_w).split("\n")
        check(f"qs probe {name} is four lines", len(rows), 4)
        check(f"qs probe {name} overruns the label", len(rows[0]) > label_w, True)
        check(f"qs probe {name} overruns the status", len(rows[1]) > status_w, True)

    # The address the tile's long press opens. Derived from the one the client
    # talks to, so it cannot drift from it; checked here because the browser is
    # the one caller that would show a wrong address rather than fail on it.
    check("portal url is the api's host", z.BASE_URL.startswith(z.PORTAL_URL), True)
    check("portal url drops the api path", "api" in z.PORTAL_URL, False)
    check("portal url ends in a slash", z.PORTAL_URL.endswith("/"), True)

    check("qs unknown is four lines", len(QS_UNKNOWN), 4)
    at_most("qs unknown label", len(QS_UNKNOWN[0]), QS_LABEL)
    at_most("qs unknown status", len(QS_UNKNOWN[1]), QS_STATUS)
    # A tap is how a tile with no reading recovers, so the tile that has none
    # must still be an ordinary tappable one. Android's third state is not.
    check("qs unknown stays tappable", QS_UNKNOWN[2] in ("active", "inactive"), True)
    check("qs unknown level is not a level", QS_UNKNOWN[3] in QS_LEVELS.values(), False)
    check("--qs-width unset is unset", parse_args([]).qs_width, None)
    check("--qs-width takes two", parse_args(["--qs-width", "8", "9"]).qs_width, [8, 9])

    # --full is for a wide tile or a terminal, so it only has to keep inside the
    # width it was given; the box frame draws a right border against it.
    for value in (0, 174_000_000, 1_800_000_000):
        rows = compose(_fake(value), plain, INNER)
        at_most(f"full {value} width", max(len(row) for row, _ in rows), INNER)

    # The reset row is longest in the hour after the reset: the countdown near
    # a full day and the local day label reading "tmrw". The wall clock only
    # provides that state once a day, so pin it here instead of inheriting it.
    late_doc = _fake(1_800_000_000)
    tomorrow = dt.datetime.now().astimezone().replace(
        hour=23, minute=0, second=0, microsecond=0
    ) + dt.timedelta(days=1)
    late_doc["reset"]["local"] = tomorrow.isoformat(timespec="seconds")
    late_doc["reset"]["seconds_until"] = 23 * 3600 + 48 * 60
    rows = compose(late_doc, plain, INNER)
    reset_row = next(row for row, _ in rows if row.lstrip().startswith("+"))
    at_most("full tmrw width", max(len(row) for row, _ in rows), INNER)
    check("full tmrw keeps the day", "tmrw" in reset_row, True)

    # Precision gives way to fitting, never the other way round. The budget the
    # tile actually hands it is the first of these: the width less the gap and
    # the columns the small print is owed.
    check("tile budget", TILE - FACE_GAP - FACE_SPINE, 18)
    check("full precision fits", face_value(1_800_000_000, 18), ("1.68", "GiB"))
    check("tighter drops a decimal", face_value(1_800_000_000, 11), ("1.7", "GiB"))
    check("narrow drops both", face_value(1_800_000_000, 8), ("2", "GiB"))
    check("MiB under a GiB", face_value(174_000_000, 18)[1], "MiB")
    check("KiB under a MiB", face_value(900 * 1024, 18)[1], "KiB")

    print(
        f"{passed} passed, {failed} failed" if failed else f"all {passed} checks passed"
    )
    return 1 if failed else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the CLI and parse *argv*."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="ignore the cache and ask the portal (blocking)",
    )
    parser.add_argument(
        "--refresh-only",
        action="store_true",
        help="update the cache and print nothing; what the "
        "detached background refresh runs",
    )
    parser.add_argument(
        "--max-age",
        type=float,
        default=DEFAULT_MAX_AGE,
        metavar="S",
        help="reuse a reading younger than this (default: %(default)s s)",
    )
    parser.add_argument("--plain", action="store_true", help="never emit colour")
    parser.add_argument(
        "--line", action="store_true", help="one line instead of the tile"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="the detailed box (bar, top-up, reserve) instead of "
        "the tile face; wants about 40 columns",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the derived figures as JSON for other scripts, "
        "instead of drawing anything",
    )
    parser.add_argument(
        "--qs",
        action="store_true",
        help="four lines for an Android Quick Settings tile: label, "
        "subtitle, state, level (see docs/quota-tile.md)",
    )
    parser.add_argument(
        "--qs-size",
        choices=sorted(QS_SIZES),
        default="medium",
        help="which tile you dragged out of the panel, since nothing "
        "tells us (default: %(default)s). small has no subtitle",
    )
    parser.add_argument(
        "--qs-width",
        type=int,
        nargs=2,
        default=None,
        metavar=("LABEL", "STATUS"),
        help="characters the tile's two strings get, overriding "
        f"--qs-size (default: {QS_LABEL} {QS_STATUS}). "
        "A status of 0 means the tile has no subtitle",
    )
    parser.add_argument(
        "--qs-probe",
        action="store_true",
        help="a tile made of rulers instead of the quota, to read the "
        "real widths off the real panel; no network",
    )
    parser.add_argument(
        "--frame",
        choices=("corners", "box", "none"),
        default="corners",
        help="--full only: corner marks (default), a full box, "
        "or nothing. 'box' assumes a true monospace font — "
        "a terminal, not a launcher widget",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        metavar="N",
        help=f"width in characters (default: {TILE} for the tile, {INNER} for --full)",
    )
    # Defaulted to None rather than to the numbers, so the tile can place itself
    # in the tile and --full can go on drawing flush the way a terminal wants.
    parser.add_argument(
        "--margin",
        type=int,
        default=None,
        metavar="N",
        help="blank columns on the left, to centre in the tile "
        f"(default: {TILE_MARGIN} for the tile, 0 for --full). "
        "Gives way to a figure that needs the columns",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        metavar="N",
        help="blank lines above, to centre in the tile "
        f"(default: {TILE_TOP} for the tile, 0 for --full)",
    )
    parser.add_argument(
        "--probe",
        nargs="?",
        const="ascii",
        metavar="PAGE",
        choices=sorted(GLYPH_PAGES),
        help="print a ruler and a glyph test page instead of the "
        f"quota ({' or '.join(sorted(GLYPH_PAGES))}; default "
        f"ascii), and log the environment to {PROBE_LOG}",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="check the face fits the tile; no network",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Non-zero only when there is nothing at all to show."""
    args = parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.probe:
        # Deliberately before any network work: calibration must cost nothing.
        print(probe(args.probe))
        return 0
    if args.qs_probe:
        print(qs_probe(*(args.qs_width or QS_SIZES[args.qs_size])))
        return 0

    if args.refresh_only:
        try:
            store(gather(stale()))
            return 0
        except z.PortalError:
            return 1

    paint = Paint(
        sys.stdout.isatty() and not args.plain and os.environ.get("TERM") != "dumb"
    )

    try:
        data, live = current(args.max_age, force=args.refresh)
    except z.PortalError as exc:
        data, live = stale(), False
        if data is None:
            # The tile is drawn from whatever comes back on stdout, so it gets a
            # tile that says it knows nothing rather than an empty one holding
            # last week's number. Still a failure exit for anything checking.
            if args.qs:
                print("\n".join(QS_UNKNOWN))
            print(paint(f"  quota unavailable\n  {exc}", "31"), file=sys.stderr)
            return 1

    doc = derive(data, time.time() - data["ts"], live)
    # An explicit width wins over the preset, so a measured tile beats a named
    # one — the presets are a starting point, not the answer.
    label_width, status_width = args.qs_width or QS_SIZES[args.qs_size]
    if args.qs:
        print(render_qs(doc, label_width, status_width))
    elif args.json:
        print(json.dumps(doc, indent=2))
    elif args.line:
        print(render_line(doc, paint))
    elif args.full:
        print("\n" * (args.top or 0), end="")
        print(
            render(
                doc,
                paint,
                width=args.width or INNER,
                frame=args.frame,
                margin=args.margin or 0,
            )
        )
    else:
        top = TILE_TOP if args.top is None else args.top
        margin = TILE_MARGIN if args.margin is None else args.margin
        print("\n" * top, end="")
        print(render_tile(doc, paint, width=args.width or TILE, margin=margin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
