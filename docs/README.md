# Flawless AIOps 用户使用与部署手册

这份文档只面向平台使用者、部署人员和现场运维人员。开发代码、模块负责人和扩展规则请看 [代码架构和维护扩展规则.md](代码架构和维护扩展规则.md)。

## 1. 产品是什么

Flawless AIOps 是面向 Kubernetes、Rancher 多集群、数据库、虚拟机、中间件、企业存储和混合云环境的全栈基础设施 AI SRE 控制平台。它把资源证据、CMDB 拓扑、Prometheus 指标、日志、链路、LLM 诊断和受控修复组织成一个闭环：

```text
发现异常 -> 收集证据 -> 判断根因 -> 计算影响 -> 生成方案
        -> 人工确认 -> 执行变更 -> 验证恢复 -> 记录效果
```

平台不会直接执行 LLM 输出的任意 Shell。所有写操作都必须进入动作白名单、风险门禁、人工确认、最小权限执行和恢复验证流程。

## 2. 主要功能怎么用

### SRE 对话

1. 选择集群、Namespace 和 Workload，也可以保持“所有”。
2. 描述现象，例如“某个业务 Pod 一直 CrashLoopBackOff”。
3. 查看证据、根因、影响范围和受控运维计划。
4. 核对目标与变更内容后点击执行。
5. 在执行流中查看日志、Events、变更回执和恢复验证。

与 Kubernetes 无关的问题会自动切换为普通 LLM 问答。

### 全栈资源

“全栈资源”用于接入 Kubernetes 之外的基础设施，包括数据库、虚拟机、Kafka/MQ、企业存储和公有云资源。

1. 在 ConfigMap 中配置 `INFRASTRUCTURE_RESOURCES_JSON`，或按类型配置 `DATABASE_TARGETS_JSON`、`VM_TARGETS_JSON`、`MIDDLEWARE_TARGETS_JSON`、`STORAGE_TARGETS_JSON`。
2. 打开“全栈资源”，选择资源类型和目标资源。
3. 点击“AI SRE 巡检”，平台会执行只读探测、读取资源指标、匹配全栈运维 Skill，并让 LLM 生成受控运维预演。
4. 数据库、虚拟机、存储和云平台的真实变更必须提交到 `INFRASTRUCTURE_ACTION_WEBHOOK_URL` 指向的企业受控执行器；平台不会执行任意 Shell 或 SQL。
5. 执行器返回结果后，平台继续做审计、恢复验证和运维成效记录。

资源配置示例：

```json
[
  {
    "id": "prod-mysql-01",
    "type": "database",
    "engine": "mysql",
    "name": "生产订单 MySQL",
    "host": "db.example.com",
    "port": 3306,
    "business_service": "order",
    "criticality": "high",
    "metrics": { "connections_percent": 91, "replication_lag_seconds": 72 }
  },
  {
    "id": "vm-logstash-01",
    "type": "virtual_machine",
    "provider": "virtualization-platform",
    "name": "Logstash 虚拟机 01",
    "host": "192.0.2.31",
    "port": 22,
    "business_service": "logging",
    "metrics": { "disk_percent": 89, "memory_percent": 82 }
  }
]
```

### AI 巡检

1. 选择所有集群或指定集群、Namespace。
2. 生产模式会额外检查单副本、资源边界、探针、可变镜像、高权限和潜在风险。
3. 异常队列按严重度、故障类型、影响、冗余和证据置信度排序。
4. 未开启自动运维时，逐项查看预演并确认执行。
5. 开启自动运维时，只自动执行通过门禁的低、中风险动作；高风险动作仍需人工确认。

### 运维 Skill 库

运维 Skill 用来把资深运维人员的经验注入平台。它不是脚本市场，也不是让 LLM 随便执行命令，而是结构化 Runbook。

1. 打开“资源事件 -> Skill 库”。
2. 填写 Skill 名称、类别、风险、适用症状、需要证据、诊断步骤、允许动作和恢复判据。
3. 点击“测试匹配”，输入一个故障描述，确认 AI 能把场景匹配到正确 Skill。
4. 保存后，SRE 对话和 AI 巡检会自动引用匹配到的 Skill。
5. 执行时仍然走预演、人工确认、动作白名单、审计和恢复验证。

