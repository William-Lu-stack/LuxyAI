#!/usr/bin/env bash
set -euo pipefail

private_registry="registry.example.com"
image_namespace="flawless"
core_only="false"
skip_registry_login="false"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/linux/Push-LocalImages.sh [options]

Options:
  --registry HOST[:PORT]   Registry prefix already used by the local image tags
  --image-namespace PATH   Repository namespace after registry (default: flawless)
  --core-only              Push only the application and node executor
  --skip-registry-login    Do not run interactive docker login
  -h, --help               Show this help

This script never pulls images. It requires existing Linux AMD64 images tagged
under the selected private registry and image namespace.
EOF
}

while (($#)); do
  case "$1" in
    --registry)
      private_registry="${2:?missing value for --registry}"
      shift 2
      ;;
    --image-namespace)
      image_namespace="${2:?missing value for --image-namespace}"
      shift 2
      ;;
    --core-only)
      core_only="true"
      shift
      ;;
    --skip-registry-login)
      skip_registry_login="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

private_registry="${private_registry%/}"
if [[ ! "$private_registry" =~ ^[A-Za-z0-9._:-]+$ ]]; then
  echo "--registry must be HOST[:PORT] without a URL scheme or path" >&2
  exit 2
fi
if [[ ! "$image_namespace" =~ ^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]]; then
  echo "--image-namespace must be a relative registry path such as platform or team/platform" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 1
fi
docker info >/dev/null

image_names=(
  "$image_namespace/flawless:3.2.2"
  "$image_namespace/flawless-node-exec:1.36"
)
if [[ "$core_only" != "true" ]]; then
  image_names+=(
    "google_containers/prometheus:v2.45.0"
    "kube-state-metrics/kube-state-metrics:v2.10.1"
    "grafana/loki:3.7.3"
    "grafana/tempo:2.10.5"
    "grafana/alloy:v1.16.1"
    "grafana/grafana:13.0.2"
    "grafana/beyla:3.24.0"
  )
fi

missing_images=()
for image_name in "${image_names[@]}"; do
  image_reference="$private_registry/$image_name"
  if ! docker image inspect "$image_reference" >/dev/null 2>&1; then
    missing_images+=("$image_reference")
    continue
  fi
  architecture="$(docker image inspect --format '{{.Architecture}}' "$image_reference")"
  if [[ "$architecture" != "amd64" ]]; then
    echo "$image_reference has architecture $architecture; linux/amd64 is required" >&2
    exit 1
  fi
done

if ((${#missing_images[@]})); then
  echo "The following required local image tags are missing:" >&2
  printf '  %s\n' "${missing_images[@]}" >&2
  echo "Retag the existing images exactly as listed, then rerun this command." >&2
  exit 1
fi

if [[ "$skip_registry_login" != "true" ]]; then
  docker login "$private_registry"
fi
for image_name in "${image_names[@]}"; do
  image_reference="$private_registry/$image_name"
  echo "[k8s-agent-offline] Pushing $image_reference"
  docker push "$image_reference"
done

echo
echo "[k8s-agent-offline] All selected local images were pushed without public pulls"
