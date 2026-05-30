# Per-ticket gate targets (CLAUDE.md §10). `make check` is the gate that must pass
# before a ticket is presented for review.
PY ?= python

.PHONY: lint type test cov check dryrun

lint:
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

type:
	$(PY) -m mypy

test:
	$(PY) -m pytest

cov:
	$(PY) -m pytest --cov --cov-branch --cov-fail-under=90

check: lint type cov

dryrun:
	$(PY) scripts/run_dryrun.py --config config.yaml
