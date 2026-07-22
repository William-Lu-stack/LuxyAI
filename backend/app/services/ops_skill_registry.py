"""运维 Skill 注册、匹配和持久化服务。

Skill 是一线运维人员沉淀经验的最小资产：它描述适用场景、必须采集的证据、
允许映射到哪些受控动作，以及恢复成功的判据。Skill 可以声明 YAML 或 Shell
动作，但真正执行仍然必须经过动作目录、逐步人工确认和 OpsJob 审计。
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.app.services.agent_skill_packages import (
    AGENT_SKILL_SPEC,
    AgentSkillPackageError,
    delete_package,
    export_package,
    import_archive,
    normalize_skill_name,
    read_package,
    write_package,
)


DEFAULT_OPERATOR_SKILLS: list[dict[str, Any]] = [
    {
        "id": "skill-volume-permission-recovery",
        "name": "容器卷权限修复与重新发布",
        "category": "storage",
        "summary": "根据 mkdir permission denied 的真实日志、运行用户、挂载和 Workload 配置动态选择 YAML Patch 或受审批命令，并重新发布验证。",
        "symptoms": ["mkdir", "permission denied", "can't create directory", "read-only file system", "CrashLoopBackOff"],
        "applies_to": ["Pod", "Deployment", "StatefulSet", "DaemonSet", "PVC", "PV"],
        "evidence_required": ["previous_logs", "workload_spec", "pod_security_context", "storage_chain", "recent_changes"],
        "diagnostic_steps": [
            "读取失败容器的 current/previous logs，锁定实际 mkdir 路径、运行阶段和退出码。",
            "读取镜像运行用户、Pod/容器 securityContext、volumeMount、PVC/PV 与后端访问模式，不预设 fsGroup 一定正确。",
            "让 AI 基于证据生成实际 YAML Patch 或受控命令，展示完整 diff、重新发布方式、回滚和恢复判据。",
        ],
        "allowed_actions": ["patch_workload", "patch_workload_runtime_security", "patch_resource", "apply_manifest", "exec_pod", "exec_node"],
        "success_criteria": ["rollout_complete", "pod_ready", "restart_count_stable", "events_no_new_backoff", "write_errors_absent"],
        "risk": "high",
        "rollback": "恢复变更前 Workload/PVC 快照并重新 rollout；节点或 Pod 命令使用计划中逐项声明的回滚命令。",
        "owner": "Flawless",
        "enabled": True,
        "builtin": True,
    },
    {
        "id": "skill-crashloop-root-cause",
        "name": "CrashLoop 根因分流",
        "category": "runtime",
        "summary": "把 CrashLoopBackOff 按 OOM、探针、配置、挂载、镜像和依赖故障分流，避免只会重启。",
        "symptoms": ["CrashLoopBackOff", "Back-off restarting", "exit code 137", "probe failed", "permission denied"],
        "applies_to": ["Pod", "Deployment", "StatefulSet", "DaemonSet"],
        "evidence_required": ["previous_logs", "events", "last_state", "workload_spec", "recent_changes"],
        "diagnostic_steps": [
            "读取 current/previous logs 和 lastState，先判定退出码与信号。",
            "按时间线合并 Events、镜像、挂载、探针、依赖超时和最近发布。",
            "只有证据能指向模板级问题时才提出 patch，否则继续收集证据。",
        ],
        "allowed_actions": ["patch_workload", "recreate_pod", "rollback_workload"],
        "success_criteria": ["pod_ready", "restart_count_stable", "events_no_new_backoff"],
        "risk": "medium",
        "owner": "Flawless",
        "enabled": True,
        "builtin": True,
    },
    {
        "id": "skill-storage-pvc-pv",
        "name": "PVC/PV 存储链路修复",
        "category": "storage",
        "summary": "处理 PVC 缺失、Pending、PV 未绑定、挂载失败和目录权限问题。",
        "symptoms": ["FailedMount", "FailedAttachVolume", "persistentvolumeclaim not found", "no persistent volumes available", "read-only file system"],
        "applies_to": ["Pod", "PVC", "PV", "StatefulSet"],
        "evidence_required": ["storage_chain", "events", "storage_class", "workload_spec"],
        "diagnostic_steps": [
            "沿 Pod volume -> PVC -> PV -> StorageClass/CSI 读取真实状态。",
            "区分 PVC 不存在、PVC 未绑定、PV 权限/路径错误和 CSI 组件异常。",
            "静态 PV 只允许使用平台预配置模板，避免 AI 编造存储路径。",
        ],
        "allowed_actions": ["create_pvc", "create_pv", "patch_workload_volume", "patch_workload"],
        "success_criteria": ["pvc_bound", "mount_events_absent", "pod_ready"],
        "risk": "high",
        "owner": "Flawless",
        "enabled": True,
        "builtin": True,
    },
    {
        "id": "skill-config-reference-recovery",
        "name": "ConfigMap/Secret 配置引用恢复",
        "category": "configuration",
        "summary": "处理 Workload 引用的 ConfigMap/Secret 缺失、键名错误或发布后配置漂移；只从已批准内容创建或修补资源。",
        "symptoms": ["configmap not found", "secret not found", "couldn't find key", "CreateContainerConfigError", "配置引用", "runtime-config"],
        "applies_to": ["Pod", "Deployment", "StatefulSet", "DaemonSet", "ConfigMap", "Secret"],
        "evidence_required": ["events", "workload_spec", "recent_changes"],
        "diagnostic_steps": [
            "从 Events 与 Workload 模板确认缺失对象、键名、namespace 和最近发布差异。",
            "Secret 只在服务端解码校验，模型、日志、Skill 与审计只接收字段名和长度等脱敏证据。",
            "展示完整对象差异、来源、回滚和重新发布影响，由操作员确认后再创建或修补。",
        ],
        "allowed_actions": ["create_configmap", "apply_manifest", "patch_resource"],
        "success_criteria": ["config_ref_exists", "rollout_complete", "pod_ready", "events_no_new_backoff"],
        "risk": "high",
        "rollback": "恢复审批前的 ConfigMap/Secret 与 Workload revision；敏感值不进入回滚日志。",
        "owner": "Flawless",
        "enabled": True,
        "builtin": True,
    },
    {
        "id": "skill-service-endpoint-flow",
        "name": "Service/Endpoint 流量黑洞排查",
        "category": "network",
        "summary": "排查 Service selector、EndpointSlice、DNS、NetworkPolicy、Ingress/Gateway 与 Kafka/ELK 数据流。",
        "symptoms": ["no endpoints", "503 service unavailable", "connection refused", "i/o timeout", "dns"],
        "applies_to": ["Service", "Ingress", "Gateway", "Deployment", "Kafka"],
        "evidence_required": ["service_endpoints", "dns", "network_policy", "dependency_topology", "traffic_baseline"],
        "diagnostic_steps": [
            "核对 Service selector 与 Workload labels 是否能匹配 Ready Pod。",
            "验证 EndpointSlice、DNS 解析、NetworkPolicy 和上游调用路径。",
            "结合 CMDB 数据流判断 Kafka/ELK 或跨集群链路是否放大影响。",
        ],
        "allowed_actions": ["patch_service", "patch_workload", "restart"],
        "success_criteria": ["endpoint_ready", "dependency_reachable", "error_rate_recovered"],
        "risk": "high",
        "owner": "Flawless",
        "enabled": True,
        "builtin": True,
    },
    {
        "id": "skill-database-performance-rca",
        "name": "数据库性能与连接耗尽根因分析",
        "category": "database",
        "summary": "面向数据库连接耗尽、慢 SQL、锁等待、复制延迟、磁盘容量和备份异常的证据化诊断。",
        "symptoms": ["too many connections", "slow query", "lock wait", "deadlock", "replication lag", "tablespace full", "connection pool exhausted"],
        "applies_to": ["Database", "MySQL", "PostgreSQL", "Oracle", "Redis", "MongoDB", "Elasticsearch"],
        "evidence_required": ["db_connectivity", "db_slow_queries", "db_locks", "db_replication", "db_capacity", "db_backup_status", "dependency_topology"],
        "diagnostic_steps": [
            "确认实例角色、连接状态、错误日志、备份状态和最近变更，先排除网络或凭据问题。",
            "按连接数、慢 SQL、锁等待、长事务、复制延迟、表空间和磁盘使用率分流根因。",
            "结合 CMDB 判断哪些应用受数据库问题影响，执行前给出会话、参数、扩容或切换的风险说明。",
        ],
        "allowed_actions": ["db_kill_session", "db_expand_storage", "db_failover", "db_apply_parameter", "db_restart_instance"],
        "success_criteria": ["db_connection_recovered", "db_replication_caught_up", "db_slow_query_reduced", "db_backup_healthy", "business_probe_ok"],
        "risk": "high",
        "owner": "Flawless",
        "enabled": True,
        "builtin": True,
    },
    {
        "id": "skill-vm-system-recovery",
        "name": "虚拟机系统级故障恢复",
        "category": "virtual_machine",
        "summary": "处理 VM 不可达、关键服务异常、磁盘满、CPU/内存/IO 压力、系统日志错误和安全基线漂移。",
        "symptoms": ["host unreachable", "service down", "disk full", "cpu high", "memory high", "iowait", "systemd failed", "filesystem readonly"],
        "applies_to": ["VirtualMachine", "VM", "LinuxHost", "WindowsHost", "ECS", "OpenStackInstance", "VMwareVM"],
        "evidence_required": ["vm_agent_status", "vm_system_metrics", "vm_service_status", "vm_disk_usage", "vm_system_logs", "vm_snapshot_state", "dependency_topology"],
        "diagnostic_steps": [
            "先确认 Agent、网络、控制台、最近变更和业务冗余，区分主机不可达与服务不可用。",
            "检查 CPU、内存、磁盘、IO、文件句柄、系统日志和关键服务状态，定位最小恢复动作。",
            "优先服务级恢复和磁盘扩容；整机重启必须展示业务影响、快照状态和回滚/接管方案。",
        ],
        "allowed_actions": ["vm_restart_service", "vm_expand_disk", "vm_snapshot", "vm_run_approved_script", "vm_reboot"],
        "success_criteria": ["vm_agent_online", "vm_service_active", "vm_disk_pressure_relieved", "vm_probe_healthy", "business_probe_ok"],
        "risk": "high",
        "owner": "Flawless",
        "enabled": True,
        "builtin": True,
    },
]


SKILL_OPTION_CATALOG: dict[str, list[dict[str, str]]] = {
    "applies_to": [
        {"id": "Pod", "label": "Pod", "description": "单个运行实例，适合日志、重启、挂载、探针和调度问题。"},
        {"id": "Deployment", "label": "Deployment", "description": "无状态应用工作负载，适合模板、镜像、副本和滚动发布问题。"},
        {"id": "StatefulSet", "label": "StatefulSet", "description": "有状态工作负载，需额外关注顺序、稳定身份和持久卷。"},
        {"id": "DaemonSet", "label": "DaemonSet", "description": "节点级工作负载，常用于日志、网络、存储和安全组件。"},
        {"id": "Job", "label": "Job", "description": "一次性任务；Succeeded 属于正常完成，不应按运行中 Pod 风险处理。"},
        {"id": "CronJob", "label": "CronJob", "description": "定时任务，重点检查调度、并发策略、历史 Job 和执行时限。"},
        {"id": "Service", "label": "Service", "description": "服务发现和流量入口，适合 selector、端口和 Endpoint 问题。"},
        {"id": "Ingress", "label": "Ingress / Gateway", "description": "外部流量入口，适合路由、证书、后端服务和网关策略问题。"},
        {"id": "HPA", "label": "HPA", "description": "自动扩缩容对象，适合指标缺失、上下限和扩缩容失效问题。"},
        {"id": "PDB", "label": "PDB", "description": "中断预算，适合驱逐失败、发布阻塞和可用副本约束问题。"},
        {"id": "PVC", "label": "PVC", "description": "命名空间级存储声明，适合 Pending、扩容和绑定问题。"},
        {"id": "PV", "label": "PV", "description": "集群级持久卷，适合静态供卷、回收策略和后端路径问题。"},
        {"id": "Node", "label": "Node", "description": "集群节点，适合压力、NotReady、隔离和恢复调度问题。"},
        {"id": "ConfigMap", "label": "ConfigMap", "description": "非敏感配置对象，适合缺失引用和模板恢复。"},
        {"id": "ServiceAccount", "label": "ServiceAccount", "description": "工作负载身份，适合镜像拉取密钥和最小权限问题。"},
        {"id": "Kafka", "label": "Kafka / 中间件", "description": "跨集群数据流依赖，适合积压、连通性和上下游影响分析。"},
        {"id": "Database", "label": "数据库实例", "description": "MySQL、PostgreSQL、Oracle、Redis、MongoDB、ES 等数据库与缓存。"},
        {"id": "MySQL", "label": "MySQL", "description": "连接耗尽、慢 SQL、锁等待、复制延迟、表空间和参数问题。"},
        {"id": "PostgreSQL", "label": "PostgreSQL", "description": "连接、锁、Vacuum、复制槽、WAL、表膨胀和参数问题。"},
        {"id": "Oracle", "label": "Oracle", "description": "会话、锁、表空间、归档、Data Guard 和备份恢复问题。"},
        {"id": "Redis", "label": "Redis", "description": "内存、慢命令、主从复制、哨兵/Cluster 和热点 key 问题。"},
        {"id": "VirtualMachine", "label": "虚拟机 / 主机", "description": "VMware、OpenStack、ECS、裸机 Linux/Windows 等主机对象。"},
        {"id": "LinuxHost", "label": "Linux 主机", "description": "systemd 服务、磁盘、IO、网络、内核和安全基线问题。"},
        {"id": "WindowsHost", "label": "Windows 主机", "description": "Windows 服务、事件日志、磁盘、补丁和域控相关问题。"},
        {"id": "StorageArray", "label": "企业存储", "description": "Generic CSI Storage、Virtualization Platform、SAN/NAS、Ceph 和 NFS 后端。"},
        {"id": "CloudResource", "label": "云资源", "description": "阿里云、通用云、私有云等云主机、磁盘、网络和安全组。"},
    ],
    "evidence_required": [
        {"id": "current_logs", "label": "当前日志", "description": "读取容器当前日志，判断正在发生的错误。"},
        {"id": "previous_logs", "label": "上一次容器日志", "description": "CrashLoop 场景优先读取，定位容器上次退出前的错误。"},
        {"id": "events", "label": "Kubernetes Events", "description": "确认调度、挂载、拉镜像、探针和准入失败的时间线。"},
        {"id": "last_state", "label": "容器退出状态", "description": "读取 exit code、reason、signal 和结束时间。"},
        {"id": "workload_spec", "label": "Workload 配置", "description": "读取镜像、探针、资源、卷、环境变量和安全上下文。"},
        {"id": "recent_changes", "label": "最近变更", "description": "核对 revision、镜像和配置变化，判断是否为发布回归。"},
        {"id": "pod_metrics", "label": "Pod 指标", "description": "读取 CPU、内存、throttling 和重启趋势。"},
        {"id": "node_conditions", "label": "节点状态", "description": "读取 Ready、DiskPressure、MemoryPressure、PIDPressure。"},
        {"id": "node_capacity", "label": "节点容量", "description": "检查可分配 CPU、内存、Pod 数和资源碎片。"},
        {"id": "scheduler_constraints", "label": "调度约束", "description": "检查 affinity、taint、toleration、topology spread 和 nodeSelector。"},
        {"id": "quota", "label": "Quota / LimitRange", "description": "检查命名空间配额和准入限制。"},
        {"id": "hpa", "label": "HPA 状态", "description": "检查指标、当前/期望副本和扩缩容条件。"},
        {"id": "service_endpoints", "label": "Service / Endpoint", "description": "确认 selector 是否匹配 Ready Pod，端口是否一致。"},
        {"id": "dns", "label": "DNS 证据", "description": "验证 Service 域名解析和 CoreDNS 状态。"},
        {"id": "network_policy", "label": "网络策略", "description": "检查 NetworkPolicy、CNI 和服务网格路由是否阻断流量。"},
        {"id": "dependency_topology", "label": "依赖拓扑", "description": "读取 CMDB、调用链和跨集群中间件数据流。"},
        {"id": "traffic_baseline", "label": "流量基线", "description": "对比正常流量、错误率、延迟和重试。"},
        {"id": "storage_chain", "label": "PVC/PV 存储链", "description": "沿 Pod volume、PVC、PV、StorageClass 和 CSI 检查。"},
        {"id": "storage_class", "label": "StorageClass", "description": "检查动态供卷、扩容支持、绑定模式和 provisioner。"},
        {"id": "csi_status", "label": "CSI 状态", "description": "检查 CSI controller/node 组件及其事件。"},
        {"id": "image_pull_secrets", "label": "镜像拉取凭据", "description": "只检查 Secret 引用和 ServiceAccount 绑定，不读取明文。"},
        {"id": "registry_connectivity", "label": "镜像仓库连通性", "description": "检查仓库 DNS、TLS、鉴权和镜像是否存在。"},
        {"id": "pod_security_context", "label": "安全上下文", "description": "检查 runAsUser、fsGroup、只读根文件系统和 capability。"},
        {"id": "pdb_state", "label": "PDB 状态", "description": "检查 disruptionsAllowed 和不可用副本限制。"},
        {"id": "certificate_chain", "label": "证书链", "description": "检查有效期、签发链和服务端名称。"},
        {"id": "db_connectivity", "label": "数据库连通性", "description": "检查实例地址、端口、角色、只读状态、连接池和错误日志摘要。"},
        {"id": "db_slow_queries", "label": "慢 SQL", "description": "读取慢 SQL 摘要、执行次数、耗时、扫描行数和索引命中情况。"},
        {"id": "db_locks", "label": "锁等待 / 长事务", "description": "检查阻塞链、等待时长、会话来源和事务状态。"},
        {"id": "db_replication", "label": "复制 / 主备状态", "description": "检查主从延迟、复制槽、Data Guard、哨兵或集群复制状态。"},
        {"id": "db_capacity", "label": "数据库容量", "description": "检查表空间、磁盘、WAL/binlog、连接数和内存水位。"},
        {"id": "db_backup_status", "label": "备份状态", "description": "确认最近备份、恢复点、备份失败和保留策略。"},
        {"id": "vm_agent_status", "label": "主机 Agent 状态", "description": "检查云/监控/堡垒机 Agent 是否在线且能执行批准动作。"},
        {"id": "vm_system_metrics", "label": "主机系统指标", "description": "检查 CPU、内存、磁盘、IO、网络、文件句柄和进程数。"},
        {"id": "vm_service_status", "label": "系统服务状态", "description": "检查 systemd/Windows Service 状态、重启次数和失败日志。"},
        {"id": "vm_disk_usage", "label": "主机磁盘使用率", "description": "检查分区、inode、文件系统只读、增长趋势和可扩容空间。"},
        {"id": "vm_system_logs", "label": "系统日志", "description": "读取 syslog/journal/EventLog 摘要，不导出敏感正文。"},
        {"id": "vm_snapshot_state", "label": "快照 / 回滚点", "description": "确认快照、备份或镜像回滚点是否可用。"},
    ],
    "success_criteria": [
        {"id": "pod_ready", "label": "Pod Ready", "description": "目标 Pod 连续通过 readiness，状态稳定。"},
        {"id": "rollout_complete", "label": "发布完成", "description": "Workload generation 收敛，期望副本全部可用。"},
        {"id": "restart_count_stable", "label": "重启数稳定", "description": "观察窗口内 restart count 不再增长。"},
        {"id": "events_no_new_backoff", "label": "无新增 BackOff", "description": "观察窗口内不再产生新的 BackOff/Failed 事件。"},
        {"id": "pvc_bound", "label": "PVC Bound", "description": "PVC 成功绑定 PV，容量与访问模式符合要求。"},
        {"id": "mount_events_absent", "label": "挂载错误消失", "description": "不再出现 FailedMount/FailedAttachVolume。"},
        {"id": "endpoint_ready", "label": "Endpoint 恢复", "description": "Service 后端出现 Ready EndpointSlice 地址。"},
        {"id": "dependency_reachable", "label": "依赖可达", "description": "DNS、连接和应用探测均能访问关键依赖。"},
        {"id": "error_rate_recovered", "label": "错误率恢复", "description": "错误率回到 SLO 或发布前基线。"},
        {"id": "latency_recovered", "label": "延迟恢复", "description": "P95/P99 回到基线或 SLO 门槛内。"},
        {"id": "cpu_below_threshold", "label": "CPU 回落", "description": "CPU 和 throttling 在观察窗口内回落并稳定。"},
        {"id": "pod_scheduled", "label": "Pod 已调度", "description": "Pod 已绑定健康节点，不再处于 Pending。"},
        {"id": "node_condition_recovered", "label": "节点恢复", "description": "节点 Ready 且压力条件恢复为 False。"},
        {"id": "workloads_rescheduled", "label": "工作负载已迁移", "description": "受影响工作负载已在健康节点恢复。"},
        {"id": "image_pulled", "label": "镜像拉取成功", "description": "镜像已拉取且不再出现 ImagePullBackOff。"},
        {"id": "config_ref_exists", "label": "配置引用恢复", "description": "所需 ConfigMap/Key 已存在并被正确挂载。"},
        {"id": "business_probe_ok", "label": "业务探测通过", "description": "业务健康检查或关键接口验证通过。"},
        {"id": "tls_handshake_ok", "label": "TLS 恢复", "description": "证书链和 TLS 握手验证通过。"},
        {"id": "replica_budget_safe", "label": "副本预算安全", "description": "可用副本和 PDB 约束满足生产安全范围。"},
        {"id": "db_connection_recovered", "label": "数据库连接恢复", "description": "连接成功率、连接池水位和错误日志恢复到基线。"},
        {"id": "db_replication_caught_up", "label": "复制追平", "description": "复制延迟回到阈值内，主备角色和只读状态正确。"},
        {"id": "db_slow_query_reduced", "label": "慢 SQL 回落", "description": "慢 SQL 数量、P95/P99 查询耗时和锁等待明显下降。"},
        {"id": "db_backup_healthy", "label": "备份健康", "description": "最新备份成功，恢复点满足生产策略。"},
        {"id": "vm_agent_online", "label": "主机 Agent 在线", "description": "监控/执行 Agent 已恢复在线并能回传状态。"},
        {"id": "vm_service_active", "label": "主机服务恢复", "description": "目标服务 active/running，端口和业务探测通过。"},
        {"id": "vm_disk_pressure_relieved", "label": "磁盘压力解除", "description": "磁盘、inode、只读状态和增长趋势回到安全范围。"},
        {"id": "vm_probe_healthy", "label": "主机探测健康", "description": "ICMP/TCP/HTTP 或 Agent 探测恢复正常。"},
    ],
    "script_triggers": [
        {"id": "symptom_matched", "label": "症状精确命中", "description": "告警、日志或事件命中 Skill 配置的症状关键词。"},
        {"id": "required_evidence_collected", "label": "必要证据已齐", "description": "必须先采集本 Skill 选择的全部必要证据。"},
        {"id": "root_cause_confirmed", "label": "根因已确认", "description": "证据评分达到确认阈值，不允许只凭用户描述触发。"},
        {"id": "severity_p0_p1", "label": "仅 P0/P1", "description": "只在高严重级故障时允许进入脚本候选。"},
        {"id": "repeated_failure", "label": "重复失败后触发", "description": "同一目标在观察窗口内重复失败或替代策略仍未恢复。"},
        {"id": "target_in_scope", "label": "目标在授权范围", "description": "目标集群、Namespace 和资源类型必须位于授权范围。"},
        {"id": "dry_run_passed", "label": "预演通过", "description": "脚本或等价变更预演通过安全检查后才允许执行。"},
        {"id": "manual_confirmation", "label": "必须人工确认", "description": "由运维人员查看证据、影响范围和参数后点击确认。"},
    ],
}


def skill_option_catalog() -> dict[str, list[dict[str, str]]]:
    return deepcopy(SKILL_OPTION_CATALOG)


def approved_script_catalog() -> list[dict[str, Any]]:
    """读取 ConfigMap 注入的企业批准脚本目录，不接收脚本正文。"""
    try:
        raw = json.loads(os.getenv("OPS_APPROVED_SCRIPTS_JSON", "[]") or "[]")
    except json.JSONDecodeError:
        return []
    items = raw if isinstance(raw, list) else []
    approved = []
    for item in items:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            continue
        approved.append({
            "id": str(item["id"]).strip(),
            "name": str(item.get("name") or item["id"]).strip(),
            "description": _clip(item.get("description") or "", 800),
            "risk": str(item.get("risk") or "high").lower(),
            "runner": str(item.get("runner") or "enterprise-script-runner").strip(),
            "allowed_targets": [str(x).strip() for x in item.get("allowed_targets") or [] if str(x).strip()],
            "required_evidence": [str(x).strip() for x in item.get("required_evidence") or [] if str(x).strip()],
            "enabled": bool(item.get("enabled", True)),
            "lifecycle": str(item.get("lifecycle") or ("published" if item.get("enabled", True) else "candidate")),
        })
    return approved


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokenize(value: Any) -> set[str]:
    text = json.dumps(value, ensure_ascii=False, default=str).lower() if not isinstance(value, str) else value.lower()
    return {item for item in re.split(r"[^a-z0-9\u4e00-\u9fff._/-]+", text) if len(item) >= 2}


def _clip(value: Any, limit: int = 2200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


class OpsSkillRegistry:
    """线程安全的 Skill 注册表，以 Agent Skills 标准目录作为事实来源。"""

    def __init__(self, path: Path, legacy_path: Path | None = None):
        # 兼容早期 ``OpsSkillRegistry(skills.json)`` 调用；新部署应直接传目录。
        if path.suffix.lower() == ".json":
            self.root = path.with_name(f"{path.stem}.d")
            self.legacy_path = legacy_path or path
        else:
            self.root = path
            self.legacy_path = legacy_path
        self.path = self.root
        self._lock = threading.RLock()
        self._skills: dict[str, dict[str, Any]] = {}
        self._writable = True
        self._load_errors: list[str] = []
        self._load()

    def _load(self):
        with self._lock:
            builtin_by_id = {
                str(item["id"]): deepcopy(item)
                for item in DEFAULT_OPERATOR_SKILLS
            }
            self._skills = {
                item["id"]: self._normalize(deepcopy(item), actor="builtin-loader")
                for item in DEFAULT_OPERATOR_SKILLS
            }
            upgraded_builtin_ids: set[str] = set()
            if self.legacy_path and self.legacy_path.exists():
                try:
                    raw = json.loads(self.legacy_path.read_text(encoding="utf-8"))
                    for item in raw.get("skills", []) if isinstance(raw, dict) else raw:
                        if isinstance(item, dict) and item.get("id"):
                            self._skills[str(item["id"])] = self._normalize(item, actor=item.get("updated_by") or "store")
                except Exception as exc:
                    self._load_errors.append(f"legacy:{type(exc).__name__}:{exc}")
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                for skill_file in sorted(self.root.glob("*/SKILL.md")):
                    try:
                        record = read_package(skill_file.parent)
                        builtin = builtin_by_id.get(str(record.get("id") or ""))
                        if builtin and record.get("builtin"):
                            policy_fields = {
                                "name", "description", "category", "summary", "symptoms", "applies_to",
                                "evidence_required", "diagnostic_steps", "allowed_actions", "success_criteria",
                                "risk", "rollback", "script_policy", "execution_ready",
                            }
                            if any(record.get(key) != builtin.get(key) for key in policy_fields if key in builtin):
                                upgraded_builtin_ids.add(str(record["id"]))
                            # Built-in policy is versioned with the application.
                            # Operators can disable it, but custom changes belong
                            # in a separate Skill id so security upgrades cannot
                            # be shadowed by an old persisted package.
                            record = {
                                **record,
                                **builtin,
                                "enabled": bool(record.get("enabled", True)),
                                "lifecycle": record.get("lifecycle") or "published",
                                "created_at": record.get("created_at"),
                                "builtin": True,
                            }
                        self._skills[record["id"]] = self._normalize(record, actor=record.get("updated_by") or "package-loader")
                    except Exception as exc:
                        self._load_errors.append(f"{skill_file.parent.name}:{type(exc).__name__}:{exc}")
                # 首次启动时把内置 Skill 和旧 JSON 原子迁移为标准目录包。
                for skill in self._skills.values():
                    package_dir = self.root / str(skill["id"])
                    if not (package_dir / "SKILL.md").exists() or skill["id"] in upgraded_builtin_ids:
                        write_package(self.root, skill)
            except Exception as exc:
                self._writable = False
                self._load_errors.append(f"store:{type(exc).__name__}:{exc}")

    def _persist(self, skill_ids: list[str] | None = None):
        if not self._writable:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            selected = (
                [self._skills[skill_id] for skill_id in skill_ids if skill_id in self._skills]
                if skill_ids is not None
                else list(self._skills.values())
            )
            for skill in selected:
                package_dir = write_package(self.root, skill)
                skill["package_path"] = str(package_dir)
                skill["portable"] = True
                skill["format"] = AGENT_SKILL_SPEC
        except Exception as exc:
            self._writable = False
            self._load_errors.append(f"persist:{type(exc).__name__}:{exc}")

    @staticmethod
    def _next_version(value: str) -> str:
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(value or ""))
        if not match:
            return "1.0.0"
        major, minor, patch = (int(part) for part in match.groups())
        return f"{major}.{minor}.{patch + 1}"

    def _normalize(self, item: dict[str, Any], *, actor: str) -> dict[str, Any]:
        raw_id = str(item.get("id") or f"ops-{item.get('category') or 'custom'}-{uuid.uuid4().hex[:8]}").strip()
        skill_id = normalize_skill_name(raw_id)
        now = _now()
        raw_script_policy = item.get("script_policy") or {}
        script_policy = {
            "enabled": bool(raw_script_policy.get("enabled", False)),
            "script_id": str(raw_script_policy.get("script_id") or "").strip(),
            "trigger_conditions": [
                str(x).strip() for x in raw_script_policy.get("trigger_conditions") or [] if str(x).strip()
            ],
            "trigger_description": _clip(raw_script_policy.get("trigger_description") or "", 1200),
            "timeout_seconds": max(10, min(600, int(raw_script_policy.get("timeout_seconds") or 120))),
            "require_confirmation": True,
        }
        normalized = {
            "id": skill_id,
            "name": str(item.get("name") or skill_id).strip(),
            "description": _clip(item.get("description") or item.get("summary") or "", 1024),
            "category": str(item.get("category") or "custom").strip(),
            "summary": _clip(item.get("summary") or item.get("description") or ""),
            "instructions": _clip(item.get("instructions") or "", 20000),
            "version": str(item.get("version") or "1.0.0"),
            "symptoms": [str(x).strip() for x in (item.get("symptoms") or item.get("triggers") or []) if str(x).strip()],
            "applies_to": [str(x).strip() for x in (item.get("applies_to") or []) if str(x).strip()],
            "evidence_required": [str(x).strip() for x in (item.get("evidence_required") or []) if str(x).strip()],
            "diagnostic_steps": [str(x).strip() for x in (item.get("diagnostic_steps") or item.get("steps") or []) if str(x).strip()],
            "allowed_actions": [str(x).strip() for x in (item.get("allowed_actions") or []) if str(x).strip()],
            "success_criteria": [str(x).strip() for x in (item.get("success_criteria") or []) if str(x).strip()],
            "risk": str(item.get("risk") or "medium").lower(),
            "rollback": _clip(item.get("rollback") or ""),
            "owner": str(item.get("owner") or actor or "operator").strip(),
            "enabled": bool(item.get("enabled", True)),
            "lifecycle": str(item.get("lifecycle") or ("published" if item.get("enabled", True) else "candidate")),
            "script_policy": script_policy,
            "builtin": bool(item.get("builtin", False)),
            "created_at": item.get("created_at") or now,
            "updated_at": now,
            "updated_by": actor,
            "format": str(item.get("format") or AGENT_SKILL_SPEC),
            "portable": bool(item.get("portable", True)),
            "execution_ready": bool(item.get("execution_ready", bool(item.get("allowed_actions")))),
            "package_path": str(item.get("package_path") or self.root / skill_id),
            "package_files": int(item.get("package_files") or 0),
            "bundled_scripts": [str(x) for x in item.get("bundled_scripts") or []],
            "bundled_scripts_trusted": False,
            "unsupported_actions": [str(x) for x in item.get("unsupported_actions") or []],
            "checksum": str(item.get("checksum") or ""),
        }
        if normalized["risk"] not in {"low", "medium", "high"}:
            normalized["risk"] = "medium"
        if normalized["lifecycle"] not in {"candidate", "published", "disabled"}:
            normalized["lifecycle"] = "published" if normalized["enabled"] else "candidate"
        if normalized["enabled"] and normalized["lifecycle"] == "candidate":
            normalized["lifecycle"] = "published"
        return normalized

    def list(self) -> dict[str, Any]:
        with self._lock:
            skills = sorted(self._skills.values(), key=lambda item: (not item.get("enabled", True), item.get("category", ""), item.get("name", "")))
            return {
                "status": "ok",
                "writable": self._writable,
                "store_path": str(self.root),
                "store_format": AGENT_SKILL_SPEC,
                "load_errors": deepcopy(self._load_errors[-20:]),
                "skills": deepcopy(skills),
                "summary": {
                    "total": len(skills),
                    "enabled": sum(1 for item in skills if item.get("enabled", True)),
                    "builtin": sum(1 for item in skills if item.get("builtin")),
                    "custom": sum(1 for item in skills if not item.get("builtin")),
                    "portable": sum(1 for item in skills if item.get("portable")),
                    "execution_ready": sum(1 for item in skills if item.get("execution_ready")),
                    "candidates": sum(1 for item in skills if item.get("lifecycle") == "candidate"),
                },
            }

    def upsert(self, item: dict[str, Any], *, actor: str) -> dict[str, Any]:
        with self._lock:
            existing = self._skills.get(str(item.get("id") or ""))
            if existing and not item.get("version"):
                item = {**item, "version": self._next_version(existing.get("version", "1.0.0"))}
            normalized = self._normalize(item, actor=actor)
            if not normalized["symptoms"] and not normalized["summary"]:
                raise ValueError("Skill 至少需要 summary 或 symptoms，AI 才能判断适用场景")
            if not normalized["diagnostic_steps"]:
                raise ValueError("Skill 至少需要一个 diagnostic_steps，避免只沉淀口号")
            if not normalized["allowed_actions"]:
                raise ValueError("Skill 至少需要声明 allowed_actions，且必须映射到平台动作目录")
            script_policy = normalized["script_policy"]
            if script_policy["enabled"]:
                if not script_policy["script_id"]:
                    raise ValueError("启用脚本处置时必须选择企业批准脚本 ID")
                if not script_policy["trigger_conditions"]:
                    raise ValueError("启用脚本处置时至少选择一个触发条件")
                if len(script_policy["trigger_description"].strip()) < 8:
                    raise ValueError("请用至少 8 个字符说明脚本在什么具体故障场景下可以触发")
            self._skills[normalized["id"]] = normalized
            self._persist([normalized["id"]])
            return deepcopy(normalized)

    def import_packages(
        self,
        filename: str,
        data: bytes,
        *,
        actor: str,
        supported_actions: set[str],
    ) -> list[dict[str, Any]]:
        """导入标准包；未知动作保留在包内，但不会进入本平台执行目录。"""
        with self._lock:
            raw_records = import_archive(self.root, filename, data)
            imported: list[dict[str, Any]] = []
            for raw in raw_records:
                requested_actions = [str(item) for item in raw.get("allowed_actions") or []]
                unknown = sorted(set(requested_actions) - supported_actions)
                raw["unsupported_actions"] = unknown
                raw["allowed_actions"] = [item for item in requested_actions if item in supported_actions]
                raw["execution_ready"] = bool(raw["allowed_actions"]) and not unknown
                raw["builtin"] = False
                raw["updated_by"] = actor
                normalized = self._normalize(raw, actor=actor)
                self._skills[normalized["id"]] = normalized
                imported.append(deepcopy(normalized))
            return imported

    def export_package(self, skill_id: str) -> tuple[str, bytes]:
        with self._lock:
            if skill_id not in self._skills:
                raise AgentSkillPackageError("Skill 不存在")
            package_dir = self.root / normalize_skill_name(skill_id)
            if not (package_dir / "SKILL.md").exists():
                write_package(self.root, self._skills[skill_id])
            return export_package(self.root, skill_id)

    def delete(self, skill_id: str, *, actor: str) -> dict[str, Any]:
        with self._lock:
            skill = self._skills.get(skill_id)
            if not skill:
                return {"status": "not_found", "id": skill_id}
            if skill.get("builtin"):
                skill["enabled"] = False
                skill["updated_at"] = _now()
                skill["updated_by"] = actor
                status = "disabled"
                self._persist([skill_id])
            else:
                self._skills.pop(skill_id, None)
                delete_package(self.root, skill_id)
                status = "deleted"
            return {"status": status, "id": skill_id}

    def match(self, payload: dict[str, Any], *, top_k: int = 5) -> dict[str, Any]:
        query_tokens = _tokenize(payload)
        matches: list[dict[str, Any]] = []
        with self._lock:
            for skill in self._skills.values():
                if not skill.get("enabled", True):
                    continue
                skill_tokens = _tokenize({
                    "name": skill.get("name"),
                    "summary": skill.get("summary"),
                    "symptoms": skill.get("symptoms"),
                    "applies_to": skill.get("applies_to"),
                    "evidence_required": skill.get("evidence_required"),
                    "category": skill.get("category"),
                    "script_policy": skill.get("script_policy"),
                    "instructions": skill.get("instructions"),
                })
                hits = sorted(query_tokens & skill_tokens)
                symptom_hits = sorted(_tokenize(skill.get("symptoms")) & query_tokens)
                evidence_hits = sorted(_tokenize(skill.get("evidence_required")) & query_tokens)
                score = len(hits) * 0.12 + len(symptom_hits) * 0.24 + len(evidence_hits) * 0.16
                if skill.get("category") and str(skill.get("category")).lower() in query_tokens:
                    score += 0.15
                if score <= 0:
                    continue
                confidence = min(0.98, round(0.18 + score, 3))
                matches.append({
                    "skill": deepcopy(skill),
                    "score": round(score, 3),
                    "confidence": confidence,
                    "matched_terms": hits[:12],
                    "matched_symptoms": symptom_hits[:8],
                    "matched_evidence": evidence_hits[:8],
                    "why": f"命中 {len(hits)} 个语义词、{len(symptom_hits)} 个症状词、{len(evidence_hits)} 个证据词。",
                })
        matches.sort(key=lambda item: (-item["score"], item["skill"]["id"]))
        return {
            "status": "ok",
            "matches": matches[: max(1, min(20, top_k))],
            "query_terms": sorted(query_tokens)[:80],
            "policy": "Skill 只增强诊断和计划生成，不直接扩大 Kubernetes 写权限。",
        }

    def steps_from_matches(self, matches: list[dict[str, Any]], *, limit: int = 2) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        for match in matches[:limit]:
            skill = match.get("skill") or {}
            for index, text in enumerate(skill.get("diagnostic_steps") or []):
                steps.append({
                    "id": f"skill:{skill.get('id')}:{index}",
                    "title": f"运维 Skill：{skill.get('name')}",
                    "description": text,
                    "status": "pending",
                    "skill_id": skill.get("id"),
                })
        return steps

    def agent_context(self, payload: dict[str, Any], *, top_k: int = 3, max_chars: int = 12000) -> list[dict[str, Any]]:
        """按渐进披露原则返回匹配 Skill 的指令正文，供诊断智能体按需注入。"""
        result = self.match(payload, top_k=top_k)
        context: list[dict[str, Any]] = []
        remaining = max_chars
        for match in result.get("matches") or []:
            if float(match.get("confidence") or 0) < 0.28 or remaining <= 0:
                continue
            skill = match.get("skill") or {}
            instructions = str(skill.get("instructions") or "")
            if not instructions:
                try:
                    instructions = read_package(self.root / str(skill.get("id")))["instructions"]
                except Exception:
                    instructions = ""
            instructions = instructions[:remaining]
            remaining -= len(instructions)
            context.append({
                "name": skill.get("id"),
                "display_name": skill.get("name"),
                "description": skill.get("description") or skill.get("summary"),
                "version": skill.get("version"),
                "instructions": instructions,
                "evidence_required": skill.get("evidence_required") or [],
                "allowed_actions": skill.get("allowed_actions") or [],
                "success_criteria": skill.get("success_criteria") or [],
                "confidence": match.get("confidence"),
                "execution_ready": skill.get("execution_ready"),
            })
        return context
