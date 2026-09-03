"""How ``make mutants`` runs poodle over this repo.

Mutation testing is the suite's own check. poodle changes one operator, literal
or comparison at a time and runs the tests against it; a mutant that *survives*
is a line no test was actually asserting anything about — which is the question
coverage cannot answer, since coverage says only that a line ran.

It is a **ratchet and never a gate**. Some mutants are equivalent — the same
behaviour spelled differently — and can never be killed, and a large share of
the rest are a message reworded, which this suite declines to pin on purpose:
what a refusal *says* is free to improve, and a test that fails when it does is
a test that has to be rewritten to say the same thing again. So ``--fail_under``
is set under the score already reached rather than at 100, and it goes up when
the score does. ``make mutants`` is deliberately outside ``make test`` and ``make
check``: a push here is a deploy, and the pre-push hook has to stay quick.

Five things here are load-bearing:

* ``source_folders = ["."]``. The modules sit at the repo root, not under
  ``src/``, which is poodle's default and would find nothing to mutate.
* ``only_files``. The five modules this repo owns. ``ytdl_item.py`` is the shim
  pre-split items import and belongs to ytq's suite.
* ``YTQ_HOME`` and ``ZWANA_HOME``. poodle copies the tree into
  ``.poodle-temp/`` and runs the suite from there, where there is no sibling
  checkout beside it and ``~`` is not where these are. Set from this file's own
  location, which is the real one.
* ``file_copy_filters``. That copy is made per worker; without this it drags in
  ``.venv``, ``.git``, the ``.hypothesis`` cache and — worse — the live
  ``queue/``, ``work/`` and ``state.json``, which is the runtime state of the
  real queue being handed to the run it is meant to be hidden from.
* ``runner_opts.command_line``. Spelled with this interpreter rather than a
  bare ``python``, which in a worker's copy is whatever is first on PATH and on
  this machine has no pytest in it.

The curses event loops and the two that wait on a thread are fenced in
``expire_ui.py`` with ``# nomut: start`` / ``# nomut: end``. A mutant inside one
does not fail a test: it hangs a terminal until the timeout below kills it, and
a whole suite's timeout per mutant is what turns an afternoon into a week. What
those loops do is checked under a pty instead (``tests/test_screens.py`` and
``tests/test_settings_screen.py``), which is also why the mutation run skips
those tests — they are most of the suite's wall clock and none of the code they
drive is mutated.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

source_folders = ["."]

#: The five modules this repo owns.
only_files = [
    "dlq.py",
    "expire_dl.py",
    "expire_runner.py",
    "expire_sched.py",
    "expire_ui.py",
]

file_filters = ["test_*.py", "*_test.py", "conftest.py", "poodle_config.py"]

file_copy_filters = [
    "__pycache__/**",
    "*.pyc",
    ".git/**",
    ".venv/**",
    ".pytest_cache/**",
    ".ruff_cache/**",
    ".hypothesis/**",
    ".poodle-temp/**",
    # The runtime state of the real queue. A worker's copy must not hold it:
    # the suite builds its own queue root under a temporary directory, and this
    # device's config or state read by one of those runs is the one thing every
    # test here is arranged to prevent. ``queue/README.md`` is deliberately not
    # excluded — it is the item contract, it is what ``root_problem`` proves a
    # queue root by, and the suite copies it into the root it builds.
    "queue/*.py",
    "queue/.staging/**",
    "work/**",
    "out/**",
    "done/**",
    "failed/**",
    "logs/**",
    "state.json",
    "config.json",
    "heartbeat",
    "runner.lock",
    "*.log",
]

#: The suite is a few seconds; a mutant needs room to be slow rather than to be
#: reported as a timeout, and a phone under thermal throttling is slower again.
min_timeout = 120
timeout_multiplier = 10

max_workers = 4

#: Which tests each mutant is put to. The whole suite by default, minus the pty
#: tests — the code those drive is fenced out of mutation anyway, and they are
#: most of its wall clock.
#:
#: ``POODLE_TESTS`` narrows it, because the run poodle does by default is the
#: ratchet and not the way to get an *answer* about one module: the command is
#: run once per mutant, so a scoped run — one module, against the files that
#: reach it — is the same mutants and the same verdicts in a fraction of the
#: time. It pairs with ``--only``::
#:
#:     POODLE_TESTS="tests/test_gate.py tests/test_tonight_budget.py" \
#:         uv run --group mutants poodle --only expire_runner.py
TESTS = os.environ.get("POODLE_TESTS", "-m 'not tui'")

#: ``-x`` because a mutant only has to be caught once, and the suite is run
#: once per mutant. The conftest notices it is running inside ``.poodle-temp``
#: and turns the property tests down to a smaller, derandomised sample for the
#: same reason — a mutation run is thousands of suites, not one.
runner_opts = {
    "command_line": (
        f"{sys.executable} -m pytest -x -q -p no:cacheprovider {TESTS}"
    ),
    # The two sibling checkouts, named outright. A worker runs the suite from
    # inside ``.poodle-temp``, where there is no clone beside it and ``~`` is
    # not where these live, so the "found, not configured" answer every module
    # here gives has nothing to find.
    "command_line_env": {
        "YTQ_HOME": str(HERE.parent / "ytq"),
        "ZWANA_HOME": str(HERE.parent / "zwana-quota"),
    },
}
