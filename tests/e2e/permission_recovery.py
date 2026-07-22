"""Real Kubernetes permission-failure recovery check.

Run with E2E_KUBECONFIG pointing at an isolated cluster. The script injects a
bad initContainer chmod, proves that an unapproved mutation is blocked, then
executes a Skill-bound patch with explicit approval and verifies rollout health.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REQUIRE_LLM = os.getenv("E2E_REQUIRE_LLM", "false").lower() in {"1", "true", "yes", "on"}
if REQUIRE_LLM:
    # A caller can pipe a trusted provider profile into this isolated process.
    # The API key never appears in argv, repository files, logs or test output.
    model_config = json.load(sys.stdin)
    os.environ.update({
        "LLM_API_BASE": str(model_config["base_url"]),
        "LLM_API_KEY": str(model_config["api_key"]),
        "LLM_MODEL": str(model_config["model"]),
        "LLM_AUTH_TYPE": "api_key",
        "LLM_MAX_TOKENS": str(model_config.get("max_tokens") or 4096),
    })
    override_ip = str(model_config.get("dns_override_ip") or "").strip()
    override_host = urlparse(str(model_config["base_url"])).hostname or ""
    if override_ip and override_host:
        original_getaddrinfo = socket.getaddrinfo

        def e2e_getaddrinfo(host, port, *args, **kwargs):
            return original_getaddrinfo(override_ip if host == override_host else host, port, *args, **kwargs)

        socket.getaddrinfo = e2e_getaddrinfo

KUBECONFIG_PATH = Path(os.environ["E2E_KUBECONFIG"]).resolve()
STATE_DIR = Path(tempfile.mkdtemp(prefix="flawless-e2e-state-"))
os.environ.update({
    "CLUSTER_REGISTRY_PATH": str(STATE_DIR / "clusters.db"),
    "OPS_SKILL_ROOT": str(STATE_DIR / "ops-skills"),
    "OPS_SKILL_STORE_PATH": str(STATE_DIR / "ops-skills.json"),
    "OPS_MUTATION_ENABLED": "true",
    "SKILL_EXECUTION_REQUIRED": "true",
    "OPS_VERIFY_INITIAL_GRACE_SECONDS": "8",
    "OPS_VERIFY_TIMEOUT_SECONDS": "80",
    "OPS_VERIFY_INTERVAL_SECONDS": "2",
    "OPS_LLM_PLANNER_TIMEOUT_SECONDS": "90",
    "OPS_LLM_PLANNER_MAX_TOKENS": "6000",
})

from backend.app import main as application  # noqa: E402


NAMESPACE = "flawless-e2e"
WORKLOAD = "permission-check"
IMAGE = os.getenv("E2E_WORKLOAD_IMAGE", "flawless-local:latest")


def deployment(init_command: str) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": WORKLOAD, "namespace": NAMESPACE},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": WORKLOAD}},
            "template": {
                "metadata": {"labels": {"app": WORKLOAD}},
                "spec": {
                    "restartPolicy": "Always",
                    "initContainers": [{
                        "name": "prepare-volume",
                        "image": IMAGE,
                        "imagePullPolicy": "Never",
                        "command": ["/bin/sh", "-c", init_command],
                        "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
                    }],
                    "containers": [{
                        "name": "app",
                        "image": IMAGE,
                        "imagePullPolicy": "Never",
                        "command": ["/bin/sh", "-c", "mkdir -p /work/runtime && echo ready && sleep 3600"],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "allowPrivilegeEscalation": False,
                        },
                        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
                    }],
                    "volumes": [{"name": "work", "emptyDir": {}}],
                },
            },
        },
    }


def wait_for_fault(cluster_id: str) -> tuple[str, dict]:
    deadline = time.monotonic() + 90
    last = {}
    while time.monotonic() < deadline:
        inventory = application.CLUSTER_REGISTRY.inventory(cluster_id)
        pods = [
            pod for pod in inventory.get("pods") or []
            if (pod.get("metadata") or {}).get("namespace") == NAMESPACE
            and ((pod.get("metadata") or {}).get("labels") or {}).get("app") == WORKLOAD
        ]
        if pods:
            pod_name = (pods[0].get("metadata") or {}).get("name")
            last = application.CLUSTER_REGISTRY.pod_diagnostics(
                cluster_id,
                namespace=NAMESPACE,
                pod_name=pod_name,
                tail_lines=100,
            )
            text = str(last.get("logs") or {}).lower()
            if "permission denied" in text and "mkdir" in text:
                return pod_name, last
        time.sleep(2)
    raise AssertionError(f"fault evidence not observed: {last}")


async def run() -> None:
    cluster_id = "kube-e2e"
    registry = application.CLUSTER_REGISTRY
    saved = registry.save_verified(
        content=KUBECONFIG_PATH.read_text(encoding="utf-8"),
        name="isolated-e2e",
        cluster_id=cluster_id,
    )
    assert saved["status"] == "connected", saved
    registry.apply_manifest(cluster_id, {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
    registry.apply_manifest(cluster_id, deployment("mkdir -p /work && chmod 0500 /work"))
    try:
        pod_name, diagnostics = await asyncio.to_thread(wait_for_fault, cluster_id)
        plan = {
            "id": "permission-recovery-e2e",
            "cluster": "isolated-e2e",
            "cluster_id": cluster_id,
            "source": "kubeconfig",
            "namespace": NAMESPACE,
            "target": f"Deployment/{WORKLOAD}",
            "pod_name": pod_name,
            "summary": "mkdir /work/runtime permission denied；initContainer chmod 0500 与业务 UID 10001 冲突。",
            "root_cause": "错误 initContainer 权限配置导致业务容器无法写 emptyDir。",
            "evidence": {"state_text": "mkdir permission denied", "pod": diagnostics.get("raw_pod") or {}},
            "steps": [{"id": "previous_logs", "title": "复核失败日志"}],
            "changes": [],
            "success_criteria": ["rollout_complete", "pod_ready", "restart_count_stable"],
            "requires_confirmation": True,
        }
        if REQUIRE_LLM:
            plan["_runtime_evidence"] = await application._collect_plan_deep_evidence(plan)
            replans = await application._evidence_based_replan(plan, [], include_llm=True)
            assert replans, {"error": "DeepSeek did not produce an executable replan", "runtime_replan": plan.get("_runtime_replan")}
            plan = replans[0]
            planning = plan.get("planning") or {}
            assert str(planning.get("source") or "").startswith("llm+"), planning
            assert not planning.get("llm_error"), planning
        else:
            # Dynamic Skills authorize mutations only after their complete
            # evidence_required set has been collected from the live cluster.
            deep_evidence = await application._collect_plan_deep_evidence(plan)
            assert not deep_evidence.get("error"), deep_evidence
            plan["evidence"] = deep_evidence
            plan["changes"] = [{
                "type": "patch_resource",
                "api_version": "apps/v1",
                "kind": "Deployment",
                "name": WORKLOAD,
                "namespace": NAMESPACE,
                "patch": {
                    "spec": {"template": {"spec": {"initContainers": [{
                        "name": "prepare-volume",
                        "image": IMAGE,
                        "imagePullPolicy": "Never",
                        "command": ["/bin/sh", "-c", "mkdir -p /work && chown 10001:10001 /work && chmod 0770 /work"],
                        "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
                    }]}}},
                },
                "reason": "实时日志证明 prepare-volume 把 emptyDir 改成 0500，业务 UID 10001 无法 mkdir；修正错误初始化命令后重新发布。",
                "rollback": "恢复审批前 Deployment revision。",
            }]
            plan = application._attach_operator_skills_to_plan(plan, {
                "question": plan["summary"],
                "diagnosis": {"root_cause": plan["root_cause"]},
                "evidence": deep_evidence,
                "plan": plan,
            })
        assert plan["changes"][0]["skill_id"] == "skill-volume-permission-recovery", plan

        blocked = await application._execute_change(copy.deepcopy(plan["changes"][0]), copy.deepcopy(plan))
        assert blocked["status"] == "blocked", blocked

        plan["high_risk_confirmed"] = True
        plan["operator_force_execute"] = True
        approvals: list[dict] = []

        async def approve(index: int, total: int, approved_change: dict, target: str) -> bool:
            approvals.append({"index": index, "total": total, "target": target, "command": approved_change.get("command")})
            return True

        result = await application._execute_ops_plan_once(
            plan,
            summarize=False,
            change_approval=approve,
        )
        assert approvals and approvals[0]["index"] == 1, approvals
        assert result["status"] == "completed", result
        assert not (result.get("results") or [{}])[0].get("permission_guidance"), result
        assert (result.get("verification") or {}).get("recovered") is True, result
        candidate = result.get("candidate_skill") or {}
        assert candidate.get("lifecycle") == "candidate" and candidate.get("enabled") is False, candidate
        print({
            "status": result["status"],
            "fault": "mkdir permission denied",
            "planner": (plan.get("planning") or {}).get("source") or "deterministic-test-plan",
            "action": plan["changes"][0].get("type"),
            "skill": plan["changes"][0]["skill_id"],
            "approval_blocked_before_confirm": True,
            "approved_steps": len(approvals),
            "recovered": result["verification"]["recovered"],
            "candidate_skill": candidate.get("id"),
        })
    finally:
        try:
            registry.delete_resource(cluster_id, api_version="v1", kind="Namespace", name=NAMESPACE, namespace="")
        finally:
            registry.delete(cluster_id)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
