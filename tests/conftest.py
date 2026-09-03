"""The one seam the suite has: a queue root that is not the real one.

Every module here anchors to a checkout rather than to ``__file__`` — that is
the decision that keeps an installed ``dlq`` managing the real queue — so the
only honest way to test one is to make a checkout somewhere else and run the
modules out of *it*. That is what these fixtures do: this checkout's modules
are copied into a temporary directory, ``EXPIRE_HOME`` and ``HOME`` are pointed
at it, and they are imported from there — so ``expire_runner.ROOT``,
``ytq.HERE`` and every path spelled from them land under it.

The modules are imported under their own names, because that is how they import
each other and how a queue item imports them; what makes that safe is putting
back whatever was in ``sys.modules`` afterwards. It is done **once for the
session**, because the import is the expensive part, and the root is emptied
between tests instead — :func:`dlq` is what empties it, so every test starts on
a queue with nothing in it.

Nothing here writes outside that root and nothing here opens a socket: with no
credentials under the temporary ``HOME`` the portal call fails before it
reaches the network, and every test that needs a reading builds one with
:meth:`Checkout.reading`. An autouse fixture checks the second half of that
afterwards, by looking at the checkout the suite is being run from.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _pty import MODULES, YTQ, ZWANA, make_root  # noqa: E402

from hypothesis import HealthCheck, settings  # noqa: E402

# The function-scoped-fixture health check is off because the properties that
# take the ``dlq`` fixture only reach *pure* functions through it — the
# projection, the admission rule, the layout — so one temporary root per test
# rather than per example changes nothing they can see.
#
# The property tests are turned down inside a mutation run: poodle runs the
# whole suite once per mutant, and a few hundred suites at the everyday sample
# is an overnight job rather than a lunchtime one. Derandomised there, so that
# a mutant is killed — or survives — on the same examples every time it is
# tried, which is what makes a surviving mutant worth reading.
settings.register_profile(
    "dlq",
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
settings.register_profile(
    "mutants",
    max_examples=12,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
settings.load_profile("mutants" if ".poodle-temp" in str(Path.cwd()) else "dlq")


#: Everything a queue root puts into ``sys.modules``, plus the two sibling
#: checkouts it reaches across into. Taken out and put back around each test so
#: a module imported from the temporary root cannot outlive it.
SWAPPED = (
    "dlq",
    "expire_dl",
    "expire_runner",
    "expire_sched",
    "expire_ui",
    "ytdl_item",
    "ytq",
    "quota_widget",
    "zwana_quota",
)

MiB = 1024**2


class Checkout(SimpleNamespace):
    """A temporary queue root, its modules, and the things a test writes into it.

    The attributes are the modules as the root sees them — ``runner``,
    ``sched``, ``ui``, ``dl``, ``queuer`` (``dlq.py``), ``ytq`` and ``qw`` — so
    a test asserts against the same objects the screen under it is running.
    """

    def item(
        self,
        name: str,
        cap: int = 100 * MiB,
        *,
        partial: bool = True,
        desc: str = "",
        dest: str = "",
        body: str = "",
        header: str = "# EXPIRE: v1\n",
        where: str = "queue",
    ) -> Path:
        """Write a queue item. Headers only unless *body* says otherwise."""
        lines = [header, f"# EXPECT_BYTES: {cap}\n"]
        if partial:
            lines.append("# PARTIAL: yes\n")
        if desc:
            lines.append(f"# DESC: {desc}\n")
        if dest:
            lines.append(f"# DEST: {dest}\n")
        path = self.root / where / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(lines) + body, encoding="utf-8")
        return path

    def script(self, name: str, body: str, cap: int = MiB, **rest) -> Path:
        """A queue item that actually runs, under *this* interpreter.

        The shebang is spelled from ``sys.executable`` rather than from the
        contract's Termux path, because the runner checks that an item's
        interpreter is on disk before spawning it — which is the check that
        turns three wasted nights into one line in the log, and which would
        refuse every item in this suite if it were written the other way.
        """
        path = self.item(
            name, cap, header=f"#!{sys.executable}\n# EXPIRE: v1\n", body=body, **rest
        )
        path.chmod(0o755)
        return path

    def state(self, items: dict | None = None, **rest) -> dict:
        """Write ``state.json`` and hand back what was written."""
        state = {"items": items or {}, **rest}
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        return state

    def config(self, raw: str | dict) -> Path:
        """Write ``config.json`` — a mapping, or the exact text of a broken one."""
        path = self.root / "config.json"
        path.write_text(
            raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8"
        )
        return path

    def reading(
        self,
        *,
        free: int = 500 * MiB,
        paid: int = 0,
        grant: int = 763 * MiB,
        age: float = 0.0,
        live: bool = True,
        online: bool = True,
        until: int | None = None,
    ) -> dict:
        """A portal reading with *free* free bytes and *paid* paid ones behind it.

        Built by handing raw portal figures to :func:`quota_widget.derive`, the
        function that builds the real one, rather than by writing the shape of
        its answer out here — a reading this suite invented could keep passing
        after the portal client changed what it hands over.
        """
        doc = self.qw.derive(
            {
                "ts": time.time() - age,
                "credits": 4.0,
                "per_credit": 1_000_000,
                # Paid data is what the pool holds above the grant, and the
                # grant is spent first, so this is a reading with *free* of the
                # grant left and *paid* of paid data behind it.
                "remainder": free + paid,
                "pool": grant + paid,
                "grant": grant,
                "drawn_today": 0,
                "allocated": 0,
                "online": online,
            },
            age,
            live,
        )
        if until is not None:
            doc["reset"]["seconds_until"] = until
        return doc

    def facts(self, **changes) -> dict:
        """A snapshot as :func:`expire_runner.snapshot` shapes one, changed.

        Taken from the runner rather than written out here, with the portal
        call replaced: what a screen is handed has to be the shape the runner
        actually hands it, or a test passes on a listing nothing would draw.
        """
        doc = changes.pop("portal", None)
        problem = "" if doc else "no credentials"
        real = self.runner.portal_now
        self.runner.portal_now = lambda: (doc, problem)
        try:
            facts = self.runner.snapshot(
                force=changes.pop("force", False), blind=changes.pop("blind", False)
            )
        finally:
            self.runner.portal_now = real
        facts.update(changes)
        return facts


@contextlib.contextmanager
def _rooted(root: Path):
    """Import the queue's modules out of *root*, and put sys.modules back."""
    saved = {name: sys.modules.pop(name, None) for name in SWAPPED}
    path = list(sys.path)
    sys.path.insert(0, str(root))
    try:
        import dlq as queuer
        import expire_dl
        import expire_runner
        import expire_sched
        import expire_ui
        import quota_widget
        import ytq

        yield SimpleNamespace(
            runner=expire_runner,
            sched=expire_sched,
            ui=expire_ui,
            dl=expire_dl,
            queuer=queuer,
            ytq=ytq,
            qw=quota_widget,
        )
    finally:
        for name in SWAPPED:
            sys.modules.pop(name, None)
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module
        sys.path[:] = path


