#!/usr/bin/env python3
"""Claude Code status line for this phone, with the Zwana quota on the left.

Claude Code runs the status line command on every render, so the one hard
requirement is that it must not block. The portal is on the LAN (``ic.zwana.io``
resolves to ``192.168.8.251``) and a live read costs about a second, which is
far too slow to sit in the render path.

That policy now lives in :mod:`quota_widget`, not here. ``quota_widget.current()``
serves a cached reading immediately and starts a detached refresh when it has
gone stale, so this file only has to ask for the number and colour it. Keeping
one cache in the command layer means the widget, this status line and any script
share the same reading and the same refresh, rather than each keeping a private
copy and racing to update it.

The figure shown is ``today.remainder_bytes`` -- everything usable before the
00:00 UTC reset, free grant and paid reserve together. That is the same number
``quota_widget.py --line`` leads with, and it is an upper bound, never a floor
(see the accuracy notes in that module).

Usage::

    statusline.py            # read stdin JSON, print the status line
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import quota_widget as qw  # noqa: E402

#: Past this the number is too old to trust; it is dimmed and marked.
STALE = 300

MIB = 1024 * 1024

RESET = "\033[0m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
CYAN = "\033[36m"

SEP = f"{DIM} · {RESET}"


# --------------------------------------------------------------------------- #
# Quota
# --------------------------------------------------------------------------- #


def quota_field() -> str:
    """The quota, from whatever reading the command layer can serve instantly."""
    try:
        data, _ = qw.current()
    except Exception:
        # Never let a portal problem take the whole status line down with it.
        return f"{DIM}-- MiB{RESET}"

    age = time.time() - data["ts"]
    mib = data["remainder"] / MIB
    colour = GREEN if mib >= 500 else YELLOW if mib >= 150 else RED
    text = f"{mib:,.0f} MiB"
    if age > STALE:
        return f"{DIM}{text} ({age / 60:.0f}m old){RESET}"
    return f"{colour}{text}{RESET}"


# --------------------------------------------------------------------------- #
# The rest of the line
# --------------------------------------------------------------------------- #


def short_dir(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~/" + path[len(home) + 1 :]
    return path


def git_branch(cwd: str) -> str | None:
    """Branch name, or ``None`` outside a repo.

    Kept on a short timeout: a wedged git here would stall every render.
    """
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    branch = out.stdout.strip()
    return branch if out.returncode == 0 and branch else None


def context_field(data: dict) -> str | None:
    window = data.get("context_window") or {}
    left = window.get("remaining_percentage")
    if left is None:
        return None
    colour = RED if left < 15 else YELLOW if left < 30 else DIM
    return f"{colour}{left:.0f}% ctx{RESET}"


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}

    cwd = (
        (data.get("workspace") or {}).get("current_dir")
        or data.get("cwd")
        or os.getcwd()
    )

    parts = [quota_field(), f"{CYAN}{short_dir(cwd)}{RESET}"]

    branch = git_branch(cwd)
    if branch:
        parts.append(f"{DIM}{branch}{RESET}")

    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(f"{DIM}{model}{RESET}")

    ctx = context_field(data)
    if ctx:
        parts.append(ctx)

    print(SEP.join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
