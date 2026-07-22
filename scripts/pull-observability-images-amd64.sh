#!/usr/bin/env bash
set -euo pipefail

# Pull through a China-accessible mirror and produce one linux/amd64 archive.
# TARGET_REGISTRY can be set to the group Harbor prefix to push the same images.
MIRROR_PREFIX="${MIRROR_PREFIX:-m.daocloud.io/docker.io}"
OUTPUT_DIR="${OUTPUT_DIR:-offline-images}"
TARGET_REGISTRY="${TARGET_REGISTRY:-}"
PLATFORM="linux/amd64"

images=(
  "grafana/loki:3.7.3"
  "grafana/tempo:2.10.5"
  "grafana/grafana:13.0.2"
  "grafana/alloy:v1.16.1"
)

mkdir -p "${OUTPUT_DIR}"
saved=()
for image in "${images[@]}"; do
  source_image="${MIRROR_PREFIX}/${image}"
  docker pull --platform "${PLATFORM}" "${source_image}"
  docker tag "${source_image}" "${image}"
  saved+=("${source_image}" "${image}")
  if [[ -n "${TARGET_REGISTRY}" ]]; then
    target_image="${TARGET_REGISTRY%/}/${image}"
    docker tag "${source_image}" "${target_image}"
    docker push "${target_image}"
    saved+=("${target_image}")
  fi
done

archive="${OUTPUT_DIR}/flawless-observability-linux-amd64-2026-06-30.tar"
docker save -o "${archive}" "${saved[@]}"
sha256sum "${archive}" > "${archive}.sha256"
printf 'Created %s\n' "${archive}"
