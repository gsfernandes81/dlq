#!/usr/bin/env python3
"""Download the Windows Python installer, late but safely, and only if quota allows.

Why the timing matters
----------------------
The Zwana portal grants a fresh data allowance just after **00:00 UTC** (the
allocation history shows the grant landing at 00:02:19). Whatever is left before
then is lost, so the sensible moment to spend it is as late as possible while
still finishing comfortably. This script therefore refuses to start too early,
refuses to start so late it could not finish, and gives up rather than run past
the deadline.

Sizing, with real numbers
-------------------------
The installer is ~30.2 MB. At the assumed ~1 MB/s that is about **30 seconds**.
The default window opens 15 minutes before the deadline and the transfer is hard
-stopped 60 seconds before it, so the download gets ~14 minutes to do a
30-second job -- it still succeeds at roughly 1/28th of the assumed speed.

The quota gate
--------------
The download proceeds only if::

    remaining >= installer size + reserve      (reserve defaults to 100 MiB)

Remaining quota is read live from the portal via :mod:`zwana_quota`, which knows
that the portal's ``Balance`` is denominated in credits (1 credit = 400 MiB).
The check is deliberately made at run time, not at scheduling time: quota can be
spent by anything else on the connection in the meantime.

Safe to run at any time
-----------------------
Every guard exits 0 with an explanation when it simply is not time yet, and the
download is skipped outright if the file is already present and complete. That
makes the script safe to drive from a coarse periodic watchdog as well as from a
precisely-timed one-shot -- the two can both be armed without racing, because
completion is recorded on disk.

Usage
-----
::

    python fetch_python_installer.py --check-only   # evaluate the gate, download nothing
    python fetch_python_installer.py                # honour the window
    python fetch_python_installer.py --ignore-window  # run now regardless of clock
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import zwana_quota  # noqa: E402  (deliberate: lives in termux/, one level up)

URL = "https://www.python.org/ftp/python/3.14.3/python-3.14.3-amd64.exe"

#: Verified with a HEAD request on 2026-08-01. A mismatch means the release was
#: respun, so the download is rejected rather than silently accepting a
#: different artifact.
EXPECTED_SIZE = 30_213_192

DEFAULT_DEST = (
    Path.home() / "or3" / "work" / "bootstrap" / "downloads" / "python-3.14.3-amd64.exe"
)
LOG_FILE = Path.home() / "or3" / "work" / "bootstrap" / "downloads" / "download.log"

MIB = 1024 * 1024
DEFAULT_RESERVE = 100 * MIB
DEFAULT_LEAD_SECONDS = 15 * 60
DEFAULT_STOP_MARGIN = 60
ASSUMED_RATE_BPS = 1_000_000

CHUNK = 64 * 1024


def log(message: str) -> None:
    """Print *message* and append it to the log with a UTC timestamp."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    line = f"{stamp}  {message}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass  # logging must never be the reason a download fails


def human(byte_count: float) -> str:
    """Format a byte count for humans."""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(byte_count) < 1024 or unit == "GiB":
            return (
                f"{byte_count:,.0f} B" if unit == "B" else f"{byte_count:,.2f} {unit}"
            )
        byte_count /= 1024
    return f"{byte_count:,.2f} GiB"


def next_midnight_utc(now: datetime) -> datetime:
    """Return the next 00:00 UTC strictly after *now*."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=1)


def parse_deadline(value: str | None) -> datetime:
    """Return the deadline: an ISO-8601 UTC instant, or the next midnight UTC."""
    now = datetime.now(UTC)
    if not value:
        return next_midnight_utc(now)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def already_complete(dest: Path, expected_size: int) -> bool:
    """Whether *dest* is already downloaded in full."""
    return dest.is_file() and dest.stat().st_size == expected_size


def remote_size() -> int:
    """Return the installer's size from a HEAD request (a few hundred bytes)."""
    request = urllib.request.Request(URL, method="HEAD")
    with urllib.request.urlopen(request, timeout=30) as response:
        length = response.headers.get("Content-Length")
    if not length:
        raise RuntimeError("server did not report Content-Length")
    return int(length)