| 字段 | 写什么 |
|---|---|
| Skill 名称 | 一眼能看懂的运维能力，如“PVC Pending 静态 PV 恢复” |
| 一句话说明 | 这条经验解决什么问题，什么时候不该用 |
| 症状关键词 | CrashLoopBackOff、FailedMount、permission denied 等 |
| 需要证据 | previous_logs、events、storage_chain、db_locks、vm_disk_usage 等 |
| 诊断步骤 | 真实运维顺序，一行一步 |
| 允许动作 | 必须来自平台动作目录，如 patch_workload、create_pvc |
| 恢复判据 | pod_ready、pvc_bound、error_rate_recovered 等 |
| 风险等级 | low、medium、high；高风险必须人工确认 |

### 拓扑影响

- 2D 模式适合日常查看所属关系和关键依赖。
- 3D 模式把每个集群显示为球形星系，支持拖拽、缩放和复位。
- 选择节点后运行 AI 影响分析，可查看上游、下游、关键路径、放大系数和爆炸半径。
- 拓扑事实来自 CMDB 和集群资源；LLM 负责解释，数值由确定性算法计算。

### 外部数据流

“外部数据流”只展示跨出集群边界的流向，包括：

- 外部系统访问集群内 Service、Ingress 或 Workload。
- 集群内应用访问外部数据库、接口、中间件、对象存储或其他企业系统。
- 一个集群中的应用访问另一个集群中的服务，例如中间件集群 Kafka/ELK 数据链路。

页面中的 `observed` 表示来自 Hubble、Kiali、Service Mesh、eBPF 或自研流量系统的真实观测；`inferred` 表示平台通过 Pod 环境变量、启动参数、Service ExternalName、Endpoint、Ingress 和 CMDB 关系推断出来的流向。没有接入真实流量系统时，平台不会伪造字节数或 QPS。

可选配置：

| 配置 | 作用 |
|---|---|
| `CNI_PLUGIN_MODE` | 当前集群网络插件，可填 `auto`、`flannel`、`canal`、`calico`、`cilium` |
| `EBPF_FLOW_PROVIDER` | 观测源类型，默认 `auto`；可指定 `flannel`、`canal`、`calico`、`goldmane` |
| `EBPF_FLOW_URL` | 接入企业 eBPF Collector HTTP API，推荐生产使用 |
| `FLANNEL_EBPF_FLOW_URL` | flannel 集群接入外部 eBPF Collector 的 HTTP API |
| `CANAL_EBPF_FLOW_URL` | canal 集群接入 canal/eBPF Collector 的 HTTP API |
| `CALICO_FLOW_URL` / `CALICO_GOLDMANE_FLOW_URL` | Calico Flow Logs、Whisker/Goldmane 或企业导出的 flow API |
| `HUBBLE_FLOW_URL` / `HUBBLE_RELAY_HTTP_URL` | 接入 Cilium Hubble 输出的 flow JSON |
| `BEYLA_LOKI_FLOW_ENABLED` | 没有现成 eBPF 服务时，读取 Beyla 输出到 Loki 的 `network_flow` 日志，默认 `true` |
| `BEYLA_LOKI_NAMESPACE` | Beyla 采集器命名空间，默认 `flawless-ebpf` |
| `EBPF_TOPOLOGY_FUSION_ENABLED` | 是否把 eBPF 观测到的真实调用边融合进拓扑影响分析，默认 `true` |
| `FLOW_OBSERVATION_URL` | 接入 Hubble/Kiali/自研流量接口，返回 flows/items/data 数组 |
| `FLOW_OBSERVATION_TOKEN` | 流量接口 Token，生产建议放 Secret |
| `CLUSTER_INTERNAL_DOMAINS` | 识别集群内部域名，默认 `svc,svc.cluster.local,cluster.local` |
| `EXTERNAL_FLOW_CLUSTER_DOMAINS_JSON` | 把特定域名后缀标记为跨集群，例如 `[{"domain":".mid.example","cluster":"middleware"}]` |

