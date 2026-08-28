# make dev      uv sync: the locked pytest into .venv (and hatchling once,
#               where the repo builds) — a few hundred KB, then cached
# make test     the self-tests through pytest
# make check    the same self-tests through the push gate's own runner —
#               what .githooks/pre-push runs, needing only bash + python3;
#               it knows nothing about uv on purpose, because it runs
#               wherever the push happens
# make lint     ruff — the one already on PATH if there is one (Termux ships
#               a native build; uv cannot install ruff on Android), else the
#               locked one from the lint group

dev:
	uv sync

test:
	uv run pytest -q

check:
	bash .githooks/checks.sh

lint:
	@if command -v ruff >/dev/null 2>&1; then ruff check .; \
	else uv run --group lint ruff check .; fi

.PHONY: dev test check lint
