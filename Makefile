PY ?= python

.PHONY: install test lint format dashboards check-dashboards

install:             ## CPU only: load-test client, observability kit, tests
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

dashboards:          ## regenerate the Grafana dashboards from obs/scripts/gen_dashboards.py
	$(PY) obs/scripts/gen_dashboards.py

check-dashboards:    ## fail if the committed dashboards differ from what the generator produces
	$(PY) -m pytest tests/test_obs_config.py -k dashboards