权限边界：

- Flawless 主应用容器不直接运行 eBPF 抓包逻辑，不要求 `privileged`、`hostNetwork` 或额外 Linux capability。
- 如果集群没有 eBPF 服务，可从 0 部署内置 Beyla Collector：

```bash
kubectl apply -f manifests/grafana-observability.yaml
kubectl apply -f manifests/ebpf-beyla.yaml
kubectl rollout restart deployment/flawless -n k8s-agent
```

- Beyla 以 DaemonSet 运行在 `flawless-ebpf` 命名空间，输出 `network_flow` 日志；Alloy 会收集到 Loki，平台再把这些真实流量融合到“拓扑影响”和“外部数据流”。
- flannel：flannel 负责基础三层网络，本身不提供 Hubble 风格的 flow API；可使用上面的 Beyla Collector，或接入企业 eBPF Collector、DeepFlow/Pixie/Tetragon 等已批准采集器的 HTTP API，并填 `FLANNEL_EBPF_FLOW_URL` 或 `EBPF_FLOW_URL`。
- canal：canal 通常是 flannel 数据面 + Calico policy；优先接 `CANAL_EBPF_FLOW_URL`，也可接 Calico Flow/Goldmane API。
- calico：可接 `CALICO_FLOW_URL` 或 `CALICO_GOLDMANE_FLOW_URL`；平台会解析 Calico 常见的 `src_* / dst_* / action / proto / bytes` 字段。
- cilium：如集群已有 Cilium/Hubble，直接把 Hubble/Relay/企业 Collector 的 HTTP 地址填入 `HUBBLE_FLOW_URL` 或 `HUBBLE_RELAY_HTTP_URL`。
- 如组织安全部门要求更细粒度权限，可以把 Beyla 的 `privileged: true` 收敛为官方支持的 Linux capabilities；生产评审前请在测试集群验证内核和容器运行时兼容性。
- 主应用继续以非 root、只读根文件系统运行；运行时状态写入 `/var/lib/flawless` PVC，临时缓存写入 `/tmp`。

### 发布治理与错误预算

- 默认 SLO 为 99.9%，30 天错误预算约 43.2 分钟。
- 错误预算耗尽后冻结新功能和常规发布。
- 常规发布支持已有 Workload 变更和新 Workload YAML 发布。
- 紧急修复通道只允许稳定版本回滚、恢复误删配置和受控重启。
- 紧急修复只豁免错误预算冻结，不豁免 YAML 安全校验、审批、审计和恢复验证。

### 模型实验室

- 支持 Token URL + Base URL 的 OAuth 企业模型。
- 支持 Base URL + API Key 的 OpenAI-compatible 模型。
- 可切换模型并使用同一批巡检证据做六维运维能力测评。
- 线上真实修复率、恢复 Pod 数和风险降低率在“运维成效”中单独统计。

### 知识库与可观测

- 知识库可导入文本、PDF、Word 等文件，并按产品知识或运维 Runbook 检索。
- 可观测模块展示 Trace、Token、延迟、吞吐、成本、Tool Call 和数据流向。
- Langfuse 可关闭；关闭后核心运维仍可工作，但模型全链路分析会减少。

## 3. 部署前准备

需要准备：

- 一个可部署 Namespace，默认 `k8s-agent`。
- 可访问的镜像仓库。
- Rancher URL 与 Token，或本集群 ServiceAccount 权限。
- LLM 的 OAuth Client Credentials，或 API Key。
- 可选的 Prometheus、CMDB、Langfuse、Loki、Tempo、Grafana。
- 用于运行时审计、观测数据和本地模型数据的默认 StorageClass；当前清单已统一使用 `standard`。

严禁把真实 Token、Client Secret、API Key 写进 ConfigMap 或 Git。

### 持久化存储

本项目的 Kubernetes 清单已把需要保留的数据挂载到 PVC，默认 StorageClass 为 `standard`：

