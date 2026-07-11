#!/usr/bin/env bash
# Build the quality-loop image. If using minikube, build into its docker daemon:
#   eval "$(minikube docker-env)"; ./build-image.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG="${1:-glm-loop-quality:latest}"
echo "==> building $TAG"
docker build -t "$TAG" "$DIR"
echo "==> done: $TAG"