def download(dest: Path, size: int, stop_by: datetime) -> None:
    """Fetch the installer, resuming a partial file, aborting at *stop_by*.

    Written to ``<dest>.part`` and renamed only once the byte count matches, so
    an aborted attempt can never be mistaken for a finished download.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    have = partial.stat().st_size if partial.is_file() else 0
    if have > size:  # a stale or corrupt part file; start over
        partial.unlink()
        have = 0

    headers = {"Range": f"bytes={have}-"} if have else {}
    if have:
        log(f"resuming at {human(have)} of {human(size)}")

    request = urllib.request.Request(URL, headers=headers)
    started = time.monotonic()
    with (
        urllib.request.urlopen(request, timeout=60) as response,
        partial.open("ab" if have else "wb") as handle,
    ):
        while True:
            remaining_seconds = (stop_by - datetime.now(UTC)).total_seconds()
            if remaining_seconds <= 0:
                raise TimeoutError(
                    f"hard stop reached with {human(size - have)} still to fetch"
                )
            chunk = response.read(CHUNK)
            if not chunk:
                break
            handle.write(chunk)
            have += len(chunk)

    elapsed = time.monotonic() - started
    if have != size:
        raise RuntimeError(f"got {human(have)}, expected {human(size)}")

    os.replace(partial, dest)
    rate = have / elapsed if elapsed else float("inf")
    log(f"downloaded {human(have)} in {elapsed:,.1f}s ({human(rate)}/s)")


def sha256_of(path: Path) -> str:
    """Return the SHA256 of *path*, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the CLI and parse *argv* (defaults to ``sys.argv[1:]``)."""
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help="where to save the installer (default: %(default)s)",
    )
    parser.add_argument(
        "--deadline",
        metavar="ISO8601",
        help="must finish before this instant (default: next 00:00 UTC)",
    )
    parser.add_argument(
        "--lead-seconds",
        type=int,
        default=DEFAULT_LEAD_SECONDS,
        help="how long before the deadline the window opens (default: %(default)s)",
    )
    parser.add_argument(
        "--stop-margin",
        type=int,
        default=DEFAULT_STOP_MARGIN,
        help="hard-stop this many seconds before the deadline (default: %(default)s)",
    )
    parser.add_argument(
        "--reserve-bytes",
        type=int,
        default=DEFAULT_RESERVE,
        help="quota that must remain after the download "
        "(default: %(default)s, i.e. 100 MiB)",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=zwana_quota.DEFAULT_ENV,
        help="credentials file for the quota check",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="evaluate every gate and report, but download nothing",
    )
    parser.add_argument(
        "--ignore-window", action="store_true", help="skip the timing gates and run now"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. 0 = done or legitimately not yet; 1 = blocked or failed."""
    args = parse_args(argv)
    now = datetime.now(UTC)
    deadline = parse_deadline(args.deadline)
    window_opens = deadline - timedelta(seconds=args.lead_seconds)
    stop_by = deadline - timedelta(seconds=args.stop_margin)

    if already_complete(args.dest, EXPECTED_SIZE):
        log(f"already complete: {args.dest} ({human(EXPECTED_SIZE)}) - nothing to do")
        return 0

    print(f"now          : {now:%Y-%m-%d %H:%M:%SZ}")
    print(f"window opens : {window_opens:%Y-%m-%d %H:%M:%SZ}")
    print(f"hard stop    : {stop_by:%Y-%m-%d %H:%M:%SZ}")
    print(f"deadline     : {deadline:%Y-%m-%d %H:%M:%SZ}")

    if not args.ignore_window:
        if now < window_opens:
            wait = (window_opens - now).total_seconds()
            print(f"not yet: window opens in {wait / 60:,.1f} min")
            return 0
        needed = EXPECTED_SIZE / ASSUMED_RATE_BPS
        if (stop_by - now).total_seconds() < needed:
            log(
                f"too late: {(stop_by - now).total_seconds():,.0f}s left, "
                f"need ~{needed:,.0f}s at {human(ASSUMED_RATE_BPS)}/s - skipping"
            )
            return 1

    try:
        credits, remaining = zwana_quota.available_bytes(args.env)
    except zwana_quota.PortalError as exc:
        log(f"quota check failed, refusing to download: {exc}")
        return 1

    required = EXPECTED_SIZE + args.reserve_bytes
    print(f"quota        : {credits:g} credits = {human(remaining)}")
    print(
        f"required     : {human(EXPECTED_SIZE)} file + {human(args.reserve_bytes)} "
        f"reserve = {human(required)}"
    )

    if remaining < required:
        log(
            f"BLOCKED: {human(remaining)} available, need {human(required)} "
            f"(short by {human(required - remaining)})"
        )
        return 1
    print(f"gate         : PASS ({human(remaining - required)} to spare)")

    if args.check_only:
        print("check-only: stopping before download")
        return 0

    try:
        size = remote_size()
        if size != EXPECTED_SIZE:
            log(
                f"ABORT: server reports {human(size)}, expected {human(EXPECTED_SIZE)} "
                "- the release may have been respun"
            )
            return 1
        download(args.dest, size, stop_by)
    except (urllib.error.URLError, OSError, RuntimeError, TimeoutError) as exc:
        log(f"download failed: {type(exc).__name__}: {exc}")
        return 1

    digest = sha256_of(args.dest)
    log(f"OK {args.dest} sha256={digest}")
    (args.dest.parent / "download.json").write_text(
        json.dumps(
            {
                "url": URL,
                "size": EXPECTED_SIZE,
                "sha256": digest,
                "completed": datetime.now(UTC).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