| PVC | 用途 | 默认容量 |
|---|---|---:|
| `flawless-runtime-store` | 运维审计、SLO、发布治理、模型实验、前端添加的运维 Skills、知识库索引等运行时数据 | 5Gi |
| `k8s-agent-prometheus-data` | 本地 Prometheus 指标数据 | 30Gi |
| `k8s-agent-loki-data` | Loki 日志数据 | 20Gi |
| `k8s-agent-tempo-data` | Tempo Trace 数据 | 20Gi |
| `k8s-agent-grafana-data` | Grafana 面板、数据源和本地配置 | 5Gi |
| `langfuse-postgres-pvc` | 本地 Langfuse PostgreSQL 数据 | 10Gi |
| `ollama-models-pvc` | 本地 Ollama 模型文件 | 50Gi |

如果集群里已经创建过同名 PVC，Kubernetes 不允许原地修改 `storageClassName` 和 `accessModes`。迁移时先备份旧 PVC 数据，再删除旧 PVC 并重新 `kubectl apply`；或者新建不同名称的 PVC，同时修改 Deployment 中的 `claimName`。

## 4. 构建镜像

推荐使用仓库脚本，它已配置国内 Python、Node 和系统包镜像：

```bash
./scripts/build-push.sh
```

也可以手工构建单镜像：

```bash
docker build --target backend-runtime -t <registry>/flawless-api:<tag> .
docker push <registry>/flawless-api:<tag>
```

`backend-runtime` 镜像内已经包含 `frontend/dist`，默认不需要单独构建 `frontend-runtime`。只有明确采用独立 Nginx Web 部署时，才设置 `BUILD_WEB_IMAGE=true` 并额外构建 Web 镜像。

把 `manifests/deployment.yaml` 和 `manifests/frontend.yaml` 中的 API 镜像地址改成实际地址，生产环境建议使用不可变 digest。

## 5. 配置 Secret

至少配置 LLM 或 Rancher 所需密钥。示例键名如下，真实值由组织 Secret Manager、External Secrets 或人工创建：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: k8s-agent-oauth
  namespace: k8s-agent
type: Opaque
stringData:
  OAUTH_CLIENT_ID: "<client-id>"
  OAUTH_CLIENT_SECRET: "<client-secret>"
  RANCHER_TOKEN: "<rancher-token>"
