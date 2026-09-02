#!/data/data/com.termux/files/usr/bin/python3
"""Resumable, budget-bounded downloader for expiring-quota queue items.

A queue item gets a *slice* of tonight's spendable data, not the whole file. It
takes that slice, stops cleanly, and continues on a later firing or a later
night. This module is the shared machinery for doing that safely.

The three ideas that make it work
---------------------------------
**Bounded Range requests.** Every request asks for ``bytes=off-(off+slice-1)``,
so the *server* ends the transfer at the slice boundary. Stopping exactly at
budget stops being a timing race against a local abort, and the response ends
with a clean EOF instead of a killed socket.

**The resume offset is always ``os.stat(part).st_size``.** Never a counter in a
metadata file. Writes are append-only, so whatever survives on disk is a valid
prefix of the remote file no matter how the process died — SIGKILL included.
The sidecar metadata holds validators and history, never position.

**The item stops itself before the runner would.** The slice it is handed is
already derated below the runner's kill thresholds, and it stops ~20s before the
hard deadline. The runner's guards remain as a backstop that a well-behaved item
never trips, because a cooperative stop leaves a resumable file and a SIGKILL
mid-write may not.

Contract with the runner
------------------------
Read from the environment: ``EXPIRE_WORK``, ``EXPIRE_OUT``,
``EXPIRE_SLICE_BYTES``, ``EXPIRE_STOP_EPOCH``, ``EXPIRE_RUN_ID``.

Progress is reported through ``$EXPIRE_WORK/.status.json``, rewritten atomically
every few seconds so a killed item still leaves a nearly-current figure. It
carries the run id it was given, so the runner can never mistake last night's
report for tonight's.

Typical item::

    #!/data/data/com.termux/files/usr/bin/python3
    # EXPIRE: v1
    # EXPECT_BYTES: 4200000000
    # PARTIAL: yes
    import sys; sys.path.insert(0, "/data/data/com.termux/files/home/dlq")
    import expire_dl
    sys.exit(expire_dl.run("https://example/big.iso", "big.iso"))

Note the shebang: on Termux ``/usr/bin/env`` does not exist, and the runner
rejects an item whose interpreter is not on disk.

A file smaller than one CHUNK must be fetched, not declined. The runner hands a
partial item a slice of exactly what it still needs, so every such file asks
for less than a chunk, and a sub-chunk guard that refused it before opening a
connection would strand it: exit 75 with nothing moved is "not tonight" to the
runner, and a cap under a MiB counts no stall against it, so no strike is taken
and no attempt is spent -- the item would be offered the same too-small slice
again on every firing, for ever, saying only that it was still queued.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path
import contextlib

#: Four TLS records per read. At satellite speeds the syscall overhead is
#: irrelevant, while stop-condition checks between chunks stay sub-second.
CHUNK = 64 * 1024

#: Bounds how much paid-for data a SIGKILL can lose from the page cache.
FSYNC_EVERY = 16 * 1024 * 1024

#: Stop this long before the runner's hard stop, leaving room to flush, fsync
#: and write the status file without doing any network work.
QUIT_MARGIN = 20

#: Give up if the link is this slow for this long — the satellite equivalent of
#: curl's --speed-limit / --speed-time.
SLOW_BPS = 2 * 1024
SLOW_SECONDS = 60

SOCKET_TIMEOUT = 30
STATUS_EVERY_SECONDS = 5
STATUS_EVERY_BYTES = 8 * 1024 * 1024

#: Hashing is CPU-bound; refuse to start it without time to finish.
HASH_BPS = 100 * 1024 * 1024
HASH_SLACK = 30

COMPLETE, PROGRESS, DECLINED, FATAL = "complete", "progress", "declined", "fatal"

#: What the runner expects back: 0 done, 75 not tonight, anything else a strike.
EXIT = {COMPLETE: 0, PROGRESS: 75, DECLINED: 75, FATAL: 1}

_stop = False


def _on_term(signum, frame) -> None:
    """Only set a flag. Doing I/O here risks being killed mid-write."""
    global _stop
    _stop = True


class Env:
    """The runner's instructions, with defaults that make a manual run safe."""

    def __init__(self) -> None:
        self.work = Path(os.environ.get("EXPIRE_WORK", "."))
        self.out = Path(os.environ.get("EXPIRE_OUT", "."))
        self.slice = int(
            os.environ.get("EXPIRE_SLICE_BYTES")
            or os.environ.get("EXPIRE_BUDGET_BYTES")
            or 0
        )
        self.stop_epoch = float(os.environ.get("EXPIRE_STOP_EPOCH") or 0)
        self.run_id = os.environ.get("EXPIRE_RUN_ID", "manual")
        self.total_hint = int(os.environ.get("EXPIRE_TOTAL_BYTES") or 0)

    def deadline(self) -> float:
        """When to stop, or +inf when run by hand outside the scheduler."""
        return self.stop_epoch - QUIT_MARGIN if self.stop_epoch else float("inf")


