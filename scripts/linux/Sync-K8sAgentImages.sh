#!/usr/bin/env bash
set -euo pipefail

platform="amd64"
private_registry=""
include_observability="false"
include_ebpf="false"
include_langfuse="false"
skip_registry_login="false"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/linux/Sync-K8sAgentImages.sh [options]

Options:
  --platform amd64|arm64       Target Kubernetes node architecture (default: amd64)
  --registry HOST[:PORT]       Retag and push images to this private registry
  --include-observability      Include Prometheus, kube-state-metrics, Loki, Tempo, Alloy and Grafana
  --include-ebpf               Include Grafana Beyla
  --include-langfuse           Include optional PostgreSQL and Langfuse images
  --skip-registry-login        Do not run interactive docker login
  -h, --help                   Show this help
EOF
}

while (($#)); do
  case "$1" in
    --platform)
      platform="${2:?missing value for --platform}"
      shift 2
      ;;
    --registry)
      private_registry="${2:?missing value for --registry}"
      shift 2
      ;;
    --include-observability)
      include_observability="true"
      shift
      ;;
    --include-ebpf)
      include_ebpf="true"
      shift
      ;;
    --include-langfuse)
      include_langfuse="true"
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

if [[ "$platform" != "amd64" && "$platform" != "arm64" ]]; then
  echo "--platform must be amd64 or arm64" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 1
fi
docker info >/dev/null

private_registry="${private_registry%/}"
if [[ "$private_registry" == *"://"* ]]; then
  echo "--registry must be HOST[:PORT] without http:// or https://" >&2
  exit 2
fi
if [[ -n "$private_registry" && "$skip_registry_login" != "true" ]]; then
  docker login "$private_registry"
fi

names=(
  "Kubernetes Agent"
  "Approved Node Executor"
)
sources=(
  "m.daocloud.io/ghcr.io/your-org/flawless:3.2.2"
  "m.daocloud.io/ghcr.io/your-org/flawless-node-exec:1.36"
)
targets=(
  "your-org/flawless:3.2.2"
  "your-org/flawless-node-exec:1.36"
)

if [[ "$include_observability" == "true" ]]; then
  names+=("Prometheus" "kube-state-metrics" "Loki" "Tempo" "Alloy" "Grafana")
  sources+=(
    "registry.cn-hangzhou.aliyuncs.com/google_containers/prometheus:v2.45.0"
    "m.daocloud.io/registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.10.1"
    "m.daocloud.io/docker.io/grafana/loki:3.7.3"
    "m.daocloud.io/docker.io/grafana/tempo:2.10.5"
    "m.daocloud.io/docker.io/grafana/alloy:v1.16.1"
    "m.daocloud.io/docker.io/grafana/grafana:13.0.2"
  )
  targets+=(
    "google_containers/prometheus:v2.45.0"
    "kube-state-metrics/kube-state-metrics:v2.10.1"
    "grafana/loki:3.7.3"
    "grafana/tempo:2.10.5"
    "grafana/alloy:v1.16.1"
    "grafana/grafana:13.0.2"
  )
fi

if [[ "$include_ebpf" == "true" ]]; then
  names+=("Beyla eBPF")
  sources+=("m.daocloud.io/docker.io/grafana/beyla:3.24.0")
  targets+=("grafana/beyla:3.24.0")
fi

if [[ "$include_langfuse" == "true" ]]; then
  echo "WARNING: review passwords and pin Langfuse image digests before production use." >&2
  names+=("PostgreSQL for Langfuse" "Langfuse Web" "Langfuse Worker")
  sources+=(
    "m.daocloud.io/docker.io/library/postgres:16-alpine"
    "m.daocloud.io/docker.io/langfuse/langfuse:latest"
    "m.daocloud.io/docker.io/langfuse/langfuse-worker:latest"
  )
  targets+=(
    "k8s-agent-postgres:16-alpine"
    "k8s-agent-langfuse:latest"
    "k8s-agent-langfuse-worker:latest"
  )
fi

echo "[k8s-agent-images] Pull order: application, node executor, then optional dependencies"
for index in "${!sources[@]}"; do
  echo
  echo "[k8s-agent-images] Pulling ${names[$index]}"
  docker pull --platform "linux/$platform" "${sources[$index]}"
  if [[ -n "$private_registry" ]]; then
    destination="$private_registry/${targets[$index]}"
    docker tag "${sources[$index]}" "$destination"
    docker push "$destination"
    echo "[k8s-agent-images] Published: $destination"
  fi
done

echo
if [[ -n "$private_registry" ]]; then
  echo "[k8s-agent-images] All selected images were pushed to $private_registry"
else
  echo "[k8s-agent-images] All selected public images were pulled locally"
fi
