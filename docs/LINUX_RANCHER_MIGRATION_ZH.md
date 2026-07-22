# Linux 导入与新 Rancher 迁移说明

本说明用于把 k8s-agent 部署到 Linux 管理机可访问的 Kubernetes 集群，并切换到新的 Rancher 环境。

## 重要安全处理

生产 ConfigMap 中不应出现 `RANCHER_TOKEN`。Bearer Token、OAuth Client Secret、Webhook 和云平台密钥都必须保存在 Kubernetes Secret 或企业 Secret 管理系统中。

本发布包不会保存任何真实 Token。部署脚本会在终端关闭回显后读取新 Rancher Token，并写入 `k8s-agent-oauth` Secret。由于 Token 已经出现在聊天内容和旧 ConfigMap 中，应在迁移验证完成后吊销新旧 Token，并生成替换 Token。

## 配置变更

生产 values 位于：

```text
charts/flawless/values-production.example.yaml
```

关键变化：

- Rancher 地址切换为新的测试 Rancher。
- `RANCHER_TOKEN` 从 ConfigMap 删除，改由 `k8s-agent-oauth` Secret 注入。
- 所有旧版服务地址改为 Helm 自动生成的 `k8s-agent` / `k8s-agent-agents` 服务。
- `fullnameOverride: k8s-agent`，Deployment 和 Service 使用正式资源名。
- 华为 NAS StorageClass 保留为 `rwx-storage-class`，访问模式为 `ReadWriteMany`。
- 内部 vLLM、Embedding、Langfuse、Prometheus、Loki、Tempo、Grafana 地址继续保留。
- 运行数据路径统一由 Chart 固定到 `/var/lib/flawless` PVC。
- `AUTONOMOUS_OPS_ENABLED=false`。
- `OPS_STEPWISE_CONFIRMATION_REQUIRED=true`。
- `SKILL_EXECUTION_REQUIRED=true`。
- `ALLOWED_NAMESPACES=k8s-agent` 按当前生产限制保留；只有明确允许跨 namespace 变更时才改成 `all`。

## 1. 在 Linux 机器上解压

```bash
tar -xzf k8s-agent-linux-<版本>.tar.gz
cd k8s-agent-linux-<版本>
chmod +x scripts/linux/*.sh
```

检查目标环境：

```bash
kubectl config current-context
kubectl get nodes -o custom-columns=NAME:.metadata.name,ARCH:.status.nodeInfo.architecture
kubectl get storageclass
getent hosts rancher.example.com
```

## 2. 同步镜像到私库

如果节点都是 `amd64`：

```bash
./scripts/linux/Sync-K8sAgentImages.sh \
  --platform amd64 \
  --registry registry.example.com
```

必需镜像按以下顺序上传：

1. `registry.example.com/platform/flawless:3.2.2`
2. `registry.example.com/platform/flawless-node-exec:1.36`

如果同时部署内置可观测组件：

```bash
./scripts/linux/Sync-K8sAgentImages.sh \
  --platform amd64 \
  --registry registry.example.com \
  --include-observability \
  --include-ebpf
```

这会继续上传 Prometheus、kube-state-metrics、Loki、Tempo、Alloy、Grafana 和 Beyla。使用内部 vLLM/DeepSeek 网关时不需要 Ollama 镜像。

## 3. 创建私库拉取 Secret（私库需要认证时）

```bash
kubectl create namespace k8s-agent --dry-run=client -o yaml | kubectl apply -f -

kubectl -n k8s-agent create secret docker-registry regcred \
  --docker-server=registry.example.com \
  --docker-username='<用户名>' \
  --docker-password='<密码>'
```

不要把私库密码写入 Git 或 YAML。

## 4. 部署并切换 Rancher

私库无认证：

```bash
./scripts/linux/Deploy-K8sAgent.sh \
  --storage-class rwx-storage-class \
  --image-mode private \
  --registry registry.example.com
```

私库需要 `regcred`：

```bash
./scripts/linux/Deploy-K8sAgent.sh \
  --storage-class rwx-storage-class \
  --image-mode private \
  --registry registry.example.com \
  --image-pull-secret regcred
```

