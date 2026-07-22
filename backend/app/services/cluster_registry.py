"""Encrypted registry for Kubernetes clusters connected without Rancher."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from typing import Any

import yaml
from cryptography.fernet import Fernet, InvalidToken
from kubernetes import client
from kubernetes.config import kube_config
from kubernetes.dynamic import DynamicClient
from kubernetes.stream import stream


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClusterRegistry:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.getenv("CLUSTER_REGISTRY_PATH", "/var/lib/flawless/clusters.db"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connect().close()
        except (OSError, sqlite3.Error) as exc:
            primary_path = self.path
            fallback_path = Path(os.getenv("CLUSTER_REGISTRY_FALLBACK_PATH", "/tmp/flawless-clusters.db"))
            if fallback_path == primary_path:
                raise
            self.path = fallback_path
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connect().close()
            logger.warning(
                "cluster registry path %s is not writable (%s); using non-primary path %s",
                primary_path,
                exc,
                self.path,
            )
        self._lock = threading.RLock()
        self._fernet = Fernet(self._key())
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _key(self) -> bytes:
        configured = os.getenv("CLUSTER_CREDENTIAL_ENCRYPTION_KEY", "").strip().encode()
        if configured:
            try:
                Fernet(configured)
                return configured
            except ValueError as exc:
                raise RuntimeError("CLUSTER_CREDENTIAL_ENCRYPTION_KEY 必须是 Fernet key") from exc
        key_path = self.path.with_suffix(self.path.suffix + ".key")
        if key_path.exists():
            return key_path.read_bytes().strip()
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        key_path.chmod(0o600)
        return key

    def _init_schema(self) -> None:
        with closing(self._connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS managed_clusters (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    context_name TEXT NOT NULL,
                    server_fingerprint TEXT NOT NULL,
                    encrypted_kubeconfig BLOB NOT NULL,
                    status TEXT NOT NULL,
                    version TEXT NOT NULL DEFAULT '',
                    node_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL DEFAULT ''
                )
            """)

    @staticmethod
    def parse(
        content: str,
        selected_context: str = "",
        bearer_token: str = "",
    ) -> tuple[dict[str, Any], str, str]:
        try:
            payload = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"kubeconfig YAML 无效：{exc}") from exc
        contexts = {str(item.get("name")): item for item in payload.get("contexts") or [] if item.get("name")}
        context_name = selected_context or str(payload.get("current-context") or "")
        if not context_name or context_name not in contexts:
            raise ValueError("请选择 kubeconfig 中有效的 context")
        cluster_name = str((contexts[context_name].get("context") or {}).get("cluster") or "")
        clusters = {str(item.get("name")): item.get("cluster") or {} for item in payload.get("clusters") or []}
        server = str((clusters.get(cluster_name) or {}).get("server") or "")
        if not server.startswith(("https://", "http://")):
            raise ValueError("kubeconfig 缺少有效 Kubernetes API server")
        # KubeConfigLoader 会执行 user.exec 认证插件。Web 上传阶段绝不能把一份
        # 配置文件变成绕过审批的服务端命令执行入口，因此这里只接受内嵌凭据。
        users: dict[str, dict[str, Any]] = {}
        for item in payload.get("users") or []:
            if not item.get("name"):
                continue
            user_payload = item.get("user")
            if not isinstance(user_payload, dict):
                user_payload = {}
                item["user"] = user_payload
            users[str(item["name"])] = user_payload
        user_name = str((contexts[context_name].get("context") or {}).get("user") or "")
        selected_user = users.get(user_name) or {}
        token = str(bearer_token or "").strip()
        if token:
            if user_name not in users:
                raise ValueError("所选 context 没有关联有效 user，无法注入动态 Token")
            # Dynamic enterprise tokens are pasted separately.  Replace, rather
            # than execute, an exec/auth-provider stanza so uploaded YAML can
            # never trigger a local command on the server.
            selected_user.clear()
            selected_user["token"] = token
        elif selected_user.get("exec") or selected_user.get("auth-provider"):
            raise ValueError("Web kubeconfig 不允许 exec/auth-provider 动态认证；请使用内嵌 Token/证书，或通过 Rancher 纳管")
        forbidden_file_fields = {
            "client-certificate", "client-key", "tokenFile", "passwordFile",
        }
        if forbidden_file_fields & set(selected_user):
            raise ValueError("Web kubeconfig 不允许引用服务端本地凭据文件；请改用内嵌 Token/证书")
        selected_cluster = clusters.get(cluster_name) or {}
        if selected_cluster.get("certificate-authority"):
            raise ValueError("Web kubeconfig 不允许引用服务端 CA 文件；请使用 certificate-authority-data")
        return payload, context_name, server

    def contexts(self, content: str) -> list[dict[str, Any]]:
        payload = yaml.safe_load(content) or {}
        current = str(payload.get("current-context") or "")
        return [{"name": str(item.get("name")), "current": str(item.get("name")) == current}
                for item in payload.get("contexts") or [] if item.get("name")]

    def save(
        self,
        *,
        content: str,
        name: str = "",
        context_name: str = "",
        cluster_id: str = "",
        bearer_token: str = "",
    ) -> dict[str, Any]:
        payload, selected, server = self.parse(content, context_name, bearer_token)
        serialized = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        cid = cluster_id or f"kube-{uuid.uuid4().hex[:12]}"
        display_name = name.strip() or selected
        now = _now()
        encrypted = self._fernet.encrypt(serialized.encode())
        fingerprint = hashlib.sha256(server.encode()).hexdigest()[:16]
        with self._lock, closing(self._connect()) as db, db:
            db.execute("""
                INSERT INTO managed_clusters
                    (id,name,context_name,server_fingerprint,encrypted_kubeconfig,status,created_at,updated_at)
                VALUES (?,?,?,?,?,'pending',?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, context_name=excluded.context_name,
                    server_fingerprint=excluded.server_fingerprint, encrypted_kubeconfig=excluded.encrypted_kubeconfig,
                    status='pending', last_error='', updated_at=excluded.updated_at
            """, (cid, display_name, selected, fingerprint, encrypted, now, now))
        return self.get(cid)

    def list(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM managed_clusters ORDER BY name,id").fetchall()
        return [self._public(row) for row in rows]

    def get(self, cluster_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM managed_clusters WHERE id=?", (cluster_id,)).fetchone()
        if row is None:
            raise KeyError(cluster_id)
        return self._public(row)

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys() if key != "encrypted_kubeconfig"}

    def configuration(self, cluster_id: str) -> client.Configuration:
        with closing(self._connect()) as db:
            row = db.execute("SELECT context_name,encrypted_kubeconfig FROM managed_clusters WHERE id=?", (cluster_id,)).fetchone()
        if row is None:
            raise KeyError(cluster_id)
        try:
            payload = yaml.safe_load(self._fernet.decrypt(row["encrypted_kubeconfig"]).decode())
        except InvalidToken as exc:
            raise RuntimeError("集群凭据无法解密；请检查加密密钥") from exc
        return self._configuration_from_payload(payload, row["context_name"])

    @staticmethod
    def _configuration_from_payload(payload: dict[str, Any], context_name: str) -> client.Configuration:
        loader = kube_config.KubeConfigLoader(config_dict=payload, active_context=context_name)
        configuration = client.Configuration()
        loader.load_and_set(configuration)
        return configuration

    def probe_content(
        self,
        *,
        content: str,
        context_name: str = "",
        bearer_token: str = "",
    ) -> dict[str, Any]:
        """Validate uploaded credentials without modifying the stored registry."""
        payload, selected, _ = self.parse(content, context_name, bearer_token)
        now = _now()
        try:
            api_client = client.ApiClient(self._configuration_from_payload(payload, selected))
            version = client.VersionApi(api_client).get_code().git_version or ""
            nodes = client.CoreV1Api(api_client).list_node(limit=500).items
            return {
                "status": "connected",
                "version": version,
                "node_count": len(nodes),
                "last_error": "",
                "last_checked_at": now,
            }
        except Exception as exc:
            return {
                "status": "unreachable",
                "version": "",
                "node_count": 0,
                "last_error": f"{type(exc).__name__}: {exc}"[:2000],
                "last_checked_at": now,
            }

    def save_verified(
        self,
        *,
        content: str,
        name: str = "",
        context_name: str = "",
        cluster_id: str = "",
        bearer_token: str = "",
    ) -> dict[str, Any]:
        """Probe first, then persist so a failed update cannot replace good credentials."""
        verified = self.probe_content(
            content=content,
            context_name=context_name,
            bearer_token=bearer_token,
        )
        if verified["status"] != "connected":
            return verified
        saved = self.save(
            content=content,
            name=name,
            context_name=context_name,
            cluster_id=cluster_id,
            bearer_token=bearer_token,
        )
        now = _now()
        with self._lock, closing(self._connect()) as db, db:
            db.execute(
                """UPDATE managed_clusters
                   SET status='connected',version=?,node_count=?,last_error='',last_checked_at=?,updated_at=?
                   WHERE id=?""",
                (verified["version"], verified["node_count"], verified["last_checked_at"], now, saved["id"]),
            )
        return self.get(saved["id"])

    def refresh_token(self, cluster_id: str, bearer_token: str) -> dict[str, Any]:
        """Atomically verify and replace the token in an existing kubeconfig."""
        token = str(bearer_token or "").strip()
        if not token:
            raise ValueError("Bearer Token 不能为空")
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT name,context_name,encrypted_kubeconfig FROM managed_clusters WHERE id=?",
                (cluster_id,),
            ).fetchone()
        if row is None:
            raise KeyError(cluster_id)
        try:
            content = self._fernet.decrypt(row["encrypted_kubeconfig"]).decode()
        except InvalidToken as exc:
            raise RuntimeError("集群凭据无法解密；请检查加密密钥") from exc
        return self.save_verified(
            content=content,
            name=str(row["name"]),
            context_name=str(row["context_name"]),
            cluster_id=cluster_id,
            bearer_token=token,
        )

    def api_client(self, cluster_id: str) -> client.ApiClient:
        return client.ApiClient(self.configuration(cluster_id))

    @classmethod
    def configuration_from_content(cls, content: str, context_name: str = "") -> client.Configuration:
        """Build an in-memory client from trusted inline kubeconfig without persisting credentials."""
        payload, selected, _server = cls.parse(content, context_name)
        return cls._configuration_from_payload(payload, selected)

    def inventory(self, cluster_id: str) -> dict[str, Any]:
        api_client = self.api_client(cluster_id)
        core = client.CoreV1Api(api_client)
        apps = client.AppsV1Api(api_client)
        encode = api_client.sanitize_for_serialization
        nodes = encode(core.list_node(limit=500)).get("items", [])
        namespaces = encode(core.list_namespace(limit=1000)).get("items", [])
        pods = encode(core.list_pod_for_all_namespaces(limit=5000)).get("items", [])
        deployments = encode(apps.list_deployment_for_all_namespaces(limit=2000)).get("items", [])
        statefulsets = encode(apps.list_stateful_set_for_all_namespaces(limit=2000)).get("items", [])
        daemonsets = encode(apps.list_daemon_set_for_all_namespaces(limit=2000)).get("items", [])
        replicasets = encode(apps.list_replica_set_for_all_namespaces(limit=5000)).get("items", [])
        services = encode(core.list_service_for_all_namespaces(limit=3000)).get("items", [])
        networking = client.NetworkingV1Api(api_client)
        ingresses = encode(networking.list_ingress_for_all_namespaces(limit=2000)).get("items", [])
        return {
            "nodes": nodes,
            "namespaces": namespaces,
            "pods": pods,
            "deployments": deployments,
            "statefulsets": statefulsets,
            "daemonsets": daemonsets,
            "replicasets": replicasets,
            "services": services,
            "ingresses": ingresses,
        }

    def pod_diagnostics(self, cluster_id: str, *, namespace: str, pod_name: str, tail_lines: int = 180) -> dict[str, Any]:
        api_client = self.api_client(cluster_id)
        encode = api_client.sanitize_for_serialization
        core = client.CoreV1Api(api_client)
        apps = client.AppsV1Api(api_client)
        raw_pod = encode(core.read_namespaced_pod(pod_name, namespace))
        field_selector = f"involvedObject.name={pod_name}"
        events = encode(core.list_namespaced_event(namespace, field_selector=field_selector, limit=500)).get("items", [])
        logs: dict[str, Any] = {}
        pod_status = raw_pod.get("status") or {}
        for status in [
            *(pod_status.get("containerStatuses") or []),
            *(pod_status.get("initContainerStatuses") or []),
        ]:
            name = str(status.get("name") or "")
            if not name:
                continue
            current = previous = current_error = previous_error = ""
            try:
                current = core.read_namespaced_pod_log(pod_name, namespace, container=name, tail_lines=tail_lines)
            except Exception as exc:
                current_error = f"{type(exc).__name__}: {exc}"
            if int(status.get("restartCount") or 0) > 0:
                try:
                    previous = core.read_namespaced_pod_log(pod_name, namespace, container=name, previous=True, tail_lines=tail_lines)
                except Exception as exc:
                    previous_error = f"{type(exc).__name__}: {exc}"
            logs[name] = {"current": current[-10000:], "previous": previous[-10000:], "current_error": current_error, "previous_error": previous_error}
        workload: dict[str, Any] = {}
        owners = (raw_pod.get("metadata") or {}).get("ownerReferences") or []
        owner = owners[0] if owners else {}
        if owner.get("kind") == "ReplicaSet":
            replica = encode(apps.read_namespaced_replica_set(owner.get("name"), namespace))
            replica_owners = (replica.get("metadata") or {}).get("ownerReferences") or []
            owner = replica_owners[0] if replica_owners else owner
        readers = {
            "Deployment": apps.read_namespaced_deployment,
            "StatefulSet": apps.read_namespaced_stateful_set,
            "DaemonSet": apps.read_namespaced_daemon_set,
        }
        if owner.get("kind") in readers and owner.get("name"):
            workload = encode(readers[owner["kind"]](owner["name"], namespace))
        storage = []
        for volume in (raw_pod.get("spec") or {}).get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if not claim:
                continue
            try:
                pvc = encode(core.read_namespaced_persistent_volume_claim(claim, namespace))
                pv_name = (pvc.get("spec") or {}).get("volumeName")
                pv = encode(core.read_persistent_volume(pv_name)) if pv_name else {}
                storage.append({"volume": volume.get("name"), "pvc": claim, "pvc_phase": (pvc.get("status") or {}).get("phase"), "requested": ((((pvc.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage")), "capacity": ((pvc.get("status") or {}).get("capacity") or {}).get("storage"), "storage_class": (pvc.get("spec") or {}).get("storageClassName"), "access_modes": (pvc.get("spec") or {}).get("accessModes") or [], "pv": pv_name, "pv_phase": (pv.get("status") or {}).get("phase"), "csi_driver": (((pv.get("spec") or {}).get("csi") or {}).get("driver")), "nfs": bool((pv.get("spec") or {}).get("nfs"))})
            except Exception as exc:
                storage.append({"volume": volume.get("name"), "pvc": claim, "error": f"{type(exc).__name__}: {exc}"})
        pod_labels = (raw_pod.get("metadata") or {}).get("labels") or {}
        services = []
        try:
            discovery = client.DiscoveryV1Api(api_client)
            for service in encode(core.list_namespaced_service(namespace, limit=1000)).get("items", []):
                selector = (service.get("spec") or {}).get("selector") or {}
                if not selector or not all(pod_labels.get(key) == value for key, value in selector.items()):
                    continue
                service_name = str((service.get("metadata") or {}).get("name") or "")
                slices = encode(discovery.list_namespaced_endpoint_slice(
                    namespace,
                    label_selector=f"kubernetes.io/service-name={service_name}",
                    limit=1000,
                )).get("items", [])
                services.append({
                    "name": service_name,
                    "type": (service.get("spec") or {}).get("type"),
                    "selector": selector,
                    "ports": (service.get("spec") or {}).get("ports") or [],
                    "ready_endpoints": sum(
                        1
                        for endpoint_slice in slices
                        for endpoint in endpoint_slice.get("endpoints") or []
                        if (endpoint.get("conditions") or {}).get("ready") is not False
                    ),
                    "endpoint_slices": len(slices),
                })
        except Exception as exc:
            services = [{"error": f"{type(exc).__name__}: {exc}"}]
        node = {}
        node_name = str((raw_pod.get("spec") or {}).get("nodeName") or "")
        if node_name:
            try:
                raw_node = encode(core.read_node(node_name))
                node = {
                    "name": node_name,
                    "unschedulable": bool((raw_node.get("spec") or {}).get("unschedulable")),
                    "conditions": (raw_node.get("status") or {}).get("conditions") or [],
                    "capacity": (raw_node.get("status") or {}).get("capacity") or {},
                    "allocatable": (raw_node.get("status") or {}).get("allocatable") or {},
                    "taints": (raw_node.get("spec") or {}).get("taints") or [],
                }
            except Exception as exc:
                node = {"name": node_name, "error": f"{type(exc).__name__}: {exc}"}
        # Decode referenced Secret data inside this bounded service method, then
        # discard the values. Only key names, byte lengths and validation state
        # may enter evidence/model/audit payloads.
        secret_names: set[str] = set()
        pod_spec = raw_pod.get("spec") or {}
        for volume in pod_spec.get("volumes") or []:
            name = ((volume.get("secret") or {}).get("secretName"))
            if name:
                secret_names.add(str(name))
        for container in [*(pod_spec.get("initContainers") or []), *(pod_spec.get("containers") or [])]:
            for source in container.get("envFrom") or []:
                name = ((source.get("secretRef") or {}).get("name"))
                if name:
                    secret_names.add(str(name))
            for env in container.get("env") or []:
                name = ((((env.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")))
                if name:
                    secret_names.add(str(name))
        credential_evidence = []
        for secret_name in sorted(secret_names):
            try:
                secret = encode(core.read_namespaced_secret(secret_name, namespace))
                fields = []
                for key, encoded in sorted((secret.get("data") or {}).items()):
                    try:
                        decoded = base64.b64decode(str(encoded), validate=True)
                        fields.append({"key": key, "decoded": True, "non_empty": bool(decoded), "bytes": len(decoded)})
                    except Exception:
                        fields.append({"key": key, "decoded": False, "non_empty": False, "bytes": 0})
                credential_evidence.append({"name": secret_name, "type": secret.get("type") or "Opaque", "fields": fields})
            except Exception as exc:
                credential_evidence.append({"name": secret_name, "error": f"{type(exc).__name__}: {exc}"})
        return {
            "raw_pod": raw_pod,
            "events": events,
            "logs": logs,
            "workload": workload,
            "storage": storage,
            "services": services,
            "node": node,
            "credential_evidence": credential_evidence,
            "source": "kubeconfig",
        }

    def apply_manifest(self, cluster_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        api_version = str(manifest.get("apiVersion") or "")
        kind = str(manifest.get("kind") or "")
        metadata = manifest.get("metadata") or {}
        name = str(metadata.get("name") or "")
        namespace = str(metadata.get("namespace") or "")
        if not api_version or not kind or not name:
            raise ValueError("manifest 必须包含 apiVersion、kind 和 metadata.name")
        resource = DynamicClient(self.api_client(cluster_id)).resources.get(api_version=api_version, kind=kind)
        kwargs = {"name": name, "body": manifest, "content_type": "application/merge-patch+json"}
        if resource.namespaced:
            kwargs["namespace"] = namespace or "default"
        try:
            result = resource.patch(**kwargs)
            operation = "patched"
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
            create_args = {"body": manifest}
            if resource.namespaced:
                create_args["namespace"] = namespace or "default"
            result = resource.create(**create_args)
            operation = "created"
        return {"operation": operation, "resource": result.to_dict() if hasattr(result, "to_dict") else dict(result)}

    def patch_resource(self, cluster_id: str, *, api_version: str, kind: str, name: str, namespace: str, patch: dict[str, Any]) -> dict[str, Any]:
        resource = DynamicClient(self.api_client(cluster_id)).resources.get(api_version=api_version, kind=kind)
        kwargs = {"name": name, "body": patch, "content_type": "application/merge-patch+json"}
        if resource.namespaced:
            kwargs["namespace"] = namespace or "default"
        result = resource.patch(**kwargs)
        return result.to_dict() if hasattr(result, "to_dict") else dict(result)

    def delete_resource(self, cluster_id: str, *, api_version: str, kind: str, name: str, namespace: str) -> dict[str, Any]:
        resource = DynamicClient(self.api_client(cluster_id)).resources.get(api_version=api_version, kind=kind)
        kwargs: dict[str, Any] = {"name": name, "body": client.V1DeleteOptions(propagation_policy="Background")}
        if resource.namespaced:
            kwargs["namespace"] = namespace or "default"
        result = resource.delete(**kwargs)
        return result.to_dict() if hasattr(result, "to_dict") else {"status": "accepted"}

    def exec_pod(self, cluster_id: str, *, namespace: str, pod_name: str, container_name: str, command: str, timeout_seconds: int) -> dict[str, Any]:
        return self.exec_pod_with_configuration(
            self.configuration(cluster_id),
            namespace=namespace,
            pod_name=pod_name,
            container_name=container_name,
            command=command,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def exec_pod_with_configuration(configuration: client.Configuration, *, namespace: str, pod_name: str, container_name: str, command: str, timeout_seconds: int) -> dict[str, Any]:
        core = client.CoreV1Api(client.ApiClient(configuration))
        response = stream(
            core.connect_get_namespaced_pod_exec,
            pod_name,
            namespace or "default",
            container=container_name or None,
            command=["/bin/sh", "-c", command],
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _request_timeout=max(10, min(timeout_seconds, 900)),
            _preload_content=False,
        )
        stdout: list[str] = []
        stderr: list[str] = []
        exit_code = 0
        deadline = time.monotonic() + max(1, min(timeout_seconds, 900))
        try:
            while response.is_open():
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"pod command exceeded {timeout_seconds}s")
                response.update(timeout=1)
                while response.peek_stdout():
                    stdout.append(response.read_stdout())
                while response.peek_stderr():
                    stderr.append(response.read_stderr())
                if response.peek_channel(3):
                    status = json.loads(response.read_channel(3) or "{}")
                    if status.get("status") == "Failure":
                        causes = ((status.get("details") or {}).get("causes") or [])
                        exit_code = next(
                            (int(item.get("message")) for item in causes if item.get("reason") == "ExitCode"),
                            1,
                        )
            return {
                "exit_code": exit_code,
                "stdout": "".join(stdout)[-100_000:],
                "stderr": "".join(stderr)[-100_000:],
            }
        finally:
            response.close()

    def exec_node(self, cluster_id: str, *, node_name: str, command: str, timeout_seconds: int, image: str = "busybox:1.36") -> dict[str, Any]:
        return self.exec_node_with_configuration(
            self.configuration(cluster_id),
            node_name=node_name,
            command=command,
            timeout_seconds=timeout_seconds,
            image=image,
        )

    @staticmethod
    def exec_node_with_configuration(configuration: client.Configuration, *, node_name: str, command: str, timeout_seconds: int, image: str = "busybox:1.36") -> dict[str, Any]:
        core = client.CoreV1Api(client.ApiClient(configuration))
        pod_name = f"flawless-node-exec-{uuid.uuid4().hex[:8]}"
        namespace = os.getenv("NODE_EXEC_NAMESPACE", "k8s-agent")
        image_pull_secret = os.getenv("DEFAULT_IMAGE_PULL_SECRET", "").strip()
        body = client.V1Pod(
            metadata=client.V1ObjectMeta(name=pod_name, labels={"app.kubernetes.io/managed-by": "flawless", "flawless.io/purpose": "node-exec"}),
            spec=client.V1PodSpec(
                node_name=node_name,
                restart_policy="Never",
                host_pid=True,
                host_network=True,
                image_pull_secrets=(
                    [client.V1LocalObjectReference(name=image_pull_secret)]
                    if image_pull_secret
                    else None
                ),
                containers=[client.V1Container(
                    name="executor",
                    image=image,
                    security_context=client.V1SecurityContext(privileged=True),
                    command=["/bin/sh", "-c", f"chroot /host /bin/sh -c {json.dumps(command)}"],
                    volume_mounts=[client.V1VolumeMount(name="host", mount_path="/host")],
                )],
                volumes=[client.V1Volume(name="host", host_path=client.V1HostPathVolumeSource(path="/"))],
            ),
        )
        core.create_namespaced_pod(namespace, body)
        deadline = time.monotonic() + max(10, min(timeout_seconds, 900))
        phase = ""
        try:
            while time.monotonic() < deadline:
                phase = core.read_namespaced_pod_status(pod_name, namespace).status.phase or ""
                if phase in {"Succeeded", "Failed"}:
                    break
                time.sleep(1)
            output = core.read_namespaced_pod_log(pod_name, namespace, timestamps=True, tail_lines=5000)
            if phase not in {"Succeeded", "Failed"}:
                raise TimeoutError(f"node command exceeded {timeout_seconds}s")
            return {"exit_code": 0 if phase == "Succeeded" else 1, "phase": phase, "output": output[-100_000:]}
        finally:
            try:
                core.delete_namespaced_pod(pod_name, namespace, grace_period_seconds=0)
            except Exception:
                pass

    @staticmethod
    def run_shell(command: str, timeout_seconds: int) -> dict[str, Any]:
        completed = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=max(1, min(timeout_seconds, 900)),
            env={**os.environ, "HISTFILE": "/dev/null"},
        )
        return {"exit_code": completed.returncode, "stdout": completed.stdout[-100_000:], "stderr": completed.stderr[-100_000:]}

    def probe(self, cluster_id: str) -> dict[str, Any]:
        now = _now()
        try:
            api_client = client.ApiClient(self.configuration(cluster_id))
            version = client.VersionApi(api_client).get_code().git_version or ""
            nodes = client.CoreV1Api(api_client).list_node(limit=500).items
            status, error = "connected", ""
        except Exception as exc:
            version, nodes, status, error = "", [], "unreachable", f"{type(exc).__name__}: {exc}"
        with self._lock, closing(self._connect()) as db, db:
            db.execute("UPDATE managed_clusters SET status=?,version=?,node_count=?,last_error=?,last_checked_at=?,updated_at=? WHERE id=?",
                       (status, version, len(nodes), error[:2000], now, now, cluster_id))
        return self.get(cluster_id)

    def delete(self, cluster_id: str) -> bool:
        with self._lock, closing(self._connect()) as db, db:
            cursor = db.execute("DELETE FROM managed_clusters WHERE id=?", (cluster_id,))
        return cursor.rowcount > 0
