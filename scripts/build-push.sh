#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

TAG="${TAG:-$(date +%Y%m%d%H%M)}"
IMAGE="${IMAGE:-flawless:${TAG}}"
WEB_IMAGE="${WEB_IMAGE:-}"
BUILD_WEB_IMAGE="${BUILD_WEB_IMAGE:-false}"
NODE_IMAGE="${NODE_IMAGE:-docker.m.daocloud.io/library/node:24-slim}"
PYTHON_IMAGE="${PYTHON_IMAGE:-docker.m.daocloud.io/library/python:3.13-slim}"
NGINX_IMAGE="${NGINX_IMAGE:-docker.m.daocloud.io/nginxinc/nginx-unprivileged:1.27-alpine}"
DEBIAN_MIRROR="${DEBIAN_MIRROR:-https://mirrors.aliyun.com/debian}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
NPM_FETCH_TIMEOUT="${NPM_FETCH_TIMEOUT:-120000}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"
PIP_EXTRA_INDEX_URL="${PIP_EXTRA_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-mirrors.aliyun.com}"
PIP_TIMEOUT="${PIP_TIMEOUT:-120}"
PIP_RETRIES="${PIP_RETRIES:-5}"
PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-:all:}"
SYNC_MANIFESTS="${SYNC_MANIFESTS:-true}"

docker build \
  --target backend-runtime \
  --progress=plain \
  --build-arg NODE_IMAGE="${NODE_IMAGE}" \
  --build-arg PYTHON_IMAGE="${PYTHON_IMAGE}" \
  --build-arg NPM_REGISTRY="${NPM_REGISTRY}" \
  --build-arg NPM_FETCH_TIMEOUT="${NPM_FETCH_TIMEOUT}" \
  --build-arg HTTP_PROXY="${HTTP_PROXY:-}" \
  --build-arg HTTPS_PROXY="${HTTPS_PROXY:-}" \
  --build-arg DEBIAN_MIRROR="${DEBIAN_MIRROR}" \
  --build-arg PIP_INDEX_URL="${PIP_INDEX_URL}" \
  --build-arg PIP_EXTRA_INDEX_URL="${PIP_EXTRA_INDEX_URL}" \
  --build-arg PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST}" \
  --build-arg PIP_TIMEOUT="${PIP_TIMEOUT}" \
  --build-arg PIP_RETRIES="${PIP_RETRIES}" \
  --build-arg PIP_ONLY_BINARY="${PIP_ONLY_BINARY}" \
  -t "${IMAGE}" \
  .

if [[ "${BUILD_WEB_IMAGE}" == "true" ]]; then
  if [[ -z "${WEB_IMAGE}" ]]; then
    echo "BUILD_WEB_IMAGE=true requires WEB_IMAGE=<registry>/<name>:<tag>" >&2
    exit 1
  fi
  docker build \
    --target frontend-runtime \
    --progress=plain \
    --build-arg NODE_IMAGE="${NODE_IMAGE}" \
    --build-arg NGINX_IMAGE="${NGINX_IMAGE}" \
    --build-arg NPM_REGISTRY="${NPM_REGISTRY}" \
    --build-arg NPM_FETCH_TIMEOUT="${NPM_FETCH_TIMEOUT}" \
    -t "${WEB_IMAGE}" \
    .
else
  echo "Skipping frontend-runtime image; backend-runtime already contains frontend/dist."
fi

docker run --rm -i "${IMAGE}" python - <<'PY'
from cmdb.local_cmdb import app as cmdb_app
from agents.postmortem_agent import app as postmortem_app
from backend.app.main import app as control_plane_app
from frontend.server import app as compatibility_app

source = open("/app/backend/app/main.py", encoding="utf-8").read()
assert "PlatformSelfHealRequest" not in source
assert "platform_self_heal_run(req:" not in source
assert compatibility_app is control_plane_app
print("IMAGE_IMPORT_OK", cmdb_app.title, postmortem_app.title, control_plane_app.title)
PY

docker push "${IMAGE}"
if [[ "${BUILD_WEB_IMAGE}" == "true" ]]; then
  docker push "${WEB_IMAGE}"
fi

echo "Pushed ${IMAGE}"
if [[ "${BUILD_WEB_IMAGE}" == "true" ]]; then
  echo "Pushed optional web image ${WEB_IMAGE}"
fi

if [[ "${SYNC_MANIFESTS}" == "true" ]]; then
  python - "${IMAGE}" <<'PY'
from pathlib import Path
import re
import sys

image = sys.argv[1]
root = Path.cwd()
files = [
    root / "manifests" / "deployment.yaml",
    root / "manifests" / "frontend.yaml",
    root / "manifests" / "observability-stack.yaml",
]
pattern = re.compile(r"(?:(?:[A-Za-z0-9._-]+(?::[0-9]+)?/)+)?(?:flawless|flawless):[A-Za-z0-9._-]+")
for path in files:
    text = path.read_text()
    updated = pattern.sub(image, text)
    if updated != text:
        path.write_text(updated)
        print(f"Updated {path.relative_to(root)} -> {image}")
PY
  echo "Manifests synced to ${IMAGE}"
fi
