#!/usr/bin/env bash
# Builds the sandbox, egress-proxy and (with --all) the service and web images. Only creates images tagged memorylab-*:dev and labelled
# memorylab.owner=memorylab; never touches other images on the machine.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"
mkdir -p infra/docker/.generated
uv export --frozen --no-dev --no-emit-workspace --package worker --format requirements-txt \
  -o infra/docker/.generated/requirements-worker.txt >/dev/null
# memorylab.src is the hash of the worker's sources; test_docker fails when the image's label differs from the tree.
src_hash="$(python3 scripts/image_src_hash.py)"
docker build -q -f infra/docker/Dockerfile.worker -t memorylab-worker:dev --label memorylab.owner=memorylab --label "memorylab.src=$src_hash" .
docker build -q -f infra/docker/Dockerfile.proxy -t memorylab-egress:dev --label memorylab.owner=memorylab .
docker image ls --filter label=memorylab.owner=memorylab --format '{{.Repository}}:{{.Tag}} {{.Size}}'

[ "${1:-}" = "--all" ] || exit 0
# Service images for infra/compose.yaml: name, workspace package, local packages to install (in dependency order), target.
build_service() {
  local name="$1" pkg="$2" pkgs="$3" target="$4"
  uv export --frozen --no-dev --no-emit-workspace --package "$pkg" --format requirements-txt \
    -o "infra/docker/.generated/requirements-$pkg.txt" >/dev/null
  docker build -q -f infra/docker/Dockerfile.service --target "$target" --build-arg "SERVICE=$pkg" --build-arg "PKGS=$pkgs" \
    -t "memorylab-$name:dev" --label memorylab.owner=memorylab .
}
build_service ingest ingest "packages/runlog packages/labharness services/ingest" plain
build_service api api "packages/runlog packages/labharness services/ingest services/api" plain
build_service accounts accounts "packages/runlog packages/labharness services/ingest services/accounts" plain
build_service orchestrator orchestrator "packages/runlog packages/labharness services/ingest services/orchestrator" with-docker
# The reaper is the orchestrator package's second process (python -m orchestrator.reaper) without the Docker CLI.
build_service reaper orchestrator "packages/runlog packages/labharness services/ingest services/orchestrator" plain
docker build -q -f infra/docker/Dockerfile.web -t memorylab-web:dev --label memorylab.owner=memorylab apps/web
docker image ls --filter label=memorylab.owner=memorylab --format '{{.Repository}}:{{.Tag}} {{.Size}}'
