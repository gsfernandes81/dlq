#!/bin/bash
# The checks: the pytest suite, one runner, one copy.
#
#     .githooks/checks.sh        run everything
#     make check                 the same thing
#
# The pre-push hook runs this and refuses the push on a failure — a push is a
# deploy here: the phone pulls it and the nightly runner runs what landed.
#
# Offline: no network, no scheduler, no portal. Needs the sibling checkouts
# (ytq and zwana-quota beside this one or under ~), because the modules import
# across them — the same way a real run does. The suite builds its own queue
# root under a temporary directory and points EXPIRE_HOME and HOME at it, so
# nothing here reads or writes the queue this checkout is managing.
#
# The front ends lay out to the terminal and check every line fits it down to
# 32 columns, so these fail on a long checkout path — that is the path, not a
# regression: run them from a shallow clone (~/dlq is what they are built for).
#
# `make mutants` is the suite's own check and is deliberately not here: it
# takes the better part of an hour, and this runs before every push.
set -u

cd "$(git rev-parse --show-toplevel)" || exit 1

if [ -d .venv ] && command -v uv >/dev/null 2>&1; then
    uv run pytest -q
else
    python3 -m pytest -q
fi
