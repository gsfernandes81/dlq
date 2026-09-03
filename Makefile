# make dev      uv sync: the locked pytest, pyte and hypothesis into .venv
#               (and hatchling once, where the repo builds) — a few hundred
#               KB, then cached
# make test     the pytest suite
# make check    the same thing — .githooks/checks.sh is the one runner, so
#               what a push gets checked with is what you just ran; it uses
#               .venv through uv when there is one and plain python3
#               otherwise, because it also runs wherever the push happens
# make mutants  poodle: change one operator at a time and see whether the
#               suite notices. Deliberately NOT part of test/check — it takes
#               the better part of an hour, and a surviving mutant is a
#               question rather than a failure. It is a ratchet: raise
#               --fail_under when the score goes up.
# make lint     ruff — the one already on PATH if there is one (Termux ships
#               a native build; uv cannot install ruff on Android), else the
#               locked one from the lint group

dev:
	uv sync

test: check

check:
	bash .githooks/checks.sh

# --fail_under is the ratchet's current notch, deliberately under the score
# rather than at it: raise it when a run comes back higher, never to make a run
# pass. A scoped run — one module against the tests that reach it — is
# POODLE_TESTS=... poodle --only <module>, and is the way to get an answer
# rather than a number (see poodle_config.py).
mutants:
	uv run --group mutants poodle --fail_under 50 --json .poodle-report.json

lint:
	@if command -v ruff >/dev/null 2>&1; then ruff check .; \
	else uv run --group lint ruff check .; fi

.PHONY: dev test check mutants lint
