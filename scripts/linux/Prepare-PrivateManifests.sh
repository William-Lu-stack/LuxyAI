#!/usr/bin/env bash
set -euo pipefail

private_registry="registry.example.com"
image_namespace="flawless"
output_directory=""

usage() {
  cat <<'EOF'
Usage:
  ./scripts/linux/Prepare-PrivateManifests.sh [options]

Options:
  --registry HOST[:PORT]   Private registry (default: registry.example.com)
  --image-namespace PATH   Repository namespace after registry (default: flawless)
  --output DIRECTORY       Output directory (default: generated-private-manifests)
  -h, --help               Show this help
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
    --output)
      output_directory="${2:?missing value for --output}"
      shift 2
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

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd -- "$script_dir/../.." && pwd)"
if [[ -z "$output_directory" ]]; then
  output_directory="$repository_root/generated-private-manifests"
fi
mkdir -p -- "$output_directory"

rewrite_manifest() {
  local source_path="$1"
  local target_path="$2"
  sed \
    -e "s|m.daocloud.io/ghcr.io/your-org/flawless:3.2.2|$private_registry/$image_namespace/flawless:3.2.2|g" \
    -e "s|m.daocloud.io/ghcr.io/your-org/flawless-node-exec:1.36|$private_registry/$image_namespace/flawless-node-exec:1.36|g" \
    -e "s|registry.cn-hangzhou.aliyuncs.com/google_containers/prometheus:v2.45.0|$private_registry/google_containers/prometheus:v2.45.0|g" \
    -e "s|m.daocloud.io/registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.10.1|$private_registry/kube-state-metrics/kube-state-metrics:v2.10.1|g" \
    -e "s|m.daocloud.io/docker.io/grafana/loki:3.7.3|$private_registry/grafana/loki:3.7.3|g" \
    -e "s|m.daocloud.io/docker.io/grafana/tempo:2.10.5|$private_registry/grafana/tempo:2.10.5|g" \
    -e "s|m.daocloud.io/docker.io/grafana/alloy:v1.16.1|$private_registry/grafana/alloy:v1.16.1|g" \
    -e "s|m.daocloud.io/docker.io/grafana/grafana:13.0.2|$private_registry/grafana/grafana:13.0.2|g" \
    -e "s|m.daocloud.io/docker.io/grafana/beyla:3.24.0|$private_registry/grafana/beyla:3.24.0|g" \
    "$source_path" >"$target_path"
  echo "[k8s-agent-manifests] Generated: $target_path"
}

rewrite_manifest \
  "$repository_root/manifests/observability-stack.yaml" \
  "$output_directory/30-observability-stack.yaml"
rewrite_manifest \
  "$repository_root/manifests/grafana-observability.yaml" \
  "$output_directory/40-grafana-observability.yaml"
rewrite_manifest \
  "$repository_root/manifests/ebpf-beyla.yaml" \
  "$output_directory/50-ebpf-beyla.yaml"

echo
echo "Apply only the components you need, in numeric order:"
echo "  kubectl apply -f $output_directory/30-observability-stack.yaml"
echo "  kubectl apply -f $output_directory/40-grafana-observability.yaml"
echo "  kubectl apply -f $output_directory/50-ebpf-beyla.yaml"
