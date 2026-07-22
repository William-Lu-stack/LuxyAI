# ============================================================
# Dockerfile — Flawless
# 必须在包含 Dockerfile、frontend/、requirements.lock 的项目根目录执行。
# 构建: docker build -t <your-registry>/flawless:latest .
# 推送: docker push <your-registry>/flawless:latest
# 国内网络构建:
# docker build \
#   --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.13-slim \
#   --build-arg NGINX_IMAGE=docker.m.daocloud.io/nginxinc/nginx-unprivileged:stable-alpine3.23 \
#   --build-arg DEBIAN_MIRROR=https://mirrors.aliyun.com/debian \
#   --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
#   -t <your-registry>/flawless:latest .
# ============================================================

ARG NODE_IMAGE=node:24-slim
ARG PYTHON_IMAGE=python:3.13-slim
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:stable-alpine3.23@sha256:b3f2436575bd5be7386518084d842dac414ab4962712afa31e99e0942a56e3b2

FROM ${NODE_IMAGE} AS frontend-builder

WORKDIR /frontend/modern

ARG NPM_REGISTRY=https://registry.npmjs.org
ARG NPM_FETCH_TIMEOUT=120000

COPY frontend/modern/package*.json ./
RUN set -eux; \
    npm config set registry "${NPM_REGISTRY}"; \
    npm config set fetch-timeout "${NPM_FETCH_TIMEOUT}"; \
    npm config set fund false; \
    npm config set audit false; \
    if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi

COPY frontend/modern/ ./
RUN set -eux; \
    npm run build; \
    test -f /frontend/dist/index.html; \
    test -d /frontend/dist/assets

FROM ${NGINX_IMAGE} AS frontend-runtime

USER root
RUN apk add --no-cache --upgrade curl libcurl
USER 101

COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY --from=frontend-builder /frontend/dist /usr/share/nginx/html

EXPOSE 8080

FROM ${PYTHON_IMAGE} AS backend-runtime

WORKDIR /app

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG DEBIAN_MIRROR=http://deb.debian.org/debian
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_EXTRA_INDEX_URL=
ARG PIP_TRUSTED_HOST=
ARG PIP_TIMEOUT=120
ARG PIP_RETRIES=5
ARG PIP_ONLY_BINARY=:all:

# 设置为环境变量（后续所有命令都会生效）
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=localhost,127.0.0.1,.svc,.cluster.local \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DEFAULT_TIMEOUT=${PIP_TIMEOUT} \
    PIP_RETRIES=${PIP_RETRIES} \
    PIP_ONLY_BINARY=${PIP_ONLY_BINARY} \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 安装系统依赖
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i \
        -e "s|https\?://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        -e "s|https\?://deb.debian.org/debian-security|${DEBIAN_MIRROR}-security|g" \
        /etc/apt/sources.list.d/debian.sources; \
    fi; \
    printf 'Acquire::Retries "5";\nAcquire::http::Timeout "120";\nAcquire::https::Timeout "120";\n' > /etc/apt/apt.conf.d/99-flawless-timeouts; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*

# pip 安装 — 锁定版本与制品哈希，可通过 build-arg 切换为私有网络 PyPI 镜像
COPY requirements.txt requirements.lock ./
RUN set -eux; \
    python -m pip install --upgrade pip \
      --timeout ${PIP_TIMEOUT} \
      --retries ${PIP_RETRIES}; \
    pip install --require-hashes -r requirements.lock \
      --timeout ${PIP_TIMEOUT} \
      --retries ${PIP_RETRIES}

COPY . .
# 保留静态制品用于本地单进程开发；生产环境使用 frontend-runtime 独立部署。
COPY --from=frontend-builder /frontend/dist /app/frontend/dist

ENV PYTHONPATH=/app
ENV HOME=/tmp
ENV XDG_CACHE_HOME=/tmp/.cache
ENV MPLCONFIGDIR=/tmp/matplotlib
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TOKENIZERS_PARALLELISM=false

RUN addgroup --system --gid 10001 app \
    && adduser --system --uid 10001 --ingroup app --home /tmp app \
    && mkdir -p /tmp/.cache /tmp/matplotlib /var/lib/flawless \
    && chown -R app:app /app /tmp /var/lib/flawless

USER 10001:10001

EXPOSE 8080 8100 8101 8102 8103 8105 8200 8300

# Local default starts the complete stack. Kubernetes workloads override this command per service.
CMD ["python", "scripts/run_local_stack.py", "--host", "0.0.0.0", "--api-port", "8080"]
