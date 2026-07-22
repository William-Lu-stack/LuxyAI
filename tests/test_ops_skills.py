import base64
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Request

from agents.remediation_engine import action_catalog_payload
from backend.app import main as server
from backend.app.schemas.chat import ChatRiskRankRequest
from backend.app.schemas.operations import OpsJobCreateRequest, OpsSkillDefinition
from backend.app.services.ops_skill_registry import (
    OpsSkillRegistry,
    approved_script_catalog,
    skill_option_catalog,
)


class OpsSkillCatalogTests(unittest.TestCase):
    def test_secret_values_are_removed_from_all_public_payloads(self):
        encoded_fixture = base64.b64encode("-".join(("fixture", "value")).encode("utf-8")).decode("ascii")
        value = server._redact_sensitive({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "registry-auth"},
            "data": {"password": encoded_fixture},
            "stringData": {"username": "operator"},
        })
        self.assertEqual(value["data"], "[REDACTED]")
        self.assertEqual(value["stringData"], "[REDACTED]")

    def test_candidate_lifecycle_is_persisted_until_operator_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            registry = OpsSkillRegistry(root)
            candidate = registry.upsert({
                "id": "candidate-permission-recovery",
                "name": "候选权限恢复",
                "summary": "根据已验证恢复生成，等待人工审核。",
                "symptoms": ["permission denied"],
                "applies_to": ["Deployment"],
                "evidence_required": ["previous_logs", "workload_spec"],
                "diagnostic_steps": ["核对失败路径和 YAML 差异"],
                "allowed_actions": ["patch_resource"],
                "success_criteria": ["pod_ready"],
                "enabled": False,
                "lifecycle": "candidate",
            }, actor="learning-loop")
            self.assertEqual(candidate["lifecycle"], "candidate")
            reloaded = OpsSkillRegistry(root)
            stored = next(item for item in reloaded.list()["skills"] if item["id"] == candidate["id"])
            self.assertFalse(stored["enabled"])
            self.assertEqual(stored["lifecycle"], "candidate")
            published = reloaded.upsert({**stored, "enabled": True, "lifecycle": "published"}, actor="operator")
            self.assertTrue(published["enabled"])
            self.assertEqual(published["lifecycle"], "published")

    def test_persisted_builtin_skill_is_upgraded_to_current_security_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            registry = OpsSkillRegistry(root)
            current = next(item for item in registry.list()["skills"] if item["id"] == "skill-storage-pvc-pv")
            registry.upsert({
                **current,
                "builtin": True,
                "evidence_required": ["storage_chain", "events", "storage_class", "node_storage", "csi_status"],
            }, actor="old-release")
            reloaded = OpsSkillRegistry(root)
            upgraded = next(item for item in reloaded.list()["skills"] if item["id"] == "skill-storage-pvc-pv")
            self.assertEqual(upgraded["evidence_required"], ["storage_chain", "events", "storage_class", "workload_spec"])

    def test_dynamic_skill_authorizes_and_binds_permission_action(self):
        plan = {
            "namespace": "default",
            "target": "Deployment/api",
            "summary": "mkdir /work/cache permission denied after init container chmod",
            "evidence": {
                "state_text": "mkdir: permission denied",
                "logs": {"api": {"previous": "mkdir /work/cache: permission denied"}},
                "events": [],
                "workload": {"kind": "Deployment", "metadata": {"generation": 3}},
                "pod": {"containers": [{"name": "api", "reason": "CrashLoopBackOff", "restart_count": 2}], "security_context": {}},
                "storage": [{"pvc": "api-data", "pvc_phase": "Bound", "storage_class": "nfs"}],
            },
            "changes": [{
                "type": "patch_resource",
                "api_version": "apps/v1",
                "kind": "Deployment",
                "name": "api",
                "namespace": "default",
                "patch": {"spec": {"template": {"metadata": {"annotations": {"recovery": "approved"}}}}},
            }],
        }
        attached = server._attach_operator_skills_to_plan(plan, {
            "question": plan["summary"],
            "evidence": plan["evidence"],
            "plan": plan,
        })
        self.assertEqual(attached["change_source"], "dynamic_skill")
        self.assertEqual(attached["changes"][0]["skill_id"], "skill-volume-permission-recovery")
        self.assertTrue(attached["changes"][0]["skill_supported"])

    def test_dynamic_skill_blocks_mutation_until_required_evidence_is_collected(self):
        plan = {
            "namespace": "default",
            "target": "Deployment/api",
            "summary": "mkdir /work/cache permission denied",
            "changes": [{
                "type": "patch_resource",
                "api_version": "apps/v1",
                "kind": "Deployment",
                "name": "api",
                "namespace": "default",
                "patch": {"metadata": {"annotations": {"recovery": "requested"}}},
            }],
        }
        attached = server._attach_operator_skills_to_plan(plan, {
            "question": plan["summary"],
            "evidence": {"state_text": "mkdir: permission denied"},
            "plan": plan,
        })
        self.assertEqual(attached["changes"], [])
        self.assertEqual(attached["decision"], "skill_evidence_required")
        self.assertTrue(any(item["evidence_missing"] for item in attached["operator_skills"]))

    def test_option_catalog_exposes_multiselect_fields(self):
        catalog = skill_option_catalog()
        self.assertIn("applies_to", catalog)
        self.assertIn("evidence_required", catalog)
        self.assertIn("success_criteria", catalog)
        self.assertIn("script_triggers", catalog)
        self.assertTrue(any(item["id"] == "previous_logs" for item in catalog["evidence_required"]))
        self.assertTrue(all(item.get("description") for items in catalog.values() for item in items))

    def test_action_catalog_has_operator_guidance(self):
        actions = {item["id"]: item for item in action_catalog_payload()}
        self.assertIn("patch_workload", actions)
        self.assertIn("Deployment", actions["patch_workload"]["when_to_use"])
        self.assertTrue(actions["patch_workload"]["label"])
        self.assertTrue(actions["patch_workload"]["operator_note"])

    def test_approved_script_catalog_only_reads_metadata(self):
        value = """[{"id":"inspect-pvc","name":"PVC 权限检查","description":"只读检查挂载目录权限","risk":"medium","allowed_targets":["Pod","PVC"],"required_evidence":["previous_logs"]}]"""
        with patch.dict(os.environ, {"OPS_APPROVED_SCRIPTS_JSON": value}):
            scripts = approved_script_catalog()
        self.assertEqual(scripts[0]["id"], "inspect-pvc")
        self.assertNotIn("content", scripts[0])
        self.assertNotIn("command", scripts[0])

    def test_registry_persists_script_trigger_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills.json")
            skill = registry.upsert({
                "name": "PVC 权限复核",
                "summary": "挂载卷后容器因权限问题反复重启。",
                "symptoms": ["permission denied", "CrashLoopBackOff"],
                "applies_to": ["Pod", "PVC"],
                "evidence_required": ["previous_logs", "storage_chain"],
                "diagnostic_steps": ["读取 previous logs", "核对 PVC 和 securityContext"],
                "allowed_actions": ["patch_workload"],
                "success_criteria": ["pod_ready", "restart_count_stable"],
                "script_policy": {
                    "enabled": True,
                    "script_id": "inspect-pvc",
                    "trigger_conditions": ["required_evidence_collected", "root_cause_confirmed", "manual_confirmation"],
                    "trigger_description": "连续 CrashLoop 且日志明确出现 permission denied 时触发。",
                    "timeout_seconds": 60,
                },
            }, actor="tester")
            self.assertTrue(skill["script_policy"]["enabled"])
            self.assertEqual(skill["script_policy"]["script_id"], "inspect-pvc")
            self.assertTrue(skill["script_policy"]["require_confirmation"])
            package_dir = Path(skill["package_path"])
            self.assertTrue((package_dir / "SKILL.md").is_file())
            self.assertTrue((package_dir / "agents" / "openai.yaml").is_file())
            self.assertTrue((package_dir / "references" / "ops-policy.yaml").is_file())

    def test_existing_frontend_payload_becomes_portable_package(self):
        """旧前端字段无需变化，保存后自动生成标准目录包。"""
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "ops-skills")
            skill = registry.upsert({
                "id": "",
                "name": "Service 端点恢复",
                "category": "network",
                "summary": "处理 Service selector 与 Endpoint 不匹配。",
                "symptoms": ["no endpoints", "503"],
                "applies_to": ["Service", "Deployment"],
                "evidence_required": ["service_endpoints", "events"],
                "diagnostic_steps": ["核对 selector 与 Pod label", "验证 EndpointSlice"],
                "allowed_actions": ["patch_service"],
                "success_criteria": ["endpoint_ready", "error_rate_recovered"],
                "risk": "high",
                "owner": "frontend-operator",
                "script_policy": {"enabled": False},
            }, actor="tester")
            package_dir = Path(skill["package_path"])
            content = (package_dir / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(skill["portable"])
            self.assertIn("name:", content)
            self.assertIn("Service 端点恢复", content)
            matched_ids = [item["skill"]["id"] for item in registry.match({"question": "service no endpoints 503"})["matches"]]
            self.assertIn(skill["id"], matched_ids)

    def test_export_and_import_preserve_runtime_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = OpsSkillRegistry(Path(directory) / "source")
            skill = source.upsert({
                "id": "pvc-pending-recovery",
                "name": "PVC Pending 恢复",
                "summary": "定位并恢复 PVC Pending。",
                "symptoms": ["PVC Pending", "FailedMount"],
                "applies_to": ["PVC", "Pod"],
                "evidence_required": ["storage_chain", "events"],
                "diagnostic_steps": ["检查 PVC、PV、StorageClass 和 CSI"],
                "allowed_actions": ["create_pv", "create_pvc"],
                "success_criteria": ["pvc_bound", "pod_ready"],
                "risk": "high",
            }, actor="tester")
            filename, payload = source.export_package(skill["id"])
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = set(archive.namelist())
            self.assertIn("pvc-pending-recovery/SKILL.md", names)
            self.assertIn("pvc-pending-recovery/references/ops-policy.yaml", names)

            target = OpsSkillRegistry(Path(directory) / "target")
            imported = target.import_packages(
                filename,
                payload,
                actor="importer",
                supported_actions={"create_pv", "create_pvc"},
            )
            self.assertEqual(imported[0]["id"], "pvc-pending-recovery")
            self.assertTrue(imported[0]["execution_ready"])
            self.assertEqual(imported[0]["allowed_actions"], ["create_pv", "create_pvc"])

    def test_generic_agent_skill_import_is_instruction_only(self):
        skill_md = """---
name: generic-k8s-check
description: Inspect Kubernetes resources and explain observed failures.
---

# Workflow

Collect evidence and explain the result. Do not mutate infrastructure.
"""
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            # 兼容用户直接压缩 Skill 目录内容、ZIP 中没有顶层文件夹的常见情况。
            archive.writestr("SKILL.md", skill_md)
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills")
            imported = registry.import_packages(
                "generic-k8s-check.zip",
                output.getvalue(),
                actor="importer",
                supported_actions={"patch_workload"},
            )
        self.assertFalse(imported[0]["execution_ready"])
        self.assertEqual(imported[0]["allowed_actions"], [])

    def test_legacy_json_is_migrated_without_changing_skill_id(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = Path(directory) / "skills.json"
            legacy_path.write_text(json.dumps({"skills": [{
                "id": "legacy-crashloop",
                "name": "旧版 CrashLoop Skill",
                "summary": "兼容旧前端保存的数据。",
                "symptoms": ["CrashLoopBackOff"],
                "diagnostic_steps": ["读取 previous logs"],
                "allowed_actions": ["patch_workload"],
                "success_criteria": ["pod_ready"],
            }]}, ensure_ascii=False), encoding="utf-8")
            registry = OpsSkillRegistry(Path(directory) / "packages", legacy_path=legacy_path)
            skills = {item["id"]: item for item in registry.list()["skills"]}
            self.assertIn("legacy-crashloop", skills)
            self.assertTrue((Path(directory) / "packages" / "legacy-crashloop" / "SKILL.md").is_file())

    def test_registry_rejects_script_without_trigger_description(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills.json")
            with self.assertRaisesRegex(ValueError, "具体故障场景"):
                registry.upsert({
                    "name": "无触发说明",
                    "summary": "测试",
                    "diagnostic_steps": ["读取证据"],
                    "allowed_actions": ["patch_workload"],
                    "script_policy": {
                        "enabled": True,
                        "script_id": "inspect-pvc",
                        "trigger_conditions": ["manual_confirmation"],
                        "trigger_description": "太短",
                    },
                }, actor="tester")


class OpsSkillApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_request_merges_explicit_high_risk_approval_into_plan(self):
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/ops/jobs",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        })
        payload = OpsJobCreateRequest(
            plan={"target": "StatefulSet/log-store", "changes": [{"type": "create_pv"}]},
            confirm=True,
            high_risk_confirmed=True,
            operator_override_reason="已核对存储后端和回滚方式",
            stepwise_confirmation=True,
        )
        with patch.object(server, "_enqueue_ops_job", new=AsyncMock(return_value={"status": "queued"})) as enqueue:
            response = await server.create_ops_job(payload, request)
        submitted = enqueue.await_args.args[0]
        self.assertEqual(response["status"], "queued")
        self.assertTrue(submitted["high_risk_confirmed"])
        self.assertTrue(submitted["stepwise_confirmation"])
        self.assertIn("回滚", submitted["operator_override_reason"])

    async def test_risk_ranking_falls_back_without_changing_real_targets(self):
        request = ChatRiskRankRequest(risks=[
            {"key": "workload:a", "name": "a", "severity": "P2", "score": 210},
            {"key": "workload:b", "name": "b", "severity": "P1", "score": 420},
        ])
        with patch("agents.llm_client.get_llm", side_effect=RuntimeError("model unavailable")):
            response = await server.rank_chat_risks(request)
        self.assertEqual(response["source"], "deterministic_fallback")
        self.assertEqual(response["ordered_keys"], ["workload:b", "workload:a"])
        self.assertEqual(set(response["rationales"]), {"workload:a", "workload:b"})

    async def test_operator_step_approval_happens_before_kubernetes_change(self):
        order = []

        async def approve(index, total, change, target):
            order.append(("approval", index, target))
            return True

        async def execute(change, plan):
            order.append(("execute", change["type"], plan["target"]))
            return {"status": "completed", "change": change, "result": {"accepted": True}}

        plan = {
            "target": "Deployment/web",
            "namespace": "default",
            "changes": [{
                "type": "restart",
                "namespace": "default",
                "workload_type": "Deployment",
                "workload_name": "web",
            }],
        }
        with (
            patch.object(server, "_ops_release_gate", return_value={"allowed": True}),
            patch.object(server, "_collect_plan_deep_evidence", new=AsyncMock(return_value={"pod": {"name": "web-abc"}, "events": []})),
            patch.object(server, "_attach_operator_skills_to_plan", side_effect=lambda plan, _signal, **_kwargs: plan),
            patch.object(server, "_execute_change", side_effect=execute),
            patch.object(server, "_verify_plan_recovery", new=AsyncMock(return_value={"status": "verified", "recovered": True, "message": "Ready"})),
            patch.object(server, "record_remediation", return_value={"status": "recorded"}),
        ):
            result = await server._execute_ops_plan_once(plan, summarize=False, change_approval=approve)
        self.assertEqual(order[0][0], "approval")
        self.assertEqual(order[1][0], "execute")
        self.assertEqual(result["status"], "completed")

    async def test_change_executor_exception_becomes_structured_failure(self):
        events = []

        async def progress(stage, message, **extra):
            events.append({"stage": stage, "message": message, **extra})

        plan = {
            "target": "Deployment/web",
            "namespace": "default",
            "changes": [{
                "type": "restart",
                "namespace": "default",
                "workload_type": "Deployment",
                "workload_name": "web",
            }],
        }
        with (
            patch.object(server, "_ops_release_gate", return_value={"allowed": True}),
            patch.object(server, "_collect_plan_deep_evidence", new=AsyncMock(return_value={"pod": {"name": "web-abc"}, "events": []})),
            patch.object(server, "_attach_operator_skills_to_plan", side_effect=lambda plan, _signal, **_kwargs: plan),
            patch.object(server, "_execute_change", new=AsyncMock(side_effect=RuntimeError("mcp transport closed"))),
            patch.object(server, "_verify_plan_recovery", new=AsyncMock(return_value={"status": "unknown", "recovered": None, "message": "not verified"})),
            patch.object(server, "record_remediation", return_value={"status": "recorded"}),
        ):
            result = await server._execute_ops_plan_once(plan, summarize=False, progress=progress)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertIn("mcp transport closed", result["results"][0]["result"]["error"])
        self.assertTrue(any(event["stage"] == "change_exception" for event in events))

    async def test_fresh_evidence_failure_blocks_preapproved_mutation(self):
        plan = {
            "target": "Deployment/web",
            "namespace": "default",
            "high_risk_confirmed": True,
            "operator_force_execute": True,
            "changes": [{
                "type": "restart",
                "namespace": "default",
                "workload_type": "Deployment",
                "workload_name": "web",
                "human_approved": True,
            }],
        }
        execute = AsyncMock()
        with (
            patch.object(server, "_ops_release_gate", return_value={"allowed": True}),
            patch.object(server, "_collect_plan_deep_evidence", new=AsyncMock(return_value={"error": "Rancher timeout"})),
            patch.object(server, "_execute_change", execute),
            patch.object(server, "_probe_plan_recovery", new=AsyncMock(return_value={"status": "unknown", "recovered": None})),
            patch.object(server, "_evidence_based_replan", new=AsyncMock(return_value=[])),
        ):
            result = await server._execute_ops_plan_once(plan, summarize=False)
        execute.assert_not_awaited()
        self.assertFalse(result["executed"])
        self.assertIsNone(result["verification"]["recovered"])
        self.assertEqual(plan["decision"], "fresh_evidence_required")

    async def test_api_rejects_unapproved_script_id(self):
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/ops/skills",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        })
        definition = OpsSkillDefinition(
            name="未批准脚本",
            summary="用于验证脚本白名单。",
            diagnostic_steps=["读取日志和 Events"],
            allowed_actions=["patch_workload"],
            script_policy={
                "enabled": True,
                "script_id": "not-approved",
                "trigger_conditions": ["required_evidence_collected", "manual_confirmation"],
                "trigger_description": "证据齐全且运维人员确认后才允许触发。",
            },
        )
        with patch.dict(os.environ, {"OPS_APPROVED_SCRIPTS_JSON": "[]"}):
            with self.assertRaises(HTTPException) as context:
                await server.upsert_ops_skill(definition, request)
        self.assertEqual(context.exception.status_code, 422)
        self.assertIn("企业批准目录", str(context.exception.detail))

    async def test_high_risk_plan_requires_explicit_second_confirmation(self):
        plan = {
            "namespace": "default",
            "target": "PV/static-data",
            "steps": [{"title": "核对存储模板"}],
            "changes": [{
                "type": "create_pv",
                "manifest": {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "static-data"}},
            }],
        }
        with patch.dict(os.environ, {"OPS_MUTATION_ENABLED": "true"}):
            with self.assertRaises(HTTPException) as context:
                await server._enqueue_ops_job(plan, "tester", autonomous=False, confirmed=True)
        self.assertEqual(context.exception.status_code, 409)
        self.assertTrue(context.exception.detail["requires_high_risk_confirmation"])
        self.assertIn("create_pv", context.exception.detail["high_risk_actions"])

    async def test_confirmed_high_risk_plan_reaches_job_queue(self):
        class DeferredTask:
            def done(self):
                return False

            def cancel(self):
                return True

        def defer(coroutine):
            coroutine.close()
            return DeferredTask()

        plan = {
            "namespace": "default",
            "target": "ConfigMap/runtime-config",
            "summary": "ConfigMap runtime-config is missing and blocks Deployment/api startup",
            "evidence": {
                "events": [{"reason": "CreateContainerConfigError", "message": "configmap runtime-config not found"}],
                "workload": {"kind": "Deployment", "metadata": {"name": "api", "generation": 2}},
            },
            "high_risk_confirmed": True,
            "operator_override_reason": "已核对配置模板、影响范围和回滚方式",
            "stepwise_confirmation": True,
            "steps": [{"id": "config_ref_exists", "title": "确认缺失配置引用"}],
            "changes": [{
                "type": "create_configmap",
                "namespace": "default",
                "configmap_name": "runtime-config",
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "runtime-config", "namespace": "default"},
                    "data": {"MODE": "stable"},
                },
            }],
        }
        job_id = ""
        try:
            with (
                patch.dict(os.environ, {"OPS_MUTATION_ENABLED": "true"}),
                patch.object(server.asyncio, "create_task", side_effect=defer),
            ):
                response = await server._enqueue_ops_job(plan, "tester", autonomous=False, confirmed=True)
            job_id = response["id"]
            self.assertEqual(response["status"], "queued")
            self.assertTrue(response["stepwise_confirmation"])
            self.assertTrue(response["execution_readiness"]["ready"])
        finally:
            if job_id:
                server.OPS_JOBS.pop(job_id, None)
                server.OPS_JOB_TASKS.pop(job_id, None)
                server.OPS_JOB_CANCEL_EVENTS.pop(job_id, None)

    async def test_operator_confirmed_high_risk_autonomous_plan_is_queued_stepwise(self):
        class DeferredTask:
            def done(self):
                return False

            def cancel(self):
                return True

        def defer(coroutine):
            coroutine.close()
            return DeferredTask()

        plan = {
            "namespace": "default",
            "target": "ConfigMap/runtime-config",
            "summary": "ConfigMap runtime-config is missing and blocks Deployment/api startup",
            "evidence": {
                "events": [{"reason": "CreateContainerConfigError", "message": "configmap runtime-config not found"}],
                "workload": {"kind": "Deployment", "metadata": {"name": "api", "generation": 2}},
            },
            "high_risk_confirmed": True,
            "operator_force_execute": True,
            "steps": [{"title": "核对配置引用"}],
            "changes": [{
                "type": "create_configmap",
                "namespace": "default",
                "configmap_name": "runtime-config",
                "manifest": {
                    "apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {"name": "runtime-config", "namespace": "default"},
                    "data": {"MODE": "stable"},
                },
            }],
        }
        job_id = ""
        try:
            with (
                patch.dict(os.environ, {"OPS_MUTATION_ENABLED": "true", "AUTONOMOUS_OPS_ENABLED": "true"}),
                patch.object(server.asyncio, "create_task", side_effect=defer),
            ):
                response = await server._enqueue_ops_job(plan, "tester", autonomous=True, confirmed=True)
            job_id = response["id"]
            self.assertEqual(response["status"], "queued")
            self.assertTrue(response["stepwise_confirmation"])
        finally:
            if job_id:
                server.OPS_JOBS.pop(job_id, None)
                server.OPS_JOB_TASKS.pop(job_id, None)
                server.OPS_JOB_CANCEL_EVENTS.pop(job_id, None)

    async def test_inspection_routes_finding_through_matching_skill(self):
        finding = {
            "id": "finding-pvc",
            "category": "storage_config",
            "severity": "P1",
            "title": "PVC Pending and FailedMount",
            "summary": "Pod volume references a PVC that cannot bind to a PV",
            "cluster": "local-cluster",
            "namespace": "default",
            "name": "api-0",
            "workload": {"kind": "StatefulSet", "name": "api", "replicas": 1, "ready_replicas": 0},
            "evidence": {
                "state_text": "FailedMount no persistent volumes available for this claim",
                "events": [{"reason": "FailedMount", "message": "PVC is Pending"}],
                "pod": {"name": "api-0", "containers": []},
            },
        }
        finding["ops_plan"] = server._ops_plan_from_finding(finding)
        payload = {"findings": [finding], "summary": {"total": 1}}
        with patch.dict(os.environ, {"INSPECTION_SKILL_ROUTER_ENABLED": "false"}):
            routed = await server._route_inspection_findings_with_skills(payload)
        routed_finding = routed["findings"][0]
        self.assertTrue(routed_finding["matched_skills"])
        self.assertEqual(routed["summary"]["skill_routed"], 1)
        self.assertIn("AgentSkillRouter/v2", routed_finding["ops_plan"]["planning_engine"])
        self.assertTrue(any("存储" in item["name"] for item in routed_finding["matched_skills"]))

    async def test_inspection_preview_recollects_evidence_and_locks_target(self):
        finding = {
            "id": "finding-orders",
            "category": "crashloop",
            "severity": "P1",
            "title": "orders CrashLoop",
            "summary": "orders pod is restarting",
            "source": "rancher",
            "cluster": "nonprod",
            "cluster_id": "c-nonprod",
            "namespace": "prod",
            "name": "orders-api-abc",
            "workload": {"kind": "Deployment", "name": "orders-api", "replicas": 2, "ready_replicas": 1},
            "evidence": {"pod": {"name": "orders-api-abc", "containers": []}, "events": []},
        }
        previous = server.LAST_INSPECTION_PAYLOAD
        server.LAST_INSPECTION_PAYLOAD = {"findings": [finding]}
        replacement = {
            "id": "ai-plan",
            "title": "AI plan",
            "namespace": "prod",
            "target": "Deployment/orders-api",
            "steps": [{"id": "previous_logs", "title": "读取上次退出日志"}],
            "changes": [{
                "type": "patch_workload",
                "namespace": "prod",
                "workload_type": "Deployment",
                "workload_name": "orders-api",
                "patch": {"spec": {"replicas": 3}},
                "reason": "live evidence",
            }],
            "root_cause_hypotheses": [{"title": "CrashLoop evidence"}],
            "success_criteria": ["pod_ready"],
        }
        deep = {
            "pod": {
                "name": "orders-api-abc",
                "workload": {"kind": "Deployment", "name": "orders-api"},
                "containers": [{"name": "app", "state": "waiting", "reason": "CrashLoopBackOff", "restart_count": 3}],
                "security_context": {},
            },
            "events": [{"reason": "BackOff", "message": "back-off restarting failed container"}],
            "logs": {"app": {"previous": "startup failed"}},
            "storage": [],
            "services": [{"name": "orders"}],
            "workload": {"metadata": {"name": "orders-api"}},
        }
        try:
            with patch.object(server, "_collect_plan_deep_evidence", AsyncMock(return_value=deep)) as collect, patch.object(
                server, "_evidence_based_replan", AsyncMock(return_value=[replacement])
            ):
                result = await server.preview_ai_inspection_finding(
                    server.InspectionPreviewRequest(finding_id="finding-orders", model_profile_id="primary")
                )
            collect.assert_awaited_once()
            plan = result["plan"]
            self.assertEqual(plan["preview_mode"], "live_evidence_ai")
            self.assertEqual(plan["target"], "Deployment/orders-api")
            self.assertEqual(plan["changes"][0]["workload_name"], "orders-api")
            self.assertEqual(plan["evidence_summary"]["events"], 1)
            self.assertEqual(plan["target_binding"], "inspection_finding_id")
        finally:
            server.LAST_INSPECTION_PAYLOAD = previous


if __name__ == "__main__":
    unittest.main()
