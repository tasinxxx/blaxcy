# BLAXCY — common developer commands.
# The venv must be created with system site packages so GI/AT-SPI resolve:
#   python -m venv --system-site-packages .venv

PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: venv install install-dev run probe session config test test-safety test-recorded coverage lint fmt typecheck check package clean

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

# The opt-in real-display end-to-end suite. It drives the real assembled Body
# against the section 74 fixture, so it needs an X11/DISPLAY and moves the real
# pointer; it is deliberately not part of the default gate.
test-recorded:
	BLAXCY_E2E_REAL_DISPLAY=1 $(PY) -m pytest tests/e2e

# Branch coverage over the shipped source, gated at the floor in pyproject.toml.
coverage:
	$(PY) -m pytest --cov --cov-report=term-missing

lint:
	$(PY) -m ruff check .

fmt:
	$(PY) -m black . && $(PY) -m ruff check --fix .

typecheck:
	$(PY) -m mypy .

# Full gate used after any meaningful change.
check: lint typecheck test

# Build the distribution and prove it installs into a throwaway venv, imports,
# and exposes a working console script (section 87). The venv is created with
# system site packages because the runtime needs the system GI/AT-SPI bindings.
package:
	$(PY) -m pip wheel . -w dist --no-deps
	rm -rf .packaging-check
	python3 -m venv --system-site-packages .packaging-check
	.packaging-check/bin/pip install --no-deps dist/*.whl
	.packaging-check/bin/python -c "import main, installer, control, core, policy, schemas"
	.packaging-check/bin/blaxcy --help > /dev/null
	.packaging-check/bin/pip uninstall -y blaxcy
	rm -rf .packaging-check

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov .packaging-check **/__pycache__
