# Docker image name and tag
BEETDECK_IMAGE ?= semsemyonoff/beetdeck
BEETDECK_TAG ?= latest
# Target platforms for multi-arch build
BEETDECK_PLATFORMS ?= linux/amd64,linux/arm64

export BEETDECK_IMAGE BEETDECK_TAG BEETDECK_PLATFORMS

PYTHON ?= .venv/bin/python
PYTEST ?= .venv/bin/pytest
RUFF   ?= .venv/bin/ruff

.PHONY: build test lint fmt coverage build-frontend dev-frontend dev

# Build multi-arch image and push to registry
build:
	./build.sh

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

# Build the Vite frontend into src/static/dist/
build-frontend:
	cd frontend && npm ci && npm run build

# Run the Vite dev server (proxies /api and /static to Flask on :5000)
dev-frontend:
	cd frontend && npm run dev

# Run Flask and Vite concurrently for local development
dev:
	$(PYTHON) app.py & FLASK_PID=$$!; trap "kill $$FLASK_PID 2>/dev/null" EXIT; cd frontend && npm run dev
