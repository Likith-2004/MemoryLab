#!/usr/bin/env bash
# Builds the sandbox and egress-proxy images. Only creates images tagged memorylab-*:dev and labelled
# memorylab.owner=memorylab; never touches other images on the machine.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"
mkdir -p infra/docker/.generated
uv export --frozen --no-dev --no-emit-workspace --package worker --format requirements-txt \
  -o infra/docker/.generated/requirements-worker.txt >/dev/null
docker build -q -f infra/docker/Dockerfile.worker -t memorylab-worker:dev --label memorylab.owner=memorylab .
docker build -q -f infra/docker/Dockerfile.proxy -t memorylab-egress:dev --label memorylab.owner=memorylab .
docker image ls --filter label=memorylab.owner=memorylab --format '{{.Repository}}:{{.Tag}} {{.Size}}'
