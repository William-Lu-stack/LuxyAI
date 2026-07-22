"""Evidence-driven remediation planning and approval policy."""
from __future__ import annotations

import math
import os
import re
from copy import deepcopy
from typing import Any


ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "apply_manifest": {
        "risk": "high", "auto_allowed": False, "rollback": "restore the pre-change resource snapshot or delete the newly created object",
        "description": "Create or update a Kubernetes resource from an operator-approved manifest.",
    },
    "patch_resource": {
        "risk": "high", "auto_allowed": False, "rollback": "restore the recorded pre-change resource snapshot",
        "description": "Patch any Kubernetes API resource after showing the exact target and diff.",
    },
    "delete_resource": {
        "risk": "high", "auto_allowed": False, "rollback": "recreate from the recorded pre-delete snapshot when the resource type permits it",
        "description": "Delete one explicitly targeted Kubernetes resource after approval.",
    },
    "run_shell": {
        "risk": "high", "auto_allowed": False, "rollback": "use the command-specific rollback steps shown in the approved plan",
        "description": "Run a displayed shell command inside the Flawless runtime after step approval.",
    },
    "exec_pod": {
        "risk": "high", "auto_allowed": False, "rollback": "use the command-specific rollback steps shown in the approved plan",
        "description": "Run a displayed command in one explicitly targeted Pod/container after step approval.",
    },
    "exec_node": {
        "risk": "high", "auto_allowed": False, "rollback": "use the command-specific rollback steps shown in the approved plan",
        "description": "Run a displayed command on one Kubernetes node through the controlled node executor after step approval.",
    },
    "create_workload": {
        "risk": "high", "auto_allowed": False, "rollback": "delete the newly created workload after approval",
        "description": "Create one validated apps/v1 Deployment, StatefulSet or DaemonSet from the release gate.",
    },
    "patch_workload": {
        "risk": "medium", "auto_allowed": True, "rollback": "restore the previous workload template",
        "description": "Patch Deployment, StatefulSet or DaemonSet pod template/replicas.",
    },
    "restart": {
        "risk": "medium", "auto_allowed": True, "rollback": "not applicable; rollout is convergent",
        "description": "Trigger a controlled rolling restart.",
    },
    "scale_out": {
        "risk": "medium", "auto_allowed": True, "rollback": "restore the previous replica count",
        "description": "Increase workload replicas within the configured cap.",
    },
    "recreate_pod": {
        "risk": "medium", "auto_allowed": True, "rollback": "controller recreates the pod from the unchanged template",
        "description": "Delete one controller-owned unhealthy pod for clean rescheduling.",
    },
    "patch_hpa": {
        "risk": "medium", "auto_allowed": True, "rollback": "restore previous HPA min/max replicas",
        "description": "Adjust HPA bounds without changing its metric semantics.",
    },
    "expand_pvc": {
        "risk": "high", "auto_allowed": False, "rollback": "volume expansion is normally irreversible",
        "description": "Expand a bound PVC when its StorageClass supports expansion.",
    },
    "create_pvc": {
        "risk": "high", "auto_allowed": False, "rollback": "delete the new PVC only after confirming no business data was written",
        "description": "Create a missing PersistentVolumeClaim from workload evidence and approved storage policy.",
    },
    "create_pv": {
        "risk": "high", "auto_allowed": False, "rollback": "delete the PV only after confirming reclaim policy and data safety",
        "description": "Create a statically bound PersistentVolume when the storage backend template is configured.",
    },
    "patch_workload_volume": {
        "risk": "high", "auto_allowed": False, "rollback": "restore the previous workload volume claim reference",
        "description": "Patch a workload volume reference after PVC/PV evidence proves the original claim is wrong.",
    },
    "patch_workload_runtime_security": {
        "risk": "high", "auto_allowed": False, "rollback": "restore the previous pod template security/initContainer section",
        "description": "Patch pod security context or a bounded initContainer when storage ownership evidence proves fsGroup alone is insufficient.",
    },
    "cordon_node": {
        "risk": "high", "auto_allowed": False, "rollback": "uncordon the node",
        "description": "Stop new scheduling on a proven unhealthy node.",
    },
    "evict_pod": {
        "risk": "high", "auto_allowed": False, "rollback": "controller recreates the pod; PDB is honored",
        "description": "Evict a pod through the policy API for node maintenance.",
    },
    "uncordon_node": {
        "risk": "high", "auto_allowed": False, "rollback": "cordon the node again",
        "description": "Return a recovered node to scheduling after condition and capacity verification.",
    },
    "rollback_workload": {
        "risk": "high", "auto_allowed": False, "rollback": "restore the superseded immutable image reference",
        "description": "Patch a workload back to a previously observed immutable image/template revision.",
    },
    "patch_service": {
        "risk": "high", "auto_allowed": False, "rollback": "restore the previous selector and port map",
        "description": "Repair a proven Service selector or port mismatch using a bounded patch.",
    },
    "patch_service_account": {
        "risk": "medium", "auto_allowed": True, "rollback": "remove the injected imagePullSecret from the ServiceAccount",
        "description": "Attach an approved imagePullSecret to a workload ServiceAccount.",
    },
    "create_configmap": {
        "risk": "high", "auto_allowed": False, "rollback": "delete the created ConfigMap after confirming no workload depends on it",
        "description": "Recreate a missing ConfigMap only from an operator-approved template.",
    },
    "patch_pdb": {
        "risk": "high", "auto_allowed": False, "rollback": "restore the previous disruption budget",
        "description": "Repair a PDB deadlock after replica and availability evidence is collected.",
    },
    "db_restart_instance": {
        "risk": "high", "auto_allowed": False, "rollback": "follow the database HA/startup runbook and restore from the pre-change snapshot if needed",
        "description": "Restart a database instance through an approved external executor after connection, role and backup evidence is collected.",
    },
    "db_kill_session": {
        "risk": "high", "auto_allowed": False, "rollback": "application reconnects; keep the killed session evidence for audit",
        "description": "Terminate proven harmful database sessions such as runaway SQL, blocker sessions or abandoned long transactions.",
    },
    "db_expand_storage": {
        "risk": "high", "auto_allowed": False, "rollback": "storage expansion is normally irreversible; validate backup and capacity policy first",
        "description": "Expand database storage through an approved DBA/storage executor.",
    },
    "db_failover": {
        "risk": "high", "auto_allowed": False, "rollback": "execute the approved HA rollback or rejoin procedure",
        "description": "Perform a controlled database failover only when replication, role and application impact evidence supports it.",
    },
    "db_apply_parameter": {
        "risk": "high", "auto_allowed": False, "rollback": "restore the previous parameter snapshot",
        "description": "Apply a bounded database parameter change through an approved executor.",
    },
    "vm_restart_service": {
        "risk": "medium", "auto_allowed": False, "rollback": "restore the previous service unit/configuration and restart again if needed",
        "description": "Restart a specific unhealthy OS service through an approved host executor.",
    },
    "vm_reboot": {
        "risk": "high", "auto_allowed": False, "rollback": "no in-place rollback; recover from snapshot or HA capacity if reboot fails",
        "description": "Reboot a virtual machine only after redundancy, service impact and console access are verified.",
    },
    "vm_expand_disk": {
        "risk": "high", "auto_allowed": False, "rollback": "disk expansion is normally irreversible; confirm snapshot and filesystem procedure",
        "description": "Expand a VM disk and filesystem through an approved virtualization/OS executor.",
    },
    "vm_run_approved_script": {
        "risk": "high", "auto_allowed": False, "rollback": "use the script-specific rollback instruction recorded in the approved script catalog",
        "description": "Run an enterprise-approved host script; arbitrary shell from the LLM is never accepted.",
    },
    "vm_snapshot": {
        "risk": "medium", "auto_allowed": False, "rollback": "delete the snapshot after the maintenance window or restore from it when approved",
        "description": "Create a VM snapshot before a risky remediation step.",
    },
    "middleware_rebalance": {
        "risk": "high", "auto_allowed": False, "rollback": "follow the middleware-specific rebalance rollback plan",
        "description": "Rebalance middleware traffic or partitions through an approved executor.",
    },
    "storage_expand_volume": {
        "risk": "high", "auto_allowed": False, "rollback": "storage expansion is normally irreversible; confirm pool capacity and snapshot policy",
        "description": "Expand enterprise storage volume through an approved storage executor.",
    },
    "infra_run_approved_action": {
        "risk": "high", "auto_allowed": False, "rollback": "use the external executor returned rollback plan",
        "description": "Generic non-Kubernetes infrastructure action routed to an approved external executor.",
    },
}

