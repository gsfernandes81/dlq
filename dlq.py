#!/data/data/com.termux/files/usr/bin/python3
"""Queue any direct file URL for the expiring-quota runner.

ytq covers pages yt-dlp understands; this covers everything else. It sizes the
file from its response headers (headers only, no payload — costs nothing
meaningful), writes a queue item that downloads through :mod:`expire_dl`'s
bounded-Range machinery, and the nightly runner does the rest.

The queue contract says "unknown" is not a valid ``EXPECT_BYTES``, so when the
server will not state a size you must pass ``--expect-bytes`` yourself: it is
the cap enforced against the item, i.e. the most you are willing to let it
cost.

Usage::

    dlq <url>                    # probe the size, queue it
    dlq <url> --name x.iso       # choose the saved file name
    dlq <url> --probe            # print size and resume support only
    dlq <url> --expect-bytes N   # server states no size: set the cap
"""

from __future__ import annotations

import argparse
import json
import os
import math
import re
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ytq lives in its own checkout: $YTQ_HOME, a clone beside this one, or
# ~/ytq — the same resolution expire_sched uses, spelled here too because
# `dlq` is not the only door (python3 dlq.py runs this file directly).
_ytq = os.environ.get("YTQ_HOME")
_beside = Path(__file__).resolve().parent.parent / "ytq"
sys.path.insert(1, str(
    Path(_ytq).expanduser().resolve() if _ytq
    else (_beside.resolve() if _beside.is_dir() else Path.home() / "ytq")
))

import ytq  # noqa: E402  (from the ytq checkout: shared queue plumbing)

BASE_HEADERS = {
    # Sizes are meaningless if the server gzips, and expire_dl will ask for
    # identity too — probe what will actually be fetched.
    "Accept-Encoding": "identity",
    "User-Agent": "dlq/1",
}

TIMEOUT = 30


class ProbeError(RuntimeError):
    """The URL could not be sized."""


def _from_head(headers) -> tuple[int, bool | None]:
    """``(bytes, resumable)`` from a HEAD response. ``None`` = server not saying."""
    length = headers.get("Content-Length", "")
    size = int(length) if str(length).isdigit() else 0
    ranges = (headers.get("Accept-Ranges") or "").strip().lower()
    resumable = True if ranges == "bytes" else (False if ranges == "none" else None)
    return size, resumable


def _from_ranged(code: int, headers) -> tuple[int, bool | None]:
    """``(bytes, resumable)`` from a ``Range: bytes=0-0`` response.

    A 206 proves the server honours Range and names the total after the slash;
    a 200 proves it does not, and its Content-Length is the whole file.
    """
    if code == 206:
        content_range = headers.get("Content-Range", "")
        total = content_range.rsplit("/", 1)[-1].strip()
        return (int(total) if total.isdigit() else 0), True
    if code == 200:
        length = headers.get("Content-Length", "")
        return (int(length) if str(length).isdigit() else 0), False
    return 0, None