```

模型、Rancher 和观测系统地址放在 `manifests/deployment.yaml` 的 `k8s-agent-config` ConfigMap 中。

## 6. 关键环境变量

| 变量 | 用途 |
|---|---|
| `RANCHER_URL` | Rancher 地址 |
| `RANCHER_CLUSTER_IDS=all` | 枚举 Token 可见的全部集群 |
| `PROMETHEUS_URL` | 中心 Prometheus 地址 |
| `PROMETHEUS_CLUSTER_LABELS` | 多集群指标标签候选 |
| `CMDB_URL` | CMDB 服务地址 |
| `LLM_API_BASE` | OpenAI-compatible 模型地址 |
| `OAUTH_TOKEN_URL` | OAuth 动态 Token 地址 |
| `MODEL_PROFILES_JSON` | 多模型注册配置 |
| `OPS_MUTATION_ENABLED` | 是否允许提交受控变更 |
| `AUTONOMOUS_OPS_ENABLED` | 是否允许自动策略升级 |
| `ALLOWED_NAMESPACES` | 本地 MCP 可写 Namespace 白名单 |
| `DEFAULT_IMAGE_PULL_SECRET` | 镜像拉取失败时可注入的企业批准 imagePullSecret 名称 |
| `AUTO_OPS_CONFIGMAP_TEMPLATES_JSON` | 允许自动恢复的 ConfigMap 模板，支持 `namespace/name` 或 `name` 索引 |
| `AUTO_OPS_STATIC_PV_TEMPLATE_JSON` | 允许自动创建静态 PV 的批准模板 |
| `AUTO_OPS_STATIC_PV_NFS_SERVER` / `AUTO_OPS_STATIC_PV_NFS_BASE_PATH` | NFS 静态 PV 恢复所需的受控后端路径 |
| `OPS_SKILL_ROOT` | Agent Skills 标准目录根路径；生产应挂载 PVC |
| `OPS_SKILL_STORE_PATH` | 旧 JSON 注册表路径，仅用于自动迁移 |
| `INSPECTION_SKILL_ROUTER_ENABLED` | 巡检是否启用“语义匹配 + LLM 批量复核”的 Skill 路由 |
| `RELIABILITY_STORE_PATH` | SLO 与发布审计持久化路径 |
| `IMAGE_SECURITY_SCAN_URL` | 可选镜像安全扫描服务地址，发布治理会 POST `{images:[...]}` 并把结果纳入风险判定 |
| `LANGFUSE_ENABLED` | 是否启用 Langfuse |
| `INFRASTRUCTURE_RESOURCES_JSON` | Kubernetes 之外的统一资源清单，支持 database、virtual_machine、middleware、storage、cloud_service |
| `DATABASE_TARGETS_JSON` / `VM_TARGETS_JSON` | 数据库和虚拟机专用资源清单，适合分团队维护 |
| `MIDDLEWARE_TARGETS_JSON` / `STORAGE_TARGETS_JSON` | 中间件和存储专用资源清单 |
| `INFRASTRUCTURE_ACTION_WEBHOOK_URL` | DB、VM、存储、云平台真实变更的企业受控执行器入口 |
| `INFRASTRUCTURE_LLM_PLANNER_ENABLED` | 是否让 LLM 在确定性证据基础上生成全栈资源运维预演 |

`RANCHER_CLUSTER_IDS=all` 不能绕过 Rancher 权限。Token 必须对每个目标集群具有相应的读取或写入权限。

## 7. 部署顺序

```bash
kubectl apply -f manifests/rbac.yaml
# 可选：开启全局高级运维能力，包括全 namespace 修复、节点隔离、静态 PV 创建和发布治理。
# 生产建议先保持 AUTONOMOUS_OPS_ENABLED=false，由人确认每次变更。
kubectl apply -f manifests/advanced-ops-clusterrolebindings.yaml
kubectl apply -f manifests/deployment.yaml
kubectl apply -f manifests/frontend.yaml
kubectl apply -f manifests/observability-stack.yaml
kubectl apply -f manifests/grafana-observability.yaml
```

观测栈可按组织已有平台选择性部署。更新后执行：

```bash
kubectl rollout restart deployment/flawless -n k8s-agent
kubectl rollout restart deployment/k8s-agent-api -n k8s-agent
kubectl rollout status deployment/k8s-agent-api -n k8s-agent
```

## 8. 部署后检查

```bash
kubectl get pod,svc,pvc -n k8s-agent
kubectl logs deployment/k8s-agent-api -n k8s-agent --tail=200
kubectl get --raw /readyz
```

浏览器检查：

1. 左下角 Agent 状态不是全部离线。
2. 集成页面能看到 Rancher、CMDB、Prometheus 的配置状态。
3. 资源页面可选择不同集群。
4. 发布治理可以创建风险判定记录。
5. SRE 对话可以流式返回，并出现预演或可执行方案。

## 9. 运维 Skill 编写与添加指南

Skill 是把资深运维人员的经验注入平台的标准方式。当前实现兼容 Agent Skills 开放规范：每项 Skill 都是独立目录，以 `SKILL.md` 为跨智能体可读主体，以 `references/ops-policy.yaml` 保存 Flawless 的证据、动作和恢复门禁。SRE 对话按“元数据发现 -> 命中后加载正文”的渐进披露方式使用 Skill。

```text
ops-skills/
└── pvc-pending-recovery/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    └── references/
        └── ops-policy.yaml
