#!/usr/bin/env python3
"""Report how much internet quota is left on the Zwana crew portal.

The portal at https://ic.zwana.io is an AngularJS single-page app talking to a
plain REST API, so the numbers behind the dashboard can be read directly:

* ``POST api/account/token`` -- log in. Two surprises here, both worth stating
  because they make the obvious implementation fail:

  1. It looks like an OAuth2 *password* grant, but the body is **JSON**, not
     form-encoded -- the Angular client posts a JS object with
     ``Content-Type: application/json``.
  2. The response's ``token_type`` is literally ``"password"`` (it echoes the
     grant type), and the ``access_token`` it returns is **not** what
     authorises later calls. Authentication is really the
     ``.AspNetCore.Identity.Application`` **cookie** delivered in ``Set-Cookie``.
     Sending ``Authorization: <token_type> <access_token>``, which is what the
     browser client appears to do, gets a 302 to the login page.

* ``GET  api/Balance/GetForCurrentUser`` -- what is left.
* ``GET  api/UserProvider/GetStatus`` -- whether a session is currently up.

Only the standard library is used, so this runs on a bare Termux install with
nothing to install and nothing to download.

Credentials
-----------
Read from a ``.env`` file (``~/or3/.env`` by default), which should be mode 600::

    zwana_username=...
    zwana_password=...

The password is never printed, never logged, and never placed on a command line
where it could show up in ``ps`` output. Environment variables of the same names
override the file, so a caller may supply them another way.

Session caching
---------------
The session cookie is cached under ``~/.cache/zwana/`` (mode 600) and reused
until it expires, so a repeat check costs one request instead of two and the
login endpoint is not hammered. If a cached cookie turns out to be stale the
request is retried once after logging in again. :option:`--login` forces it.

Usage
-----
::

    python zwana_quota.py              # one-line summary
    python zwana_quota.py --verbose    # summary plus session status
    python zwana_quota.py --json       # raw API payloads, for scripting
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "https://ic.zwana.io/api/"

#: The portal's own web front end: the same host, without the API path. Derived
#: rather than spelled again, so the address exists once in this repo — a second
#: copy is a thing that can disagree with the first, and the one that opens in a
#: browser would be the copy nobody tests.
PORTAL_URL = BASE_URL[: -len("api/")]
DEFAULT_ENV = Path.home() / "or3" / ".env"
CACHE_DIR = Path.home() / ".cache" / "zwana"
COOKIE_FILE = CACHE_DIR / "cookies.txt"

#: The cookie that actually carries the session.
SESSION_COOKIE = ".AspNetCore.Identity.Application"

#: ``Balance`` is denominated in *credits*, not bytes. The conversion was
#: established from the allocation history, where a ``-0.1`` credit entry
#: corresponds to an ``Allocation`` of 41,943,040 bytes (40 MiB), and confirmed
#: against the provider's ``UnitCost`` of 2.38418579102e-09 credits per byte:
#: ``1 / UnitCost == 419430400``. So one credit is exactly 400 MiB.
BYTES_PER_CREDIT = 419_430_400

TIMEOUT_SECONDS = 30


class PortalError(RuntimeError):
    """Any failure talking to the portal, already stripped of secrets."""


class NotAuthenticated(PortalError):
    """The portal redirected to the login page instead of answering."""


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def warn_if_world_readable(path: Path) -> None:
    """Complain on stderr if a secrets file is readable by anyone else."""
    if path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH):
        print(f"warning: {path} is readable by others; chmod 600 it", file=sys.stderr)


def load_credentials(env_path: Path) -> tuple[str, str]:
    """Return ``(username, password)`` from the environment or *env_path*.

    Real environment variables win, so a caller can inject credentials without
    touching the file at all.
    """
    values: dict[str, str] = {}
    if env_path.is_file():
        warn_if_world_readable(env_path)
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip().lower()] = value.strip().strip("'\"")

    username = os.environ.get("zwana_username") or values.get("zwana_username", "")
    password = os.environ.get("zwana_password") or values.get("zwana_password", "")
    if not username or not password:
        raise PortalError(
            f"no credentials: set zwana_username and zwana_password in {env_path} "
            "(or export them)"
        )
    return username, password


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects.

    The API answers an unauthenticated request with ``302`` to the login page
    rather than ``401``. Followed blindly that yields the HTML shell and a
    confusing JSON parse error, so a redirect is surfaced as
    :class:`NotAuthenticated` instead.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_jar = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
_opener = urllib.request.build_opener(
    NoRedirect, urllib.request.HTTPCookieProcessor(_jar)
)


def request(path: str, payload: Any = None) -> Any:
    """Call the API and return the decoded JSON body.

    ``GET`` unless *payload* is given, in which case it is sent as a JSON body.
    Cookies are carried automatically by the shared opener.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers)
    try:
        with _opener.open(req, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            raise NotAuthenticated(
                f"{path}: session not valid (HTTP {exc.code})"
            ) from None
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        # Never echo the request body back: it may hold the password.
        raise PortalError(f"{path}: HTTP {exc.code} {exc.reason} {detail}") from None
    except urllib.error.URLError as exc:
        raise PortalError(f"{path}: cannot reach portal ({exc.reason})") from None

    if not body.strip():
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise PortalError(f"{path}: expected JSON, got {body[:120]!r}") from None


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


def load_session() -> bool:
    """Load cached cookies. Returns whether a session cookie is present."""
    try:
        _jar.load(ignore_discard=True, ignore_expires=False)
    except (OSError, http.cookiejar.LoadError):
        return False
    return any(cookie.name == SESSION_COOKIE for cookie in _jar)


def save_session() -> None:
    """Persist cookies, readable only by this user."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.chmod(0o700)
    # Create the file with restrictive permissions *before* the jar writes to
    # it: MozillaCookieJar.save() opens with the process umask, which would
    # otherwise leave a window where the session cookie is world-readable.
    os.close(os.open(COOKIE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    _jar.save(ignore_discard=True, ignore_expires=False)


def log_in(env_path: Path) -> None:
    """Authenticate and persist the resulting session cookie."""
    username, password = load_credentials(env_path)
    _jar.clear()
    request(
        "account/token",
        payload={"grant_type": "password", "username": username, "password": password},
    )
    if not any(cookie.name == SESSION_COOKIE for cookie in _jar):
        raise PortalError("login returned no session cookie; credentials rejected?")
    save_session()


def fetch(path: str, env_path: Path, allow_login: bool = True) -> Any:
    """GET *path*, logging in once if the cached session has gone stale."""
    try:
        return request(path)
    except NotAuthenticated:
        if not allow_login:
            raise
        log_in(env_path)
        return request(path)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def human_bytes(value: float) -> str:
    """Format a byte count the way the portal's own unit filter does."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:,.0f} B" if unit == "B" else f"{value:,.2f} {unit}"
        value /= 1024
    return f"{value:,.2f} TiB"


def bytes_per_credit(env_path: Path) -> int:
    """Return how many bytes one credit buys.

    Read from the provider's own ``UnitCost`` so the figure follows any change
    of plan, falling back to :data:`BYTES_PER_CREDIT` if the history is empty or
    the field is missing. Only byte-metered providers are meaningful here;
    ``IsByteType`` false would mean the allowance is measured in seconds.
    """
    try:
        history = fetch("Allocation/GetHistoryForCurrentUser", env_path)
    except PortalError:
        return BYTES_PER_CREDIT
    if isinstance(history, list):
        for entry in history:
            provider = (entry.get("UserProvider") or {}).get("Provider") or {}
            unit_cost = provider.get("UnitCost")
            if (
                provider.get("IsByteType")
                and isinstance(unit_cost, (int, float))
                and unit_cost > 0
            ):
                return round(1 / unit_cost)
    return BYTES_PER_CREDIT


def available_bytes(env_path: Path = DEFAULT_ENV) -> tuple[float, int]:
    """Return ``(credits remaining, bytes remaining)``.

    The single entry point other tools should use to gate a download on quota.
    Logs in if there is no usable cached session.
    """
    if not load_session():
        log_in(env_path)
    balance = fetch("Balance/GetForCurrentUser", env_path)
    if not isinstance(balance, dict) or not isinstance(
        balance.get("Balance"), (int, float)
    ):
        raise PortalError("balance response has no numeric Balance field")
    credits = float(balance["Balance"])
    return credits, int(credits * bytes_per_credit(env_path))


def summarise(balance: Any, status: Any, verbose: bool, per_credit: int) -> None:
    """Print a short human-readable report."""
    if not isinstance(balance, dict) or not isinstance(
        balance.get("Balance"), (int, float)
    ):
        print("Could not read a Balance field. Raw payload:")
        print(json.dumps(balance, indent=2, sort_keys=True))
    else:
        credits = float(balance["Balance"])
        print(f"Remaining: {credits:g} credits  =  {human_bytes(credits * per_credit)}")
        print(f"           (1 credit = {human_bytes(per_credit)})")
        if balance.get("Online") is not None:
            state = "online" if balance.get("Online") else "offline"
            print(f"Session  : {state}, IP {balance.get('IP', '?')}")

    if verbose:
        print("\nFull balance payload:")
        print(json.dumps(balance, indent=2, sort_keys=True))
        if status is not None:
            print("\nConnection status:")
            print(json.dumps(status, indent=2, sort_keys=True))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the CLI and parse *argv* (defaults to ``sys.argv[1:]``)."""
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=DEFAULT_ENV,
        help="file holding zwana_username / zwana_password (default: %(default)s)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the raw API payloads instead of a summary",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="also show the full payload and connection status",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="ignore the cached session and log in again",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a shell exit code: non-zero on failure."""
    args = parse_args(argv)
    try:
        if args.login or not load_session():
            log_in(args.env)
        balance = fetch("Balance/GetForCurrentUser", args.env)
        per_credit = bytes_per_credit(args.env)
        status = (
            fetch("UserProvider/GetStatus", args.env)
            if (args.verbose or args.json)
            else None
        )
    except PortalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        credits = balance.get("Balance") if isinstance(balance, dict) else None
        print(
            json.dumps(
                {
                    "balance": balance,
                    "status": status,
                    "bytes_per_credit": per_credit,
                    "remaining_bytes": int(credits * per_credit)
                    if isinstance(credits, (int, float))
                    else None,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        summarise(balance, status, args.verbose, per_credit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
