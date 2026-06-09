#!/usr/bin/env bash
set -euo pipefail

IMAGE="${BEETDECK_IMAGE:-semsemyonoff/beetdeck}"
TAG="${BEETDECK_TAG:-latest}"
PLATFORMS="${BEETDECK_PLATFORMS:-linux/amd64,linux/arm64}"

MANIFEST="src/static/dist/.vite/manifest.json"
if [ ! -f "$MANIFEST" ]; then
    echo "ERROR: Frontend assets not built — $MANIFEST is missing." >&2
    echo "Run 'make sync-frontend-dist' to build the SPA and copy it here, then retry." >&2
    exit 1
fi

BUILDER="beetDeck-multiarch"
if ! docker buildx inspect "$BUILDER" &>/dev/null; then
    docker buildx create --name "$BUILDER" --use
else
    docker buildx use "$BUILDER"
fi

docker buildx build \
    --platform "$PLATFORMS" \
    --tag "${IMAGE}:${TAG}" \
    --push .
