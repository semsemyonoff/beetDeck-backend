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

.PHONY: install test lint fmt coverage run build

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
