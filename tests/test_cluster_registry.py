import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kubernetes import client

from backend.app.services.cluster_registry import ClusterRegistry


CLUSTER_FIXTURE_CREDENTIAL = "-".join(("fixture", "cluster", "credential"))

KUBECONFIG = f"""
apiVersion: v1
kind: Config
clusters:
- name: local
  cluster:
    server: https://127.0.0.1:6443
    insecure-skip-tls-verify: true
contexts:
- name: operator@local
  context:
    cluster: local
    user: operator
current-context: operator@local
users:
- name: operator
  user:
    token: {CLUSTER_FIXTURE_CREDENTIAL}
"""


class ClusterRegistryTests(unittest.TestCase):
    def test_sqlite_open_failure_uses_configured_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "readonly" / "clusters.db"
            fallback = Path(directory) / "fallback" / "clusters.db"
            real_connect = sqlite3.connect

            def connect(path, *args, **kwargs):
                if Path(path) == primary:
                    raise sqlite3.OperationalError("unable to open database file")
                return real_connect(path, *args, **kwargs)

            with (
                patch.dict(os.environ, {"CLUSTER_REGISTRY_FALLBACK_PATH": str(fallback)}, clear=False),
                patch("backend.app.services.cluster_registry.sqlite3.connect", side_effect=connect),
            ):
                registry = ClusterRegistry(primary)

            self.assertEqual(registry.path, fallback)
            self.assertTrue(fallback.exists())

    def test_credentials_are_encrypted_and_cluster_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clusters.db"
            with patch.dict(os.environ, {"CLUSTER_REGISTRY_PATH": str(path)}, clear=False):
                registry = ClusterRegistry(path)
                saved = registry.save(content=KUBECONFIG, name="本地集群")
                self.assertEqual(saved["context_name"], "operator@local")
                self.assertNotIn("encrypted_kubeconfig", saved)
                self.assertNotIn(CLUSTER_FIXTURE_CREDENTIAL.encode("utf-8"), path.read_bytes())
                self.assertTrue(registry.delete(saved["id"]))
                self.assertEqual(registry.list(), [])

    def test_contexts_marks_current_context(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ClusterRegistry(Path(directory) / "clusters.db")
            self.assertEqual(registry.contexts(KUBECONFIG), [{"name": "operator@local", "current": True}])

    def test_executable_kubeconfig_auth_is_rejected(self):
        unsafe = KUBECONFIG.replace(
            f"token: {CLUSTER_FIXTURE_CREDENTIAL}",
            "exec:\n      apiVersion: client.authentication.k8s.io/v1\n      command: arbitrary-command",
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = ClusterRegistry(Path(directory) / "clusters.db")
            with self.assertRaisesRegex(ValueError, "不允许 exec"):
                registry.save(content=unsafe, name="unsafe")

    def test_dynamic_token_replaces_exec_without_executing_it(self):
        dynamic = KUBECONFIG.replace(
            f"token: {CLUSTER_FIXTURE_CREDENTIAL}",
            "exec:\n      apiVersion: client.authentication.k8s.io/v1\n      command: arbitrary-command",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clusters.db"
            registry = ClusterRegistry(path)
            dynamic_credential = "-".join(("fixture", "dynamic", "credential"))
            saved = registry.save(
                content=dynamic,
                name="dynamic-token-cluster",
                bearer_token=dynamic_credential,
            )
            authorization = registry.configuration(saved["id"]).api_key["authorization"]
            self.assertIn(dynamic_credential, authorization)
            self.assertNotIn(dynamic_credential.encode("utf-8"), path.read_bytes())
            self.assertNotIn(b"arbitrary-command", path.read_bytes())

    def test_dynamic_token_refresh_is_verified_before_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ClusterRegistry(Path(directory) / "clusters.db")
            registry.save(content=KUBECONFIG, name="dynamic", cluster_id="kube-dynamic")
            with patch.object(registry, "probe_content", return_value={
                "status": "connected",
                "version": "v1.30.0",
                "node_count": 2,
                "last_error": "",
                "last_checked_at": "now",
            }):
                rotated_credential = "-".join(("fixture", "rotated", "credential"))
                result = registry.refresh_token("kube-dynamic", rotated_credential)
            self.assertEqual(result["status"], "connected")
            self.assertIn(rotated_credential, registry.configuration("kube-dynamic").api_key["authorization"])

    def test_failed_verified_update_preserves_existing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ClusterRegistry(Path(directory) / "clusters.db")
            existing = registry.save(content=KUBECONFIG, name="原集群", cluster_id="kube-stable")
            before = registry.configuration(existing["id"]).api_key["authorization"]
            invalid_credential = "-".join(("fixture", "invalid", "credential"))
            replacement = KUBECONFIG.replace(CLUSTER_FIXTURE_CREDENTIAL, invalid_credential)
            with patch.object(registry, "probe_content", return_value={
                "status": "unreachable",
                "version": "",
                "node_count": 0,
                "last_error": "connection refused",
                "last_checked_at": "now",
            }):
                result = registry.save_verified(
                    content=replacement,
                    name="新名称",
                    cluster_id="kube-stable",
                )
            self.assertEqual(result["status"], "unreachable")
            self.assertEqual(registry.get("kube-stable")["name"], "原集群")
            self.assertEqual(registry.configuration("kube-stable").api_key["authorization"], before)

    def test_node_executor_uses_configured_private_registry_secret(self):
        configuration = client.Configuration()
        with (
            patch.dict(os.environ, {"DEFAULT_IMAGE_PULL_SECRET": "example-registry-secret"}, clear=False),
            patch("backend.app.services.cluster_registry.client.CoreV1Api") as core_api,
        ):
            core = core_api.return_value
            core.read_namespaced_pod_status.return_value = SimpleNamespace(
                status=SimpleNamespace(phase="Succeeded")
            )
            core.read_namespaced_pod_log.return_value = "completed"

            result = ClusterRegistry.exec_node_with_configuration(
                configuration,
                node_name="worker-1",
                command="true",
                timeout_seconds=30,
                image="registry.example.com/platform/flawless-node-exec:1.36",
            )

        pod = core.create_namespaced_pod.call_args.args[1]
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(pod.spec.image_pull_secrets[0].name, "example-registry-secret")


if __name__ == "__main__":
    unittest.main()
