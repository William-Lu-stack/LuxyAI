import os
import uuid
from fastapi import FastAPI
from pydantic import BaseModel

from a2a.protocol import A2AMessage, A2AResponse
from mcp_servers.k8s_mcp_server import (
    cordon_node,
    create_persistent_volume,
    create_pvc,
    expand_pvc,
    evict_pod,
    patch_hpa,
    patch_workload,
    recreate_pod,
    restart_deployment,
    scale_deployment,
)
from agents.remediation_engine import ACTION_CATALOG, validate_change

app = FastAPI(title="Healing Agent")

AUTO_HEALING_ENABLED = os.getenv("AUTO_HEALING_ENABLED", "false").lower() == "true"
DEFAULT_SCALE_DELTA = int(os.getenv("HEALING_SCALE_DELTA", "1"))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "component": "healing-agent",
        "auto_healing_enabled": AUTO_HEALING_ENABLED,
        "mutation_mode": "dry-run-only; real writes require OpsJob per-change approval",
    }


class HealingPlan(BaseModel):
    namespace: str
    workload_type: str
    workload_name: str
    action: str
    reason: str
    dry_run: bool = True
    replicas: int | None = None
    patch: dict | None = None
    changes: list[dict] = []
    human_approved: bool = False


def _execute_structured_change(change: dict, default_namespace: str, dry_run: bool) -> dict:
    action = str(change.get("type") or "")
    namespace = change.get("namespace") or default_namespace
    checked = dict(change)
    if action in ACTION_CATALOG and ACTION_CATALOG[action].get("risk") == "high" and change.get("human_approved"):
        checked["human_approved"] = True
    valid, reason = validate_change(checked)
    if not valid:
        return {"action": action, "status": "blocked", "error": reason}
    if action == "patch_workload":
        result = patch_workload(
            namespace=namespace,
            workload_type=change.get("workload_type", "Deployment"),
            workload_name=change.get("workload_name", ""),
            patch=change.get("patch") or {},
            dry_run=dry_run,
        )
    elif action == "restart":
        workload_type = str(change.get("workload_type") or "Deployment")
        if workload_type.lower() == "deployment":
            result = restart_deployment(namespace, change.get("workload_name", ""), dry_run)
        else:
            result = patch_workload(
                namespace, workload_type, change.get("workload_name", ""),
                change.get("patch") or {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "manual"}}}}},
                dry_run,
            )
    elif action == "scale_out":
        result = scale_deployment(namespace, change.get("workload_name", ""), int(change.get("replicas") or 2), dry_run)
    elif action == "recreate_pod":
        result = recreate_pod(namespace, change.get("pod_name", ""), dry_run, int(change.get("grace_period_seconds") or 30))
    elif action == "evict_pod":
        result = evict_pod(namespace, change.get("pod_name", ""), dry_run, int(change.get("grace_period_seconds") or 30))
    elif action == "patch_hpa":
        result = patch_hpa(namespace, change.get("hpa_name", ""), change.get("min_replicas"), change.get("max_replicas"), dry_run)
    elif action == "expand_pvc":
        result = expand_pvc(namespace, change.get("pvc_name", ""), change.get("storage", ""), dry_run)
    elif action == "create_pvc":
        result = create_pvc(namespace, change.get("manifest") or {}, dry_run)
    elif action == "create_pv":
        result = create_persistent_volume(change.get("manifest") or {}, dry_run)
    elif action == "patch_workload_volume":
        result = patch_workload(
            namespace=namespace,
            workload_type=change.get("workload_type", "Deployment"),
            workload_name=change.get("workload_name", ""),
            patch=change.get("patch") or {},
            dry_run=dry_run,
            high_risk_volume_patch=True,
        )
    elif action == "cordon_node":
        result = cordon_node(change.get("node_name", ""), bool(change.get("unschedulable", True)), dry_run)
    else:
        result = {"error": f"Unsupported structured action: {action}"}
    return {
        "action": action,
        "status": "failed" if isinstance(result, dict) and result.get("error") else "completed",
        "result": result,
    }