ACTION_OPERATOR_GUIDANCE: dict[str, dict[str, str]] = {
    "apply_manifest": {"label": "应用 Kubernetes YAML", "when_to_use": "AI/Skill 已根据真实对象生成完整声明式变更。", "operator_note": "执行前展示完整 YAML 和资源差异，必须人工确认。"},
    "patch_resource": {"label": "修改 Kubernetes 资源", "when_to_use": "需要修改 Workload、ConfigMap、Secret、PV/PVC 或其他 API 资源。", "operator_note": "执行前锁定 apiVersion/kind/namespace/name 并保存原对象。"},
    "delete_resource": {"label": "删除 Kubernetes 资源", "when_to_use": "证据证明目标对象必须删除且影响范围可控。", "operator_note": "高风险，必须显示删除目标和可恢复快照。"},
    "run_shell": {"label": "执行平台 Shell", "when_to_use": "Skill 需要在 Flawless 执行环境运行诊断或处置命令。", "operator_note": "完整命令、超时、风险和回滚方式必须逐步确认。"},
    "exec_pod": {"label": "执行 Pod 命令", "when_to_use": "需要进入明确的 Pod/container 收集证据或执行处置。", "operator_note": "完整命令和目标必须逐步确认并审计 stdout/stderr。"},
    "exec_node": {"label": "执行节点命令", "when_to_use": "必须在指定 Kubernetes 节点主机完成诊断或处置。", "operator_note": "通过受控节点执行器运行，属于高风险逐步确认动作。"},
    "create_workload": {"label": "创建新 Workload", "when_to_use": "发布治理已校验完整 YAML，需要创建新的 Deployment、StatefulSet 或 DaemonSet。", "operator_note": "高风险；创建前必须检查命名空间、镜像、资源、探针、ServiceAccount 和回滚方式。"},
    "patch_workload": {"label": "修改 Workload 配置", "when_to_use": "证据确认 Deployment/StatefulSet/DaemonSet 的镜像、探针、资源、副本、环境变量或安全上下文配置有误。", "operator_note": "只修改受控字段，执行前展示差异；可通过恢复原模板回滚。"},
    "restart": {"label": "滚动重启组件", "when_to_use": "配置已正确但进程卡死、连接未刷新或需要让 Workload 重新拉起 Pod。", "operator_note": "不会修复错误配置；必须先确认有足够副本和 PDB 允许滚动。"},
    "scale_out": {"label": "增加副本", "when_to_use": "CPU、并发或流量证据显示容量不足，且应用支持水平扩展。", "operator_note": "只在副本上限内扩容，并观察下游依赖和资源配额。"},
    "recreate_pod": {"label": "重建异常 Pod", "when_to_use": "单个 controller 管理的 Pod 状态损坏，而 Workload 模板和其他副本正常。", "operator_note": "删除后由控制器按原模板重建；不适合模板级、存储级或全副本故障。"},
    "patch_hpa": {"label": "调整 HPA 范围", "when_to_use": "HPA 上下限阻止合理扩缩容，且指标语义和数据源正常。", "operator_note": "不会修改指标算法，只调整 min/max replicas。"},
    "expand_pvc": {"label": "扩容 PVC", "when_to_use": "卷使用率接近上限，StorageClass 明确支持扩容。", "operator_note": "通常不可逆；必须核对文件系统扩容支持和业务备份。"},
    "create_pvc": {"label": "创建缺失 PVC", "when_to_use": "Workload 明确引用不存在的 PVC，且存储策略、容量和访问模式已确认。", "operator_note": "高风险；只能按批准的 StorageClass 或模板创建。"},
    "create_pv": {"label": "创建静态 PV", "when_to_use": "动态供卷不可用，且存储管理员已提供批准的后端路径和绑定信息。", "operator_note": "高风险；严禁由 LLM 编造 NFS、LUN 或目录路径。"},
    "patch_workload_volume": {"label": "修正卷引用", "when_to_use": "证据证明 Workload 引用了错误的 PVC、volume 或 mount 配置。", "operator_note": "高风险；需要完整存储链证据和原配置回滚点。"},
    "patch_workload_runtime_security": {"label": "修复运行时权限", "when_to_use": "日志/事件证明挂载目录所有权导致容器无法写入，且 fsGroup 方案不足，需要受控 initContainer 或安全上下文调整。", "operator_note": "高风险；必须逐步确认，只允许有界的 chown/chmod/mkdir 权限修复命令。"},
    "cordon_node": {"label": "隔离节点", "when_to_use": "节点明确存在压力、NotReady、硬件或运行时故障，需要停止新 Pod 调度。", "operator_note": "只停止新调度，不会自动迁移现有 Pod；后续通常配合受控驱逐。"},
    "evict_pod": {"label": "受控驱逐 Pod", "when_to_use": "节点维护或隔离后需要迁移 Pod，并且 PDB 允许中断。", "operator_note": "通过 Eviction API 执行，遵守 PDB；高风险且需人工确认。"},
    "uncordon_node": {"label": "恢复节点调度", "when_to_use": "节点 Ready、压力条件恢复、容量和系统组件验证通过。", "operator_note": "恢复前必须确认故障已解除，避免工作负载重新落到问题节点。"},
    "rollback_workload": {"label": "回滚 Workload", "when_to_use": "最近发布与故障时间线一致，且存在已验证的稳定镜像或模板 revision。", "operator_note": "高风险；只回滚到真实观测过的稳定版本。"},
    "patch_service": {"label": "修正 Service", "when_to_use": "selector、端口或 targetPort 与 Ready Pod 明确不匹配。", "operator_note": "高风险；错误修改会造成流量黑洞，必须先保存原 selector 和端口映射。"},
    "patch_service_account": {"label": "修正 ServiceAccount", "when_to_use": "镜像拉取失败且证据确认缺少企业批准的 imagePullSecret 绑定。", "operator_note": "只允许添加批准 Secret 的引用，不读取或修改 Secret 明文。"},
    "create_configmap": {"label": "恢复 ConfigMap", "when_to_use": "Workload 引用的 ConfigMap 缺失，且平台已有运维人员批准的配置模板。", "operator_note": "高风险；不能让 LLM 自行生成生产配置值。"},
    "patch_pdb": {"label": "修正 PDB", "when_to_use": "PDB 与副本数配置形成发布或驱逐死锁，并且可用性证据充分。", "operator_note": "高风险；修改期间必须持续观察可用副本和业务 SLO。"},
    "db_restart_instance": {"label": "重启数据库实例", "when_to_use": "连接、角色、备份和业务影响证据齐全，且故障明确需要实例级恢复。", "operator_note": "高风险；必须通过 DBA 执行器，不允许 LLM 直接执行 SQL 或系统命令。"},
    "db_kill_session": {"label": "终止数据库会话", "when_to_use": "证据确认某个会话是锁等待、长事务或资源耗尽的直接根因。", "operator_note": "高风险；必须展示会话来源、SQL 摘要、阻塞链和业务影响。"},
    "db_expand_storage": {"label": "扩容数据库存储", "when_to_use": "表空间或磁盘容量接近上限，备份和存储策略已确认。", "operator_note": "通常不可逆；需要 DBA/存储管理员确认。"},
    "db_failover": {"label": "数据库主备切换", "when_to_use": "主库不可用或延迟/错误已达到 HA 预案阈值，且从库状态满足接管条件。", "operator_note": "极高风险；必须二次确认和业务窗口/回切方案。"},
    "db_apply_parameter": {"label": "调整数据库参数", "when_to_use": "参数配置被证明确认为锁、连接、内存或复制问题的根因。", "operator_note": "必须保存原参数快照并限制修改范围。"},
    "vm_restart_service": {"label": "重启主机服务", "when_to_use": "单个 OS 服务异常，系统资源和配置证据支持服务级恢复。", "operator_note": "通过受控执行器重启指定服务，不接受任意 shell。"},
    "vm_reboot": {"label": "重启虚拟机", "when_to_use": "主机不可达、内核/驱动/系统级异常，且有业务冗余或维护窗口。", "operator_note": "高风险；需确认快照、控制台、HA 和影响范围。"},
    "vm_expand_disk": {"label": "扩容虚拟机磁盘", "when_to_use": "文件系统容量风险明确，快照、磁盘和分区扩容步骤已确认。", "operator_note": "通常不可逆；必须确认文件系统扩容命令由批准执行器完成。"},
    "vm_run_approved_script": {"label": "执行批准主机脚本", "when_to_use": "企业脚本目录已有对应脚本，证据和触发条件完全满足。", "operator_note": "Skill 只引用脚本 ID，不保存脚本正文。"},
    "vm_snapshot": {"label": "创建虚拟机快照", "when_to_use": "高风险主机变更前需要可回退点。", "operator_note": "快照不是长期备份，需设置清理窗口。"},
    "middleware_rebalance": {"label": "中间件重平衡", "when_to_use": "Kafka/队列/缓存分片存在倾斜、积压或节点异常。", "operator_note": "必须确认客户端影响和回滚策略。"},
    "storage_expand_volume": {"label": "扩容企业存储卷", "when_to_use": "存储池容量、卷容量和业务使用率证据支持扩容。", "operator_note": "需要存储平台执行器和容量审批。"},
    "infra_run_approved_action": {"label": "执行基础设施批准动作", "when_to_use": "资源类型已接入外部执行器，但尚未细分到专用动作。", "operator_note": "必须由执行器返回审计号、结果和回滚提示。"},
}