```

### 在前端哪里添加

进入左侧菜单 **Skill 库**：

1. 填写 Skill 名称、类别、风险、负责人和一句话说明。
2. 写入症状关键词，例如 `CrashLoopBackOff`、`FailedMount`、`permission denied`、`ImagePullBackOff`。
3. 从“需要证据”菜单多选日志、Events、Workload 配置、拓扑、指标、存储链或节点状态。
4. 写入诊断步骤，一行一步，按真实值班流程写。
5. 从“允许动作”菜单选择受控动作。每个动作都可以点击查看，了解用途、适用时机、风险等级、是否可自动执行和回退方式。
6. 从“适用对象”和“恢复判据”菜单多选目标类型及客观恢复标准。
7. 点击 **保存并生成 Skill 包**，然后在右侧 **匹配测试** 输入故障描述，看 AI 是否能选中该 Skill。

原有前端注入方式保持兼容：表单字段、保存接口、匹配测试、SRE 对话和 AI 巡检用法不变，只是底层从单一 JSON 改成标准目录包。

### 导入、导出和迁移

- 点击 Skill 卡片的 **导出**，获得可移植 ZIP，可交给 Codex、Claude Code 或其他 Agent Skills 兼容智能体。
- 点击 **导入 Skill** 上传标准 ZIP。只包含 `SKILL.md` 的通用 Skill 会以“指令型”导入，不自动取得集群写权限。
- 带 `references/ops-policy.yaml` 的 Flawless Skill 会恢复结构化证据、动作和恢复判据。
- ZIP 中的 `scripts/` 可以随包迁移，但在本平台始终默认为不可信；只有 `OPS_APPROVED_SCRIPTS_JSON` 中登记的 `script_id` 才能成为执行候选。
- 旧 `OPS_SKILL_STORE_PATH` JSON 会在启动时自动迁移到 `OPS_SKILL_ROOT`，之后以目录包为事实来源。
- K8S 部署已把 `/var/lib/flawless` 挂载到 `flawless-runtime-store` PVC，Pod 重建不会丢失前端添加的 Skill。

### 可选的企业批准脚本

Skill 支持选择“是否允许脚本处置”，但不允许在前端粘贴任意 shell、`kubectl` 或脚本正文。正确流程是：

1. 脚本由运维负责人评审后进入组织的脚本执行平台。
2. 在 `manifests/deployment.yaml` 的 ConfigMap 中配置 `OPS_APPROVED_SCRIPTS_JSON`，只登记脚本 ID、名称、用途、风险、允许对象和前置证据。
3. 运维人员在前端 Skill 库开启“企业批准脚本”，选择脚本 ID。
4. 多选触发条件，并用文字明确说明什么具体故障场景允许触发。
5. Skill 命中后，脚本只会成为候选动作；仍需证据齐全、目标范围授权、人工确认、超时限制和审计。

示例配置：

```yaml
OPS_APPROVED_SCRIPTS_JSON: >-
  [{"id":"inspect-pvc-permission",
    "name":"PVC 目录权限检查",
    "description":"只读检查挂载目录属主和权限",
    "risk":"medium",
    "runner":"enterprise-script-runner",
    "allowed_targets":["Pod","PVC"],
    "required_evidence":["previous_logs","storage_chain"]}]
