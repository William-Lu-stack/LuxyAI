# k8s-agent 完全离线部署说明（Linux AMD64）

本包用于无法访问互联网、但可以访问 Kubernetes API、内部 Rancher、内部模型网关和私有镜像仓库的 Linux 管理机。镜像已由用户在本地准备并使用 `registry.example.com/最后两段路径:标签` 命名，因此 ZIP 只包含程序、Chart、YAML、离线校验/推送脚本和部署说明，不重复包含镜像大文件。

部署过程不会执行任何公网 `docker pull`。真实 Rancher Token、OAuth Client ID、OAuth Client Secret、私库密码、kubeconfig 均不在 ZIP 中。

## 1. ZIP 内容

```text
charts/flawless/                                  核心 Helm Chart
charts/flawless/values-production.example.yaml   生产配置
manifests/node-executor.yaml                   节点执行权限边界
scripts/linux/Push-LocalImages.sh              校验并推送本地镜像
scripts/linux/Deploy-K8sAgent.sh                核心部署和验证
scripts/linux/Prepare-PrivateManifests.sh       生成私库版可观测 YAML
tools/linux-amd64/helm                         包内 Helm 3（仅交付 ZIP 包含）
docs/OFFLINE_K8S_DEPLOY_ZH.md                   本说明
```

## 2. 前置条件

- Linux AMD64。
- `kubectl` 已安装，kubeconfig 已指向目标集群；Helm 3 已包含在完整离线 ZIP 中。
- Docker、`curl`、`openssl`、`base64` 已安装。
- 可以访问 Kubernetes API、新 Rancher、内部模型/OAuth 地址和 `registry.example.com`。
- `rwx-storage-class` 支持 `ReadWriteMany`。
- 下表中的镜像已存在于本地 Docker。

| 顺序 | 本地及私库镜像标签 |
|---:|---|
| 1 | `registry.example.com/platform/flawless:3.2.2` |
| 2 | `registry.example.com/platform/flawless-node-exec:1.36` |
| 3 | `registry.example.com/google_containers/prometheus:v2.45.0` |
| 4 | `registry.example.com/kube-state-metrics/kube-state-metrics:v2.10.1` |
| 5 | `registry.example.com/grafana/loki:3.7.3` |
| 6 | `registry.example.com/grafana/tempo:2.10.5` |
| 7 | `registry.example.com/grafana/alloy:v1.16.1` |
| 8 | `registry.example.com/grafana/grafana:13.0.2` |
| 9 | `registry.example.com/grafana/beyla:3.24.0` |

当前生产配置使用内部模型网关和外部 Langfuse，因此不需要 Ollama、PostgreSQL 或本地 Langfuse 镜像。

## 3. 校验并解压

把 ZIP 上传到能访问目标集群和私库的 Linux 管理机：

```bash
sha256sum k8s-agent-offline-linux-amd64-<版本>.zip
unzip k8s-agent-offline-linux-amd64-<版本>.zip
cd k8s-agent-offline-linux-amd64-<版本>
chmod +x scripts/linux/*.sh
```

确认全部私库标签存在：

```bash
docker images --format '{{.Repository}}:{{.Tag}}' | \
  grep '^registry.example.com/' | sort
```

抽查镜像架构；后面的推送脚本还会逐个强制校验：

```bash
docker image inspect \
  registry.example.com/platform/flawless:3.2.2 \
  --format '{{.Os}}/{{.Architecture}}'
```

## 4. 推送本地镜像到内网私库

私库需要认证：

```bash
./scripts/linux/Push-LocalImages.sh \
  --registry registry.example.com \
  --image-namespace platform
```

私库不需要认证：

```bash
./scripts/linux/Push-LocalImages.sh \
  --registry registry.example.com \
  --image-namespace platform \
  --skip-registry-login
```

脚本会严格检查 9 个标签和 AMD64 架构，然后按顺序执行 `docker push`，不会访问公网。若只部署核心、不部署可观测组件，可增加 `--core-only`。

如果私库仅提供 HTTP，需要在当前 Docker 以及每台 Kubernetes 节点的容器运行时中，把 `registry.example.com` 配置为受信任的 HTTP/insecure registry。修改运行时配置时合并现有配置，不要直接覆盖，随后重启相应 Docker/containerd 服务。

## 5. 创建私库认证 Secret（仅私库需要认证时）

```bash
kubectl create namespace k8s-agent --dry-run=client -o yaml | kubectl apply -f -

kubectl -n k8s-agent create secret docker-registry regcred \
  --docker-server=registry.example.com \
  --docker-username='<用户名>' \
  --docker-password='<密码>'
```

## 6. 确认生产目标