@app.post("/a2a/tasks")
async def handle_task(message: A2AMessage):
    """
    Healing Agent 接收 SRE Agent 派发的修复任务。
    真实环境中这里可以调用 MCP Server 或者 Kubernetes API。
    """

    payload = message.payload
    plan = HealingPlan(**payload)

    # This service is an A2A planning/compatibility surface, not an approval
    # authority. Even when the legacy AUTO_HEALING_ENABLED flag is true, direct
    # callers must never bypass the API's Skill gate, one-time approval receipt,
    # execution audit and recovery state machine.
    effective_dry_run = True

    if plan.action == "execute_plan":
        outcomes = []
        for change in plan.changes:
            candidate = dict(change)
            candidate["human_approved"] = bool(plan.human_approved and change.get("human_approved", True))
            outcomes.append(_execute_structured_change(candidate, plan.namespace, effective_dry_run))
        failed = [item for item in outcomes if item.get("status") in {"failed", "blocked"}]
        result = {
            "executed": not effective_dry_run and bool(outcomes) and not failed,
            "action": "execute_plan",
            "target": f"{plan.namespace}/{plan.workload_type}/{plan.workload_name}",
            "reason": plan.reason,
            "dry_run": effective_dry_run,
            "auto_healing_enabled": AUTO_HEALING_ENABLED,
            "message": "Structured remediation plan completed." if not failed else "One or more structured actions were blocked or failed.",
            "outcomes": outcomes,
        }

    elif plan.action == "restart":
        mcp_result = restart_deployment(
            namespace=plan.namespace,
            deployment_name=plan.workload_name,
            dry_run=effective_dry_run,
        )
        result = {
            "executed": not effective_dry_run,
            "action": "restart",
            "target": f"{plan.namespace}/{plan.workload_name}",
            "reason": plan.reason,
            "dry_run": effective_dry_run,
            "auto_healing_enabled": AUTO_HEALING_ENABLED,
            "message": "Restart action executed." if not effective_dry_run else "Restart action generated as dry run.",
            "mcp_result": mcp_result,
        }

    elif plan.action == "scale_out":
        replicas = plan.replicas
        if replicas is None:
            replicas = max(1, DEFAULT_SCALE_DELTA + 1)
        mcp_result = scale_deployment(
            namespace=plan.namespace,
            deployment_name=plan.workload_name,
            replicas=replicas,
            dry_run=effective_dry_run,
        )
        result = {
            "executed": not effective_dry_run,
            "action": "scale_out",
            "target": f"{plan.namespace}/{plan.workload_name}",
            "reason": plan.reason,
            "replicas": replicas,
            "dry_run": effective_dry_run,
            "auto_healing_enabled": AUTO_HEALING_ENABLED,
            "message": "Scale out action executed." if not effective_dry_run else "Scale out action generated as dry run.",
            "mcp_result": mcp_result,
        }

    elif plan.action in {"patch_workload", "patch"}:
        mcp_result = patch_workload(
            namespace=plan.namespace,
            workload_type=plan.workload_type,
            workload_name=plan.workload_name,
            patch=plan.patch or {},
            dry_run=effective_dry_run,
        )
        result = {
            "executed": not effective_dry_run,
            "action": "patch_workload",
            "target": f"{plan.namespace}/{plan.workload_type}/{plan.workload_name}",
            "reason": plan.reason,
            "patch": plan.patch or {},
            "dry_run": effective_dry_run,
            "auto_healing_enabled": AUTO_HEALING_ENABLED,
            "message": "Workload patch executed." if not effective_dry_run else "Workload patch generated as dry run.",
            "mcp_result": mcp_result,
        }

    else:
        return A2AResponse(
            id=str(uuid.uuid4()),
            source_agent="healing-agent",
            target_agent=message.source_agent,
            status="failed",
            error=f"Unsupported action: {plan.action}",
        )

    return A2AResponse(
        id=str(uuid.uuid4()),
        source_agent="healing-agent",
        target_agent=message.source_agent,
        status="completed",
        result=result,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8101)