EXPERT_PROBES = {
    "current_logs", "previous_logs", "events", "workload_spec", "pod_metrics", "node_pressure",
    "node_conditions", "node_capacity", "system_pods", "service_endpoints", "dns", "network_policy",
    "mesh_routes", "dependency_topology", "storage_chain", "node_storage", "csi_status",
    "pod_security_context", "image_pull_secrets", "registry_connectivity", "scheduler_constraints",
    "node_labels", "quota", "pvc_binding", "hpa", "traffic_baseline", "dependency_latency",
    "cni_events", "recent_changes", "pdb_state", "certificate_chain", "webhook_status",
    "config_ref_exists",
}


def _infer_expert_probe(text: str) -> str:
    """把 AI 的自然语言步骤绑定到平台真实可执行的只读探针。"""
    lowered = str(text or "").lower()
    mappings = [
        (("previous", "上一次", "退出日志", "laststate"), "previous_logs"),
        (("event", "事件", "failedscheduling", "failedmount"), "events"),
        (("pvc", "pv", "storageclass", "csi", "存储", "挂载"), "storage_chain"),
        (("service", "endpoint", "selector", "流量入口"), "service_endpoints"),
        (("networkpolicy", "网络策略"), "network_policy"),
        (("dns", "域名解析"), "dns"),
        (("node", "节点", "diskpressure", "memorypressure"), "node_conditions"),
        (("pdb", "disruption"), "pdb_state"),
        (("quota", "limitrange", "配额"), "quota"),
        (("rollout", "revision", "近期变更", "镜像版本", "回滚"), "recent_changes"),
        (("hpa", "扩缩容"), "hpa"),
        (("cmdb", "依赖", "调用链", "kafka"), "dependency_topology"),
        (("registry", "imagepull", "镜像仓库", "拉取"), "registry_connectivity"),
        (("workload", "deployment", "statefulset", "daemonset", "模板", "配置"), "workload_spec"),
        (("log", "日志", "错误栈"), "current_logs"),
    ]
    for terms, probe in mappings:
        if any(term in lowered for term in terms):
            return probe
    return "workload_spec"


def expert_steps_from_diagnosis(diagnosis: dict) -> list[dict[str, Any]]:
    """将本次 LLM 诊断转成可执行、可审计且绑定真实探针的差异化步骤。"""
    steps: list[dict[str, Any]] = []
    for index, raw in enumerate((diagnosis.get("immediate_actions") or [])[:10], start=1):
        if isinstance(raw, dict):
            title = str(raw.get("title") or raw.get("step") or raw.get("action") or f"专家步骤 {index}").strip()
            description = str(raw.get("description") or raw.get("detail") or raw.get("purpose") or title).strip()
            requested_probe = str(raw.get("probe") or "").strip()
            probe = requested_probe if requested_probe in EXPERT_PROBES else _infer_expert_probe(f"{title} {description}")
            decision_rule = str(raw.get("decision_rule") or raw.get("decision") or "根据该步骤返回的真实证据决定下一分支。").strip()
            on_match = str(raw.get("on_match") or raw.get("action_if_matched") or raw.get("next_if_true") or "进入证据支持的最小变更候选。").strip()
            on_miss = str(raw.get("on_miss") or raw.get("action_if_not_matched") or raw.get("next_if_false") or "排除该分支并检查下一候选根因。").strip()
            expected = raw.get("expected_evidence") or raw.get("evidence") or []
        else:
            text = str(raw or "").strip()
            if not text:
                continue
            title, _, detail = text.partition("：")
            if not detail:
                title, _, detail = text.partition(":")
            title = title.strip() or f"专家步骤 {index}"
            description = detail.strip() or text
            probe = _infer_expert_probe(text)
            decision_rule = "根据该步骤返回的真实证据决定下一分支。"
            on_match = "进入证据支持的最小变更候选。"
            on_miss = "排除该分支并检查下一候选根因。"
            expected = []
        if not title:
            continue
        steps.append({
            "id": probe, "sequence": index, "title": title[:120], "description": description[:500],
            "probe": probe, "expected_evidence": expected if isinstance(expected, list) else [str(expected)],
            "decision_rule": decision_rule[:500], "on_match": on_match[:500], "on_miss": on_miss[:500],
            "source": "llm_evidence_expert", "status": "pending",
        })
    return steps


RUNBOOKS: dict[str, dict[str, Any]] = {
    "oom": {
        "title": "OOM / memory pressure recovery",
        "terms": ("oomkilled", "out of memory", "exit code 137", "内存溢出", "内存不足"),
        "diagnostics": ("previous_logs", "workload_spec", "pod_metrics", "node_pressure"),
        "success": ("pod_ready", "restart_count_stable", "oom_absent"),
    },
    "probe": {
        "title": "Probe and slow-start recovery",
        "terms": ("liveness", "readiness", "startup probe", "probe failed", "connection refused", "context deadline exceeded", "探针"),
        "diagnostics": ("current_logs", "previous_logs", "workload_spec", "service_endpoints"),
        "success": ("pod_ready", "endpoint_ready", "probe_failures_absent"),
    },
    "storage_permission": {
        "title": "Volume permission recovery",
        "terms": (
            "permission denied", "operation not permitted", "read-only file system",
            "can't create directory", "cannot create directory", "mkdir:",
            "权限不足", "目录权限", "无法创建目录",
        ),
        "diagnostics": ("previous_logs", "storage_chain", "workload_spec", "pod_security_context"),
        "success": ("mount_events_absent", "pod_ready", "write_errors_absent"),
    },
    "storage_mount": {
        "title": "PVC / mount recovery",
        "terms": (
            "failedmount", "failedattachvolume", "mountvolume", "persistentvolumeclaim", "pvc", "挂载失败",
            "unbound immediate persistentvolumeclaims", "no persistent volumes available", "volume binding",
        ),
        "diagnostics": ("storage_chain", "events", "node_storage", "csi_status"),
        "success": ("pvc_bound", "mount_events_absent", "pod_ready"),
    },
    "image_auth": {
        "title": "Image registry authentication recovery",
        "terms": ("imagepullbackoff", "errimagepull", "unauthorized", "authentication required", "pull access denied", "镜像拉取"),
        "diagnostics": ("events", "workload_spec", "image_pull_secrets", "registry_connectivity"),
        "success": ("image_pulled", "pod_ready"),
    },
    "image_architecture": {
        "title": "Image architecture or runtime mismatch recovery",
        "terms": (
            "exec format error", "standard_init_linux.go", "cannot execute binary file",
            "no matching manifest for linux/amd64", "no matching manifest for linux/arm64",
            "image architecture", "platform mismatch", "镜像架构", "架构不匹配", "amd64", "arm64",
        ),
        "diagnostics": ("events", "previous_logs", "workload_spec", "node_labels", "recent_changes", "registry_connectivity"),
        "success": ("image_platform_matches_node", "pod_ready", "restart_count_stable"),
    },
    "config_missing": {
        "title": "Missing ConfigMap / configuration reference recovery",
        "terms": ("configmap", "not found", "couldn't find key", "optional: false", "configmap not found", "配置不存在", "配置缺失"),
        "diagnostics": ("events", "workload_spec", "recent_changes"),
        "success": ("config_ref_exists", "pod_ready", "restart_count_stable"),
    },
    "scheduling_capacity": {
        "title": "Scheduling and capacity recovery",
        "terms": ("failedscheduling", "insufficient cpu", "insufficient memory", "unschedulable", "无法调度", "资源不足"),
        "diagnostics": ("events", "scheduler_constraints", "node_capacity", "quota", "pvc_binding"),
        "success": ("pod_scheduled", "pod_ready"),
    },
    "scheduling_constraints": {
        "title": "Affinity, taint and topology recovery",
        "terms": ("taint", "toleration", "node affinity", "pod affinity", "topology spread", "亲和性", "污点"),
        "diagnostics": ("scheduler_constraints", "node_labels", "events", "workload_spec"),
        "success": ("pod_scheduled", "constraint_satisfied"),
    },
    "network_service": {
        "title": "Service discovery and endpoint recovery",
        "terms": ("no endpoints", "connection refused", "no route to host", "service unavailable", "endpoint", "服务发现"),
        "diagnostics": ("service_endpoints", "dns", "network_policy", "mesh_routes", "dependency_topology"),
        "success": ("endpoint_ready", "dependency_reachable", "error_rate_recovered"),
    },
    "dns_cni": {
        "title": "DNS / CNI recovery",
        "terms": ("dns", "coredns", "cni", "networkplugin", "failedcreatepodsandbox", "i/o timeout", "解析失败"),
        "diagnostics": ("dns", "cni_events", "node_conditions", "network_policy"),
        "success": ("dns_resolves", "pod_sandbox_ready", "pod_ready"),
    },
    "cpu_saturation": {
        "title": "CPU saturation recovery",
        "terms": ("highcpu", "high cpu", "cpu usage", "cpu thrott", "高 cpu", "高cpu"),
        "diagnostics": ("pod_metrics", "hpa", "workload_spec", "traffic_baseline", "dependency_latency"),
        "success": ("cpu_below_threshold", "latency_recovered", "error_rate_recovered"),
    },
    "node_pressure": {
        "title": "Node pressure containment",
        "terms": ("diskpressure", "memorypressure", "pidpressure", "notready", "node pressure", "节点压力"),
        "diagnostics": ("node_conditions", "node_capacity", "system_pods", "events"),
        "success": ("node_condition_recovered", "workloads_rescheduled"),
    },
    "crash_unknown": {
        "title": "CrashLoop evidence deep dive",
        "terms": ("crashloopbackoff", "back-off restarting", "crashloop", "反复重启", "容器崩溃"),
        "diagnostics": ("current_logs", "previous_logs", "events", "workload_spec", "recent_changes", "dependency_topology"),
        "success": ("pod_ready", "restart_count_stable", "events_no_new_backoff"),
    },
    "rollout_regression": {
        "title": "Recent rollout regression recovery",
        "terms": ("progressdeadlineexceeded", "rollout", "revision", "new replicaset", "发布后", "变更后"),
        "diagnostics": ("recent_changes", "workload_spec", "previous_logs", "events", "dependency_topology"),
        "success": ("rollout_complete", "pod_ready", "restart_count_stable", "events_no_new_backoff"),
    },
    "service_selector": {
        "title": "Service selector / EndpointSlice repair",
        "terms": ("no endpoints", "endpointslice", "selector mismatch", "503 service unavailable", "服务无端点"),
        "diagnostics": ("service_endpoints", "workload_spec", "network_policy", "dependency_topology"),
        "success": ("endpoint_ready", "dependency_reachable", "error_rate_recovered"),
    },
    "pdb_deadlock": {
        "title": "PDB and rollout deadlock recovery",
        "terms": ("disruptionbudget", "pdb", "cannot evict pod", "too many unavailable", "驱逐失败"),
        "diagnostics": ("pdb_state", "workload_spec", "events", "node_conditions"),
        "success": ("eviction_allowed", "rollout_complete", "replica_budget_safe"),
    },
    "quota_limit": {
        "title": "Quota / LimitRange admission recovery",
        "terms": ("exceeded quota", "resourcequota", "limitrange", "forbidden: exceeded", "配额不足"),
        "diagnostics": ("quota", "workload_spec", "node_capacity", "events"),
        "success": ("admission_allowed", "pod_scheduled", "pod_ready"),
    },
    "certificate_expiry": {
        "title": "Certificate and webhook trust recovery",
        "terms": ("x509", "certificate has expired", "tls handshake", "webhook", "证书过期"),
        "diagnostics": ("current_logs", "events", "certificate_chain", "webhook_status", "dependency_topology"),
        "success": ("tls_handshake_ok", "webhook_available", "error_rate_recovered"),
    },
}


