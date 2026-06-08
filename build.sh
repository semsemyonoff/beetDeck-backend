#!/usr/bin/env bash
set -euo pipefail

IMAGE="${BEETDECK_IMAGE:-semsemyonoff/beetdeck}"
TAG="${BEETDECK_TAG:-latest}"
PLATFORMS="${BEETDECK_PLATFORMS:-linux/amd64,linux/arm64}"

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
