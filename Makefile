# make test     the self-tests, through pytest (one item per module)
# make check    the same self-tests, through the push gate's own runner —
#               what .githooks/pre-push runs, needing only bash + python3
# make lint     ruff, where it is installed (not part of the push gate:
#               the gate runs on the phone, which does not carry ruff)

test:
	python3 -m pytest -q

check:
	bash .githooks/checks.sh

lint:
	ruff check .

.PHONY: test check lint