def action_catalog_payload() -> list[dict[str, Any]]:
    return [
        {"id": key, **deepcopy(value), **deepcopy(ACTION_OPERATOR_GUIDANCE.get(key, {}))}
        for key, value in ACTION_CATALOG.items()
    ]


def _flatten_text(*values: Any) -> str:
    parts: list[str] = []

    def visit(value: Any, depth: int = 0):
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() not in {"token", "secret", "password", "authorization"}:
                    visit(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value[:80]:
                visit(item, depth + 1)
        elif value is not None:
            parts.append(str(value))

    for value in values:
        visit(value)
    return " ".join(parts).lower()


def score_root_causes(alert: dict, diagnosis: dict, context: dict) -> list[dict[str, Any]]:
    """Rank runbooks with a transparent weighted evidence score.

    Alert/user text is weak evidence, status/Events are medium evidence, and
    logs/last termination details are strong evidence. The sigmoid keeps the
    reported confidence stable as the amount of evidence grows.
    """
    sources = [
        ("alert", _flatten_text(alert), 0.7),
        ("diagnosis", _flatten_text(diagnosis.get("root_cause"), diagnosis.get("signals")), 0.9),
        ("pod_status", _flatten_text(context.get("pods"), context.get("pod")), 1.25),
        ("events", _flatten_text(context.get("events")), 1.45),
        ("logs", _flatten_text(context.get("logs"), context.get("previous_logs"), context.get("diagnostics")), 1.8),
    ]
    decisive_text = _flatten_text(
        alert,
        diagnosis,
        context.get("events"),
        context.get("storage"),
        context.get("pod"),
        context.get("pods"),
        context.get("logs"),
        context.get("previous_logs"),
    )
    storage_binding_proven = any(term in decisive_text for term in (
        "unbound immediate persistentvolumeclaims",
        "no persistent volumes available",
        "persistentvolumeclaim is not bound",
        "persistentvolumeclaim pending",
    ))
    oom_proven = any(term in decisive_text for term in (
        "oomkilled",
        "exit code 137",
        "exitcode': 137",
        '"exitcode": 137',
        "last_terminated_reason': 'oomkilled",
        '"last_terminated_reason": "oomkilled',
    ))
    image_arch_proven = any(term in decisive_text for term in (
        "exec format error",
        "no matching manifest for linux/amd64",
        "no matching manifest for linux/arm64",
        "image architecture",
        "platform mismatch",
        "cannot execute binary file",
        "standard_init_linux.go",
    ))
    storage_permission_proven = any(term in decisive_text for term in (
        "permission denied",
        "operation not permitted",
        "read-only file system",
        "can't create directory",
        "cannot create directory",
        "mkdir:",
    ))
    log_unavailable_proven = any(term in decisive_text for term in (
        "current_error",
        "previous_error",
        "previous log unavailable",
        "container not found",
        "pod does not exist",
        "waiting to start",
        "logs unavailable",
    ))
    ranked = []
    for runbook_id, runbook in RUNBOOKS.items():
        matches = []
        raw_score = 0.0
        for source, text, weight in sources:
            hit = [term for term in runbook["terms"] if term in text]
            if hit:
                contribution = weight * min(2.2, 1.0 + 0.28 * (len(hit) - 1))
                raw_score += contribution
                matches.append({"source": source, "terms": hit[:4], "weight": weight})
        # FailedScheduling is only the symptom here. An unbound PVC is a
        # deterministic storage root cause and must outrank generic scheduling.
        if runbook_id == "storage_mount" and storage_binding_proven:
            raw_score += 3.2
            matches.append({
                "source": "kubernetes_storage_state",
                "terms": ["pvc_unbound"],
                "weight": 3.2,
            })
        if runbook_id == "oom" and oom_proven:
            raw_score += 2.8
            matches.append({
                "source": "container_last_state",
                "terms": ["oomkilled_or_exit_137"],
                "weight": 2.8,
            })
        if runbook_id == "crash_unknown" and oom_proven:
            raw_score = max(0.0, raw_score - 1.4)
            matches.append({
                "source": "container_last_state",
                "terms": ["downgraded_by_oom_evidence"],
                "weight": -1.4,
            })
        if runbook_id == "image_architecture" and image_arch_proven:
            raw_score += 2.8
            matches.append({
                "source": "runtime_platform_state",
                "terms": ["exec_format_or_manifest_platform_mismatch"],
                "weight": 2.8,
            })
        if runbook_id == "crash_unknown" and image_arch_proven:
            raw_score = max(0.0, raw_score - 1.2)
            matches.append({
                "source": "runtime_platform_state",
                "terms": ["downgraded_by_architecture_evidence"],
                "weight": -1.2,
            })
        if runbook_id == "storage_permission" and storage_permission_proven:
            raw_score += 2.6
            matches.append({
                "source": "container_logs",
                "terms": ["write_permission_denied"],
                "weight": 2.6,
            })
        if runbook_id == "crash_unknown" and storage_permission_proven:
            raw_score = max(0.0, raw_score - 1.2)
            matches.append({
                "source": "container_logs",
                "terms": ["downgraded_by_storage_permission_evidence"],
                "weight": -1.2,
            })
        if runbook_id == "crash_unknown" and log_unavailable_proven and not storage_binding_proven:
            raw_score += 1.4
            matches.append({
                "source": "log_probe",
                "terms": ["logs_unavailable_need_diagnostic_recreate"],
                "weight": 1.4,
            })
        if matches:
            confidence = 1.0 / (1.0 + math.exp(-(raw_score - 1.65)))
            ranked.append({
                "id": runbook_id,
                "title": runbook["title"],
                "score": round(raw_score, 3),
                "confidence": round(min(0.99, confidence), 3),
                "matched_evidence": matches,
                "diagnostics": list(runbook["diagnostics"]),
                "success_criteria": list(runbook["success"]),
            })
    return sorted(ranked, key=lambda item: (-item["score"], item["id"]))


def _target(alert: dict, diagnosis: dict, context: dict) -> tuple[str, str, str, str, dict]:
    pods = (context.get("pods") or {}).get("pods", []) if isinstance(context.get("pods"), dict) else context.get("pods", [])
    pod = context.get("pod") or (pods[0] if pods else {}) or {}
    workload = pod.get("workload") or {}
    namespace = alert.get("namespace") or pod.get("namespace") or "default"
    workload_type = alert.get("workload_type") or workload.get("kind") or pod.get("workload_kind") or "Deployment"
    workload_name = alert.get("workload_name") or alert.get("deployment") or workload.get("name") or pod.get("workload_name") or ""
    pod_name = alert.get("pod") or pod.get("name") or ""
    return namespace, workload_type, workload_name, pod_name, pod


def _first_container(pod: dict) -> dict:
    containers = pod.get("containers") or []
    return next((item for item in containers if item.get("name")), {})


def _security_group_from_pod(pod: dict) -> int:
    """Prefer the workload runtime group over an existing, possibly wrong fsGroup."""
    for container in pod.get("containers", []) or []:
        sc = container.get("security_context") or container.get("securityContext") or {}
        for key in ("runAsGroup", "run_as_group", "runAsUser", "run_as_user"):
            value = sc.get(key)
            if isinstance(value, int) and value > 0:
                return value
    pod_sc = pod.get("security_context") or pod.get("securityContext") or {}
    for key in ("runAsGroup", "run_as_group", "runAsUser", "run_as_user", "fsGroup", "fs_group"):
        value = pod_sc.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 1000


def _container_patch_base(container_name: str, container: dict) -> dict[str, Any]:
    """Base fields required when using JSON merge patch on containers lists."""
    patch = {"name": container_name}
    image = str((container or {}).get("image") or "").strip()
    if image:
        patch["image"] = image
    return patch


def _startup_probe_patch(container: dict) -> dict[str, Any]:
    """基于现有探针生成合法 startupProbe，避免提交缺少处理器的无效 patch。"""
    handler_keys = ("httpGet", "tcpSocket", "exec", "grpc")
    source = (
        container.get("startupProbe")
        or container.get("startup_probe")
        or container.get("livenessProbe")
        or container.get("liveness_probe")
        or container.get("readinessProbe")
        or container.get("readiness_probe")
        or {}
    )
    if not isinstance(source, dict):
        source = {}
    handler = {key: deepcopy(source[key]) for key in handler_keys if source.get(key)}
    if handler:
        probe: dict[str, Any] = {
            **handler,
            "failureThreshold": max(30, int(source.get("failureThreshold") or source.get("failure_threshold") or 30)),
            "periodSeconds": max(1, int(source.get("periodSeconds") or source.get("period_seconds") or 10)),
        }
        for key in ("timeoutSeconds", "initialDelaySeconds", "successThreshold"):
            if source.get(key) is not None:
                probe[key] = source[key]
        return {"startupProbe": probe}
    return {
        "livenessProbe": {"initialDelaySeconds": max(60, int(os.getenv("AUTO_OPS_PROBE_INITIAL_DELAY_SECONDS", "60")))},
        "readinessProbe": {"initialDelaySeconds": max(30, int(os.getenv("AUTO_OPS_READINESS_INITIAL_DELAY_SECONDS", "30")))},
    }


def _memory_growth(value: str | None) -> str:
    value = str(value or "").strip()
    import re
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(Mi|Gi)", value, re.I)
    if not match:
        return os.getenv("AUTO_OPS_DEFAULT_MEMORY_LIMIT", "1Gi")
    original = float(match.group(1))
    unit = match.group(2)
    amount = original * float(os.getenv("AUTO_OPS_MEMORY_GROWTH_FACTOR", "1.5"))
    # 小规格容器只乘 1.5 往往仍然不够，至少增加 256Mi，避免反复提交无效 patch。
    if unit.lower() == "mi":
        amount = max(amount, original + float(os.getenv("AUTO_OPS_MIN_MEMORY_BUMP_MI", "256")))
    if unit.lower() == "gi":
        amount = min(amount, float(os.getenv("AUTO_OPS_MAX_MEMORY_GI", "8")))
    rendered = str(int(amount)) if amount.is_integer() else str(round(amount, 1))
    return rendered + unit


def _storage_evidence_items(context: dict) -> list[dict[str, Any]]:
    items = context.get("storage") or []
    if isinstance(items, dict):
        items = items.get("items") or items.get("storage") or []
    return [item for item in items if isinstance(item, dict)]


def _log_unavailable_evidence(context: dict) -> bool:
    text = _flatten_text(context.get("logs"), context.get("diagnostics"))
    return any(term in text for term in (
        "current_error",
        "previous_error",
        "previous log unavailable",
        "container not found",
        "not found",
        "pod does not exist",
        "waiting to start",
        "logs unavailable",
    ))


def _template_blocker_evidence(context: dict) -> bool:
    """重建 Pod 前先排除重启无法修好的模板/外部阻断类证据。"""
    text = _flatten_text(
        context.get("events"),
        context.get("storage"),
        context.get("workload"),
        context.get("pod"),
        context.get("logs"),
    )
    blockers = (
        "imagepullbackoff", "errimagepull", "pull access denied", "unauthorized",
        "configmap", "secret not found", "persistentvolumeclaim", "unbound immediate",
        "no persistent volumes available", "failedmount", "failedattachvolume",
        "exceeded quota", "node affinity", "taint", "toleration",
    )
    return any(term in text for term in blockers)


def _first_storage_issue(context: dict) -> dict[str, Any]:
    for item in _storage_evidence_items(context):
        text = _flatten_text(item)
        if (
            item.get("error")
            or str(item.get("pvc_phase") or item.get("phase") or "").lower() in {"pending", "lost"}
            or (item.get("pvc") and not item.get("pv") and not item.get("volume_name"))
            or "not found" in text
            or "no persistent volumes available" in text
        ):
            return item
    return {}


def _storage_quantity(value: Any) -> str:
    text = str(value or "").strip()
    if text and len(text) <= 24:
        return text
    return os.getenv("AUTO_OPS_DEFAULT_PVC_SIZE", "10Gi")


def _pvc_manifest(namespace: str, pvc_name: str, issue: dict[str, Any]) -> dict[str, Any]:
    storage_class = str(issue.get("storage_class") or os.getenv("AUTO_OPS_DEFAULT_STORAGE_CLASS", "")).strip()
    spec: dict[str, Any] = {
        "accessModes": issue.get("access_modes") or [os.getenv("AUTO_OPS_DEFAULT_PVC_ACCESS_MODE", "ReadWriteOnce")],
        "resources": {"requests": {"storage": _storage_quantity(issue.get("requested") or issue.get("storage"))}},
    }
    if storage_class:
        spec["storageClassName"] = storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": pvc_name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "flawless"},
        },
        "spec": spec,
    }


