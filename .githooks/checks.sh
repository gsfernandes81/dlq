#!/bin/bash
# The checks: the pytest suite, one runner, one copy.
#
#     .githooks/checks.sh        run everything
#     make check                 the same thing
#
# The pre-push hook runs this and refuses the push on a failure — a push is a
# deploy here: the phone pulls it and the nightly runner runs what landed.
#
# Offline: no network, no scheduler. Needs the sibling checkouts (ytq and
# zwana-quota beside this one or under ~), because the modules import across
# them — the same way a real run does.
#
# The front ends lay out to the terminal and check every line fits it down to
# 32 columns, so these fail on a long checkout path — that is the path, not a
# regression: run them from a shallow clone (~/dlq is what they are built for).
#
# The old per-module self-tests were removed on 2026-09-02 to be rebuilt as a
# pytest suite. Until that suite lands, tests/ is empty and pytest exits 5
# ("no tests collected") — which is reported here as "no tests yet" and passed,
# so the interim commits are pushable. The suite tightens this.
set -u

cd "$(git rev-parse --show-toplevel)" || exit 1

if [ -d .venv ] && command -v uv >/dev/null 2>&1; then
    uv run pytest -q
else
    python3 -m pytest -q
fi
rc=$?

if [ "$rc" -eq 5 ]; then
    echo "checks: no tests yet — nothing was collected."
    exit 0
fi
exit $rc
