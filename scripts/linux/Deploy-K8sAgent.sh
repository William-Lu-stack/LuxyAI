#!/usr/bin/env bash
set -euo pipefail

namespace="k8s-agent"
node_exec_namespace="flawless-node-exec"
release_name="k8s-agent"
storage_class="rwx-storage-class"
image_mode="public-cn"
private_registry="registry.example.com"
image_namespace="flawless"
image_pull_secret=""
values_file=""
reuse_oauth_credentials="false"
adopt_existing_resources="false"
skip_node_executor="false"
skip_rancher_connectivity_check="false"
force="false"
rancher_url=""

usage() {
  cat <<'EOF'
Usage:
  ./scripts/linux/Deploy-K8sAgent.sh [options]

Options:
  --storage-class NAME             RWX StorageClass (default: rwx-storage-class)
  --rancher-url URL                Target Rancher HTTPS URL (required)
  --values FILE                    Private production Helm values (no credentials)
  --image-mode public-cn|private   Image source (default: public-cn)
  --registry HOST[:PORT]           Private registry (default: registry.example.com)
  --image-namespace PATH           Repository namespace after registry (default: flawless)
  --image-pull-secret NAME         Existing registry Secret used by Pods
  --reuse-oauth-credentials        Reuse OAUTH_CLIENT_ID/SECRET from k8s-agent-oauth
  --adopt-existing-resources       Back up and let Helm adopt an existing ConfigMap/ServiceAccount
  --skip-node-executor             Do not apply manifests/node-executor.yaml
  --skip-rancher-connectivity-check
  --force                          Skip the DEPLOY confirmation prompt
  -h, --help                       Show this help

The script always prompts securely for the new Rancher bearer token. Unless
--reuse-oauth-credentials is used, it also prompts for the OAuth client ID and
client secret used by the internal LLM gateway.
EOF
}

while (($#)); do
  case "$1" in
    --storage-class)
      storage_class="${2:?missing value for --storage-class}"
      shift 2
      ;;
    --rancher-url)
      rancher_url="${2:?missing value for --rancher-url}"
      shift 2
      ;;
    --values)
      values_file="${2:?missing value for --values}"
      shift 2
      ;;
    --image-mode)
      image_mode="${2:?missing value for --image-mode}"
      shift 2
      ;;
    --registry)
      private_registry="${2:?missing value for --registry}"
      shift 2
      ;;
    --image-namespace)
      image_namespace="${2:?missing value for --image-namespace}"
      shift 2
      ;;
    --image-pull-secret)
      image_pull_secret="${2:?missing value for --image-pull-secret}"
      shift 2
      ;;
    --reuse-oauth-credentials)
      reuse_oauth_credentials="true"
      shift
      ;;
    --adopt-existing-resources)
      adopt_existing_resources="true"
      shift
      ;;
    --skip-node-executor)
      skip_node_executor="true"
      shift
      ;;
    --skip-rancher-connectivity-check)
      skip_rancher_connectivity_check="true"
      shift
      ;;
    --force)
      force="true"
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

if [[ "$image_mode" != "public-cn" && "$image_mode" != "private" ]]; then
  echo "--image-mode must be public-cn or private" >&2
  exit 2
fi
rancher_url="${rancher_url%/}"
if [[ -z "$rancher_url" ]]; then
  echo "--rancher-url is required" >&2
  exit 2
fi
if [[ ! "$rancher_url" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[^[:space:]]*)?$ ]]; then
  echo "--rancher-url must be an HTTPS URL without embedded credentials" >&2
  exit 2
fi
private_registry="${private_registry%/}"
if [[ "$private_registry" == *"://"* ]]; then
  echo "--registry must be HOST[:PORT] without http:// or https://" >&2
  exit 2
fi
if [[ ! "$image_namespace" =~ ^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]]; then
  echo "--image-namespace must be a relative registry path such as platform or team/platform" >&2
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd -- "$script_dir/../.." && pwd)"
helm_command="helm"
if ! command -v helm >/dev/null 2>&1; then
  bundled_helm="$repository_root/tools/linux-amd64/helm"
  if [[ ! -x "$bundled_helm" ]]; then
    echo "helm is required; install Helm 3 or use the complete offline ZIP with bundled Helm" >&2
    exit 1
  fi
  helm_command="$bundled_helm"
  echo "[k8s-agent-deploy] Using bundled Helm: $helm_command"
fi

for command_name in kubectl curl openssl base64; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "$command_name is required" >&2
    exit 1
  fi
done

if [[ -z "$values_file" ]]; then
  values_file="$repository_root/charts/flawless/values-production.example.yaml"
fi
if [[ ! -f "$values_file" ]]; then
  echo "Helm values file not found: $values_file" >&2
  exit 1