脚本会依次提示：

1. 输入 `DEPLOY` 确认当前 kubeconfig context。
2. 输入新的 Rancher Bearer Token，终端不回显。
3. 输入内部模型网关 OAuth Client ID。
4. 输入 OAuth Client Secret，终端不回显。

Langfuse 的 `public-key` / `secret-key` 仍由独立 Secret `k8s-agent-langfuse` 提供；飞书、钉钉、企业微信、Slack 或通用 Webhook 等通知键也应从原 Secret 管理系统重新注入。不要把这些值复制到 ConfigMap 或发布包。

如果目标 namespace 已有正确的 `k8s-agent-oauth`，仅替换 Rancher Token 并复用 OAuth 凭据：

```bash
./scripts/linux/Deploy-K8sAgent.sh \
  --storage-class rwx-storage-class \
  --image-mode private \
  --registry registry.example.com \
  --reuse-oauth-credentials
```

如果同一 namespace 已存在旧版手工创建的 `k8s-agent-config` 或 `k8s-agent-sa`，Helm 会拒绝直接接管。脚本会先停止；确认备份和接管无误后增加：

```bash
--adopt-existing-resources
```

脚本会先把旧资源保存到 `migration-backup-时间戳`，再补充 Helm ownership。若旧资源由另一个 Helm release 管理，应先评估旧 release 的卸载/回滚策略，不要直接抢占。

## 5. YAML 应用顺序

核心资源由 Helm 安装，不要重复应用旧的 `manifests/deployment.yaml` 和 `manifests/frontend.yaml`。

顺序如下：

1. `charts/flawless/` + `values-production.example.yaml`：核心 API、Agents、RBAC、PVC、Service。
2. `manifests/node-executor.yaml`：经过 Skill 匹配和人工批准后的节点命令执行边界。
3. `manifests/observability-stack.yaml`：可选 CMDB、Prometheus、kube-state-metrics。
4. `manifests/grafana-observability.yaml`：可选 Loki、Tempo、Alloy、Grafana。
5. `manifests/ebpf-beyla.yaml`：可选 eBPF 网络流，建议在 Loki/Alloy 可用后部署。

核心 Helm 命令等价于：

```bash
helm upgrade --install k8s-agent ./charts/flawless \
  --namespace k8s-agent \
  --create-namespace \
  --values ./charts/flawless/values-production.example.yaml \
  --set-string image.repository=registry.example.com/platform/flawless \
  --set-string image.tag=3.2.2 \
  --set-string config.NODE_EXEC_IMAGE=registry.example.com/platform/flawless-node-exec:1.36

kubectl apply -f manifests/node-executor.yaml
```

可选组件必须先完成对应镜像同步。在 Linux 上生成已经替换成私库地址的 YAML：

```bash
./scripts/linux/Prepare-PrivateManifests.sh \
  --registry registry.example.com

kubectl apply -f generated-private-manifests/30-observability-stack.yaml
kubectl apply -f generated-private-manifests/40-grafana-observability.yaml
kubectl apply -f generated-private-manifests/50-ebpf-beyla.yaml
```

## 6. 验证

```bash
kubectl -n k8s-agent get pods -o wide
kubectl -n k8s-agent get svc
kubectl -n k8s-agent get configmap k8s-agent-config -o jsonpath='{.data.RANCHER_URL}'; echo
kubectl -n k8s-agent get configmap k8s-agent-config -o jsonpath='{.data.RANCHER_TOKEN}'; echo
kubectl -n k8s-agent get secret k8s-agent-oauth -o jsonpath='{.data.RANCHER_TOKEN}' | wc -c
```

第二条 Token 检查必须为空；Secret 检查应输出大于 1 的长度，但不要解码或打印 Token。

确认只保留正式的 Deployment 和 Service；旧版工作负载应在新版本验证成功后按变更流程清理：

```bash
kubectl -n k8s-agent get deploy,svc
```

脚本最后会从 API Pod 内使用 Secret 中的 Token 对新 Rancher `/v3/clusters` 发起只读请求，只有返回成功状态才完成。
