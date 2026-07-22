import asyncio
import json
import os
import unittest

from agents.remediation_engine import build_remediation_plan, expert_steps_from_diagnosis
from backend.app import main as server


class RemediationEngineTests(unittest.TestCase):
    def setUp(self):
        self.pod = {
            "name": "api-abc",
            "namespace": "prod",
            "workload": {"kind": "Deployment", "name": "api"},
            "containers": [{
                "name": "api",
                "image": "docker.io/library/api:current",
                "restart_count": 9,
                "resources": {"limits": {"memory": "512Mi"}},
            }],
        }
        self.alert = {
            "namespace": "prod",
            "workload_type": "Deployment",
            "workload_name": "api",
            "pod": "api-abc",
        }

    def plan(self, root_cause: str):
        return build_remediation_plan(
            self.alert,
            {"root_cause": root_cause},
            {"pod": self.pod, "pods": [self.pod]},
        )

    def test_root_cause_families_are_classified(self):
        cases = {
            "OOMKilled exit code 137": "oom",
            "startup probe failed connection refused": "probe",
            "volume permission denied": "storage_permission",
            "ImagePullBackOff unauthorized": "image_auth",
            "exec format error no matching manifest for linux/amd64": "image_architecture",
            "FailedScheduling insufficient cpu": "scheduling_capacity",
            "service has no endpoints": "network_service",
            "FailedCreatePodSandBox CNI error": "dns_cni",
            "high CPU usage": "cpu_saturation",
        }
        for evidence, expected in cases.items():
            with self.subTest(evidence=evidence):
                self.assertEqual(self.plan(evidence)["runbook_id"], expected)

    def test_mkdir_permission_denied_requires_dynamic_skill_plan(self):
        plan = build_remediation_plan(
            self.alert,
            {"root_cause": "CrashLoopBackOff"},
            {
                "pod": self.pod,
                "pods": [self.pod],
                "logs": {
                    "api": {
                        "current": "mkdir: can't create directory '/data/cache': Permission denied",
                        "previous": "",
                    }
                },
            },
        )
        self.assertEqual(plan["runbook_id"], "storage_permission")
        self.assertEqual(plan["changes"], [])
        self.assertIn("动态 Skill", plan["evidence_gap"])

    def test_missing_logs_can_trigger_diagnostic_recreate_without_template_blocker(self):
        plan = build_remediation_plan(
            self.alert,
            {"root_cause": "CrashLoopBackOff"},
            {
                "pod": self.pod,
                "pods": [self.pod],
                "logs": {
                    "api": {
                        "current_error": "HTTP 404: pod does not exist",
                        "previous_error": "previous log unavailable: container not found",
                    }
                },
                "events": {"events": [{"reason": "BackOff", "message": "back-off restarting failed container"}]},
            },
        )
        self.assertEqual(plan["runbook_id"], "crash_unknown")
        self.assertEqual(plan["changes"][0]["type"], "recreate_pod")

    def test_ai_expert_steps_keep_scenario_specific_probes_and_branches(self):
        steps = expert_steps_from_diagnosis({"immediate_actions": [
            {
                "title": "确认 PVC 绑定阻断点",
                "description": "沿 Pod volume 检查 PVC、PV、StorageClass 与 CSI 事件。",
                "probe": "storage_chain",
                "expected_evidence": ["PVC phase", "PV claimRef"],
                "decision_rule": "PVC Pending 且没有匹配 PV 时进入供卷分支。",
                "on_match": "仅使用批准模板生成 PV/PVC 候选。",
                "on_miss": "转查调度约束。",
            },
            "检查 Service 与 Endpoint：确认 selector 是否命中 Ready Pod",
        ]})
        self.assertEqual(steps[0]["probe"], "storage_chain")
        self.assertIn("PVC Pending", steps[0]["decision_rule"])
        self.assertEqual(steps[1]["probe"], "service_endpoints")
        self.assertEqual(steps[0]["source"], "llm_evidence_expert")

    def test_oom_plan_grows_memory_without_shell(self):
        plan = build_remediation_plan(
            self.alert,
            {"root_cause": "OOMKilled out of memory"},
            {
                "pod": self.pod,
                "pods": [self.pod],
                "events": {"events": [{"reason": "OOMKilled", "message": "container exceeded memory limit; exit code 137"}]},
            },
        )
        change = plan["changes"][0]
        self.assertEqual(change["type"], "patch_workload")
        container = change["patch"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], "docker.io/library/api:current")
        self.assertEqual(container["resources"]["limits"]["memory"], "768Mi")
        self.assertNotIn("command", change)

    def test_oom_last_state_outranks_generic_crashloop(self):
        pod = {
            **self.pod,
            "containers": [{
                **self.pod["containers"][0],
                "reason": "CrashLoopBackOff",
                "state_detail": {
                    "reason": "CrashLoopBackOff",
                    "last_terminated_reason": "OOMKilled",
                    "exit_code": 137,
                },
            }],
        }
        plan = build_remediation_plan(
            self.alert,
            {"root_cause": "container back-off restarting failed"},
            {
                "pod": pod,
                "pods": [pod],
                "events": {"events": [{"reason": "BackOff", "message": "Back-off restarting failed container"}]},
            },
        )
        self.assertEqual(plan["runbook_id"], "oom")
        self.assertEqual(plan["changes"][0]["type"], "patch_workload")

    def test_exec_format_error_outranks_generic_crashloop(self):
        old_mapping = os.environ.get("AUTO_OPS_IMAGE_REPLACEMENTS_JSON")
        os.environ["AUTO_OPS_IMAGE_REPLACEMENTS_JSON"] = json.dumps({
            "registry.example.com/api:bad-arch": {"amd64": "registry.example.com/api:stable-amd64"}
        })
        try:
            pod = {
                **self.pod,
                "containers": [{
                    **self.pod["containers"][0],
                    "image": "registry.example.com/api:bad-arch",
                    "reason": "CrashLoopBackOff",
                    "state_detail": {"reason": "CrashLoopBackOff", "last_terminated_reason": "Error", "exit_code": 1},
                }],
            }
            plan = build_remediation_plan(
                self.alert,
                {"root_cause": "container back-off restarting failed"},
                {
                    "pod": pod,
                    "pods": [pod],
                    "node": {"labels": {"kubernetes.io/arch": "amd64"}},
                    "logs": {"app": "exec /app: exec format error"},
                    "events": {"events": [{"reason": "BackOff", "message": "Back-off restarting failed container"}]},
                },
            )
            self.assertEqual(plan["runbook_id"], "image_architecture")
            self.assertEqual(plan["changes"][0]["type"], "rollback_workload")
        finally:
            if old_mapping is None:
                os.environ.pop("AUTO_OPS_IMAGE_REPLACEMENTS_JSON", None)
            else:
                os.environ["AUTO_OPS_IMAGE_REPLACEMENTS_JSON"] = old_mapping

    def test_high_risk_action_requires_second_confirmation(self):
        result = asyncio.run(server._execute_change(
            {"type": "cordon_node", "node_name": "worker-1", "risk": "high"},
            {"cluster_id": "local", "namespace": "default", "high_risk_confirmed": False},
        ))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["result"]["requires_high_risk_confirmation"])

    def test_bounded_init_container_can_be_planned_but_not_executed_unapproved(self):
        plan = {
            "cluster_id": "local",
            "namespace": "default",
            "target": "Deployment/api",
        }
        proposed, reason = server._normalize_planner_change({
            "type": "patch_workload",
            "namespace": "default",
            "workload_type": "Deployment",
            "workload_name": "api",
            "patch": {"spec": {"template": {"spec": {"initContainers": [{
                "name": "prepare-volume",
                "image": "busybox:1.36",
                "command": ["sh", "-c", "mkdir -p /work && chown 10001:10001 /work && chmod 0770 /work"],
                "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                "volumeMounts": [{"name": "work", "mountPath": "/work"}],
            }]}}}},
        }, plan)
        self.assertEqual(reason, "")
        self.assertEqual(proposed["type"], "patch_workload_runtime_security")
        self.assertFalse(proposed["human_approved"])
        blocked = asyncio.run(server._execute_change(proposed, plan))
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(blocked["result"]["requires_high_risk_confirmation"])

    def test_permission_guidance_is_action_specific(self):
        guidance = server._permission_guidance(
            {"error": "HTTP Error 403: Forbidden"},
            {"cluster": "nonprod", "namespace": "k8s-agent", "source": "rancher"},
            {"type": "recreate_pod"},
        )
        self.assertEqual(guidance["minimal_verbs"], ["get", "delete"])
        self.assertEqual(guidance["minimal_resources"], ["pods"])
        self.assertIn("Rancher Token", guidance["summary"])

    def test_success_resource_name_does_not_create_permission_warning(self):
        guidance = server._permission_guidance(
            {
                "kind": "Deployment",
                "metadata": {"name": "permission-check"},
                "status": {
                    "conditions": [{
                        "reason": "ReplicaSetUpdated",
                        "message": 'ReplicaSet "permission-check-abc" is progressing.',
                    }],
                },
            },
            {"cluster": "managed", "namespace": "default"},
            {"type": "patch_resource"},
        )
        self.assertIsNone(guidance)

    def test_missing_pvc_generates_create_pvc_plan(self):
        plan = build_remediation_plan(
            self.alert,
            {"root_cause": "FailedMount persistentvolumeclaim data-vol not found"},
            {
                "pod": self.pod,
                "pods": [self.pod],
                "storage": [{"volume": "data", "pvc": "data-vol", "missing": True, "error": "404 not found"}],
                "events": {"events": [{"reason": "FailedMount", "message": "persistentvolumeclaim data-vol not found"}]},
            },
        )
        self.assertEqual(plan["runbook_id"], "storage_mount")
        self.assertEqual(plan["changes"][0]["type"], "create_pvc")
        self.assertEqual(plan["changes"][0]["manifest"]["kind"], "PersistentVolumeClaim")

    def test_probe_plan_copies_existing_probe_handler(self):
        pod = {
            **self.pod,
            "containers": [{
                **self.pod["containers"][0],
                "livenessProbe": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 2},
            }],
        }
        plan = build_remediation_plan(
            self.alert,
            {"root_cause": "liveness probe failed connection refused during slow startup"},
            {
                "pod": pod,
                "pods": [pod],
                "events": {"events": [{"reason": "Unhealthy", "message": "Liveness probe failed: connection refused"}]},
            },
        )
        startup_probe = plan["changes"][0]["patch"]["spec"]["template"]["spec"]["containers"][0]["startupProbe"]
        container_patch = plan["changes"][0]["patch"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container_patch["image"], "docker.io/library/api:current")
        self.assertEqual(startup_probe["httpGet"]["path"], "/health")
        self.assertEqual(startup_probe["failureThreshold"], 30)
        self.assertTrue(server._validate_workload_patch(plan["changes"][0]["patch"])[0])

    def test_invalid_startup_probe_is_rejected_before_kubernetes(self):
        valid, reason = server._validate_workload_patch({
            "spec": {"template": {"spec": {"containers": [{
                "name": "api",
                "startupProbe": {"failureThreshold": 30, "periodSeconds": 10},
            }]}}}
        })
        self.assertFalse(valid)
        self.assertIn("startupProbe must include", reason)

    def test_unbound_pvc_outranks_generic_failed_scheduling(self):
        plan = build_remediation_plan(
            self.alert,
            {"root_cause": "FailedScheduling"},
            {
                "pod": self.pod,
                "pods": [self.pod],
                "storage": [{"pvc": "data-vol", "pvc_phase": "Pending", "requested": "5Gi"}],
                "events": {"events": [{
                    "reason": "FailedScheduling",
                    "message": "0/10 nodes are available: pod has unbound immediate PersistentVolumeClaims.",
                }]},
            },
        )
        self.assertEqual(plan["runbook_id"], "storage_mount")
        self.assertGreater(plan["hypotheses"][0]["confidence"], 0.9)
        self.assertIn("AUTO_OPS_STATIC_PV", plan["evidence_gap"])

    def test_image_pull_auth_also_patches_service_account(self):
        old_secret = os.environ.get("DEFAULT_IMAGE_PULL_SECRET")
        os.environ["DEFAULT_IMAGE_PULL_SECRET"] = "example-registry-secret"
        try:
            pod = {**self.pod, "service_account": "deploy-sa"}
            plan = build_remediation_plan(
                self.alert,
                {"root_cause": "ImagePullBackOff unauthorized authentication required"},
                {
                    "pod": pod,
                    "pods": [pod],
                    "events": {"events": [{"reason": "Failed", "message": "Failed to pull image: unauthorized"}]},
                },
            )
            actions = [change["type"] for change in plan["changes"]]
            self.assertIn("patch_workload", actions)
            self.assertIn("patch_service_account", actions)
            sa_change = next(change for change in plan["changes"] if change["type"] == "patch_service_account")
            self.assertEqual(sa_change["service_account"], "deploy-sa")
            self.assertEqual(sa_change["image_pull_secret"], "example-registry-secret")
        finally:
            if old_secret is None:
                os.environ.pop("DEFAULT_IMAGE_PULL_SECRET", None)
            else:
                os.environ["DEFAULT_IMAGE_PULL_SECRET"] = old_secret

    def test_image_architecture_uses_rollback_when_previous_image_is_known(self):
        pod = {
            **self.pod,
            "containers": [{
                **self.pod["containers"][0],
                "previous_image": "registry.example.com/api:stable-amd64",
            }],
        }
        plan = build_remediation_plan(
            self.alert,
            {"root_cause": "CrashLoopBackOff exec format error because image is arm64 on amd64 node"},
            {
                "pod": pod,
                "pods": [pod],
                "events": {"events": [{"reason": "BackOff", "message": "exec format error"}]},
            },
        )
        self.assertEqual(plan["runbook_id"], "image_architecture")
        self.assertEqual(plan["changes"][0]["type"], "rollback_workload")
        image = plan["changes"][0]["patch"]["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(image, "registry.example.com/api:stable-amd64")

    def test_image_architecture_uses_approved_replacement_mapping(self):
        old_mapping = os.environ.get("AUTO_OPS_IMAGE_REPLACEMENTS_JSON")
        os.environ["AUTO_OPS_IMAGE_REPLACEMENTS_JSON"] = json.dumps({
            "registry.example.com/api:arm64": {"amd64": "registry.example.com/api:stable-amd64"}
        })
        try:
            pod = {
                **self.pod,
                "containers": [{
                    **self.pod["containers"][0],
                    "image": "registry.example.com/api:arm64",
                }],
            }
            plan = build_remediation_plan(
                self.alert,
                {"root_cause": "CrashLoopBackOff exec format error because image is arm64 on amd64 node"},
                {
                    "pod": pod,
                    "pods": [pod],
                    "node": {"labels": {"kubernetes.io/arch": "amd64"}},
                    "events": {"events": [{"reason": "BackOff", "message": "exec format error"}]},
                },
            )
            self.assertEqual(plan["runbook_id"], "image_architecture")
            self.assertEqual(plan["changes"][0]["type"], "rollback_workload")
            image = plan["changes"][0]["patch"]["spec"]["template"]["spec"]["containers"][0]["image"]
            self.assertEqual(image, "registry.example.com/api:stable-amd64")
            self.assertTrue(server._validate_workload_patch(plan["changes"][0]["patch"])[0])
        finally:
            if old_mapping is None:
                os.environ.pop("AUTO_OPS_IMAGE_REPLACEMENTS_JSON", None)
            else:
                os.environ["AUTO_OPS_IMAGE_REPLACEMENTS_JSON"] = old_mapping

    def test_image_architecture_uses_same_multi_arch_mapping_without_node_label(self):
        old_mapping = os.environ.get("AUTO_OPS_IMAGE_REPLACEMENTS_JSON")
        os.environ["AUTO_OPS_IMAGE_REPLACEMENTS_JSON"] = json.dumps({
            "registry.example.com/api:bad-execformat": {
                "amd64": "registry.example.com/api:stable",
                "arm64": "registry.example.com/api:stable",
            }
        })
        try:
            pod = {
                **self.pod,
                "containers": [{
                    **self.pod["containers"][0],
                    "image": "registry.example.com/api:bad-execformat",
                    "reason": "CrashLoopBackOff",
                }],
            }
            plan = build_remediation_plan(
                self.alert,
                {"root_cause": "CrashLoopBackOff exec format error"},
                {
                    "pod": pod,
                    "pods": [pod],
                    "logs": {"api": "exec /badbin: exec format error"},
                    "events": {"events": [{"reason": "BackOff", "message": "Back-off restarting failed container"}]},
                },
            )
            self.assertEqual(plan["runbook_id"], "image_architecture")
            self.assertEqual(plan["changes"][0]["type"], "rollback_workload")
            image = plan["changes"][0]["patch"]["spec"]["template"]["spec"]["containers"][0]["image"]
            self.assertEqual(image, "registry.example.com/api:stable")
        finally:
            if old_mapping is None:
                os.environ.pop("AUTO_OPS_IMAGE_REPLACEMENTS_JSON", None)
            else:
                os.environ["AUTO_OPS_IMAGE_REPLACEMENTS_JSON"] = old_mapping

    def test_image_pull_bad_tag_uses_approved_replacement_before_secret_patch(self):
        old_mapping = os.environ.get("AUTO_OPS_IMAGE_REPLACEMENTS_JSON")
        old_secret = os.environ.get("DEFAULT_IMAGE_PULL_SECRET")
        os.environ["AUTO_OPS_IMAGE_REPLACEMENTS_JSON"] = json.dumps({
            "registry.example.com/api:bad-tag": "registry.example.com/api:stable"
        })
        os.environ["DEFAULT_IMAGE_PULL_SECRET"] = "example-registry-secret"
        try:
            pod = {
                **self.pod,
                "containers": [{
                    **self.pod["containers"][0],
                    "image": "registry.example.com/api:bad-tag",
                    "reason": "ImagePullBackOff",
                }],
            }
            plan = build_remediation_plan(
                self.alert,
                {"root_cause": "ImagePullBackOff pull access denied repository does not exist"},
                {
                    "pod": pod,
                    "pods": [pod],
                    "events": {"events": [{
                        "reason": "Failed",
                        "message": "Failed to pull image registry.example.com/api:bad-tag: repository does not exist",
                    }]},
                },
            )
            self.assertEqual(plan["runbook_id"], "image_auth")
            self.assertEqual(plan["changes"][0]["type"], "rollback_workload")
            image = plan["changes"][0]["patch"]["spec"]["template"]["spec"]["containers"][0]["image"]
            self.assertEqual(image, "registry.example.com/api:stable")
            self.assertNotIn("patch_service_account", [change["type"] for change in plan["changes"]])
        finally:
            if old_mapping is None:
                os.environ.pop("AUTO_OPS_IMAGE_REPLACEMENTS_JSON", None)
            else:
                os.environ["AUTO_OPS_IMAGE_REPLACEMENTS_JSON"] = old_mapping
            if old_secret is None:
                os.environ.pop("DEFAULT_IMAGE_PULL_SECRET", None)
            else:
                os.environ["DEFAULT_IMAGE_PULL_SECRET"] = old_secret

    def test_missing_configmap_uses_approved_template_only(self):
        old_templates = os.environ.get("AUTO_OPS_CONFIGMAP_TEMPLATES_JSON")
        os.environ["AUTO_OPS_CONFIGMAP_TEMPLATES_JSON"] = json.dumps({
            "prod/app-config": {"data": {"APP_MODE": "prod", "FEATURE_FLAG": "stable"}}
        })
        try:
            workload = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{
                                "name": "api",
                                "envFrom": [{"configMapRef": {"name": "app-config"}}],
                            }]
                        }
                    }
                }
            }
            plan = build_remediation_plan(
                self.alert,
                {"root_cause": 'ConfigMap "app-config" not found'},
                {
                    "pod": self.pod,
                    "pods": [self.pod],
                    "workload": workload,
                    "events": {"events": [{"reason": "Failed", "message": 'configmap "app-config" not found'}]},
                },
            )
            self.assertEqual(plan["runbook_id"], "config_missing")
            self.assertEqual(plan["changes"][0]["type"], "create_configmap")
            self.assertEqual(plan["changes"][0]["manifest"]["metadata"]["name"], "app-config")
            self.assertEqual(plan["changes"][0]["manifest"]["data"]["APP_MODE"], "prod")
        finally:
            if old_templates is None:
                os.environ.pop("AUTO_OPS_CONFIGMAP_TEMPLATES_JSON", None)
            else:
                os.environ["AUTO_OPS_CONFIGMAP_TEMPLATES_JSON"] = old_templates

    def test_pending_pvc_generates_static_pv_when_template_is_configured(self):
        old_server = os.environ.get("AUTO_OPS_STATIC_PV_NFS_SERVER")
        old_path = os.environ.get("AUTO_OPS_STATIC_PV_NFS_BASE_PATH")
        os.environ["AUTO_OPS_STATIC_PV_NFS_SERVER"] = "192.0.2.10"
        os.environ["AUTO_OPS_STATIC_PV_NFS_BASE_PATH"] = "/exports/k8s"
        try:
            plan = build_remediation_plan(
                self.alert,
                {"root_cause": "FailedMount persistentvolumeclaim data-vol has no persistent volumes available"},
                {
                    "pod": self.pod,
                    "pods": [self.pod],
                    "storage": [{
                        "volume": "data", "pvc": "data-vol", "pvc_phase": "Pending",
                        "requested": "5Gi", "access_modes": ["ReadWriteMany"], "storage_class": "nfs-static",
                    }],
                    "events": {"events": [{"reason": "FailedMount", "message": "no persistent volumes available for this claim"}]},
                },
            )
            self.assertEqual(plan["changes"][0]["type"], "create_pv")
            self.assertEqual(plan["changes"][0]["manifest"]["spec"]["claimRef"]["name"], "data-vol")
            self.assertEqual(plan["changes"][0]["manifest"]["spec"]["nfs"]["server"], "192.0.2.10")
        finally:
            if old_server is None:
                os.environ.pop("AUTO_OPS_STATIC_PV_NFS_SERVER", None)
            else:
                os.environ["AUTO_OPS_STATIC_PV_NFS_SERVER"] = old_server
            if old_path is None:
                os.environ.pop("AUTO_OPS_STATIC_PV_NFS_BASE_PATH", None)
            else:
                os.environ["AUTO_OPS_STATIC_PV_NFS_BASE_PATH"] = old_path

    def test_pending_pvc_can_use_explicit_local_static_pv_for_e2e_only(self):
        old_allow = os.environ.get("AUTO_OPS_ALLOW_LOCAL_STATIC_PV")
        old_base = os.environ.get("AUTO_OPS_STATIC_PV_LOCAL_BASE_PATH")
        old_node = os.environ.get("AUTO_OPS_STATIC_PV_LOCAL_NODE")
        old_sc = os.environ.get("AUTO_OPS_STATIC_PV_STORAGE_CLASS")
        os.environ["AUTO_OPS_ALLOW_LOCAL_STATIC_PV"] = "true"
        os.environ["AUTO_OPS_STATIC_PV_LOCAL_BASE_PATH"] = "/tmp/flawless-static-pv"
        os.environ["AUTO_OPS_STATIC_PV_LOCAL_NODE"] = "worker-a"
        os.environ["AUTO_OPS_STATIC_PV_STORAGE_CLASS"] = "manual-static"
        try:
            plan = build_remediation_plan(
                self.alert,
                {"root_cause": "FailedScheduling persistentvolumeclaim data-vol is pending and has no matching pv"},
                {
                    "pod": self.pod,
                    "pods": [self.pod],
                    "storage": [{
                        "volume": "data", "pvc": "data-vol", "pvc_phase": "Pending",
                        "requested": "256Mi", "access_modes": ["ReadWriteOnce"], "storage_class": "manual-static",
                    }],
                    "events": {"events": [{"reason": "FailedScheduling", "message": "pod has unbound immediate PersistentVolumeClaims"}]},
                },
            )
            self.assertEqual(plan["changes"][0]["type"], "create_pv")
            manifest = plan["changes"][0]["manifest"]
            self.assertEqual(manifest["spec"]["local"]["path"], "/tmp/flawless-static-pv/prod/data-vol")
            self.assertEqual(
                manifest["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"],
                ["worker-a"],
            )
            self.assertTrue(server._validate_storage_manifest(manifest, "PersistentVolume")[0])
        finally:
            for key, old in {
                "AUTO_OPS_ALLOW_LOCAL_STATIC_PV": old_allow,
                "AUTO_OPS_STATIC_PV_LOCAL_BASE_PATH": old_base,
                "AUTO_OPS_STATIC_PV_LOCAL_NODE": old_node,
                "AUTO_OPS_STATIC_PV_STORAGE_CLASS": old_sc,
            }.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old

    def test_workload_evidence_drops_literal_env_values(self):
        test_credential = "-".join(("redaction", "fixture", "value"))
        raw = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "api", "namespace": "prod"},
            "spec": {"template": {"spec": {"containers": [{
                "name": "api",
                "image": "example/api:v1",
                "env": [{"name": "DB_PASSWORD", "value": test_credential}],
            }]}}},
        }
        rendered = str(server._safe_workload_evidence(raw))
        self.assertNotIn(test_credential, rendered)
        self.assertIn("literal-present", rendered)


if __name__ == "__main__":
    unittest.main()
