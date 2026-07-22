"""发布治理到运维任务的转换服务。

本模块只负责把已审批发布转换成统一 OpsJob 计划，不直接访问 Kubernetes。
真实变更仍由运维状态机和 MCP/Rancher 执行层完成，因此发布、AI 巡检和
SRE 对话共用同一套门禁、进度、取消与恢复验证能力。
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException


EnqueueOpsJob = Callable[..., Awaitable[dict[str, Any]]]


async def submit_release_job(
    release: dict[str, Any],
    actor: str,
    enqueue_ops_job: EnqueueOpsJob,
) -> dict[str, Any]:
    """将人工批准的发布申请转换成受控运维计划。"""
    patch = copy.deepcopy(release.get("patch") or {})
    if release.get("image") and not release.get("manifest"):
        patch = {
            "spec": {"template": {"spec": {"containers": [{
                "name": release.get("container_name"),
                "image": release.get("image"),
            }]}}},
        }

    cluster_id = str(release.get("cluster") or "local")
    source = "rancher" if cluster_id not in {"local", "local-cluster", ""} else "release-api"
    is_new_workload = release.get("release_mode") == "new"
    is_emergency = release.get("change_channel") == "emergency_recovery"
    emergency_action = str(release.get("emergency_action") or "")

    if is_new_workload:
        release_change = {
            "type": "create_workload",
            "namespace": release.get("namespace") or "default",
            "workload_type": release.get("workload_kind") or "Deployment",
            "workload_name": release.get("workload_name"),
            "manifest": release.get("manifest") or {},
            "reason": f"release={release.get('id')} approved_by={release.get('approved_by', 'unknown')}",
        }
    elif is_emergency and emergency_action == "restart_component":
        release_change = {
            "type": "restart",
            "namespace": release.get("namespace") or "default",
            "workload_type": release.get("workload_kind") or "Deployment",
            "workload_name": release.get("workload_name"),
            "patch": {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "<now>"}}}}},
            "reason": f"emergency restart release={release.get('id')} approved_by={release.get('approved_by', 'unknown')}",
        }
    else:
        release_change = {
            "type": "patch_workload",
            "namespace": release.get("namespace") or "default",
            "workload_type": release.get("workload_kind") or "Deployment",
            "workload_name": release.get("workload_name"),
            "patch": patch,
            "reason": f"release={release.get('id')} approved_by={release.get('approved_by', 'unknown')}",
        }

    if not is_new_workload and not patch and not (is_emergency and emergency_action == "restart_component"):
        raise HTTPException(status_code=422, detail="发布申请缺少 image 或 patch，无法生成 Kubernetes 变更")
    if is_new_workload and not release.get("manifest"):
        raise HTTPException(status_code=422, detail="新建发布缺少经过校验的 manifest")

    plan = {
        "id": f"release-{release.get('id')}",
        "title": f"紧急修复 {release.get('service')}" if is_emergency else f"受控发布 {release.get('service')}",
        "service": release.get("service"),
        "change_class": "emergency_recovery" if is_emergency else "application_release",
        "cluster": cluster_id,
        "cluster_id": cluster_id,
        "namespace": release.get("namespace") or "default",
        "source": source,
        "target": f"{release.get('workload_kind')}/{release.get('workload_name')}",
        "summary": (
            release.get("emergency_reason") or release.get("change_summary") or "恢复稳定性的紧急修复变更。"
            if is_emergency else
            release.get("change_summary") or "通过 SLO 错误预算和变更风险门禁的应用发布。"
        ),
        "steps": [
            {"id": "workload_spec", "title": "读取发布前状态", "description": "记录当前镜像、Workload generation、Pod Ready 和 Events。"},
            {
                "id": "release_gate",
                "title": "复核 SLO 与紧急通道",
                "description": "确认该动作只用于恢复稳定性，并保存错误预算豁免与审批证据。" if is_emergency else "执行前重新计算预算，防止审批后稳定性恶化。",
            },
            {"id": "dependency_topology", "title": "核对拓扑影响", "description": "保存关键依赖和爆炸半径证据。"},
        ],
        "changes": [release_change],
        "success_criteria": ["新 Pod Ready", "Workload rollout 完成", "错误率和延迟未突破 SLO 门槛"],
        "requires_confirmation": True,
        "high_risk_confirmed": bool(release.get("approved_by")),
        "release_id": release.get("id"),
        "release_gate_snapshot": release.get("gate"),
        "emergency_audit": release.get("emergency_audit") or {},
    }
    return await enqueue_ops_job(plan, actor, autonomous=False, confirmed=True)