def log(message: str) -> None:
    """Item output is captured to the run log by the runner."""
    print(f"{time.strftime('%H:%M:%SZ', time.gmtime())} {message}", flush=True)


def _atomic(path: Path, payload: str) -> None:
    """Write via temp + rename, so a reader never sees a torn file."""
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(payload)
    temp.replace(path)


class Status:
    """The progress report the runner reads back."""

    def __init__(self, env: Env, total: int, part_bytes: int) -> None:
        self.path = env.work / ".status.json"
        self.run_id = env.run_id
        self.total = total
        self.part = part_bytes
        self.slice_bytes = 0
        self.state = PROGRESS
        self._last = 0.0
        self._last_bytes = 0

    def write(self, force: bool = False) -> None:
        now = time.time()
        if not force and (
            now - self._last < STATUS_EVERY_SECONDS
            and self.slice_bytes - self._last_bytes < STATUS_EVERY_BYTES
        ):
            return
        self._last, self._last_bytes = now, self.slice_bytes
        try:
            _atomic(
                self.path,
                json.dumps(
                    {
                        "v": 1,
                        "run_id": self.run_id,
                        "state": self.state,
                        "payload_bytes_this_slice": self.slice_bytes,
                        "part_bytes": self.part,
                        "total_bytes": self.total,
                        "updated": now,
                    }
                ),
            )
        except OSError:
            pass  # Reporting must never be able to fail the download.


def _meta_path(part: Path) -> Path:
    return part.with_suffix(part.suffix + ".meta.json")


def _load_meta(part: Path) -> dict:
    try:
        return json.loads(_meta_path(part).read_text())
    except (OSError, ValueError):
        return {}


def _save_meta(part: Path, meta: dict) -> None:
    with contextlib.suppress(OSError):
        _atomic(_meta_path(part), json.dumps(meta))