def _static_pv_manifest(namespace: str, pvc_name: str, issue: dict[str, Any]) -> dict[str, Any]:
    raw_template = os.getenv("AUTO_OPS_STATIC_PV_TEMPLATE_JSON", "").strip()
    if raw_template:
        try:
            import json
            manifest = json.loads(raw_template)
            manifest.setdefault("metadata", {}).setdefault("name", f"pv-{namespace}-{pvc_name}")
            manifest.setdefault("spec", {}).setdefault("claimRef", {"namespace": namespace, "name": pvc_name})
            return manifest
        except Exception:
            return {}
    nfs_server = os.getenv("AUTO_OPS_STATIC_PV_NFS_SERVER", "").strip()
    nfs_base = os.getenv("AUTO_OPS_STATIC_PV_NFS_BASE_PATH", "").strip().rstrip("/")
    storage_class = str(issue.get("storage_class") or os.getenv("AUTO_OPS_STATIC_PV_STORAGE_CLASS", "")).strip()
    allow_local = os.getenv("AUTO_OPS_ALLOW_LOCAL_STATIC_PV", "false").lower() in {"1", "true", "yes", "on"}
    local_base = os.getenv("AUTO_OPS_STATIC_PV_LOCAL_BASE_PATH", "").strip().rstrip("/")
    local_node = str(issue.get("node") or os.getenv("AUTO_OPS_STATIC_PV_LOCAL_NODE", "")).strip()
    if allow_local and local_base and local_node:
        spec: dict[str, Any] = {
            "capacity": {"storage": _storage_quantity(issue.get("requested") or issue.get("capacity"))},
            "accessModes": issue.get("access_modes") or [os.getenv("AUTO_OPS_DEFAULT_PVC_ACCESS_MODE", "ReadWriteOnce")],
            "persistentVolumeReclaimPolicy": os.getenv("AUTO_OPS_STATIC_PV_RECLAIM_POLICY", "Retain"),
            "claimRef": {"namespace": namespace, "name": pvc_name},
            "local": {"path": f"{local_base}/{namespace}/{pvc_name}"},
            "nodeAffinity": {
                "required": {
                    "nodeSelectorTerms": [{
                        "matchExpressions": [{
                            "key": "kubernetes.io/hostname",
                            "operator": "In",
                            "values": [local_node],
                        }]
                    }]
                }
            },
        }
        if storage_class:
            spec["storageClassName"] = storage_class
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {
                "name": f"pv-{namespace}-{pvc_name}",
                "labels": {"app.kubernetes.io/managed-by": "flawless", "flawless.io/local-e2e": "true"},
            },
            "spec": spec,
        }
    if not nfs_server or not nfs_base:
        return {}
    spec: dict[str, Any] = {
        "capacity": {"storage": _storage_quantity(issue.get("requested") or issue.get("capacity"))},
        "accessModes": issue.get("access_modes") or [os.getenv("AUTO_OPS_DEFAULT_PVC_ACCESS_MODE", "ReadWriteOnce")],
        "persistentVolumeReclaimPolicy": os.getenv("AUTO_OPS_STATIC_PV_RECLAIM_POLICY", "Retain"),
        "claimRef": {"namespace": namespace, "name": pvc_name},
        "nfs": {"server": nfs_server, "path": f"{nfs_base}/{namespace}/{pvc_name}"},
    }
    if storage_class:
        spec["storageClassName"] = storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {
            "name": f"pv-{namespace}-{pvc_name}",
            "labels": {"app.kubernetes.io/managed-by": "flawless"},
        },
        "spec": spec,
    }


