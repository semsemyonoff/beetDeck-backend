# beetDeck backend — application dev tasks only.
#
# Building and publishing the production image lives in the separate
# beetDeck/deploy repository (this backend is pinned there as a submodule).
# This Makefile covers local development against the source.

PYTHON ?= .venv/bin/python
PIP    ?= .venv/bin/pip
PYTEST ?= .venv/bin/pytest
RUFF   ?= .venv/bin/ruff

.PHONY: install test lint fmt coverage run

# Install runtime + dev dependencies (declared in pyproject.toml) into the venv
install:
	$(PIP) install -e ".[dev]"

# Run test suite
test:
	$(PYTEST)

# Run linter
lint:
	$(RUFF) check .

# Format code
fmt:
	$(RUFF) format .

# Run tests with coverage report
coverage:
	$(PYTEST) --cov=src --cov-report=term-missing

# Run the Flask dev server on :5000
run:
	$(PYTHON) app.py
