# Docker image name and tag
BEETDECK_IMAGE ?= semsemyonoff/beetdeck
BEETDECK_TAG ?= latest
# Target platforms for multi-arch build
BEETDECK_PLATFORMS ?= linux/amd64,linux/arm64

export BEETDECK_IMAGE BEETDECK_TAG BEETDECK_PLATFORMS

PYTHON ?= .venv/bin/python
PIP    ?= .venv/bin/pip
PYTEST ?= .venv/bin/pytest
RUFF   ?= .venv/bin/ruff

# Absolute paths derived from this Makefile's location (safe when invoked from any directory)
_MKFILE_DIR    := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
_WORKSPACE_DIR := $(abspath $(_MKFILE_DIR)../../..)
_FRONTEND_SRC  := $(abspath $(_MKFILE_DIR)../../frontend/src)

.PHONY: install test lint fmt coverage run build sync-frontend-dist

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

# Build multi-arch production image and push to registry.
# The SPA must already be built into src/static/dist/ (see Dockerfile / README).
build:
	./build.sh

# Build the frontend SPA (inside the DWE container) and copy its output here.
# Requires the DWE stack to be running: dwe status / dwe deploy run.
# After this succeeds, src/static/dist/.vite/manifest.json will exist and
# make build / build.sh will proceed without the missing-manifest error.
sync-frontend-dist:
	cd "$(_WORKSPACE_DIR)" && dwe cmd frontend.npm --set args="run build"
	rm -rf "$(_MKFILE_DIR)src/static/dist"
	cp -r "$(_FRONTEND_SRC)/dist" "$(_MKFILE_DIR)src/static/dist"