#: The environment a temporary queue root is read through. ``HOME`` is the one
#: that keeps the suite offline, and it has to be set before anything is
#: imported: ``quota_widget`` spells its cache path and ``zwana_quota`` its
#: credentials path from ``Path.home()`` at import.
def _environment(root: Path, home: Path) -> dict[str, str]:
    return {
        "EXPIRE_HOME": str(root),
        "YTQ_HOME": str(YTQ),
        "ZWANA_HOME": str(ZWANA),
        "HOME": str(home),
        "COLUMNS": "80",
    }


class Server(http.server.ThreadingHTTPServer):
    """One file, served over the loopback interface, as a real server would.

    Which is where "offline" still means offline: ``expire_dl`` and ``dlq``'s
    probe are the only code here that opens a socket, and the questions they
    answer — does this server honour Range, what does it say the size is, what
    happens when it will not say — are questions about a *server*. A stubbed
    urlopen would be this suite answering them itself.
    """

    daemon_threads = True
    allow_reuse_address = True

    payload = b""
    #: Whether Range is honoured. A server that ignores it answers 200 with the
    #: whole body, which is what the downloader has to notice and refuse.
    honour_range = True
    #: What HEAD answers. Plenty of servers refuse it; the ranged GET settles
    #: both questions when they do.
    head_code = 200
    #: Whether the size is stated at all. Some will not say.
    state_length = True
    state_ranges = True
    #: What GET answers. A server that refuses it after answering HEAD is the
    #: case where half an answer is still an answer.
    get_code = 200
    etag = '"first"'


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: A003 - the suite is not a web log
        pass

    def _asked(self) -> tuple[int, int]:
        payload = self.server.payload
        wanted = self.headers.get("Range", "")
        if not wanted.startswith("bytes=") or not self.server.honour_range:
            return 0, len(payload) - 1
        first, _, last = wanted[6:].partition("-")
        start = int(first)
        end = int(last) if last else len(payload) - 1
        return start, min(end, len(payload) - 1)

    def do_HEAD(self):  # noqa: N802 - BaseHTTPRequestHandler's own spelling
        self.server.state_of_the_world.setdefault("head", []).append(self.path)
        if self.server.head_code != 200:
            self.send_response(self.server.head_code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        if self.server.state_length:
            self.send_header("Content-Length", str(len(self.server.payload)))
        if self.server.state_ranges:
            self.send_header(
                "Accept-Ranges", "bytes" if self.server.honour_range else "none"
            )
        self.end_headers()

    def do_GET(self):  # noqa: N802
        payload = self.server.payload
        self.server.state_of_the_world.setdefault("asked", []).append(
            self.headers.get("Range", "")
        )
        if self.server.get_code != 200:
            self.send_response(self.server.get_code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ranged = "Range" in self.headers and self.server.honour_range
        start, end = self._asked()
        body = payload[start : end + 1]
        self.send_response(206 if ranged else 200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", self.server.etag)
        self.send_header("Accept-Ranges", "bytes")
        if ranged:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def serving():
    """A loopback server whose one file, and whose manners, a test sets."""
    server = Server(("127.0.0.1", 0), Handler)
    #: Every request the server was sent, in order. Per server, so one test
    #: cannot read another test's requests back.
    server.state_of_the_world = {}
    # A short poll so shutting it down at the end of a test is quick: the
    # default half-second, once per test, is most of a file's wall clock.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    host, port = server.server_address[:2]
    server.url = f"http://{host}:{port}/file.bin"
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="session")
def _checkout(tmp_path_factory):
    """One temporary root and one import of the modules, for the whole session.

    Imported once rather than per test because the import is the expensive
    part — this checkout's five modules plus ytq and quota_widget across the
    two sibling checkouts — and every path they spell is a constant derived
    from the root, so a root that does not move can be *emptied* between tests
    instead. :func:`dlq` is what empties it, and what makes each test start on
    a queue with nothing in it.
    """
    where = tmp_path_factory.mktemp("checkout")
    root = make_root(where)
    home = where / "home"
    home.mkdir()

    kept = {name: os.environ.get(name) for name in
            (*_environment(root, home), "zwana_username", "zwana_password")}
    os.environ.update(_environment(root, home))
    for name in ("zwana_username", "zwana_password"):
        os.environ.pop(name, None)
        os.environ.pop(name.upper(), None)
    try:
        with _rooted(root) as modules:
            checkout = Checkout(root=root, home=home, **vars(modules))
            # Every path the modules spelled has to be inside the temporary
            # root, or a test would be reading — or worse, writing — the real
            # queue.
            assert root == checkout.runner.ROOT
            assert root == checkout.sched.ROOT
            assert root == checkout.ytq.HERE
            yield checkout
    finally:
        for name, value in kept.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture
def dlq(_checkout, monkeypatch):
    """A queue root with nothing in it, and this checkout's modules over it.

    The tree is emptied rather than rebuilt: what a test writes into ``queue/``,
    ``work/``, ``state.json`` or ``config.json`` is what the next one would
    read, and every one of them expects to start on an empty queue.
    """
    root = _checkout.root
    # Everything the last test left: the runner's directories, its state and
    # config, a destination it made — and the modules too, since a test may
    # have rewritten one (the shebang check does). Then the root is built
    # again by the same function that built it in the first place.
    for path in root.iterdir():
        if path.name == "__pycache__":
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    make_root(root.parent)
    for name, value in _environment(root, _checkout.home).items():
        monkeypatch.setenv(name, value)
    return _checkout


@pytest.fixture
def pty_root(tmp_path):
    """The same root, for the tests that open the real screen on a terminal."""
    home = tmp_path / "home"
    home.mkdir()
    return SimpleNamespace(root=make_root(tmp_path), home=home)


def pytest_configure(config):
    config.addinivalue_line("markers", "tui: drives the real screen under a pty")


def pytest_report_header(config):
    return (
        f"dlq: {', '.join(MODULES)} run out of a temporary root; "
        "HOME redirected, so no portal"
    )


#: What a tool of its own may leave in the checkout while the suite runs.
DROPPINGS = {"__pycache__", ".pytest_cache", ".hypothesis", ".ruff_cache"}


@pytest.fixture(autouse=True)
def _real_queue_untouched():
    """Nothing in this suite may write the checkout it is being run from.

    Autouse and cheap, and worth having twice over: a stray ``ROOT`` would land
    in this device's ``config.json`` or the runner's ``state.json`` — a test
    that quietly set a real destination or retired a real item would otherwise
    pass — and an item run with no ``EXPIRE_WORK`` writes its progress file
    into whatever the working directory happens to be, which is here.
    """
    real = Path(__file__).resolve().parents[1]
    watched = (real / "config.json", real / "state.json", real / "queue")
    before = {path: path.stat().st_mtime for path in watched if path.exists()}
    listed = {path.name for path in real.iterdir()}
    yield
    for path, when in before.items():
        assert path.stat().st_mtime == when, f"the suite wrote {path}"
    left = {path.name for path in real.iterdir()} - listed - DROPPINGS
    assert not left, f"the suite left {sorted(left)} in the checkout"