```bash
kubectl config current-context
kubectl get nodes -o custom-columns=NAME:.metadata.name,ARCH:.status.nodeInfo.architecture
kubectl get storageclass rwx-storage-class
getent hosts rancher.example.com
curl -k -I --connect-timeout 8 https://rancher.example.com/
```

这里访问的是企业内网 Rancher，不属于公网依赖。

## 7. 部署核心服务

先从脱敏模板创建只属于当前环境的 Helm values。该文件只保存在离线管理机，禁止提交到 Git；其中只写内部端点、模型名、命名空间、StorageClass 等非凭据配置，不要写 Token、密码、OAuth Client Secret 或 kubeconfig：

```bash
cp charts/flawless/values-production.example.yaml private-values.yaml
chmod 600 private-values.yaml
vi private-values.yaml
```

把原生产 ConfigMap 中除 `RANCHER_TOKEN` 和其他凭据以外的配置迁移到 `private-values.yaml` 的 `config:` 下。下面的 `--rancher-url` 会覆盖 values 内的 `config.RANCHER_URL`；Rancher Token 和模型 OAuth 凭据由脚本隐藏读取并写入 Kubernetes Secret。

私库无认证：

```bash
./scripts/linux/Deploy-K8sAgent.sh \
  --storage-class rwx-storage-class \
  --rancher-url https://rancher.example.com \
  --values ./private-values.yaml \
  --image-mode private \
  --registry registry.example.com \
  --image-namespace platform
```

私库需要认证：

```bash
./scripts/linux/Deploy-K8sAgent.sh \
  --storage-class rwx-storage-class \
  --rancher-url https://rancher.example.com \
  --values ./private-values.yaml \
  --image-mode private \
  --registry registry.example.com \
  --image-namespace platform \
  --image-pull-secret regcred
```

执行时会依次：

1. 显示 kubeconfig context，只有输入 `DEPLOY` 才继续。
2. 隐藏读取新的 Rancher Token。
3. 读取内部模型网关 OAuth Client ID，并隐藏读取 Client Secret。
4. 创建/更新 Kubernetes Secret，ConfigMap 中不保存 Token。
5. 通过 Helm 部署 API 和 Agents。
6. 应用 `manifests/node-executor.yaml`；使用私库认证时，把 dockerconfig Secret 安全同步到独立的 `flawless-node-exec` 命名空间。
7. 等待工作负载 Ready，并在 API Pod 内只读验证 Rancher `/v3/clusters`。

如果目标集群已有正确的 OAuth Secret，仅更新 Rancher Token：

```bash
./scripts/linux/Deploy-K8sAgent.sh \
  --storage-class rwx-storage-class \
  --rancher-url https://rancher.example.com \
  --values ./private-values.yaml \
  --image-mode private \
  --registry registry.example.com \
  --image-namespace platform \
  --reuse-oauth-credentials
```

如果报告旧 `k8s-agent-config` 或 `k8s-agent-sa` 不是当前 Helm release 管理，先检查旧资源。确认接管后在部署命令末尾加入 `--adopt-existing-resources`，脚本会先生成迁移备份。

## 8. 按顺序部署可观测组件

生成全部使用两段式私库路径的 YAML：

```bash
./scripts/linux/Prepare-PrivateManifests.sh \
  --registry registry.example.com \
  --image-namespace platform
```

依次执行：

```bash
kubectl apply -f generated-private-manifests/30-observability-stack.yaml
kubectl apply -f generated-private-manifests/40-grafana-observability.yaml
kubectl apply -f generated-private-manifests/50-ebpf-beyla.yaml
```

核心资源已由 Helm 安装，节点执行边界也已由部署脚本应用。不要再应用旧的 `manifests/deployment.yaml` 或 `manifests/frontend.yaml`。

## 9. 验证完整闭环

```bash
kubectl -n k8s-agent get pods -o wide
kubectl -n k8s-agent get deploy,daemonset,svc,pvc
kubectl -n k8s-agent get events --sort-by=.lastTimestamp | tail -50

kubectl -n k8s-agent get configmap k8s-agent-config \
  -o jsonpath='{.data.RANCHER_URL}'; echo
kubectl -n k8s-agent get configmap k8s-agent-config \
  -o jsonpath='{.data.RANCHER_TOKEN}'; echo
kubectl -n k8s-agent get secret k8s-agent-oauth \
  -o jsonpath='{.data.RANCHER_TOKEN}' | wc -c
```

ConfigMap Token 检查必须为空，Secret 长度必须大于 1。不要解码或打印 Token。

从 `http://<任一节点IP>:30080` 打开控制台，创建故障工作负载，依次确认诊断、动态 Skill 匹配、人工逐步批准、执行、Pod 恢复和变更后验证闭环。