fi
node_executor_file="$repository_root/manifests/node-executor.yaml"

context="$(kubectl config current-context)"
echo "[k8s-agent-deploy] Target context: $context"
echo "[k8s-agent-deploy] Namespace: $namespace"
echo "[k8s-agent-deploy] Rancher: $rancher_url"
echo "[k8s-agent-deploy] Values: $values_file"
echo "[k8s-agent-deploy] StorageClass: $storage_class (must support ReadWriteMany)"
echo "[k8s-agent-deploy] Private image namespace: $image_namespace"
if [[ "$force" != "true" ]]; then
  read -r -p "Type DEPLOY to continue: " confirmation
  if [[ "$confirmation" != "DEPLOY" ]]; then
    echo "Deployment cancelled" >&2
    exit 1
  fi
fi

if [[ "$skip_rancher_connectivity_check" != "true" ]]; then
  rancher_host="${rancher_url#https://}"
  rancher_host="${rancher_host%%/*}"
  rancher_host="${rancher_host%%:*}"
  if ! getent hosts "$rancher_host" >/dev/null 2>&1; then
    echo "Cannot resolve Rancher host $rancher_host from this Linux machine" >&2
    exit 1
  fi
  rancher_status="$(curl -k -sS -o /dev/null -w '%{http_code}' --connect-timeout 8 --max-time 15 "$rancher_url/")"
  if [[ "$rancher_status" == "000" ]]; then
    echo "Cannot connect to $rancher_url" >&2
    exit 1
  fi
  echo "[k8s-agent-deploy] Rancher HTTPS endpoint responded with HTTP $rancher_status"
fi

kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f -

backup_directory=""
adopt_resource_if_needed() {
  local kind="$1"
  local name="$2"
  local current_owner
  if ! kubectl -n "$namespace" get "$kind" "$name" >/dev/null 2>&1; then
    return
  fi
  current_owner="$(kubectl -n "$namespace" get "$kind" "$name" -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}')"
  if [[ "$current_owner" == "$release_name" ]]; then
    return
  fi
  if [[ "$adopt_existing_resources" != "true" ]]; then
    echo "$kind/$name already exists and is not owned by Helm release $release_name" >&2
    echo "Review it, then rerun with --adopt-existing-resources if takeover is intended." >&2
    exit 1
  fi
  if [[ -z "$backup_directory" ]]; then
    backup_directory="$repository_root/migration-backup-$(date +%Y%m%d-%H%M%S)"
    mkdir -p -- "$backup_directory"
  fi
  kubectl -n "$namespace" get "$kind" "$name" -o yaml >"$backup_directory/${kind}-${name}.yaml"
  kubectl -n "$namespace" label "$kind" "$name" app.kubernetes.io/managed-by=Helm --overwrite
  kubectl -n "$namespace" annotate "$kind" "$name" \
    meta.helm.sh/release-name="$release_name" \
    meta.helm.sh/release-namespace="$namespace" --overwrite
  echo "[k8s-agent-deploy] Adopted $kind/$name; backup: $backup_directory"
}

adopt_resource_if_needed configmap k8s-agent-config
adopt_resource_if_needed serviceaccount k8s-agent-sa