def _validators(response) -> tuple[str, str]:
    """ETag and Last-Modified, used to notice the remote file changing."""
    return (
        response.headers.get("ETag") or "",
        response.headers.get("Last-Modified") or "",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _deliver(
    part: Path, dest: Path, expect_sha256: str | None, env: Env, status: Status
) -> str:
    """Verify a complete .part and move it into place."""
    size = part.stat().st_size
    if expect_sha256:
        # Hashing is not interruptible-and-resumable, so refuse to start it
        # without room to finish; the file keeps until the next firing.
        need = size / HASH_BPS + HASH_SLACK
        if time.time() + need > env.deadline():
            log(f"complete but no time to verify ({need:.0f}s needed); next firing")
            status.state = PROGRESS
            status.write(force=True)
            return PROGRESS
        found = _sha256(part)
        if found != expect_sha256:
            # Never silently re-download a whole file: that is exactly the
            # runaway a human should see a strike for.
            log(f"FATAL sha256 mismatch: got {found}, want {expect_sha256}")
            part.unlink(missing_ok=True)
            _meta_path(part).unlink(missing_ok=True)
            status.state = FATAL
            status.write(force=True)
            return FATAL
        log(f"sha256 verified {found}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    # shutil.move, not Path.replace: a rename cannot cross a filesystem, and
    # ``$EXPIRE_OUT`` is not guaranteed to be on the same one as the scratch
    # directory. On the phone, anything under /storage is a different mount
    # from $HOME, and replace() there fails with EXDEV *after* the whole file
    # has been paid for.
    shutil.move(str(part), str(dest))
    _meta_path(part).unlink(missing_ok=True)
    log(f"complete: {dest} ({size:,} bytes)")
    status.state = COMPLETE
    status.part = size
    status.write(force=True)
    return COMPLETE


def fetch(
    url: str, name: str, expect_sha256: str | None = None, total_hint: int = 0
) -> str:
    """Take one slice of *url* into ``$EXPIRE_WORK``, delivering when complete.

    Returns one of :data:`COMPLETE`, :data:`PROGRESS`, :data:`DECLINED`,
    :data:`FATAL`.
    """
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    env = Env()
    env.work.mkdir(parents=True, exist_ok=True)
    part = env.work / f"{name}.part"
    dest = env.out / name
    meta = _load_meta(part)
    total = int(meta.get("total") or total_hint or env.total_hint or 0)

    offset = part.stat().st_size if part.exists() else 0
    status = Status(env, total, offset)

    if total and offset >= total:
        # Last night moved the final bytes but died before verifying.
        return _deliver(part, dest, expect_sha256, env, status)

    budget = env.slice

    # Known not to support Range, and too big for one slice: every attempt
    # would download a slice, discover it cannot resume, throw it away, and
    # start over. That oscillation spends real data every other night and never
    # converges, so refuse once and let a human see the strike.
    if meta.get("no_range") and total and total > budget:
        log(
            f"FATAL: this server ignores Range and the file is {total:,} bytes, "
            f"larger than a {budget:,} byte slice; it cannot be fetched in parts"
        )
        status.state = FATAL
        status.write(force=True)
        return FATAL

    # A slice below one chunk is mostly connection setup and strikes nothing
    # off the queue -- unless it is the whole of what is left, in which case
    # the request finishes the file and retires the item.
    #
    # That exemption is not a nicety. The runner hands a partial item a slice
    # of exactly what it still needs, so *every* file smaller than CHUNK asks
    # for less than CHUNK on its first firing and was refused here having made
    # no request at all. It then exits 75, which the runner reads as "not
    # tonight" and -- because the cap it was given is under a MiB, so no stall
    # is counted -- logs as "made progress but is not finished; left queued".
    # Nothing raises, no strike is taken, no attempt is spent, and the item is
    # offered the same too-small slice again on the next firing for ever. A
    # 15 KiB wheel is not a slow download; it is one that can never happen.
    left = max(0, total - offset) if total else 0
    if budget < CHUNK and not (left and budget >= left):
        short = f"{left:,} still to fetch" if left else "size unknown"
        log(f"slice of {budget} bytes is too small to be worth a request ({short})")
        status.state = DECLINED
        status.write(force=True)
        return DECLINED

    want_end = offset + budget - 1
    headers = {
        # Ranges and byte counting are both meaningless if the server gzips.
        "Accept-Encoding": "identity",
        "Range": f"bytes={offset}-{want_end}",
        "User-Agent": "expire-dl/1",
    }
    # Let the protocol do the same-file check: a compliant server answers 206
    # if unchanged and 200 with the whole body if not. A weak ETag is not
    # allowed in If-Range, so fall back to the date in that case.
    etag, modified = meta.get("etag", ""), meta.get("modified", "")
    if offset and etag and not etag.startswith("W/"):
        headers["If-Range"] = etag
    elif offset and modified:
        headers["If-Range"] = modified

    log(f"requesting bytes {offset}-{want_end} ({budget:,} byte slice)")
    try:
        response = urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=SOCKET_TIMEOUT
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and total and offset >= total:
            return _deliver(part, dest, expect_sha256, env, status)
        log(f"HTTP {exc.code} {exc.reason}")
        status.state = DECLINED
        status.write(force=True)
        return DECLINED
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log(f"cannot reach {url}: {exc}")
        status.state = DECLINED
        status.write(force=True)
        return DECLINED

    with response:
        code = response.getcode()
        new_etag, new_modified = _validators(response)

        if offset and code == 200:
            # Range ignored, or the file changed under us. Appending here would
            # corrupt silently, so start over — but only if that can go
            # anywhere, otherwise say so plainly instead of looping nightly.
            restarts = int(meta.get("restarts") or 0) + 1
            log(f"server returned 200 for a ranged request (restart {restarts})")
            if restarts > 2:
                log(
                    "FATAL: this server will not resume; it cannot be fetched in slices"
                )
                status.state = FATAL
                status.write(force=True)
                return FATAL
            part.unlink(missing_ok=True)
            _save_meta(
                part,
                {
                    "restarts": restarts,
                    "no_range": True,
                    "etag": new_etag,
                    "modified": new_modified,
                },
            )
            status.state = DECLINED
            status.write(force=True)
            return DECLINED

        if offset and code == 206:
            content_range = response.headers.get("Content-Range", "")
            start = -1
            if content_range.startswith("bytes "):
                try:
                    start = int(content_range[6:].split("-")[0])
                except ValueError:
                    start = -1
            if start != offset:
                log(f"FATAL: asked for {offset}, server sent {content_range!r}")
                status.state = FATAL
                status.write(force=True)
                return FATAL
            if etag and new_etag and etag != new_etag:
                log("remote file changed (ETag differs); discarding partial")
                part.unlink(missing_ok=True)
                _save_meta(part, {"etag": new_etag, "modified": new_modified})
                status.state = DECLINED
                status.write(force=True)
                return DECLINED

        # Content-Range carries the true total; it is better than any hint.
        content_range = response.headers.get("Content-Range", "")
        if "/" in content_range:
            tail = content_range.rsplit("/", 1)[1].strip()
            if tail.isdigit():
                total = int(tail)
        elif code == 200:
            length = response.headers.get("Content-Length")
            if length and length.isdigit():
                total = int(length)
        status.total = total

        meta.update(
            {
                "url": url,
                "etag": new_etag,
                "modified": new_modified,
                "total": total,
                "restarts": int(meta.get("restarts") or 0),
            }
        )
        _save_meta(part, meta)

        taken = 0
        since_sync = 0
        started = time.time()
        window_start, window_bytes = started, 0
        reason = "slice complete"

        with part.open("ab") as sink:
            while True:
                if _stop:
                    reason = "asked to stop"
                    break
                if time.time() >= env.deadline():
                    reason = "deadline"
                    break
                if taken >= budget:
                    reason = "slice budget reached"
                    break
                try:
                    block = response.read(min(CHUNK, budget - taken))
                except (OSError, TimeoutError) as exc:
                    reason = f"read failed: {exc}"
                    break
                if not block:
                    reason = "end of slice"
                    break

                sink.write(block)
                taken += len(block)
                since_sync += len(block)
                window_bytes += len(block)
                status.slice_bytes = taken
                status.part = offset + taken

                if since_sync >= FSYNC_EVERY:
                    sink.flush()
                    os.fsync(sink.fileno())
                    since_sync = 0

                now = time.time()
                if now - window_start >= SLOW_SECONDS:
                    if window_bytes / (now - window_start) < SLOW_BPS:
                        reason = "link too slow"
                        break
                    window_start, window_bytes = now, 0

                status.write()

            sink.flush()
            os.fsync(sink.fileno())

    have = part.stat().st_size
    rate = taken / max(1e-6, time.time() - started)
    log(
        f"took {taken:,} bytes ({reason}); {have:,} of "
        f"{total or '?'} at {rate / 1024:,.0f} KiB/s"
    )

    status.part = have
    status.slice_bytes = taken
    if total and have >= total:
        return _deliver(part, dest, expect_sha256, env, status)

    status.state = PROGRESS if taken else DECLINED
    status.write(force=True)
    return status.state


def run(
    url: str, name: str, expect_sha256: str | None = None, total_hint: int = 0
) -> int:
    """:func:`fetch`, mapped to the exit code the runner expects."""
    try:
        return EXIT[fetch(url, name, expect_sha256, total_hint)]
    except Exception as exc:  # noqa: BLE001 - an item must never traceback out
        log(f"unhandled error: {exc!r}")
        return EXIT[FATAL]


if __name__ == "__main__":
    print(__doc__)
    raise SystemExit(0)