def _load_json_env(name: str) -> Any:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        import json
        return json.loads(raw)
    except Exception:
        return {}


def _extract_missing_configmap(context: dict) -> str:
    text = _flatten_text(context)
    patterns = [
        r'configmap\s+"([^"]+)"\s+not\s+found',
        r"configmap\s+'([^']+)'\s+not\s+found",
        r"configmap\s+([a-z0-9.-]+)\s+not\s+found",
        r"couldn't\s+find\s+key\s+[^ ]+\s+in\s+configmap\s+([a-z0-9.-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).strip()
    for ref in _config_refs_from_context(context).get("config_maps", []):
        if ref and ref.lower() in text and "not found" in text:
            return ref
    return ""


def _config_refs_from_context(context: dict) -> dict[str, list[str]]:
    refs = {"config_maps": [], "secrets": [], "service_accounts": []}

    def add(kind: str, value: Any):
        text = str(value or "").strip()
        if text and text not in refs[kind]:
            refs[kind].append(text)

    def visit_pod_spec(spec: dict):
        add("service_accounts", spec.get("serviceAccountName") or spec.get("service_account"))
        for volume in spec.get("volumes", []) or []:
            add("config_maps", ((volume.get("configMap") or {}).get("name")))
            add("secrets", ((volume.get("secret") or {}).get("secretName")))
        for container in spec.get("containers", []) or []:
            for item in container.get("envFrom", []) or []:
                add("config_maps", ((item.get("configMapRef") or {}).get("name")))
                add("secrets", ((item.get("secretRef") or {}).get("name")))
            for env in container.get("env", []) or []:
                ref = ((env.get("valueFrom") or {}).get("configMapKeyRef") or {})
                add("config_maps", ref.get("name"))
                sref = ((env.get("valueFrom") or {}).get("secretKeyRef") or {})
                add("secrets", sref.get("name"))

    pod = context.get("pod") or {}
    if pod:
        add("service_accounts", pod.get("service_account") or pod.get("serviceAccountName"))
        for volume in pod.get("volumes", []) or []:
            add("config_maps", volume.get("config_map"))
            add("secrets", volume.get("secret"))
    workload = context.get("workload") or {}
    spec = workload.get("spec") or {}
    template_spec = ((spec.get("template") or {}).get("spec") or {})
    if template_spec:
        visit_pod_spec(template_spec)
    diagnostics = context.get("diagnostics") or {}
    for key in ("workload", "pod"):
        value = diagnostics.get(key)
        if isinstance(value, dict):
            visit_pod_spec(((value.get("spec") or {}).get("template") or {}).get("spec") or value.get("spec") or {})
    return refs


def _configmap_manifest_from_template(namespace: str, name: str) -> dict[str, Any]:
    templates = _load_json_env("AUTO_OPS_CONFIGMAP_TEMPLATES_JSON")
    if not isinstance(templates, dict):
        return {}
    candidate = (
        templates.get(f"{namespace}/{name}")
        or templates.get(name)
        or ((templates.get(namespace) or {}).get(name) if isinstance(templates.get(namespace), dict) else None)
    )
    if not isinstance(candidate, dict):
        return {}
    manifest = deepcopy(candidate)
    if "data" in manifest or "binaryData" in manifest:
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/managed-by": "flawless"}},
            **manifest,
        }
    manifest.setdefault("apiVersion", "v1")
    manifest.setdefault("kind", "ConfigMap")
    manifest.setdefault("metadata", {})
    manifest["metadata"]["name"] = name
    manifest["metadata"]["namespace"] = namespace
    manifest["metadata"].setdefault("labels", {})["app.kubernetes.io/managed-by"] = "flawless"
    return manifest


def _workload_service_account(context: dict) -> str:
    refs = _config_refs_from_context(context)
    if refs["service_accounts"]:
        return refs["service_accounts"][0]
    pod = context.get("pod") or {}
    return str(pod.get("service_account") or pod.get("serviceAccountName") or "default").strip() or "default"


def _node_architecture(context: dict) -> str:
    node = context.get("node") or {}
    labels = node.get("labels") or node.get("metadata", {}).get("labels") or {}
    arch = labels.get("kubernetes.io/arch") or labels.get("beta.kubernetes.io/arch")
    text = _flatten_text(context.get("events"), context.get("logs"), context.get("pod"))
    if not arch:
        for candidate in ("amd64", "arm64", "arm", "ppc64le", "s390x"):
            if candidate in text:
                arch = candidate
                break
    return str(arch or "").strip().lower()


def _approved_image_replacement(namespace: str, workload_type: str, workload_name: str, container: dict, context: dict) -> str:
    """Return an operator-approved replacement image for image/platform faults.

    The LLM may identify the failure mode, but image replacement must come from
    release history or an explicit platform mapping.  Supported env shape:

    AUTO_OPS_IMAGE_REPLACEMENTS_JSON='{
      "registry/app:arm64": "registry/app:amd64",
      "prod/Deployment/api": {"amd64": "registry/api:stable-amd64"},
      "prod/api": {"replacement": "registry/api:stable"}
    }'
    """
    image = str(container.get("image") or "").strip()
    if not image:
        return ""
    mappings = _load_json_env("AUTO_OPS_IMAGE_REPLACEMENTS_JSON") or _load_json_env("AUTO_OPS_IMAGE_ROLLBACK_MAP_JSON")
    if not isinstance(mappings, dict):
        return ""
    arch = _node_architecture(context)
    keys = [
        image,
        f"{namespace}/{workload_type}/{workload_name}",
        f"{namespace}/{workload_name}",
        workload_name,
    ]
    for key in keys:
        candidate = mappings.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            for lookup in (arch, "replacement", "stable", "default", "image"):
                value = candidate.get(lookup)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            string_values = [str(value).strip() for value in candidate.values() if isinstance(value, str) and value.strip()]
            unique_values = sorted(set(string_values))
            if len(unique_values) == 1:
                return unique_values[0]
    return ""