def probe(url: str, timeout: int = TIMEOUT) -> tuple[int, bool | None]:
    """Size the file and test resume support without downloading it.

    HEAD first; if that answers with both a length and an Accept-Ranges verdict
    we are done. Otherwise a GET for ``bytes=0-0``, closed unread, settles both
    at the cost of headers plus at most one payload byte.
    """
    size, resumable = 0, None
    try:
        request = urllib.request.Request(url, method="HEAD", headers=BASE_HEADERS)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            size, resumable = _from_head(response.headers)
    except urllib.error.HTTPError:
        pass  # Plenty of servers refuse HEAD; the ranged GET settles it.
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ProbeError(f"cannot reach {url}: {exc}")

    if size and resumable is not None:
        return size, resumable

    request = urllib.request.Request(
        url, headers={**BASE_HEADERS, "Range": "bytes=0-0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ranged_size, ranged_resumable = _from_ranged(
                response.getcode(), response.headers
            )
    except urllib.error.HTTPError as exc:
        if size:
            return size, resumable
        raise ProbeError(f"HTTP {exc.code} {exc.reason}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if size:
            return size, resumable
        raise ProbeError(f"cannot reach {url}: {exc}")
    return (
        ranged_size or size,
        ranged_resumable if ranged_resumable is not None else resumable,
    )


def name_from_url(url: str) -> str:
    """A filesystem-safe file name taken from the URL's last path segment."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(path).name).strip("-.")
    return base or "download"


def expect_bytes(size: int) -> int:
    """The cap to declare: the measurement plus the same margin ytq uses."""
    return int(math.ceil(size * ytq.OVERHEAD_EXACT)) + ytq.OVERHEAD_FIXED


def render(
    url: str,
    name: str,
    cap: int,
    size: int,
    sha256: str | None,
    probed: str,
    dest: str = "file",
) -> str:
    """The queue item source. Strings go through json.dumps so no URL or name
    can produce a file that does not parse."""
    if size:
        note = (
            f"Sized {size:,} bytes by a header probe on {probed}; "
            f"EXPECT_BYTES adds a 3% + {ytq.human(ytq.OVERHEAD_FIXED)} "
            f"margin for wire overhead and retries."
        )
        shown = ytq.human(size)
    else:
        note = (
            "The server would not state a size, so EXPECT_BYTES is a "
            "declared spending cap rather than a measurement."
        )
        shown = "size unstated"
    return f'''{ytq.SHEBANG}
# EXPIRE: v1
# EXPECT_BYTES: {cap}
# PARTIAL: yes
# DEST: {dest}
# SOURCE: url:{url}
# DESC: {f"{name} ({shown}, direct download)"[:160]}
"""{name}

{url}

{textwrap.fill(note, 78)}
"""

import sys

sys.path.insert(0, {json.dumps(str(ytq.HERE))})
import expire_dl  # noqa: E402

sys.exit(expire_dl.run(
    {json.dumps(url)},
    {json.dumps(name)},
    expect_sha256={ytq.literal(sha256)},
    total_hint={size},
))
'''


def _self_test() -> int:
    """Check everything that decides what gets written, without a network."""
    import ast
    import contextlib
    import io
    import os
    import tempfile

    passed = failed = 0
    terminal = sys.stdout

    @contextlib.contextmanager
    def _quiet():
        """Run a command with its output captured, so checking it is not noisy."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield out, err

    def check(label: str, got, want) -> None:
        nonlocal passed, failed
        if got == want:
            passed += 1
        else:
            failed += 1
            # To the real terminal, so a check made inside _quiet() reports
            # rather than disappearing into the buffer it is inspecting.
            print(f"FAIL {label}: got {got!r}, want {want!r}", file=terminal)

    check(
        "name comes from the path, not the query",
        name_from_url("https://x.example/a/b/tool-1.2.zip?tok=..%2F"),
        "tool-1.2.zip",
    )
    check(
        "percent-encoding is undone",
        name_from_url("https://x/My%20File%20(v2).iso"),
        "My-File-v2-.iso",
    )
    check(
        "a bare host still names something",
        name_from_url("https://x.example/"),
        "download",
    )
    check(
        "dot-leading names cannot hide", name_from_url("https://x/..%2F.."), "download"
    )

    check(
        "HEAD with length and ranges",
        _from_head({"Content-Length": "1000", "Accept-Ranges": "bytes"}),
        (1000, True),
    )
    check(
        "HEAD silent on ranges is unknown, not no",
        _from_head({"Content-Length": "1000"}),
        (1000, None),
    )
    check(
        "HEAD refusing ranges is no",
        _from_head({"Content-Length": "5", "Accept-Ranges": "none"}),
        (5, False),
    )
    check("HEAD without a length sizes nothing", _from_head({}), (0, None))
    check(
        "206 names the total and proves resume",
        _from_ranged(206, {"Content-Range": "bytes 0-0/12345"}),
        (12345, True),
    )
    check(
        "206 with an unknown total sizes nothing",
        _from_ranged(206, {"Content-Range": "bytes 0-0/*"}),
        (0, True),
    )
    check(
        "200 for a ranged request means no resume",
        _from_ranged(200, {"Content-Length": "777"}),
        (777, False),
    )

    check(
        "the cap is the ytq margin over the measurement",
        expect_bytes(100_000_000),
        math.ceil(100_000_000 * ytq.OVERHEAD_EXACT) + ytq.OVERHEAD_FIXED,
    )
    check("the cap is above the measurement", expect_bytes(1) > 1, True)

    # The written item has to survive the runner's own admission check.
    with tempfile.TemporaryDirectory() as raw:
        keep = (ytq.QUEUE, ytq.STAGING)
        ytq.QUEUE = Path(raw) / "queue"
        ytq.STAGING = ytq.QUEUE / ".staging"
        try:
            url = 'https://x.example/f.iso?a=1&b="2"'
            source = render(
                url, "f.iso", expect_bytes(9_000_000), 9_000_000, None, "2026-08-02"
            )
            path = ytq.write_item(30, "f", source)
            # As in ytq: off the phone the interpreter the item names is not
            # here, and that one objection is what the runner should raise.
            admits = (
                None
                if ytq.shebang_here()
                else f"shebang interpreter not found: {ytq.SHEBANG[2:].strip()!r}"
            )
            check("the runner would admit it", ytq.validate(path), admits)
            check("it is executable", os.access(path, os.X_OK), True)
            check(
                "shebang is the one Termux has",
                path.read_text().split("\n")[0],
                ytq.SHEBANG,
            )
            check("nothing is left staged", list(ytq.STAGING.iterdir()), [])
            # A file URL is what a dlq item is a download of, so queueing the
            # same one twice is caught by the same door ytq's is.
            check(
                "the item says what it is a download of",
                ytq.source_of(source),
                f"url:{url}",
            )
            clash = None
            try:
                ytq.write_item(40, "f", source)
            except ytq.Duplicate as exc:
                clash = exc
            check(
                "a second copy of the same URL is refused",
                clash and clash.name,
                "30-f.py",
            )
            check(
                "and --again is what writes it anyway",
                ytq.write_item(40, "f", source, again=True).name,
                "40-f.py",
            )
            tree = ast.parse(source, str(path))
            check("the item parses with the url intact", url in source, True)
            check(
                "the item imports from the queue root",
                f'sys.path.insert(0, "{ytq.HERE}")' in source,
                True,
            )
            check(
                "an unsized item renders a cap, not a measurement",
                "declared spending cap"
                in render(url, "f.iso", 5_000_000, 0, None, "2026-08-02"),
                True,
            )
            # Without --sha256 the item is rendered with None, which json.dumps
            # spells `null` — a name Python parses and does not have. That gets
            # past every check that stops at "does it compile", and the item
            # dies with NameError on the night it was queued for.
            check("no --sha256 still renders Python", ytq.json_leaks(source), [])
            check(
                "and with one",
                ytq.json_leaks(
                    render(url, "f.iso", 9_000_000, 9_000_000, "ab" * 32, "2026-08-02")
                ),
                [],
            )
            check("the docstring survives", ast.get_docstring(tree) is not None, True)
        finally:
            ytq.QUEUE, ytq.STAGING = keep

    # The entry point, twice over. Nothing here reaches the network: the probe
    # is a connection to a closed port on this machine, which fails instantly
    # and falls through to --expect-bytes exactly as it does on a dead link.
    with tempfile.TemporaryDirectory() as raw:
        keep = (ytq.QUEUE, ytq.STAGING, ytq.DONE, ytq.FAILED)
        ytq.QUEUE = Path(raw) / "queue"
        ytq.STAGING = ytq.QUEUE / ".staging"
        ytq.DONE = Path(raw) / "done"
        ytq.FAILED = Path(raw) / "failed"
        argv = ["http://127.0.0.1:9/thing.iso", "--expect-bytes", "1000"]
        queued = lambda: len(sorted(ytq.QUEUE.glob("*.py")))  # noqa: E731
        try:
            # Counted rather than read off the exit code: off the phone a
            # queued item still earns a warning about the Termux interpreter,
            # so the code says 1 for a success as well as for a refusal, and
            # what is written is the thing being checked anyway.
            with _quiet():
                main(argv)
            check("dlq queues it the first time", queued(), 1)
            with _quiet() as (_, err):
                second = main(argv)
            check("and refuses the second time", second, 1)
            check("writing nothing", queued(), 1)
            said = err.getvalue()
            check("saying what it already is", "already queued" in said, True)
            check("and how to mean it anyway", "--again" in said, True)
            with _quiet():
                main(argv + ["--again"])
            check("which then queues it a second time", queued(), 2)
        finally:
            ytq.QUEUE, ytq.STAGING, ytq.DONE, ytq.FAILED = keep

    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dlq",
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              dlq https://releases.ubuntu.com/24.04/ubuntu-24.04-desktop-amd64.iso
              dlq https://x.example/build --name build.tar.gz --sha256 <hex>
              dlq https://x.example/feed --expect-bytes 50000000

            for video pages use ytq; the queue itself is dlq — bare for
            the screen, or dlq status. docs: ~/dlq/docs/download-queue.md"""),
    )
    parser.add_argument("url", nargs="?", help="the file to download")
    parser.add_argument("--name", help="saved file name (default: from the URL)")
    parser.add_argument(
        "--number",
        type=int,
        metavar="NN",
        help="queue priority prefix (default: after the last)",
    )
    parser.add_argument(
        "--sha256", metavar="HEX", help="verify the finished file before delivery"
    )
    parser.add_argument(
        "--dest",
        metavar="DIR",
        help="put this one somewhere other than the configured file directory "
        "(dlq dest sets that)",
    )
    parser.add_argument(
        "--expect-bytes",
        type=int,
        metavar="N",
        help="spending cap when the server states no size",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="print the size and resume support, write nothing",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the item instead of writing it"
    )
    parser.add_argument(
        "--again",
        action="store_true",
        help="queue it even though this URL is already in the queue or done",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="check the sizing and item logic, no network",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.url:
        parser.error("a URL is required")

    name = args.name or name_from_url(args.url)
    try:
        size, resumable = probe(args.url)
    except ProbeError as exc:
        if args.expect_bytes and not args.probe:
            print(f"probe failed ({exc}); trusting --expect-bytes", file=sys.stderr)
            size, resumable = 0, None
        else:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    resume_note = {
        True: "server supports resume (Range honoured)",
        False: "server IGNORES Range - cannot resume across slices",
        None: "resume support unknown",
    }[resumable]
    if args.probe:
        print(
            f"{name}: "
            f"{f'{ytq.human(size)} ({size:,} bytes)' if size else 'size not stated'}"
            f"  ·  {resume_note}"
        )
        if size:
            print(
                f"would declare EXPECT_BYTES {expect_bytes(size):,} "
                f"({ytq.human(expect_bytes(size))})"
            )
        return 0

    if args.expect_bytes:
        cap = args.expect_bytes
        if size and cap < size:
            print(
                f"error: --expect-bytes {cap:,} is below the measured {size:,} bytes",
                file=sys.stderr,
            )
            return 1
    elif size:
        cap = expect_bytes(size)
    else:
        print(
            "error: the server would not state a size; pass --expect-bytes "
            "with the most you are willing to let it cost",
            file=sys.stderr,
        )
        return 1

    number = args.number if args.number is not None else ytq.next_number()
    slug = ytq.slugify(Path(name).stem)
    source = render(
        args.url,
        name,
        cap,
        size,
        args.sha256,
        time.strftime("%Y-%m-%d", time.gmtime()),
        dest=str(Path(args.dest).expanduser()) if args.dest else "file",
    )
    if args.dry_run:
        print(source, end="")
        return 0

    try:
        path = ytq.write_item(number, slug, source, again=args.again)
    except ytq.Duplicate as clash:
        # The same door ytq queues through, so the same answer: say what it
        # already is and how to mean it anyway. Re-buying a file that is
        # already on the disk is the whole thing this queue exists to avoid.
        print(f"error: {clash.says()} — {clash.stem}", file=sys.stderr)
        if clash.how == "name":
            print(
                "       matched by name, not by URL; it may be another file",
                file=sys.stderr,
            )
        print("       dlq --again queues it a second time", file=sys.stderr)
        return 1
    problem = ytq.validate(path)
    if problem:
        print(f"warning: the runner would reject this item: {problem}")
        print(f"written anyway at {path}")
        return 1
    print(
        f"queued {path.name} — {ytq.human(size) if size else 'size unstated'}, "
        f"cap {ytq.human(cap)}"
    )
    where = ytq.landing(args.dest or "file")
    print(f"lands in {where} once the nightly window has worked through it")
    if resumable is False and size > ytq.SLICE_MIN_BYTES:
        print(
            "warning: this server ignores Range requests, so the whole file "
            "must fit one night's slice or the item will fail"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