umask 077
secret_dir="$(mktemp -d)"
cleanup() {
  rm -f -- "$secret_dir"/* 2>/dev/null || true
  rmdir -- "$secret_dir" 2>/dev/null || true
  unset rancher_token oauth_client_id oauth_client_secret
}
trap cleanup EXIT INT TERM

read -r -s -p "New Rancher bearer token: " rancher_token
echo
if [[ -z "$rancher_token" ]]; then
  echo "Rancher token cannot be empty" >&2
  exit 1
fi
printf '%s' "$rancher_token" >"$secret_dir/RANCHER_TOKEN"

if [[ "$reuse_oauth_credentials" == "true" ]]; then
  existing_client_id="$(kubectl -n "$namespace" get secret k8s-agent-oauth -o jsonpath='{.data.OAUTH_CLIENT_ID}')"
  existing_client_secret="$(kubectl -n "$namespace" get secret k8s-agent-oauth -o jsonpath='{.data.OAUTH_CLIENT_SECRET}')"
  if [[ -z "$existing_client_id" || -z "$existing_client_secret" ]]; then
    echo "Existing k8s-agent-oauth does not contain reusable OAuth credentials" >&2
    exit 1
  fi
  rancher_token_base64="$(base64 <"$secret_dir/RANCHER_TOKEN" | tr -d '\n')"
  printf '{"data":{"RANCHER_TOKEN":"%s"}}' "$rancher_token_base64" >"$secret_dir/rancher-token-patch.json"
  kubectl -n "$namespace" patch secret k8s-agent-oauth \
    --type merge --patch-file "$secret_dir/rancher-token-patch.json"
  unset rancher_token_base64
else
  read -r -p "Internal LLM OAuth client ID: " oauth_client_id
  read -r -s -p "Internal LLM OAuth client secret: " oauth_client_secret
  echo
  if [[ -z "$oauth_client_id" || -z "$oauth_client_secret" ]]; then
    echo "OAuth client ID and secret cannot be empty" >&2
    exit 1
  fi
  printf '%s' "$oauth_client_id" >"$secret_dir/OAUTH_CLIENT_ID"
  printf '%s' "$oauth_client_secret" >"$secret_dir/OAUTH_CLIENT_SECRET"
  kubectl -n "$namespace" create secret generic k8s-agent-oauth \
    --from-file=RANCHER_TOKEN="$secret_dir/RANCHER_TOKEN" \
    --from-file=OAUTH_CLIENT_ID="$secret_dir/OAUTH_CLIENT_ID" \
    --from-file=OAUTH_CLIENT_SECRET="$secret_dir/OAUTH_CLIENT_SECRET" \
    --dry-run=client -o yaml | kubectl apply -f -
fi
unset existing_client_id existing_client_secret

if ! kubectl -n "$namespace" get secret flawless-cluster-credentials >/dev/null 2>&1; then
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n' >"$secret_dir/fernet-key"
  kubectl -n "$namespace" create secret generic flawless-cluster-credentials \
    --from-file=fernet-key="$secret_dir/fernet-key" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  echo "[k8s-agent-deploy] Keeping the existing cluster credential encryption key"
fi

helm_args=(
  upgrade --install "$release_name" "$repository_root/charts/flawless"
  --namespace "$namespace"
  --create-namespace
  --values "$values_file"
  --set-string "persistence.storageClass=$storage_class"
  --set-string "config.RANCHER_URL=$rancher_url"
)

if [[ "$image_mode" == "private" ]]; then
  helm_args+=(
    --set-string "image.repository=$private_registry/$image_namespace/flawless"
    --set-string "image.tag=3.2.2"
    --set-string "config.NODE_EXEC_IMAGE=$private_registry/$image_namespace/flawless-node-exec:1.36"
  )
fi
if [[ -n "$image_pull_secret" ]]; then
  helm_args+=(
    --set-string "imagePullSecrets[0].name=$image_pull_secret"
    --set-string "config.DEFAULT_IMAGE_PULL_SECRET=$image_pull_secret"
  )
fi

"$helm_command" "${helm_args[@]}"

if [[ "$skip_node_executor" != "true" ]]; then
  kubectl apply -f "$node_executor_file"
  if [[ -n "$image_pull_secret" ]]; then
    node_exec_dockerconfig="$secret_dir/node-exec-dockerconfig.json"
    kubectl -n "$namespace" get secret "$image_pull_secret" \
      -o jsonpath='{.data.\.dockerconfigjson}' | base64 --decode >"$node_exec_dockerconfig"
    if [[ ! -s "$node_exec_dockerconfig" ]]; then
      echo "Secret $namespace/$image_pull_secret has no usable .dockerconfigjson" >&2
      exit 1
    fi
    kubectl -n "$node_exec_namespace" create secret generic "$image_pull_secret" \
      --type=kubernetes.io/dockerconfigjson \
      --from-file=.dockerconfigjson="$node_exec_dockerconfig" \
      --dry-run=client -o yaml | kubectl apply -f -
    echo "[k8s-agent-deploy] Synchronized registry pull Secret into $node_exec_namespace"
  fi
fi

kubectl -n "$namespace" rollout status deployment/k8s-agent-api --timeout=5m
kubectl -n "$namespace" rollout status deployment/k8s-agent-agents --timeout=10m
kubectl -n "$namespace" get pods -o wide

if kubectl -n "$namespace" get configmap k8s-agent-config \
  -o jsonpath='{.data.RANCHER_TOKEN}' | grep -q .; then
  echo "RANCHER_TOKEN must not be present in ConfigMap k8s-agent-config" >&2
  exit 1
fi

kubectl -n "$namespace" exec deployment/k8s-agent-api -- python -c \
  'import os,httpx; r=httpx.get(os.environ["RANCHER_URL"].rstrip("/")+"/v3/clusters",headers={"Authorization":"Bearer "+os.environ["RANCHER_TOKEN"]},verify=False,timeout=20); print("rancher_api_status",r.status_code); r.raise_for_status()'

echo
echo "[k8s-agent-deploy] Deployment and Rancher API verification completed"
echo "[k8s-agent-deploy] Open: http://<any-node-ip>:30080"