def build_remediation_plan(alert: dict, diagnosis: dict, context: dict) -> dict[str, Any]:
    hypotheses = score_root_causes(alert, diagnosis, context)
    namespace, workload_type, workload_name, pod_name, pod = _target(alert, diagnosis, context)
    primary = hypotheses[0] if hypotheses else None
    runbook_id = primary["id"] if primary else "unknown"
    container = _first_container(pod)
    container_name = container.get("name", "")
    changes: list[dict[str, Any]] = []
    patchable = workload_name and str(workload_type).lower() in {"deployment", "statefulset", "daemonset"}
    confidence = float((primary or {}).get("confidence") or 0.0)

    def workload_patch(patch: dict, reason: str):
        changes.append({
            "type": "patch_workload", "namespace": namespace, "workload_type": workload_type,
            "workload_name": workload_name, "patch": patch, "reason": reason,
            "runbook_id": runbook_id, **ACTION_CATALOG["patch_workload"],
        })

    if runbook_id == "storage_mount":
        issue = _first_storage_issue(context)
        issue_text = _flatten_text(issue)
        pvc_name = str(issue.get("pvc") or issue.get("pvc_name") or issue.get("claim") or "").strip()
        missing_pvc = bool(
            pvc_name
            and (
                issue.get("missing") is True
                or "not found" in issue_text
                or "404" in issue_text
                or "does not exist" in issue_text
            )
        )
        phase = str(issue.get("pvc_phase") or issue.get("phase") or "").lower()
        if missing_pvc:
            changes.append({
                "type": "create_pvc", "namespace": namespace, "pvc_name": pvc_name,
                "manifest": _pvc_manifest(namespace, pvc_name, issue),
                "reason": "Kubernetes API 已确认 Pod 引用的 PVC 不存在；创建受存储策略约束的 PVC，并等待动态供卷后验证 Pod。",
                "runbook_id": runbook_id, **ACTION_CATALOG["create_pvc"],
            })
        elif pvc_name and phase in {"pending", "lost"}:
            pv_manifest = _static_pv_manifest(namespace, pvc_name, issue)
            if pv_manifest:
                changes.append({
                    "type": "create_pv", "namespace": namespace, "pvc_name": pvc_name,
                    "manifest": pv_manifest,
                    "reason": "Kubernetes API 已确认 PVC 未绑定，且平台配置了批准的静态存储模板；创建预绑定 PV 并验证 PVC=Bound。",
                    "runbook_id": runbook_id, **ACTION_CATALOG["create_pv"],
                })

    if primary and confidence >= 0.62 and patchable:
        if runbook_id == "oom" and container_name:
            resources = container.get("resources") or {}
            requests, limits = resources.get("requests") or {}, resources.get("limits") or {}
            workload_patch({"spec": {"template": {"spec": {"containers": [{
                **_container_patch_base(container_name, container),
                "resources": {
                    "requests": {"cpu": requests.get("cpu") or "100m", "memory": requests.get("memory") or "256Mi"},
                    "limits": {"cpu": limits.get("cpu") or "1", "memory": _memory_growth(limits.get("memory"))},
                },
            }]}}}}, "OOM 证据达到执行阈值：提高内存上限并保留 CPU 约束，滚动后验证 OOM 是否消失。")
        elif runbook_id == "probe" and container_name:
            workload_patch({"spec": {"template": {"spec": {"containers": [{
                **_container_patch_base(container_name, container), **_startup_probe_patch(container)
            }]}}}}, "探针/慢启动证据达到执行阈值：增加 startupProbe 容错窗口并验证 Endpoint Ready。")
        elif runbook_id == "storage_permission":
            # 权限错误的正确修复可能位于错误的 initContainer、readOnly 挂载、
            # 镜像用户、CSI/NFS 后端或应用路径。确定性引擎只确认根因类别，
            # 具体动作必须由实时证据 + 匹配 Skill 生成，不能固定套 fsGroup。
            changes = []
            evidence_gap = "需要动态 Skill 比较失败路径、运行用户、挂载、Workload 模板和存储后端后生成实际变更。"
        elif runbook_id == "storage_mount" and not changes:
            issue = _first_storage_issue(context)
            issue_text = _flatten_text(issue)
            pvc_name = str(issue.get("pvc") or issue.get("pvc_name") or issue.get("claim") or "").strip()
            if pvc_name and ("not found" in issue_text or "404" in issue_text or issue.get("missing") is True):
                changes.append({
                    "type": "create_pvc", "namespace": namespace, "pvc_name": pvc_name,
                    "manifest": _pvc_manifest(namespace, pvc_name, issue),
                    "reason": "Pod 引用了不存在的 PVC；先创建受策略约束的 PVC，再等待存储插件动态供卷并验证 Pod 挂载。",
                    "runbook_id": runbook_id, **ACTION_CATALOG["create_pvc"],
                })
            elif pvc_name and str(issue.get("pvc_phase") or issue.get("phase") or "").lower() in {"pending", "lost"}:
                pv_manifest = _static_pv_manifest(namespace, pvc_name, issue)
                if pv_manifest:
                    changes.append({
                        "type": "create_pv", "namespace": namespace, "pvc_name": pvc_name,
                        "manifest": pv_manifest,
                        "reason": "PVC 已存在但未绑定 PV，且平台已配置静态 PV 模板；创建预绑定 PV 让 PVC 收敛到 Bound。",
                        "runbook_id": runbook_id, **ACTION_CATALOG["create_pv"],
                    })
        elif runbook_id == "image_auth" and container_name:
            replacement_image = _approved_image_replacement(namespace, workload_type, workload_name, container, context)
            if replacement_image:
                changes.append({
                    "type": "rollback_workload", "namespace": namespace, "workload_type": workload_type,
                    "workload_name": workload_name,
                    "patch": {"spec": {"template": {"spec": {"containers": [{"name": container_name, "image": replacement_image}]}}}},
                    "reason": "镜像拉取失败且命中批准镜像替换映射；优先替换到稳定镜像，再验证 Pod 是否完成拉取和启动。",
                    "runbook_id": runbook_id, **ACTION_CATALOG["rollback_workload"],
                })
            secret_name = os.getenv("DEFAULT_IMAGE_PULL_SECRET", "").strip()
            if secret_name and not changes:
                workload_patch({"spec": {"template": {"spec": {"imagePullSecrets": [{"name": secret_name}]}}}},
                               f"镜像鉴权证据明确：注入平台预配置 imagePullSecret {secret_name}。")
                service_account = _workload_service_account(context)
                if service_account:
                    changes.append({
                        "type": "patch_service_account", "namespace": namespace, "service_account": service_account,
                        "image_pull_secret": secret_name,
                        "patch": {"imagePullSecrets": [{"name": secret_name}]},
                        "reason": f"将平台批准的镜像凭据 {secret_name} 绑定到 ServiceAccount/{service_account}，后续同账号 Pod 也可拉取镜像。",
                        "runbook_id": runbook_id, **ACTION_CATALOG["patch_service_account"],
                    })
        elif runbook_id == "image_architecture" and container_name:
            previous_image = str(
                container.get("previous_image")
                or ((context.get("recent_changes") or {}).get("previous_image") if isinstance(context.get("recent_changes"), dict) else "")
                or _approved_image_replacement(namespace, workload_type, workload_name, container, context)
                or ""
            ).strip()
            if previous_image:
                changes.append({
                    "type": "rollback_workload", "namespace": namespace, "workload_type": workload_type,
                    "workload_name": workload_name,
                    "patch": {"spec": {"template": {"spec": {"containers": [{"name": container_name, "image": previous_image}]}}}},
                    "reason": "证据显示镜像架构或运行时平台与节点不匹配；回滚/替换到发布历史或批准映射中的稳定镜像并验证平台架构匹配。",
                    "runbook_id": runbook_id, **ACTION_CATALOG["rollback_workload"],
                })
        elif runbook_id == "config_missing":
            configmap_name = _extract_missing_configmap(context)
            manifest = _configmap_manifest_from_template(namespace, configmap_name) if configmap_name else {}
            if configmap_name and manifest:
                changes.append({
                    "type": "create_configmap", "namespace": namespace, "configmap_name": configmap_name,
                    "manifest": manifest,
                    "reason": f"Kubernetes 事件确认 ConfigMap/{configmap_name} 缺失，且平台存在批准模板；恢复配置后验证 Pod Ready。",
                    "runbook_id": runbook_id, **ACTION_CATALOG["create_configmap"],
                })
        elif runbook_id == "cpu_saturation":
            current = int((context.get("workload") or {}).get("spec", {}).get("replicas") or 1)
            changes.append({
                "type": "scale_out", "namespace": namespace, "workload_type": workload_type,
                "workload_name": workload_name, "replicas": min(current + 1, int(os.getenv("MAX_PATCH_REPLICAS", "20"))),
                "patch": {"spec": {"replicas": min(current + 1, int(os.getenv("MAX_PATCH_REPLICAS", "20")))}},
                "reason": "CPU 饱和证据达到执行阈值：先扩一副本恢复容量，再验证延迟、错误率和 HPA。",
                "runbook_id": runbook_id, **ACTION_CATALOG["scale_out"],
            })
        elif runbook_id == "rollout_regression" and container_name:
            previous_image = str(
                container.get("previous_image")
                or ((context.get("recent_changes") or {}).get("previous_image") if isinstance(context.get("recent_changes"), dict) else "")
                or ""
            ).strip()
            if previous_image:
                changes.append({
                    "type": "rollback_workload", "namespace": namespace, "workload_type": workload_type,
                    "workload_name": workload_name,
                    "patch": {"spec": {"template": {"spec": {"containers": [{"name": container_name, "image": previous_image}]}}}},
                    "reason": "近期发布与故障时间线强相关，回滚到证据中记录的上一不可变镜像并验证业务 SLI。",
                    "runbook_id": runbook_id, **ACTION_CATALOG["rollback_workload"],
                })
        elif runbook_id == "service_selector":
            service = context.get("service") or {}
            service_name = service.get("name") or ""
            selector = service.get("recommended_selector") or {}
            if service_name and selector:
                changes.append({
                    "type": "patch_service", "namespace": namespace, "service_name": service_name,
                    "selector": selector, "patch": {"spec": {"selector": selector}},
                    "reason": "Service selector 与健康 Workload 标签不一致的证据已确认。",
                    "runbook_id": runbook_id, **ACTION_CATALOG["patch_service"],
                })
        elif runbook_id == "pdb_deadlock":
            pdb = context.get("pdb") or {}
            if pdb.get("name") and (pdb.get("recommended_max_unavailable") is not None):
                changes.append({
                    "type": "patch_pdb", "namespace": namespace, "pdb_name": pdb.get("name"),
                    "patch": {"spec": {"maxUnavailable": pdb.get("recommended_max_unavailable")}},
                    "reason": "PDB 与副本数形成驱逐死锁，候选值已通过可用副本预算计算。",
                    "runbook_id": runbook_id, **ACTION_CATALOG["patch_pdb"],
                })
    if primary and confidence >= 0.72 and not changes and runbook_id == "node_pressure":
        node_name = pod.get("node") or pod.get("node_name") or ""
        if node_name:
            changes.append({
                "type": "cordon_node", "node_name": node_name, "patch": {"spec": {"unschedulable": True}},
                "reason": "节点压力证据较强：先隔离节点避免新 Pod 调度；该动作必须人工审批。",
                "runbook_id": runbook_id, **ACTION_CATALOG["cordon_node"],
            })
    diagnostic_recreate_needed = (
        runbook_id == "crash_unknown"
        and _log_unavailable_evidence(context)
        and not _template_blocker_evidence(context)
    )
    if primary and not changes and pod_name and (
        (confidence >= 0.72 and runbook_id in {"crash_unknown", "dns_cni"})
        or (confidence >= 0.55 and diagnostic_recreate_needed)
    ):
        changes.append({
            "type": "recreate_pod", "namespace": namespace, "pod_name": pod_name,
            "workload_type": workload_type, "workload_name": workload_name,
            "reason": (
                "日志探针不可用且未发现 PVC/镜像/ConfigMap/调度等模板级阻断；"
                "先诊断性重建单个受控制器管理的异常 Pod，随后重新采集 current/previous logs、Events 和 Ready 状态。"
                if diagnostic_recreate_needed else
                "已完成根因取证但未发现模板级安全 patch；重建单个受控制器管理的异常 Pod 以排除节点/沙箱瞬态故障。"
            ),
            "runbook_id": runbook_id, **ACTION_CATALOG["recreate_pod"],
        })

    diagnostics = list((primary or {}).get("diagnostics") or ["current_logs", "previous_logs", "events", "workload_spec", "service_endpoints", "storage_chain"])
    steps = [
        {"id": probe, "title": diagnostic_title(probe), "description": diagnostic_description(probe), "status": "pending"}
        for probe in diagnostics
    ]
    evidence_gap = (
        "需要动态 Skill 比较失败路径、运行用户、挂载、Workload 模板和存储后端后生成实际变更。"
        if runbook_id == "storage_permission" and not changes
        else "" if changes
        else "缺少足够强的根因证据、目标 Workload 状态或可回滚 patch 证据，尚不能证明直接修改模板比继续诊断更安全。"
    )
    if not changes and runbook_id == "storage_mount":
        issue = _first_storage_issue(context)
        pvc_name = str(issue.get("pvc") or issue.get("pvc_name") or issue.get("claim") or "").strip()
        if pvc_name and str(issue.get("pvc_phase") or issue.get("phase") or "").lower() in {"pending", "lost"}:
            evidence_gap = (
                f"PVC {namespace}/{pvc_name} 未绑定到 PV，但平台没有配置 AUTO_OPS_STATIC_PV_TEMPLATE_JSON "
                "或 AUTO_OPS_STATIC_PV_NFS_SERVER/AUTO_OPS_STATIC_PV_NFS_BASE_PATH，因此不能安全创建静态 PV。"
            )
    if not changes and runbook_id == "config_missing":
        configmap_name = _extract_missing_configmap(context)
        if configmap_name:
            evidence_gap = (
                f"ConfigMap {namespace}/{configmap_name} 缺失已被事件命中，但平台没有配置 "
                "AUTO_OPS_CONFIGMAP_TEMPLATES_JSON 中对应模板；请先录入批准配置模板，再执行恢复。"
            )
    if not changes and runbook_id == "image_architecture":
        evidence_gap = (
            "已命中镜像架构/运行时平台不匹配证据，但没有读取到上一稳定镜像或批准镜像映射。"
            "请在发布治理中保留 revision/image digest，或在知识库/Skill 中登记该应用的 amd64/arm64 镜像对应关系。"
        )
    return {
        "engine": "EvidenceRunbookEngine/v1",
        "runbook_id": runbook_id,
        "hypotheses": hypotheses[:5],
        "diagnostic_actions": diagnostics,
        "steps": steps,
        "changes": changes,
        "target": {
            "namespace": namespace, "workload_type": workload_type, "workload_name": workload_name, "pod_name": pod_name,
        },
        "decision": "ready_for_approval" if changes else "evidence_collection_required",
        "evidence_gap": evidence_gap,
        "reason": (
            "存在达到证据阈值且通过动作白名单的修复候选。"
            if changes else
            "当前证据不足以证明某个变更优于其他方案；先执行诊断探针，完成后自动重规划。"
        ),
        "success_criteria": list((primary or {}).get("success_criteria") or ["pod_ready", "business_probe_ok"]),
        "action_catalog": action_catalog_payload(),
    }