```

脚本触发场景应写得具体，例如：

```text
Pod 连续 3 次 CrashLoop，previous log 明确出现 permission denied，
PVC 已 Bound，且 Workload securityContext 与存储目录权限不一致时允许触发。
仅凭用户描述、没有日志或存储链证据时不得触发。
```

### 推荐写法

好的 Skill 应该像一份短而准的 Runbook：

- 症状要贴近真实告警词，不写空泛描述。
- 证据要可读取，例如日志、Events、Pod 状态、YAML、Prometheus 指标。
- 步骤要能执行，避免“综合判断”“酌情处理”这类无法自动化的话。
- 允许动作要收敛到平台受控动作，不允许直接写 `kubectl` 命令或 shell。
- 恢复判据要客观，例如 Ready、rollout、错误率、延迟、重启次数稳定。

示例：

```text
Skill 名称：PVC 权限导致 CrashLoop 修复
类别：storage
风险：medium
症状关键词：CrashLoopBackOff, permission denied, FailedMount, PVC
需要证据：previous_logs, events, workload_spec, pvc_status, security_context
诊断步骤：
1. 读取 previous log，确认是否为挂载目录权限不足。
2. 读取 Pod Events，确认 PVC 已挂载且不是调度失败。
3. 读取 Workload securityContext，确认 runAsUser/fsGroup 是否与存储目录权限匹配。
4. 读取 PVC/PV 和 StorageClass，确认卷绑定正常。
允许动作：patch_workload, recreate_pod
恢复判据：pod_ready, rollout_complete, restart_count_stable
```

### Skill 如何生效

- SRE 对话：用户描述问题后，平台会先匹配 Skill，再决定要补哪些证据和生成什么修复预演。
- AI 巡检：巡检发现异常后，会把异常症状与 Skill 库匹配，优先选择置信度高且风险可控的方案。
- 自动运维：Skill 只提供专家经验和允许动作范围；真实执行仍必须经过平台动作目录、RBAC、风险门禁、审计和人工确认。

如果匹配不到，平台会回退到通用诊断流程，并把“证据不足”和“需要补充什么”展示出来。

## 10. 常见问题

### 发布审计提示只读文件系统

应用更新后的 `manifests/frontend.yaml`。主审计写入 `/var/lib/flawless` PVC；容器 `/tmp` 已挂载 `emptyDir` 作为 Python 临时文件和应急路径。页面出现“应急存储”提示时，提交可以继续，但应尽快恢复 PVC 写入，避免 Pod 重建后丢失临时审计。

### 只能看到一个 Rancher 集群

确认 `RANCHER_CLUSTER_IDS=all`，并确认 Token 调用 `/v3/clusters` 能返回多个集群。账号看不到的集群，平台也无法纳管。

### Prometheus 数据全是 0

确认中心 Prometheus 已汇聚各集群指标，并带统一 `cluster` 或 `rancher_cluster_id` 标签。没有中心汇聚时，平台会尝试 Rancher metrics API，但每个集群必须安装 metrics-server。

### 运维计划没有执行按钮

常见原因是缺少目标 Workload、证据不足、动作属于高风险、服务端写开关关闭或 RBAC 不足。先看计划中的“为什么本轮没有执行变更”和权限步骤。

### 镜像拉取、PVC、ConfigMap 问题仍然没有修复

这三类问题需要先把企业批准的参数写入 ConfigMap 或 Secret：

- 镜像鉴权：配置 `DEFAULT_IMAGE_PULL_SECRET`，并确保目标 namespace 里已经存在同名 Secret。
- 缺失 PVC/PV：配置默认 StorageClass，或配置 `AUTO_OPS_STATIC_PV_TEMPLATE_JSON` / NFS 后端参数。
- 缺失 ConfigMap：配置 `AUTO_OPS_CONFIGMAP_TEMPLATES_JSON`，平台只会按批准模板创建，不会让 LLM 自己编配置内容。

目标 namespace 还必须绑定 `k8s-agent-remediator`，否则只会展示权限修复步骤，不会提交变更。

### Post-mortem Agent 离线

检查 `POSTMORTEM_AGENT_URL`、Service 端口 8103 和 `/health`。不要把 Pod 内的 `localhost` 地址写给前端 API 使用。

## 11. 生产安全要求

- 控制台接入组织 OIDC/OAuth2 Proxy，不直接暴露 Basic Auth。
- 生产环境必须校验企业 CA，禁止长期关闭 TLS 校验。
- Web 容器不挂载 ServiceAccount 和密钥。
- 默认只读，写权限按 Namespace 通过 RoleBinding 单独授予。
- 如果启用 `manifests/advanced-ops-clusterrolebindings.yaml`，平台会获得全局受控写权限，但仍不是 `cluster-admin`；必须保留人工确认、审计和高风险二次确认。
- 镜像使用 digest、签名、SBOM 和漏洞扫描。
- Kubernetes Audit 与应用审计统一归档到不可篡改存储。
- 先在隔离演练集群验证 CrashLoop、OOM、PVC、DNS、RBAC、LLM 超时和取消流程。
