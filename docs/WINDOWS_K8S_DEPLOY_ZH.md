# Windows 打包、镜像同步与 Kubernetes 部署说明

本发布包适用于 Windows 10/11 或 Windows Server 上的 PowerShell 5.1/7。核心部署使用 Helm，避免旧式散装 YAML 重复创建 PVC、使用旧镜像或遗漏审批安全参数。

## 1. 发布包内包含什么

- 完整项目源码和前端源码
- `charts/flawless/`：正式 Kubernetes Helm Chart
- `scripts/windows/Sync-K8sAgentImages.ps1`：拉取国内公开镜像，或转存到企业私库
- `scripts/windows/Deploy-K8sAgent.ps1`：创建 Secret、部署核心服务、启用节点执行边界并验证 rollout
- `manifests/node-executor.yaml`：允许经过人工审批的节点主机执行
- `manifests/observability-stack.yaml`：可选 CMDB、Prometheus、kube-state-metrics
- `manifests/grafana-observability.yaml`：可选 Loki、Tempo、Alloy、Grafana
- `manifests/ebpf-beyla.yaml`：可选 eBPF 网络流观测

ZIP 不包含 `.git`、`.env`、API Key、kubeconfig、Docker 登录信息、Python 虚拟环境或前端 `node_modules`。

## 2. Windows 前置软件

安装并加入 `PATH`：

1. Docker Desktop 或 Docker CLI
2. `kubectl`
3. Helm 3
4. 能访问目标 Kubernetes 集群的 kubeconfig

确认目标集群和节点架构：

```powershell
kubectl config current-context
kubectl get nodes -o custom-columns=NAME:.metadata.name,ARCH:.status.nodeInfo.architecture
kubectl get storageclass
```

生产部署需要支持 `ReadWriteMany` 的 StorageClass，例如 NFS、CephFS 或 Longhorn RWX。不要把普通的单节点 `ReadWriteOnce` StorageClass 当作 RWX 使用。

## 3. 必需镜像

| 顺序 | 组件 | 国内公开镜像 | 私库建议目标 |
|---|---|---|---|
| 1 | Web、API、MCP、全部 Agent、CMDB | `m.daocloud.io/ghcr.io/your-org/flawless:3.2.2` | `registry.example.com/k8s-agent:3.2.2` |
| 2 | 审批后的节点命令执行器 | `m.daocloud.io/ghcr.io/your-org/flawless-node-exec:1.36` | `registry.example.com/k8s-agent-node-exec:1.36` |

两个镜像都是公开、多架构镜像，包含 `linux/amd64` 和 `linux/arm64`。普通 `docker pull` 会自动选择当前机器架构。

只拉取核心镜像：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\windows\Sync-K8sAgentImages.ps1 -Platform amd64
```

拉取并推送到私有仓库：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\windows\Sync-K8sAgentImages.ps1 `
  -Platform amd64 `
  -PrivateRegistry registry.example.com
```

如果集群节点同时存在 `amd64` 和 `arm64`，普通 pull/tag/push 只能转存当前架构。应使用 `skopeo copy --all` 或 `regctl image copy` 保留完整多架构 manifest。

## 4. 可选可观测镜像

只有准备部署相应 YAML 时才需要同步这些镜像：

```powershell
.\scripts\windows\Sync-K8sAgentImages.ps1 `
  -Platform amd64 `
  -PrivateRegistry registry.example.com `
  -IncludeObservability `
  -IncludeEbpf
```

该命令按顺序同步：Prometheus、kube-state-metrics、Loki、Tempo、Alloy、Grafana、Beyla。当前使用 DeepSeek API，不需要上传 Ollama 和模型下载 Job 镜像。

如果 Kubernetes 集群只能访问私库，还要生成已经替换镜像地址的可选 YAML：

```powershell
.\scripts\windows\Prepare-PrivateManifests.ps1 `
  -PrivateRegistry registry.example.com
```

生成结果位于 `generated-private-manifests`，文件名前缀就是应用顺序。不要在私网集群中直接应用仍引用公网地址的可选 YAML。

Langfuse 是可选能力，默认不拉取；其原始清单包含需要在生产前修改的数据库密码和 `latest` 标签，因此必须经过安全评审后才能使用 `-IncludeLangfuse`。

## 5. 推荐的一键部署

先确认公司集群实际 RWX StorageClass 名称。下面以 `nfs-rwx` 为例。

直接使用国内公开镜像：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\windows\Deploy-K8sAgent.ps1 `
  -StorageClass nfs-rwx `
  -ImageMode public-cn
```

使用已经转存到 `registry.example.com` 的镜像：

```powershell
.\scripts\windows\Deploy-K8sAgent.ps1 `
  -StorageClass nfs-rwx `
  -ImageMode private `
  -PrivateRegistry registry.example.com