def diagnostic_title(probe: str) -> str:
    return {
        "current_logs": "读取当前容器日志", "previous_logs": "读取上一次退出日志", "events": "分析 Kubernetes Events",
        "workload_spec": "核对 Workload 模板", "pod_metrics": "检查 Pod 资源指标", "node_pressure": "检查节点压力",
        "node_conditions": "检查节点健康条件", "node_capacity": "核对节点容量", "system_pods": "检查节点系统组件",
        "service_endpoints": "核对 Service 与 Endpoint", "dns": "验证 DNS 解析链路", "network_policy": "分析 NetworkPolicy",
        "mesh_routes": "检查 Service Mesh 路由", "dependency_topology": "追踪 CMDB 依赖链", "storage_chain": "检查 PVC/PV/StorageClass",
        "node_storage": "检查节点与 CSI 存储", "csi_status": "检查 CSI 组件状态", "pod_security_context": "核对运行用户与卷权限",
        "image_pull_secrets": "核对镜像凭据引用", "registry_connectivity": "验证镜像仓库连通性", "scheduler_constraints": "分析调度约束",
        "node_labels": "核对节点标签", "quota": "检查 ResourceQuota", "pvc_binding": "检查 PVC 绑定", "hpa": "检查 HPA 状态",
        "traffic_baseline": "比对流量基线", "dependency_latency": "分析依赖延迟", "cni_events": "分析 CNI 事件",
        "recent_changes": "关联近期变更", "workload_spec": "核对 Workload 模板", "registry_connectivity": "验证镜像仓库连通性",
        "pdb_state": "检查 PodDisruptionBudget", "certificate_chain": "验证证书链与有效期",
        "webhook_status": "检查准入 Webhook", "hpa": "检查 HPA 状态", "config_ref_exists": "检查配置引用是否存在",
    }.get(probe, probe.replace("_", " ").title())


def diagnostic_description(probe: str) -> str:
    return {
        "current_logs": "读取目标 Pod 当前容器日志并提取错误栈、超时和依赖失败。",
        "previous_logs": "读取 --previous 日志、退出码和 lastState，区分 OOM、崩溃与探针杀死。",
        "events": "按时间线聚合调度、镜像、挂载、探针和沙箱事件。",
        "workload_spec": "读取真实模板，检查镜像、资源、探针、环境变量引用、调度和安全上下文。",
        "service_endpoints": "核对 selector、EndpointSlice 和 Ready endpoint，识别流量黑洞。",
        "storage_chain": "沿 Pod volume -> PVC -> PV -> StorageClass/CSI 检查绑定、容量和权限。",
        "scheduler_constraints": "联查 requests、quota、affinity、taint/toleration 与 topology spread。",
        "dependency_topology": "结合 CMDB/Kafka/数据库调用关系判断上游与下游影响。",
        "recent_changes": "关联 Deployment revision、镜像摘要、ConfigMap 版本和故障开始时间，判断是否需要回滚。",
        "pdb_state": "核对 expectedPods、currentHealthy、disruptionsAllowed 与 Workload 副本数，识别驱逐死锁。",
        "quota": "核对 ResourceQuota、LimitRange、requests/limits 和准入失败事件。",
        "config_ref_exists": "读取 Workload 引用的 ConfigMap/Secret 名称，确认缺失对象和批准恢复模板。",
        "certificate_chain": "读取证书有效期、SAN、issuer 和信任链，不导出私钥。",
        "webhook_status": "检查 Webhook Service、Endpoint、CABundle、failurePolicy 与超时事件。",
    }.get(probe, f"执行 {diagnostic_title(probe)}，将结果作为下一轮根因评分证据。")


def validate_change(change: dict) -> tuple[bool, str]:
    action = str(change.get("type") or "")
    if action not in ACTION_CATALOG:
        return False, f"unsupported remediation action: {action}"
    if ACTION_CATALOG[action]["risk"] == "high" and not change.get("human_approved"):
        return False, f"{action} is high risk and requires explicit human approval"
    return True, ""
