# BLAXCY — common developer commands.
# The venv must be created with system site packages so GI/AT-SPI resolve:
#   python -m venv --system-site-packages .venv

PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: venv install install-dev run probe session config test test-safety lint fmt typecheck check clean

venv:
	python3 -m venv --system-site-packages .venv

install: venv
	$(PIP) install -r requirements.txt

install-dev: venv
	$(PIP) install -r requirements-dev.txt

run: probe

probe:
	$(PY) main.py probe

session:
	$(PY) main.py session

config:
	$(PY) main.py config

test:
	$(PY) -m pytest

test-safety:
	$(PY) -m pytest tests/safety

lint:
	$(PY) -m ruff check .

fmt:
	$(PY) -m black . && $(PY) -m ruff check --fix .

typecheck:
	$(PY) -m mypy .

# Full gate used after any meaningful change.
check: lint typecheck test

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache **/__pycache__