```

如果私库需要认证：

```powershell
.\scripts\windows\Deploy-K8sAgent.ps1 `
  -StorageClass nfs-rwx `
  -ImageMode private `
  -PrivateRegistry registry.example.com `
  -RegistryUsername YOUR_USER
```

脚本会依次：

1. 显示当前 kubeconfig context，并要求输入 `DEPLOY`。
2. 创建 `k8s-agent` namespace。
3. 提示输入 DeepSeek/OpenAI 兼容 API Key，不把 Key 写入文件。
4. 创建并保留 kubeconfig 加密使用的 Fernet Key。
5. 通过 Helm 部署 API 和全部 Agent。
6. 应用 `manifests/node-executor.yaml`。
7. 等待 API、Agents rollout 完成并输出 Pod 状态。

部署参数已经固定为：所有变更必须人工逐步批准、必须匹配 Skill、禁止自治策略升级，但允许用户审批后的真实 Kubernetes 变更。

## 6. YAML/组件应用顺序

推荐核心服务由 Helm 生成并应用，不要再重复应用 `manifests/deployment.yaml` 和 `manifests/frontend.yaml`。

正式顺序如下：

### 必选

1. Helm 核心资源：`charts/flawless/`
2. 节点执行边界：`manifests/node-executor.yaml`

对应命令：

```powershell
helm upgrade --install k8s-agent .\charts\flawless `
  --namespace k8s-agent `
  --create-namespace `
  -f .\charts\flawless\values-public-cn.yaml `
  --set serviceAccount.name=k8s-agent-sa `
  --set-string persistence.storageClass=nfs-rwx `
  --set-string config.LLM_API_BASE=https://api.deepseek.com/v1 `
  --set-string config.LLM_MODEL=deepseek-reasoner `
  --set-string config.AUTONOMOUS_OPS_ENABLED=false `
  --set-string config.OPS_STEPWISE_CONFIRMATION_REQUIRED=true `
  --set-string config.SKILL_EXECUTION_REQUIRED=true

kubectl apply -f .\manifests\node-executor.yaml
```

### 可选：内置指标和 CMDB

第 3 个应用：

```powershell
kubectl apply -f .\manifests\observability-stack.yaml
```

如果使用私库，改为：

```powershell
kubectl apply -f .\generated-private-manifests\30-observability-stack.yaml
```

随后让核心服务连接它：

```powershell
helm upgrade k8s-agent .\charts\flawless `
  --namespace k8s-agent `
  --reuse-values `
  --set-string config.PROMETHEUS_URL=http://k8s-agent-prometheus:9090 `
  --set-string config.CMDB_URL=http://k8s-agent-cmdb:8300
```

### 可选：日志、链路和 Grafana

第 4 个应用：

```powershell
kubectl apply -f .\manifests\grafana-observability.yaml
```

私库对应文件为 `generated-private-manifests\40-grafana-observability.yaml`。

### 可选：eBPF 网络流

第 5 个应用，建议在 Loki/Alloy 可用后再部署：

```powershell
kubectl apply -f .\manifests\ebpf-beyla.yaml
```

私库对应文件为 `generated-private-manifests\50-ebpf-beyla.yaml`。

### 不默认应用

- 自托管 Langfuse：使用独立的受控部署仓库和 Secret Manager，本包不附带任何含默认口令的清单。
- `manifests/ollama-local.yaml`：使用 DeepSeek API 时不需要。
- `manifests/full-access-ops-clusterrolebinding.yaml`：会授予 `cluster-admin`，默认禁止。
- `manifests/advanced-ops-clusterrolebindings.yaml`：仅在逐项完成权限评审后应用。
- `manifests/production-controls.yaml`、`platform-resilience.yaml`：需要按照实际副本数和资源配额调整。

## 7. 私库是 HTTP 时

如果 `registry.example.com` 是 HTTP 仓库，必须在 Windows Docker Desktop 和每台 Kubernetes 节点的 containerd/Docker 中信任它。Docker Desktop 可在 Docker Engine 配置中加入：

```json
{
  "insecure-registries": ["registry.example.com"]
}
```

修改后重启 Docker Desktop。K3s/RKE2 节点还需要配置对应的 `registries.yaml` 并重启服务。

## 8. 部署验证

```powershell
kubectl -n k8s-agent get pods -o wide
kubectl -n k8s-agent rollout status deployment/k8s-agent-flawless-api --timeout=5m
kubectl -n k8s-agent rollout status deployment/k8s-agent-flawless-agents --timeout=10m
kubectl auth can-i patch deployments --all-namespaces --as=system:serviceaccount:k8s-agent:k8s-agent-sa
kubectl auth can-i create pods -n flawless-node-exec --as=system:serviceaccount:k8s-agent:k8s-agent-sa
```

默认访问地址：

```text
http://任意Kubernetes节点IP:30080
```
