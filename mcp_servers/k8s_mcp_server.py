import os
import json
import ssl
import base64
import tempfile
from typing import Optional
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("k8s-mcp-server")

# ----------------------------------------------------------------------
# 通用 Kubernetes API 访问工具（使用 urllib，已通过认证验证）
# ----------------------------------------------------------------------
_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_CA_FILE    = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_HOST       = "https://kubernetes.default.svc"

_TOKEN = ""
_CLIENT_CERT_FILES: list[str] = []
_ACCESS_MODE = "unconfigured"


def _write_temp_pem(content: str | bytes, suffix: str) -> str:
    data = content if isinstance(content, bytes) else content.encode()
    handle = tempfile.NamedTemporaryFile(prefix="flawless-kube-", suffix=suffix, delete=False)
    handle.write(data)
    handle.close()
    _CLIENT_CERT_FILES.append(handle.name)
    return handle.name


def _decode_kubeconfig_blob(value: str | None) -> bytes | None:
    if not value:
        return None
    return base64.b64decode(value.encode())


def _load_kubeconfig_access() -> tuple[str, str, ssl.SSLContext]:
    """加载本地 kubeconfig，用于演示机/开发机直接运行 MCP。

    生产容器内优先使用 ServiceAccount。只有不存在集群内 token 时，才会进入
    kubeconfig 兼容模式，避免影响正式 K8s 部署。
    """
    import yaml

    kubeconfig = os.getenv("KUBECONFIG") or os.path.expanduser("~/.kube/config")
    with open(kubeconfig, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    current_context = os.getenv("KUBE_CONTEXT") or config.get("current-context")
    contexts = {item.get("name"): item.get("context") or {} for item in config.get("contexts", [])}
    clusters = {item.get("name"): item.get("cluster") or {} for item in config.get("clusters", [])}
    users = {item.get("name"): item.get("user") or {} for item in config.get("users", [])}
    context = contexts.get(current_context) or {}
    cluster = clusters.get(context.get("cluster")) or {}
    user = users.get(context.get("user")) or {}
    host = str(cluster.get("server") or "").rstrip("/")
    if not host:
        raise RuntimeError(f"kubeconfig {kubeconfig} does not contain a server for context {current_context}")

    if cluster.get("insecure-skip-tls-verify"):
        ctx = ssl._create_unverified_context()
    else:
        ca_data = _decode_kubeconfig_blob(cluster.get("certificate-authority-data"))
        ca_file = cluster.get("certificate-authority")
        if ca_data:
            ca_file = _write_temp_pem(ca_data, ".ca.crt")
        ctx = ssl.create_default_context(cafile=ca_file)

    cert_data = _decode_kubeconfig_blob(user.get("client-certificate-data"))
    key_data = _decode_kubeconfig_blob(user.get("client-key-data"))
    cert_file = user.get("client-certificate")
    key_file = user.get("client-key")
    if cert_data:
        cert_file = _write_temp_pem(cert_data, ".client.crt")
    if key_data:
        key_file = _write_temp_pem(key_data, ".client.key")
    if cert_file and key_file:
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)

    return host, str(user.get("token") or ""), ctx


if os.path.exists(_TOKEN_FILE):
    with open(_TOKEN_FILE) as f:
        _TOKEN = f.read().strip()
    _CTX = ssl.create_default_context(cafile=_CA_FILE)
    _ACCESS_MODE = "service-account"
else:
    configured_kubeconfig = os.getenv("KUBECONFIG")
    default_kubeconfig = os.path.expanduser("~/.kube/config")
    if configured_kubeconfig or os.path.isfile(default_kubeconfig):
        _HOST, _TOKEN, _CTX = _load_kubeconfig_access()
        _ACCESS_MODE = "kubeconfig"
    else:
        _HOST = ""
        _CTX = ssl.create_default_context()


def kubernetes_access_status() -> dict:
    return {
        "configured": bool(_HOST),
        "mode": _ACCESS_MODE,
        "host": _HOST,
    }


def _k8s_url(path: str) -> str:
    if not _HOST:
        raise RuntimeError(
            "Kubernetes access is not configured; provide a kubeconfig or run Flawless inside a cluster"
        )
    return f"{_HOST}{path}"


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}

def _k8s_get(path: str) -> dict:
    """发送 GET 请求并返回 JSON 解析结果"""
    req = Request(
        _k8s_url(path),
        headers=_auth_headers()
    )
    with urlopen(req, context=_CTX) as resp:
        return json.loads(resp.read().decode())

def _k8s_patch(path: str, body: dict) -> dict:
    """发送 PATCH 请求（JSON）并返回 JSON"""
    data = json.dumps(body).encode()
    req = Request(
        _k8s_url(path),
        data=data,
        headers={
            **_auth_headers(),
            "Content-Type": "application/strategic-merge-patch+json"
        },
        method="PATCH"
    )
    with urlopen(req, context=_CTX) as resp:
        return json.loads(resp.read().decode())

def _k8s_post(path: str, body: dict) -> dict:
    """发送 POST 请求并返回 JSON 解析结果"""
    data = json.dumps(body).encode()
    req = Request(
        _k8s_url(path),
        data=data,
        headers={
            **_auth_headers(),
            "Content-Type": "application/json"
        },
        method="POST"
    )
    with urlopen(req, context=_CTX) as resp:
        return json.loads(resp.read().decode())


def _k8s_delete(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = _auth_headers()
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(_k8s_url(path), data=data, headers=headers, method="DELETE")
    with urlopen(req, context=_CTX) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {"status": "accepted"}


def _valid_k8s_name(name: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", str(name or "")))


def _validate_configmap_manifest(manifest: dict, namespace: str) -> tuple[bool, str]:
    if not isinstance(manifest, dict) or manifest.get("apiVersion") != "v1" or manifest.get("kind") != "ConfigMap":
        return False, "manifest must be v1 ConfigMap"
    metadata = manifest.get("metadata") or {}
    if metadata.get("namespace") != namespace:
        return False, "ConfigMap namespace must match the approved namespace"
    if not _valid_k8s_name(metadata.get("name", "")):
        return False, "metadata.name is not a valid Kubernetes name"
    illegal = set(manifest) - {"apiVersion", "kind", "metadata", "data", "binaryData", "immutable"}
    if illegal:
        return False, f"unsupported ConfigMap fields: {sorted(illegal)}"
    data = manifest.get("data") or {}
    binary = manifest.get("binaryData") or {}
    if not isinstance(data, dict) or not isinstance(binary, dict):
        return False, "ConfigMap data and binaryData must be objects"
    if not data and not binary:
        return False, "ConfigMap requires data or binaryData"
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in data.items()):
        return False, "ConfigMap data must be string key/value pairs"
    return True, ""


def _validate_service_account_patch(patch: dict) -> tuple[bool, str]:
    if not isinstance(patch, dict) or not patch:
        return False, "ServiceAccount patch must be a non-empty object"
    if set(patch) - {"imagePullSecrets"}:
        return False, "ServiceAccount patch only permits imagePullSecrets"
    secrets = patch.get("imagePullSecrets") or []
    if not isinstance(secrets, list) or not secrets:
        return False, "imagePullSecrets must be a non-empty list"
    if any(not isinstance(item, dict) or set(item) - {"name"} or not item.get("name") for item in secrets):
        return False, "imagePullSecrets must be a list of {name}"
    return True, ""

# ----------------------------------------------------------------------
def _parse_allowed_namespaces() -> list[str]:
    return [
        item.strip()
        for item in os.getenv("ALLOWED_NAMESPACES", "default").split(",")
        if item.strip()
    ]


ALLOWED_NAMESPACES = _parse_allowed_namespaces()

def check_namespace(namespace: str):
    if "all" in ALLOWED_NAMESPACES or "*" in ALLOWED_NAMESPACES:
        return
    if namespace not in ALLOWED_NAMESPACES:
        raise PermissionError(
            f"namespace {namespace} is not allowed by app guard "
            f"ALLOWED_NAMESPACES={','.join(ALLOWED_NAMESPACES)}"
        )


INFRA_NAMESPACES = {
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "ingress-nginx",
    "cert-manager",
    "monitoring",
    "prometheus",
    "istio-system",
    "linkerd",
    "k8s-agent",
}

INFRA_KEYWORDS = [
    "coredns", "kube-proxy", "calico", "flannel", "cilium", "metrics-server",
    "ingress", "nginx-ingress", "prometheus", "grafana", "alertmanager",
    "node-exporter", "kube-state-metrics", "cert-manager", "external-dns",
    "istio", "envoy", "jaeger", "opentelemetry", "otel", "vault",
]

DATA_KEYWORDS = [
    "mysql", "postgres", "redis", "mongodb", "mongo", "kafka", "zookeeper",
    "elasticsearch", "clickhouse", "minio", "etcd", "rabbitmq", "nacos",
]

JOB_KEYWORDS = ["job", "cronjob", "batch", "worker", "migration"]

NODE_NEGATIVE_CONDITIONS = {"DiskPressure", "MemoryPressure", "PIDPressure", "NetworkUnavailable"}


def normalize_node_conditions(conditions: list[dict]) -> dict:
    """Normalize Kubernetes node conditions into human/UX friendly health signals.

    Kubernetes condition semantics are not symmetric:
    - Ready=True means healthy.
    - DiskPressure/MemoryPressure/PIDPressure/NetworkUnavailable=True means unhealthy.
    - DiskPressure=False means the node does NOT have disk pressure.
    """
    normalized = []
    ready = False
    problems = []
    for cond in conditions or []:
        ctype = cond.get("type", "")
        status = cond.get("status", "Unknown")
        if ctype == "Ready":
            healthy = status == "True"
            ready = healthy
            active = healthy
            label = "Ready" if healthy else "NotReady"
        elif ctype in NODE_NEGATIVE_CONDITIONS:
            healthy = status == "False"
            active = status == "True"
            label = f"{ctype}" if active else f"No {ctype}"
            if active:
                problems.append(ctype)
        else:
            healthy = status != "False"
            active = status == "True"
            label = ctype
            if not healthy:
                problems.append(ctype)
        normalized.append({
            "type": ctype,
            "status": status,
            "healthy": healthy,
            "active": active,
            "label": label,
            "reason": cond.get("reason", ""),
            "message": cond.get("message", ""),
            "last_transition_time": cond.get("lastTransitionTime", ""),
        })
    health = "healthy" if ready and not problems else "degraded"
    if not ready:
        health = "not_ready"
    return {
        "ready": ready,
        "health": health,
        "problems": problems,
        "conditions": normalized,
        "standard": "Ready=True is healthy; DiskPressure/MemoryPressure/PIDPressure/NetworkUnavailable=True is unhealthy. False for pressure conditions means no pressure.",
    }


def _pod_identity(pod: dict) -> dict:
    metadata = pod.get("metadata", {})
    status = pod.get("status", {})
    spec = pod.get("spec", {})
    containers = status.get("containerStatuses", [])
    init_containers = status.get("initContainerStatuses", [])
    all_container_statuses = [*containers, *init_containers]
    container_specs = {
        c.get("name"): c
        for c in [*(spec.get("containers", []) or []), *(spec.get("initContainers", []) or [])]
    }
    def container_state(cs: dict) -> dict:
        state = cs.get("state", {}) or {}
        last_state = cs.get("lastState", {}) or {}
        waiting = state.get("waiting") or {}
        terminated = state.get("terminated") or {}
        last_terminated = last_state.get("terminated") or {}
        reason = waiting.get("reason") or terminated.get("reason") or last_terminated.get("reason") or ""
        message = waiting.get("message") or terminated.get("message") or last_terminated.get("message") or ""
        return {
            "raw": state,
            "last_state": last_state,
            "reason": reason,
            "message": message,
            "waiting_reason": waiting.get("reason", ""),
            "terminated_reason": terminated.get("reason", ""),
            "last_terminated_reason": last_terminated.get("reason", ""),
            "exit_code": terminated.get("exitCode", last_terminated.get("exitCode")),
        }

    return {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "labels": metadata.get("labels", {}),
        "annotations": metadata.get("annotations", {}),
        "owner_references": metadata.get("ownerReferences", []),
        "phase": status.get("phase", "Unknown"),
        "ready": bool(containers) and all(cs.get("ready", False) for cs in containers),
        "node": spec.get("nodeName"),
        "security_context": spec.get("securityContext", {}),
        "volumes": [
            {
                "name": v.get("name"),
                "persistent_volume_claim": (v.get("persistentVolumeClaim") or {}).get("claimName"),
                "config_map": (v.get("configMap") or {}).get("name"),
                "secret": (v.get("secret") or {}).get("secretName"),
            }
            for v in spec.get("volumes", [])
        ],
        "dependency_hints": _dependency_hints_from_pod_spec(spec, metadata),
        "restart_count": sum(cs.get("restartCount", 0) for cs in all_container_statuses),
        "containers": [
            {
                "name": cs.get("name"),
                "is_init": cs in init_containers,
                "ready": cs.get("ready", False),
                "restart_count": cs.get("restartCount", 0),
                "state": str(cs.get("state", {})),
                "state_detail": state,
                "reason": state.get("reason", ""),
                "image": cs.get("image", ""),
                "resources": (container_specs.get(cs.get("name")) or {}).get("resources", {}),
                "liveness_probe": (container_specs.get(cs.get("name")) or {}).get("livenessProbe"),
                "readiness_probe": (container_specs.get(cs.get("name")) or {}).get("readinessProbe"),
                "startup_probe": (container_specs.get(cs.get("name")) or {}).get("startupProbe"),
                "security_context": (container_specs.get(cs.get("name")) or {}).get("securityContext", {}),
                "volume_mounts": [
                    {
                        "name": vm.get("name"),
                        "mount_path": vm.get("mountPath"),
                        "read_only": vm.get("readOnly", False),
                        "sub_path": vm.get("subPath", ""),
                    }
                    for vm in (container_specs.get(cs.get("name")) or {}).get("volumeMounts", []) or []
                ],
            }
            for cs in all_container_statuses
            for state in [container_state(cs)]
        ],
    }


def _safe_k8s_get(path: str) -> dict:
    try:
        return _k8s_get(path)
    except Exception:
        return {"items": []}


def _dependency_hints_from_pod_spec(spec: dict, metadata: dict) -> list[dict]:
    hints = []
    text_parts = [
        metadata.get("name", ""),
        " ".join((metadata.get("labels") or {}).values()),
        " ".join((metadata.get("annotations") or {}).values()),
    ]
    for c in spec.get("containers", []) or []:
        text_parts.extend([c.get("name", ""), c.get("image", "")])
        text_parts.extend(c.get("args", []) or [])
        text_parts.extend(c.get("command", []) or [])
        for env in c.get("env", []) or []:
            text_parts.extend([env.get("name", ""), str(env.get("value", ""))])
    text = " ".join(text_parts).lower()
    patterns = [
        ("redis", "Redis/缓存", "data"),
        ("mysql", "MySQL/数据库", "data"),
        ("postgres", "PostgreSQL/数据库", "data"),
        ("mongodb", "MongoDB/数据库", "data"),
        ("kafka", "Kafka/消息队列", "data"),
        ("rabbitmq", "RabbitMQ/消息队列", "data"),
        ("nacos", "Nacos/配置中心", "infrastructure"),
        ("consul", "Consul/服务发现", "infrastructure"),
        ("csi-storage", "Generic 存储/云服务", "storage"),
        ("obs", "对象存储 OBS", "storage"),
        ("s3", "对象存储 S3", "storage"),
    ]
    for keyword, label, dep_type in patterns:
        if keyword in text:
            hints.append({
                "type": dep_type,
                "name": keyword,
                "label": label,
                "source": "pod_spec_hint",
                "confidence": 0.55,
            })
    return hints


def _workload_from_pod(pod: dict) -> dict:
    owners = pod.get("owner_references") or pod.get("ownerReferences") or []
    if not owners:
        return {"kind": "Pod", "name": pod.get("name", "standalone")}
    owner = owners[0]
    kind = owner.get("kind", "Unknown")
    name = owner.get("name", "unknown")
    if kind == "ReplicaSet" and "-" in name:
        parts = name.rsplit("-", 1)
        if len(parts[-1]) >= 5:
            return {"kind": "Deployment", "name": parts[0], "via": name}
    return {"kind": kind, "name": name}


def classify_pod(pod: dict) -> dict:
    namespace = pod.get("namespace", "")
    name = pod.get("name", "")
    labels = pod.get("labels", {}) or {}
    owners = pod.get("owner_references") or pod.get("ownerReferences") or []
    containers = pod.get("containers", []) or []
    phase = str(pod.get("phase") or "")
    text = " ".join([
        namespace,
        name,
        " ".join(labels.values()),
        " ".join(o.get("kind", "") + " " + o.get("name", "") for o in owners),
        " ".join(c.get("name", "") + " " + c.get("image", "") for c in containers),
    ]).lower()

    is_job_like = any(k in text for k in JOB_KEYWORDS) or any(o.get("kind") in ["Job", "CronJob"] for o in owners)
    if phase in {"Succeeded", "Completed"} and is_job_like:
        return {
            "class": "batch",
            "label": "批处理已完成",
            "confidence": 0.94,
            "reason": "Job/CronJob 一次性 Pod 已成功完成，不计入服务可用性风险",
        }
    if namespace in INFRA_NAMESPACES or any(k in text for k in INFRA_KEYWORDS):
        return {
            "class": "infrastructure",
            "label": "基础服务",
            "confidence": 0.9,
            "reason": "系统命名空间或基础设施组件特征",
        }
    if any(k in text for k in DATA_KEYWORDS):
        return {
            "class": "data",
            "label": "数据服务",
            "confidence": 0.86,
            "reason": "数据库/消息队列/缓存等有状态组件特征",
        }
    if is_job_like:
        return {
            "class": "batch",
            "label": "批处理",
            "confidence": 0.8,
            "reason": "Job/CronJob/worker/batch 特征",
        }
    return {
        "class": "application",
        "label": "应用服务",
        "confidence": 0.72,
        "reason": "默认业务应用工作负载",
    }


def _impact_for_workload(total: int, ready: int, category: str, completed: int = 0, failed: int = 0) -> dict:
    unavailable = max(total - ready, 0)
    if category == "batch" and completed > 0 and failed == 0 and completed >= total:
        level = "completed"
        summary = "一次性任务已成功完成，不视为服务不可用风险"
    elif category == "batch" and failed > 0:
        level = "medium"
        summary = f"批任务存在 {failed}/{total} 个失败 Pod，需要查看 Job 日志和事件"
    elif category == "batch":
        level = "low" if ready or completed else "medium"
        summary = "批处理任务正在运行或等待调度，不按在线服务单副本风险放大"
    elif total <= 0:
        level = "unknown"
        summary = "未发现副本"
    elif total == 1:
        level = "critical" if ready == 0 else "high"
        summary = "单副本，单 Pod 故障会直接影响该工作负载"
    elif ready == 0:
        level = "critical"
        summary = "所有副本不可用"
    elif unavailable > 0:
        level = "medium"
        summary = f"{unavailable}/{total} 副本不可用，容量和可用性下降"
    else:
        level = "low"
        summary = "副本全部 Ready"
    if category in {"infrastructure", "data"} and level in {"medium", "high"}:
        level = "critical" if category == "infrastructure" else "high"
    return {
        "level": level,
        "summary": summary,
        "replicas": total,
        "ready_replicas": ready,
        "completed_replicas": completed,
        "failed_replicas": failed,
        "single_pod_risk": total <= 1 and category != "batch",
    }


def _service_matches_pod(selector: dict, pod: dict) -> bool:
    labels = pod.get("labels", {}) or {}
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def _storage_dependency_for_claim(pvc: dict, pv_index: dict) -> dict:
    spec = pvc.get("spec", {})
    name = pvc.get("metadata", {}).get("name", "")
    pv_name = spec.get("volumeName", "")
    pv = pv_index.get(pv_name, {})
    pv_spec = pv.get("spec", {})
    csi = pv_spec.get("csi") or {}
    provisioner = csi.get("driver") or pv_spec.get("storageClassName") or spec.get("storageClassName") or "unknown"
    label = "持久化存储"
    text = " ".join([name, pv_name, provisioner, spec.get("storageClassName") or ""]).lower()
    if "csi-storage" in text or "everest" in text or "csi-disk" in text or "csi-nas" in text:
        label = "Generic 存储"
    elif "nfs" in text:
        label = "NFS 存储"
    elif "ceph" in text or "rbd" in text:
        label = "Ceph 存储"
    return {
        "kind": "Storage",
        "type": "storage",
        "name": name,
        "label": label,
        "storage_class": spec.get("storageClassName"),
        "volume": pv_name,
        "provisioner": provisioner,
        "access_modes": spec.get("accessModes", []),
        "capacity": (pvc.get("status", {}).get("capacity") or {}).get("storage"),
        "source": "pvc",
        "confidence": 0.95,
    }


def _ingress_dependencies(ingresses: list[dict]) -> list[dict]:
    deps = []
    for ing in ingresses:
        meta = ing.get("metadata", {})
        spec = ing.get("spec", {})
        hosts = []
        service_names = set()
        for rule in spec.get("rules", []) or []:
            if rule.get("host"):
                hosts.append(rule["host"])
            for path in ((rule.get("http") or {}).get("paths") or []):
                svc = ((path.get("backend") or {}).get("service") or {}).get("name")
                if svc:
                    service_names.add(svc)
        default_svc = (((spec.get("defaultBackend") or {}).get("service") or {}).get("name"))
        if default_svc:
            service_names.add(default_svc)
        for svc in service_names:
            deps.append({
                "kind": "Ingress",
                "type": "ingress",
                "name": meta.get("name", ""),
                "service": svc,
                "label": "Ingress 入口",
                "hosts": hosts,
                "class": spec.get("ingressClassName") or (meta.get("annotations") or {}).get("kubernetes.io/ingress.class"),
                "source": "ingress",
                "confidence": 0.95,
            })
    return deps


def _istio_dependencies(namespace: str) -> list[dict]:
    deps = []
    virtual_services = _safe_k8s_get(f"/apis/networking.istio.io/v1beta1/namespaces/{namespace}/virtualservices").get("items", [])
    gateways = _safe_k8s_get(f"/apis/networking.istio.io/v1beta1/namespaces/{namespace}/gateways").get("items", [])
    gateway_names = [g.get("metadata", {}).get("name", "") for g in gateways]
    for vs in virtual_services:
        meta = vs.get("metadata", {})
        spec = vs.get("spec", {})
        service_names = set()
        for http in spec.get("http", []) or []:
            for route in http.get("route", []) or []:
                host = ((route.get("destination") or {}).get("host") or "").split(".")[0]
                if host:
                    service_names.add(host)
        for tcp in spec.get("tcp", []) or []:
            for route in tcp.get("route", []) or []:
                host = ((route.get("destination") or {}).get("host") or "").split(".")[0]
                if host:
                    service_names.add(host)
        for svc in service_names:
            deps.append({
                "kind": "VirtualService",
                "type": "mesh",
                "name": meta.get("name", ""),
                "service": svc,
                "label": "Istio 流量治理",
                "hosts": spec.get("hosts", []),
                "gateways": spec.get("gateways", []) or gateway_names,
                "source": "istio",
                "confidence": 0.9,
            })
    return deps

# ----------------------------------------------------------------------
@mcp.tool()
def list_pods(namespace: str = "default") -> dict:
    """List pods in a Kubernetes namespace."""
    try:
        data = _k8s_get(f"/api/v1/namespaces/{namespace}/pods")
    except Exception as e:
        return {"error": f"Failed to list pods in namespace {namespace}: {str(e)}"}

    pods = []
    for item in data["items"]:
        pod = _pod_identity(item)
        pod["classification"] = classify_pod(pod)
        pod["workload"] = _workload_from_pod(pod)
        pods.append(pod)
    return {"namespace": namespace, "pods": pods}

@mcp.tool()
def get_pod_events(namespace: str, pod_name: str) -> dict:
    """Get Kubernetes events for a specific pod."""
    try:
        field_selector = f"involvedObject.name={pod_name}"
        data = _k8s_get(
            f"/api/v1/namespaces/{namespace}/events?fieldSelector={field_selector}"
        )
    except Exception as e:
        return {"error": str(e)}

    return {
        "namespace": namespace,
        "pod_name": pod_name,
        "events": [
            {
                "reason": e["reason"],
                "message": e["message"],
                "type": e["type"],
                "count": e.get("count"),
                "first_timestamp": e.get("firstTimestamp"),
                "last_timestamp": e.get("lastTimestamp"),
            }
            for e in data.get("items", [])
        ],
    }

@mcp.tool()
def get_pod_logs(
    namespace: str,
    pod_name: str,
    container: Optional[str] = None,
    tail_lines: int = 200,
    previous: bool = False,
) -> dict:
    """Get recent pod logs."""
    try:
        query = {"tailLines": max(1, min(int(tail_lines), 500)), "previous": str(bool(previous)).lower()}
        if container:
            query["container"] = container
        path = f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(pod_name, safe='')}/log?{urlencode(query)}"
        # 日志接口返回纯文本，直接读取
        req = Request(
            _k8s_url(path),
            headers={"Authorization": f"Bearer {_TOKEN}"}
        )
        with urlopen(req, context=_CTX) as resp:
            logs = resp.read().decode()
        return {
            "namespace": namespace,
            "pod_name": pod_name,
            "container": container,
            "previous": previous,
            "logs": logs,
        }
    except Exception as e:
        return {"error": str(e)}


def _workload_api_path(workload_type: str, namespace: str, workload_name: str) -> str:
    resource = WORKLOAD_RESOURCE_MAP.get(str(workload_type).lower())
    if not resource:
        raise ValueError("workload_type must be Deployment, StatefulSet, or DaemonSet")
    return f"/apis/apps/v1/namespaces/{quote(namespace, safe='')}/{resource}/{quote(workload_name, safe='')}"


def _safe_workload_evidence(raw: dict) -> dict:
    """Keep operational fields while dropping env values and sensitive data."""
    metadata = raw.get("metadata") or {}
    spec = raw.get("spec") or {}
    template = spec.get("template") or {}
    pod_spec = template.get("spec") or {}
    containers = []
    for container in pod_spec.get("containers", []) or []:
        env_refs = []
        for env in container.get("env", []) or []:
            value_from = env.get("valueFrom") or {}
            ref_type = next(iter(value_from.keys()), "literal-present" if "value" in env else "")
            env_refs.append({"name": env.get("name"), "source": ref_type})
        containers.append({
            "name": container.get("name"), "image": container.get("image"),
            "resources": container.get("resources", {}), "livenessProbe": container.get("livenessProbe"),
            "readinessProbe": container.get("readinessProbe"), "startupProbe": container.get("startupProbe"),
            "securityContext": container.get("securityContext", {}), "volumeMounts": container.get("volumeMounts", []),
            "envReferences": env_refs,
            "envFromReferences": [
                {
                    "configMap": (item.get("configMapRef") or {}).get("name"),
                    "secret": (item.get("secretRef") or {}).get("name"),
                }
                for item in container.get("envFrom", []) or []
            ],
        })
    return {
        "apiVersion": raw.get("apiVersion"), "kind": raw.get("kind"),
        "metadata": {"name": metadata.get("name"), "namespace": metadata.get("namespace"), "generation": metadata.get("generation")},
        "spec": {
            "replicas": spec.get("replicas"), "strategy": spec.get("strategy"),
            "template": {"spec": {
                "containers": containers, "volumes": pod_spec.get("volumes", []),
                "securityContext": pod_spec.get("securityContext", {}), "imagePullSecrets": pod_spec.get("imagePullSecrets", []),
                "nodeSelector": pod_spec.get("nodeSelector", {}), "tolerations": pod_spec.get("tolerations", []),
                "affinity": pod_spec.get("affinity", {}), "topologySpreadConstraints": pod_spec.get("topologySpreadConstraints", []),
            }},
        },
        "status": raw.get("status", {}),
    }


@mcp.tool()
def get_pod_diagnostics(namespace: str, pod_name: str, tail_lines: int = 160) -> dict:
    """Collect bounded, read-only evidence needed for root-cause planning.

    Secret values are never returned. ConfigMap/Secret references are reported
    only by name and existence so the planner cannot leak credential material.
    """
    pod_path = f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(pod_name, safe='')}"
    try:
        raw_pod = _k8s_get(pod_path)
    except Exception as exc:
        return {"error": f"failed to read pod/{pod_name}: {exc}"}
    pod = _pod_identity(raw_pod)
    pod["classification"] = classify_pod(pod)
    pod["workload"] = _workload_from_pod(pod)
    events = get_pod_events(namespace, pod_name).get("events", [])
    logs: dict[str, dict] = {}
    for container in pod.get("containers", [])[:8]:
        name = container.get("name")
        if not name:
            continue
        current = get_pod_logs(namespace, pod_name, name, tail_lines, False)
        previous = get_pod_logs(namespace, pod_name, name, tail_lines, True) if container.get("restart_count", 0) else {}
        logs[name] = {
            "current": current.get("logs", "") if not current.get("error") else "",
            "current_error": current.get("error", ""),
            "previous": previous.get("logs", "") if previous and not previous.get("error") else "",
            "previous_error": previous.get("error", "") if previous else "",
        }

    workload = {}
    owner = pod.get("workload") or {}
    if owner.get("kind") in {"Deployment", "StatefulSet", "DaemonSet"} and owner.get("name"):
        try:
            workload = _safe_workload_evidence(_k8s_get(_workload_api_path(owner["kind"], namespace, owner["name"])))
        except Exception as exc:
            workload = {"error": str(exc), "kind": owner.get("kind"), "name": owner.get("name")}

    pod_labels = pod.get("labels") or {}
    services = []
    for svc in _safe_k8s_get(f"/api/v1/namespaces/{quote(namespace, safe='')}/services").get("items", []):
        selector = (svc.get("spec") or {}).get("selector") or {}
        if selector and all(pod_labels.get(k) == v for k, v in selector.items()):
            name = (svc.get("metadata") or {}).get("name", "")
            slices = _safe_k8s_get(
                f"/apis/discovery.k8s.io/v1/namespaces/{quote(namespace, safe='')}/endpointslices?"
                + urlencode({"labelSelector": f"kubernetes.io/service-name={name}"})
            ).get("items", [])
            services.append({
                "name": name,
                "type": (svc.get("spec") or {}).get("type"),
                "selector": selector,
                "ports": (svc.get("spec") or {}).get("ports", []),
                "ready_endpoints": sum(
                    1 for item in slices for endpoint in item.get("endpoints", [])
                    if (endpoint.get("conditions") or {}).get("ready") is not False
                ),
                "endpoint_slices": len(slices),
            })

    storage = []
    for volume in raw_pod.get("spec", {}).get("volumes", []) or []:
        claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
        if not claim:
            continue
        try:
            pvc = _k8s_get(f"/api/v1/namespaces/{quote(namespace, safe='')}/persistentvolumeclaims/{quote(claim, safe='')}")
            pv_name = (pvc.get("spec") or {}).get("volumeName")
            pv = _safe_k8s_get(f"/api/v1/persistentvolumes/{quote(pv_name, safe='')}") if pv_name else {}
            storage.append({
                "volume": volume.get("name"), "pvc": claim, "pvc_phase": (pvc.get("status") or {}).get("phase"),
                "requested": ((pvc.get("spec") or {}).get("resources") or {}).get("requests", {}).get("storage"),
                "capacity": (pvc.get("status") or {}).get("capacity", {}).get("storage"),
                "storage_class": (pvc.get("spec") or {}).get("storageClassName"), "pv": pv_name,
                "csi_driver": ((pv.get("spec") or {}).get("csi") or {}).get("driver"),
            })
        except Exception as exc:
            message = str(exc)
            storage.append({
                "volume": volume.get("name"),
                "pvc": claim,
                "missing": "404" in message or "not found" in message.lower(),
                "error": message,
            })

    node = {}
    if raw_pod.get("spec", {}).get("nodeName"):
        node_name = raw_pod["spec"]["nodeName"]
        try:
            raw_node = _k8s_get(f"/api/v1/nodes/{quote(node_name, safe='')}")
            node = {
                "name": node_name,
                "unschedulable": bool((raw_node.get("spec") or {}).get("unschedulable")),
                "conditions": normalize_node_conditions((raw_node.get("status") or {}).get("conditions", [])),
                "capacity": (raw_node.get("status") or {}).get("capacity", {}),
                "allocatable": (raw_node.get("status") or {}).get("allocatable", {}),
                "taints": (raw_node.get("spec") or {}).get("taints", []),
            }
        except Exception as exc:
            node = {"name": node_name, "error": str(exc)}

    refs = {"config_maps": [], "secrets": []}
    for volume in raw_pod.get("spec", {}).get("volumes", []) or []:
        if (volume.get("configMap") or {}).get("name"):
            refs["config_maps"].append((volume.get("configMap") or {}).get("name"))
        if (volume.get("secret") or {}).get("secretName"):
            refs["secrets"].append((volume.get("secret") or {}).get("secretName"))
    for container in raw_pod.get("spec", {}).get("containers", []) or []:
        for env_from in container.get("envFrom", []) or []:
            if (env_from.get("configMapRef") or {}).get("name"):
                refs["config_maps"].append(env_from["configMapRef"]["name"])
            if (env_from.get("secretRef") or {}).get("name"):
                refs["secrets"].append(env_from["secretRef"]["name"])
    refs = {key: sorted(set(values)) for key, values in refs.items()}
    return {
        "namespace": namespace, "pod_name": pod_name, "pod": pod, "events": events[-30:],
        "logs": logs, "workload": workload, "services": services, "storage": storage, "node": node,
        "configuration_references": refs,
    }


@mcp.tool()
def recreate_pod(namespace: str, pod_name: str, dry_run: bool = True, grace_period_seconds: int = 30) -> dict:
    """Delete one controller-owned pod so its controller recreates it."""
    check_namespace(namespace)
    pod = _k8s_get(f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(pod_name, safe='')}")
    owners = (pod.get("metadata") or {}).get("ownerReferences") or []
    if not any(owner.get("controller") for owner in owners):
        raise ValueError("recreate_pod rejects standalone pods without a controller owner")
    grace = max(0, min(int(grace_period_seconds), 120))
    if dry_run:
        return {"action": "recreate_pod", "namespace": namespace, "pod_name": pod_name, "dry_run": True, "grace_period_seconds": grace}
    result = _k8s_delete(
        f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(pod_name, safe='')}",
        {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": grace, "propagationPolicy": "Background"},
    )
    return {"action": "recreate_pod", "namespace": namespace, "pod_name": pod_name, "dry_run": False, "status": result.get("status", "accepted")}


@mcp.tool()
def evict_pod(namespace: str, pod_name: str, dry_run: bool = True, grace_period_seconds: int = 30) -> dict:
    """Evict one controller-owned pod through policy/v1 so PDB is honored."""
    check_namespace(namespace)
    pod = _k8s_get(f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(pod_name, safe='')}")
    if not any(owner.get("controller") for owner in (pod.get("metadata") or {}).get("ownerReferences", [])):
        raise ValueError("evict_pod rejects standalone pods without a controller owner")
    grace = max(0, min(int(grace_period_seconds), 120))
    body = {
        "apiVersion": "policy/v1", "kind": "Eviction",
        "metadata": {"name": pod_name, "namespace": namespace},
        "deleteOptions": {"gracePeriodSeconds": grace, "propagationPolicy": "Background"},
    }
    if dry_run:
        return {"action": "evict_pod", "namespace": namespace, "pod_name": pod_name, "body": body, "dry_run": True}
    result = _k8s_post(f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(pod_name, safe='')}/eviction", body)
    return {"action": "evict_pod", "namespace": namespace, "pod_name": pod_name, "dry_run": False, "status": result.get("status", "accepted")}


@mcp.tool()
def patch_hpa(namespace: str, hpa_name: str, min_replicas: int | None = None, max_replicas: int | None = None, dry_run: bool = True) -> dict:
    check_namespace(namespace)
    if min_replicas is None and max_replicas is None:
        raise ValueError("min_replicas or max_replicas is required")
    maximum = int(os.getenv("MAX_PATCH_REPLICAS", "20"))
    patch = {"spec": {}}
    if min_replicas is not None:
        patch["spec"]["minReplicas"] = max(1, min(int(min_replicas), maximum))
    if max_replicas is not None:
        patch["spec"]["maxReplicas"] = max(1, min(int(max_replicas), maximum))
    if patch["spec"].get("minReplicas", 1) > patch["spec"].get("maxReplicas", maximum):
        raise ValueError("min_replicas cannot exceed max_replicas")
    if dry_run:
        return {"action": "patch_hpa", "namespace": namespace, "hpa_name": hpa_name, "patch": patch, "dry_run": True}
    result = _k8s_patch(f"/apis/autoscaling/v2/namespaces/{quote(namespace, safe='')}/horizontalpodautoscalers/{quote(hpa_name, safe='')}", patch)
    return {"action": "patch_hpa", "namespace": namespace, "hpa_name": hpa_name, "patch": patch, "dry_run": False, "resource_version": result.get("metadata", {}).get("resourceVersion")}


@mcp.tool()
def expand_pvc(namespace: str, pvc_name: str, storage: str, dry_run: bool = True) -> dict:
    check_namespace(namespace)
    if not storage or len(storage) > 24:
        raise ValueError("a bounded Kubernetes storage quantity is required")
    pvc = _k8s_get(f"/api/v1/namespaces/{quote(namespace, safe='')}/persistentvolumeclaims/{quote(pvc_name, safe='')}")
    current = (((pvc.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage")
    patch = {"spec": {"resources": {"requests": {"storage": storage}}}}
    if dry_run:
        return {"action": "expand_pvc", "namespace": namespace, "pvc_name": pvc_name, "current": current, "requested": storage, "patch": patch, "dry_run": True}
    result = _k8s_patch(f"/api/v1/namespaces/{quote(namespace, safe='')}/persistentvolumeclaims/{quote(pvc_name, safe='')}", patch)
    return {"action": "expand_pvc", "namespace": namespace, "pvc_name": pvc_name, "current": current, "requested": storage, "dry_run": False, "resource_version": result.get("metadata", {}).get("resourceVersion")}


def _valid_k8s_name(name: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", str(name or "")))


def _validate_pvc_manifest(manifest: dict, namespace: str) -> tuple[bool, str]:
    if not isinstance(manifest, dict) or manifest.get("apiVersion") != "v1" or manifest.get("kind") != "PersistentVolumeClaim":
        return False, "manifest must be v1 PersistentVolumeClaim"
    metadata = manifest.get("metadata") or {}
    if metadata.get("namespace") != namespace:
        return False, "PVC namespace must match request namespace"
    if not _valid_k8s_name(metadata.get("name", "")):
        return False, "metadata.name is invalid"
    spec = manifest.get("spec") or {}
    if not isinstance(spec.get("accessModes") or [], list) or not spec.get("accessModes"):
        return False, "spec.accessModes is required"
    if not (((spec.get("resources") or {}).get("requests") or {}).get("storage")):
        return False, "spec.resources.requests.storage is required"
    return True, ""


def _validate_pv_manifest(manifest: dict) -> tuple[bool, str]:
    if not isinstance(manifest, dict) or manifest.get("apiVersion") != "v1" or manifest.get("kind") != "PersistentVolume":
        return False, "manifest must be v1 PersistentVolume"
    metadata = manifest.get("metadata") or {}
    if not _valid_k8s_name(metadata.get("name", "")):
        return False, "metadata.name is invalid"
    spec = manifest.get("spec") or {}
    if spec.get("hostPath"):
        return False, "hostPath PV is forbidden"
    if not ((spec.get("capacity") or {}).get("storage")):
        return False, "spec.capacity.storage is required"
    if not isinstance(spec.get("accessModes") or [], list) or not spec.get("accessModes"):
        return False, "spec.accessModes is required"
    allow_local = os.getenv("AUTO_OPS_ALLOW_LOCAL_STATIC_PV", "false").lower() in {"1", "true", "yes", "on"}
    allowed_sources = ("nfs", "csi", "fc", "iscsi", "rbd", "cephfs") + (("local",) if allow_local else ())
    if spec.get("local") and not allow_local:
        return False, "local PV is only allowed when AUTO_OPS_ALLOW_LOCAL_STATIC_PV=true"
    if spec.get("local") and not spec.get("nodeAffinity"):
        return False, "local PV requires nodeAffinity"
    if not any(spec.get(key) for key in allowed_sources):
        return False, "PV must use approved network or CSI storage source"
    claim_ref = spec.get("claimRef") or {}
    if claim_ref and (not claim_ref.get("namespace") or not claim_ref.get("name")):
        return False, "claimRef must include namespace and name"
    return True, ""


@mcp.tool()
def create_pvc(namespace: str, manifest: dict, dry_run: bool = True) -> dict:
    """Create one approved PersistentVolumeClaim."""
    check_namespace(namespace)
    valid, reason = _validate_pvc_manifest(manifest, namespace)
    if not valid:
        raise ValueError(f"PVC manifest rejected by safety policy: {reason}")
    name = (manifest.get("metadata") or {}).get("name")
    if dry_run:
        return {"action": "create_pvc", "namespace": namespace, "pvc_name": name, "manifest": manifest, "dry_run": True}
    result = _k8s_post(f"/api/v1/namespaces/{quote(namespace, safe='')}/persistentvolumeclaims", manifest)
    return {"action": "create_pvc", "namespace": namespace, "pvc_name": name, "dry_run": False, "resource_version": (result.get("metadata") or {}).get("resourceVersion")}


@mcp.tool()
def create_persistent_volume(manifest: dict, dry_run: bool = True) -> dict:
    """Create one approved static PersistentVolume. hostPath is forbidden."""
    valid, reason = _validate_pv_manifest(manifest)
    if not valid:
        raise ValueError(f"PV manifest rejected by safety policy: {reason}")
    name = (manifest.get("metadata") or {}).get("name")
    if dry_run:
        return {"action": "create_persistent_volume", "pv_name": name, "manifest": manifest, "dry_run": True}
    result = _k8s_post("/api/v1/persistentvolumes", manifest)
    return {"action": "create_persistent_volume", "pv_name": name, "dry_run": False, "resource_version": (result.get("metadata") or {}).get("resourceVersion")}


@mcp.tool()
def cordon_node(node_name: str, unschedulable: bool = True, dry_run: bool = True) -> dict:
    if not node_name:
        raise ValueError("node_name is required")
    patch = {"spec": {"unschedulable": bool(unschedulable)}}
    if dry_run:
        return {"action": "cordon_node", "node_name": node_name, "patch": patch, "dry_run": True}
    result = _k8s_patch(f"/api/v1/nodes/{quote(node_name, safe='')}", patch)
    return {"action": "cordon_node", "node_name": node_name, "unschedulable": bool(unschedulable), "dry_run": False, "resource_version": result.get("metadata", {}).get("resourceVersion")}


@mcp.tool()
def get_remediation_target_state(kind: str, name: str, namespace: str = "default") -> dict:
    """Return a bounded, non-secret state view for post-change verification."""
    normalized = str(kind or "").lower()
    if normalized == "node":
        raw = _k8s_get(f"/api/v1/nodes/{quote(name, safe='')}")
        return {
            "kind": "Node", "name": name,
            "spec": {"unschedulable": bool((raw.get("spec") or {}).get("unschedulable"))},
            "status": normalize_node_conditions((raw.get("status") or {}).get("conditions", [])),
        }
    check_namespace(namespace)
    if normalized == "hpa":
        raw = _k8s_get(f"/apis/autoscaling/v2/namespaces/{quote(namespace, safe='')}/horizontalpodautoscalers/{quote(name, safe='')}")
        spec, status = raw.get("spec") or {}, raw.get("status") or {}
        return {"kind": "HPA", "name": name, "namespace": namespace, "spec": {"minReplicas": spec.get("minReplicas"), "maxReplicas": spec.get("maxReplicas")}, "status": status}
    if normalized == "pvc":
        raw = _k8s_get(f"/api/v1/namespaces/{quote(namespace, safe='')}/persistentvolumeclaims/{quote(name, safe='')}")
        spec, status = raw.get("spec") or {}, raw.get("status") or {}
        return {"kind": "PVC", "name": name, "namespace": namespace, "spec": {"requested": ((spec.get("resources") or {}).get("requests") or {}).get("storage"), "storageClassName": spec.get("storageClassName")}, "status": {"phase": status.get("phase"), "capacity": (status.get("capacity") or {}).get("storage")}}
    if normalized == "pv":
        raw = _k8s_get(f"/api/v1/persistentvolumes/{quote(name, safe='')}")
        return {"kind": "PV", "name": name, "spec": raw.get("spec") or {}, "status": raw.get("status") or {}}
    if normalized == "configmap":
        raw = _k8s_get(f"/api/v1/namespaces/{quote(namespace, safe='')}/configmaps/{quote(name, safe='')}")
        data = raw.get("data") or {}
        return {
            "kind": "ConfigMap", "name": name, "namespace": namespace,
            "data_keys": sorted(data.keys())[:80],
            "immutable": bool(raw.get("immutable")),
            "resource_version": (raw.get("metadata") or {}).get("resourceVersion"),
        }
    if normalized in {"serviceaccount", "service_account"}:
        raw = _k8s_get(f"/api/v1/namespaces/{quote(namespace, safe='')}/serviceaccounts/{quote(name, safe='')}")
        return {
            "kind": "ServiceAccount", "name": name, "namespace": namespace,
            "imagePullSecrets": (raw.get("imagePullSecrets") or []),
            "resource_version": (raw.get("metadata") or {}).get("resourceVersion"),
        }
    raise ValueError("kind must be Node, HPA, PVC, PV, ConfigMap, or ServiceAccount")

@mcp.tool()
def restart_deployment(
    namespace: str,
    deployment_name: str,
    dry_run: bool = True,
) -> dict:
    """Restart a deployment by patching restart annotation."""
    check_namespace(namespace)

    if dry_run:
        return {
            "action": "restart_deployment",
            "namespace": namespace,
            "deployment": deployment_name,
            "dry_run": True,
            "message": "Dry run only. No deployment restarted.",
        }

    from datetime import datetime, timezone

    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat()
                    }
                }
            }
        }
    }

    try:
        _k8s_patch(
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}",
            patch
        )
        return {
            "action": "restart_deployment",
            "namespace": namespace,
            "deployment": deployment_name,
            "dry_run": False,
            "message": "Deployment restarted.",
        }
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def scale_deployment(
    namespace: str,
    deployment_name: str,
    replicas: int,
    dry_run: bool = True,
) -> dict:
    """Scale a deployment."""
    check_namespace(namespace)

    if replicas < 0 or replicas > 20:
        raise ValueError("replicas must be between 0 and 20")

    if dry_run:
        return {
            "action": "scale_deployment",
            "namespace": namespace,
            "deployment": deployment_name,
            "replicas": replicas,
            "dry_run": True,
        }

    patch = {"spec": {"replicas": replicas}}

    try:
        _k8s_patch(
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}/scale",
            patch
        )
        return {
            "action": "scale_deployment",
            "namespace": namespace,
            "deployment": deployment_name,
            "replicas": replicas,
            "dry_run": False,
        }
    except Exception as e:
        return {"error": str(e)}


WORKLOAD_RESOURCE_MAP = {
    "deployment": "deployments",
    "deployments": "deployments",
    "statefulset": "statefulsets",
    "statefulsets": "statefulsets",
    "daemonset": "daemonsets",
    "daemonsets": "daemonsets",
}


def _validate_workload_patch(patch: dict, allow_volume_patch: bool = False) -> tuple[bool, str]:
    if not isinstance(patch, dict):
        return False, "patch must be a JSON object"
    spec = patch.get("spec")
    if not isinstance(spec, dict):
        return False, "patch must contain spec"
    illegal_spec = set(spec) - {"replicas", "template"}
    if illegal_spec:
        return False, f"unsupported spec fields: {sorted(illegal_spec)}"
    if "replicas" in spec:
        try:
            replicas = int(spec["replicas"])
        except Exception:
            return False, "spec.replicas must be integer"
        max_replicas = int(os.getenv("MAX_PATCH_REPLICAS", "20"))
        if replicas < 0 or replicas > max_replicas:
            return False, f"spec.replicas must be between 0 and {max_replicas}"
    template = spec.get("template") or {}
    if template:
        if not isinstance(template, dict):
            return False, "spec.template must be object"
        illegal_template = set(template) - {"metadata", "spec"}
        if illegal_template:
            return False, f"unsupported template fields: {sorted(illegal_template)}"
        metadata = template.get("metadata") or {}
        if metadata and (not isinstance(metadata, dict) or set(metadata) - {"annotations"}):
            return False, "only template.metadata.annotations can be patched"
        annotations = metadata.get("annotations") or {}
        if annotations and not isinstance(annotations, dict):
            return False, "template.metadata.annotations must be object"
        pod_spec = template.get("spec") or {}
        if pod_spec:
            if not isinstance(pod_spec, dict):
                return False, "template.spec must be object"
            allowed_pod_spec = {"containers", "securityContext", "imagePullSecrets", "nodeSelector", "tolerations", "affinity"}
            if allow_volume_patch:
                allowed_pod_spec.add("volumes")
            illegal_pod_spec = set(pod_spec) - allowed_pod_spec
            if illegal_pod_spec:
                return False, f"unsupported template.spec fields: {sorted(illegal_pod_spec)}"
            pod_sc = pod_spec.get("securityContext") or {}
            if pod_sc and set(pod_sc) - {"fsGroup", "fsGroupChangePolicy", "runAsUser", "runAsGroup", "runAsNonRoot", "supplementalGroups"}:
                return False, "template.spec.securityContext contains unsupported fields"
            image_pull_secrets = pod_spec.get("imagePullSecrets") or []
            if image_pull_secrets:
                if not isinstance(image_pull_secrets, list) or any(not isinstance(x, dict) or set(x) - {"name"} or not x.get("name") for x in image_pull_secrets):
                    return False, "template.spec.imagePullSecrets must be a list of {name}"
            node_selector = pod_spec.get("nodeSelector") or {}
            if node_selector and not isinstance(node_selector, dict):
                return False, "template.spec.nodeSelector must be object"
            tolerations = pod_spec.get("tolerations") or []
            if tolerations and not isinstance(tolerations, list):
                return False, "template.spec.tolerations must be list"
            if tolerations and any(not isinstance(item, dict) for item in tolerations):
                return False, "template.spec.tolerations entries must be objects"
            affinity = pod_spec.get("affinity") or {}
            if affinity and not isinstance(affinity, dict):
                return False, "template.spec.affinity must be object"
            volumes = pod_spec.get("volumes") or []
            if volumes:
                if not allow_volume_patch:
                    return False, "template.spec.volumes requires high_risk_volume_patch=true"
                if not isinstance(volumes, list):
                    return False, "template.spec.volumes must be list"
                for volume in volumes:
                    if not isinstance(volume, dict):
                        return False, "volume patch item must be object"
                    if set(volume) - {"name", "persistentVolumeClaim"}:
                        return False, "volume patch only permits name and persistentVolumeClaim"
                    claim = volume.get("persistentVolumeClaim") or {}
                    if not volume.get("name") or not isinstance(claim, dict) or not claim.get("claimName"):
                        return False, "volume patch requires name and persistentVolumeClaim.claimName"
            containers = pod_spec.get("containers") or []
            if containers and not isinstance(containers, list):
                return False, "template.spec.containers must be list"
            for container in containers:
                if not isinstance(container, dict):
                    return False, "container patch item must be object"
                illegal_container = set(container) - {
                    "name", "image", "resources", "env", "livenessProbe", "readinessProbe", "startupProbe", "securityContext"
                }
                if illegal_container:
                    return False, f"unsupported container fields: {sorted(illegal_container)}"
                if not container.get("name"):
                    return False, "container patch must include name"
                if "image" in container and not str(container.get("image") or "").strip():
                    return False, "container image must be a non-empty immutable reference"
                csc = container.get("securityContext") or {}
                if csc and set(csc) - {"runAsUser", "runAsGroup", "runAsNonRoot", "allowPrivilegeEscalation", "readOnlyRootFilesystem"}:
                    return False, "container.securityContext contains unsupported fields"
    return True, ""


@mcp.tool()
def patch_workload(
    namespace: str,
    workload_type: str,
    workload_name: str,
    patch: dict,
    dry_run: bool = True,
    high_risk_volume_patch: bool = False,
) -> dict:
    """Patch a common apps/v1 workload using Kubernetes merge patch.

    Supported workload_type: Deployment, StatefulSet, DaemonSet.
    This intentionally accepts a prepared merge-patch object only; the caller
    must show it to a human before dry_run=False in production workflows.
    """
    check_namespace(namespace)
    resource = WORKLOAD_RESOURCE_MAP.get(str(workload_type).lower())
    if not resource:
        raise ValueError("workload_type must be Deployment, StatefulSet, or DaemonSet")
    if not isinstance(patch, dict) or not patch:
        raise ValueError("patch must be a non-empty object")
    valid, reason = _validate_workload_patch(patch, allow_volume_patch=high_risk_volume_patch)
    if not valid:
        raise ValueError(f"patch rejected by safety policy: {reason}")
    if dry_run:
        return {
            "action": "patch_workload",
            "namespace": namespace,
            "workload_type": workload_type,
            "workload_name": workload_name,
            "patch": patch,
            "dry_run": True,
            "message": "Dry run only. No workload patched.",
        }
    try:
        result = _k8s_patch(
            f"/apis/apps/v1/namespaces/{namespace}/{resource}/{workload_name}",
            patch,
        )
        return {
            "action": "patch_workload",
            "namespace": namespace,
            "workload_type": workload_type,
            "workload_name": workload_name,
            "patch": patch,
            "dry_run": False,
            "message": f"{workload_type}/{workload_name} patched.",
            "resource_version": result.get("metadata", {}).get("resourceVersion"),
        }
    except Exception as e:
        return {"error": str(e)}


def _validate_create_workload_manifest(manifest: dict) -> tuple[bool, str]:
    if not isinstance(manifest, dict) or manifest.get("apiVersion") != "apps/v1":
        return False, "manifest apiVersion must be apps/v1"
    if manifest.get("kind") not in {"Deployment", "StatefulSet", "DaemonSet"}:
        return False, "manifest kind must be Deployment, StatefulSet, or DaemonSet"
    metadata = manifest.get("metadata") or {}
    if not metadata.get("name") or not metadata.get("namespace"):
        return False, "manifest metadata.name and metadata.namespace are required"
    pod_spec = ((((manifest.get("spec") or {}).get("template") or {}).get("spec")) or {})
    if any(pod_spec.get(key) for key in ("hostNetwork", "hostPID", "hostIPC")):
        return False, "hostNetwork/hostPID/hostIPC are not allowed"
    if any((volume or {}).get("hostPath") for volume in pod_spec.get("volumes") or []):
        return False, "hostPath volumes are not allowed"
    containers = list(pod_spec.get("containers") or []) + list(pod_spec.get("initContainers") or [])
    if not containers:
        return False, "at least one container is required"
    for container in containers:
        if not container.get("name") or not container.get("image"):
            return False, "every container requires name and image"
        security = container.get("securityContext") or {}
        if security.get("privileged") or security.get("allowPrivilegeEscalation") is True:
            return False, "privileged containers and privilege escalation are not allowed"
    return True, ""


@mcp.tool()
def create_workload(manifest: dict, dry_run: bool = True) -> dict:
    """Create one release-gate validated apps/v1 workload."""
    valid, reason = _validate_create_workload_manifest(manifest)
    if not valid:
        raise ValueError(f"manifest rejected by safety policy: {reason}")
    metadata = manifest.get("metadata") or {}
    namespace = str(metadata.get("namespace"))
    check_namespace(namespace)
    kind = str(manifest.get("kind"))
    resource = WORKLOAD_RESOURCE_MAP[kind.lower()]
    if dry_run:
        return {"action": "create_workload", "kind": kind, "name": metadata.get("name"), "namespace": namespace, "dry_run": True}
    try:
        result = _k8s_post(f"/apis/apps/v1/namespaces/{quote(namespace, safe='')}/{resource}", manifest)
        return {
            "action": "create_workload", "kind": kind, "name": metadata.get("name"), "namespace": namespace,
            "dry_run": False, "resource_version": (result.get("metadata") or {}).get("resourceVersion"),
        }
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def patch_service(namespace: str, service_name: str, patch: dict, dry_run: bool = True) -> dict:
    """Patch only Service selector/ports after endpoint evidence and approval."""
    check_namespace(namespace)
    spec = (patch or {}).get("spec") or {}
    if not spec or set(spec) - {"selector", "ports"}:
        raise ValueError("service patch only permits spec.selector and spec.ports")
    if dry_run:
        return {"action": "patch_service", "namespace": namespace, "service_name": service_name, "patch": patch, "dry_run": True}
    try:
        result = _k8s_patch(f"/api/v1/namespaces/{quote(namespace, safe='')}/services/{quote(service_name, safe='')}", patch)
        return {"action": "patch_service", "namespace": namespace, "service_name": service_name, "dry_run": False, "resource_version": (result.get("metadata") or {}).get("resourceVersion")}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def patch_service_account(namespace: str, service_account: str, patch: dict, dry_run: bool = True) -> dict:
    """Attach approved imagePullSecrets to one ServiceAccount."""
    check_namespace(namespace)
    if not _valid_k8s_name(service_account):
        raise ValueError("service_account is not a valid Kubernetes name")
    valid, reason = _validate_service_account_patch(patch)
    if not valid:
        raise ValueError(reason)
    if dry_run:
        return {"action": "patch_service_account", "namespace": namespace, "service_account": service_account, "patch": patch, "dry_run": True}
    try:
        result = _k8s_patch(f"/api/v1/namespaces/{quote(namespace, safe='')}/serviceaccounts/{quote(service_account, safe='')}", patch)
        return {
            "action": "patch_service_account", "namespace": namespace, "service_account": service_account,
            "dry_run": False, "resource_version": (result.get("metadata") or {}).get("resourceVersion"),
        }
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def create_configmap(namespace: str, manifest: dict, dry_run: bool = True) -> dict:
    """Create a missing ConfigMap from an operator-approved template."""
    check_namespace(namespace)
    valid, reason = _validate_configmap_manifest(manifest, namespace)
    if not valid:
        raise ValueError(reason)
    name = (manifest.get("metadata") or {}).get("name")
    if dry_run:
        return {"action": "create_configmap", "namespace": namespace, "configmap": name, "manifest": manifest, "dry_run": True}
    try:
        result = _k8s_post(f"/api/v1/namespaces/{quote(namespace, safe='')}/configmaps", manifest)
        return {
            "action": "create_configmap", "namespace": namespace, "configmap": name,
            "dry_run": False, "resource_version": (result.get("metadata") or {}).get("resourceVersion"),
        }
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def patch_pdb(namespace: str, pdb_name: str, patch: dict, dry_run: bool = True) -> dict:
    """Patch one PDB availability threshold after replica-budget validation."""
    check_namespace(namespace)
    spec = (patch or {}).get("spec") or {}
    if not spec or set(spec) - {"minAvailable", "maxUnavailable"} or len(spec) != 1:
        raise ValueError("PDB patch must set exactly one of minAvailable or maxUnavailable")
    if dry_run:
        return {"action": "patch_pdb", "namespace": namespace, "pdb_name": pdb_name, "patch": patch, "dry_run": True}
    try:
        result = _k8s_patch(f"/apis/policy/v1/namespaces/{quote(namespace, safe='')}/poddisruptionbudgets/{quote(pdb_name, safe='')}", patch)
        return {"action": "patch_pdb", "namespace": namespace, "pdb_name": pdb_name, "dry_run": False, "resource_version": (result.get("metadata") or {}).get("resourceVersion")}
    except Exception as exc:
        return {"error": str(exc)}

@mcp.tool()
def list_nodes() -> dict:
    """List all nodes and their conditions."""
    try:
        data = _k8s_get("/api/v1/nodes")
    except Exception as e:
        return {"error": str(e)}

    nodes = []
    for n in data.get("items", []):
        conditions = n["status"].get("conditions", [])
        node_health = normalize_node_conditions(conditions)
        nodes.append({
            "name": n["metadata"]["name"],
            "conditions": conditions,
            "condition_summary": node_health["conditions"],
            "ready": node_health["ready"],
            "health": node_health["health"],
            "problems": node_health["problems"],
            "condition_standard": node_health["standard"],
            "allocatable": n["status"].get("allocatable", {}),
            "unschedulable": bool((n.get("spec") or {}).get("unschedulable")),
        })
    return {"nodes": nodes, "condition_standard": "Ready=True is healthy; pressure conditions True are unhealthy, False means no pressure."}


@mcp.tool()
def check_access(
    namespace: str = "default",
    verb: str = "patch",
    resource: str = "deployments",
    group: str = "apps",
    name: str = "",
) -> dict:
    """Check the current ServiceAccount's Kubernetes RBAC permission."""
    body = {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {
            "resourceAttributes": {
                "namespace": namespace,
                "verb": verb,
                "group": group,
                "resource": resource,
            }
        },
    }
    if name:
        body["spec"]["resourceAttributes"]["name"] = name
    try:
        result = _k8s_post("/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", body)
        status = result.get("status", {})
        return {
            "namespace": namespace,
            "verb": verb,
            "group": group,
            "resource": resource,
            "name": name,
            "allowed": bool(status.get("allowed")),
            "denied": bool(status.get("denied")),
            "reason": status.get("reason", ""),
            "evaluation_error": status.get("evaluationError", ""),
        }
    except Exception as e:
        return {"error": str(e), "request": body}


@mcp.tool()
def list_pod_metrics(namespace: str = "default") -> dict:
    """List pod CPU/memory usage from metrics-server."""
    try:
        data = _k8s_get(f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods")
    except Exception as e:
        return {"error": f"Failed to list pod metrics in namespace {namespace}: {str(e)}"}

    return {
        "namespace": namespace,
        "pods": [
            {
                "name": item["metadata"]["name"],
                "timestamp": item.get("timestamp"),
                "window": item.get("window"),
                "containers": [
                    {
                        "name": c["name"],
                        "cpu": c.get("usage", {}).get("cpu"),
                        "memory": c.get("usage", {}).get("memory"),
                    }
                    for c in item.get("containers", [])
                ],
            }
            for item in data.get("items", [])
        ],
    }

# ---------- 新增聚合工具 ----------

@mcp.tool()
def get_cluster_summary() -> dict:
    """获取集群摘要：命名空间数、Pod数、Deployment数、节点数、高重启Pod"""
    try:
        # 获取所有命名空间
        ns_data = _k8s_get("/api/v1/namespaces")
        namespaces = [ns["metadata"]["name"] for ns in ns_data.get("items", [])]
        ns_count = len(namespaces)

        # 获取所有 Pod
        all_pods = []
        for ns in namespaces:
            try:
                pods_data = _k8s_get(f"/api/v1/namespaces/{ns}/pods")
                all_pods.extend(pods_data.get("items", []))
            except:
                pass

        total_pods = len(all_pods)
        running = sum(1 for p in all_pods if p.get("status", {}).get("phase") == "Running")
        pending = sum(1 for p in all_pods if p.get("status", {}).get("phase") == "Pending")
        failed = total_pods - running - pending

        # 高重启告警
        alerts = []
        for p in all_pods:
            restart = sum(cs.get("restartCount", 0) for cs in p.get("status", {}).get("containerStatuses", []))
            if restart > 5:
                alerts.append({
                    "namespace": p["metadata"]["namespace"],
                    "name": p["metadata"]["name"],
                    "restart_count": restart
                })

        # 获取所有 Deployment
        all_deployments = []
        for ns in namespaces:
            try:
                dep_data = _k8s_get(f"/apis/apps/v1/namespaces/{ns}/deployments")
                all_deployments.extend(dep_data.get("items", []))
            except:
                pass

        total_deps = len(all_deployments)
        healthy_deps = sum(
            1 for d in all_deployments
            if d.get("status", {}).get("availableReplicas", 0) >= d.get("spec", {}).get("replicas", 0)
        )

        # 获取节点
        nodes_data = _k8s_get("/api/v1/nodes")
        nodes = nodes_data.get("items", [])
        total_nodes = len(nodes)
        ready_nodes = sum(
            1 for n in nodes
            if any(c["type"] == "Ready" and c["status"] == "True" for c in n.get("status", {}).get("conditions", []))
        )

        return {
            "namespaces": {
                "total": ns_count,
                "list": [{"name": ns} for ns in namespaces]
            },
            "pods": {
                "total": total_pods,
                "running": running,
                "pending": pending,
                "failed": failed
            },
            "deployments": {
                "total": total_deps,
                "healthy": healthy_deps,
                "unhealthy": total_deps - healthy_deps
            },
            "nodes": {
                "total": total_nodes,
                "ready": ready_nodes
            },
            "alerts": alerts
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_all_pods() -> dict:
    """列出所有命名空间的 Pod，按命名空间分组"""
    try:
        ns_data = _k8s_get("/api/v1/namespaces")
        namespaces = [ns["metadata"]["name"] for ns in ns_data.get("items", [])]
        result = {}
        for ns in namespaces:
            try:
                pods_data = _k8s_get(f"/api/v1/namespaces/{ns}/pods")
                pods = []
                for item in pods_data.get("items", []):
                    pod = _pod_identity(item)
                    pod["classification"] = classify_pod(pod)
                    pod["workload"] = _workload_from_pod(pod)
                    pods.append(pod)
                result[ns] = pods
            except:
                result[ns] = []
        return {"pods_by_namespace": result}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_all_deployments() -> dict:
    """列出所有命名空间的 Deployment，按命名空间分组"""
    try:
        ns_data = _k8s_get("/api/v1/namespaces")
        namespaces = [ns["metadata"]["name"] for ns in ns_data.get("items", [])]
        result = {}
        for ns in namespaces:
            try:
                dep_data = _k8s_get(f"/apis/apps/v1/namespaces/{ns}/deployments")
                result[ns] = [
                    {
                        "name": d["metadata"]["name"],
                        "replicas": d.get("spec", {}).get("replicas", 0),
                        "ready_replicas": d.get("status", {}).get("readyReplicas", 0),
                        "available_replicas": d.get("status", {}).get("availableReplicas", 0),
                        "containers": [
                            {"name": c["name"], "image": c["image"]}
                            for c in d.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                        ]
                    }
                    for d in dep_data.get("items", [])
                ]
            except:
                result[ns] = []
        return {"deployments_by_namespace": result}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_namespaces() -> dict:
    """列出所有命名空间"""
    try:
        data = _k8s_get("/api/v1/namespaces")
        namespaces = [
            {
                "name": ns["metadata"]["name"],
                "status": ns.get("status", {}).get("phase", "Active"),
                "created_at": ns["metadata"].get("creationTimestamp")
            }
            for ns in data.get("items", [])
        ]
        return {"namespaces": namespaces}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_resilience_topology(namespace: str = "all") -> dict:
    """Build workload/pod topology with resilience and blast-radius assessment."""
    try:
        if namespace == "all":
            ns_data = _k8s_get("/api/v1/namespaces")
            namespaces = [ns["metadata"]["name"] for ns in ns_data.get("items", [])]
        else:
            namespaces = [namespace]

        pv_index = {
            pv.get("metadata", {}).get("name", ""): pv
            for pv in _safe_k8s_get("/api/v1/persistentvolumes").get("items", [])
        }
        topology = []
        summary = {
            "namespaces": 0,
            "workloads": 0,
            "pods": 0,
            "dependencies": 0,
            "ingress_dependencies": 0,
            "mesh_dependencies": 0,
            "storage_dependencies": 0,
            "data_dependencies": 0,
            "infrastructure_pods": 0,
            "application_pods": 0,
            "data_pods": 0,
            "batch_pods": 0,
            "critical_risks": 0,
        }

        for ns in namespaces:
            try:
                pods_data = _k8s_get(f"/api/v1/namespaces/{ns}/pods")
                services_data = _k8s_get(f"/api/v1/namespaces/{ns}/services")
            except Exception:
                continue

            services = services_data.get("items", [])
            ingresses = _safe_k8s_get(f"/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses").get("items", [])
            pvcs = _safe_k8s_get(f"/api/v1/namespaces/{ns}/persistentvolumeclaims").get("items", [])
            pvc_index = {pvc.get("metadata", {}).get("name", ""): pvc for pvc in pvcs}
            ingress_deps = _ingress_dependencies(ingresses)
            istio_deps = _istio_dependencies(ns)
            workloads: dict[str, dict] = {}
            for item in pods_data.get("items", []):
                pod = _pod_identity(item)
                pod["classification"] = classify_pod(pod)
                pod["workload"] = _workload_from_pod(pod)
                ready = pod["phase"] == "Running" and all(c.get("ready") for c in pod["containers"])
                pod["ready"] = ready
                pod["completed"] = pod["phase"] in {"Succeeded", "Completed"} or all(
                    (c.get("state_detail") or {}).get("terminated_reason") == "Completed"
                    for c in pod.get("containers", []) or []
                )
                pod["failed"] = pod["phase"] == "Failed" or any(
                    (c.get("state_detail") or {}).get("terminated_reason") not in {"", "Completed"}
                    and (c.get("state_detail") or {}).get("exit_code") not in {None, 0}
                    for c in pod.get("containers", []) or []
                )
                workload = pod["workload"]
                key = f"{workload['kind']}/{workload['name']}"
                if key not in workloads:
                    workloads[key] = {
                        "kind": workload["kind"],
                        "name": workload["name"],
                        "category": pod["classification"]["class"],
                        "category_label": pod["classification"]["label"],
                        "pods": [],
                        "services": [],
                        "dependencies": [],
                    }
                workloads[key]["pods"].append(pod)

                cls = pod["classification"]["class"]
                if cls == "infrastructure":
                    summary["infrastructure_pods"] += 1
                elif cls == "data":
                    summary["data_pods"] += 1
                elif cls == "batch":
                    summary["batch_pods"] += 1
                else:
                    summary["application_pods"] += 1

            service_to_workloads: dict[str, list[dict]] = {}
            for svc in services:
                selector = svc.get("spec", {}).get("selector") or {}
                if not selector:
                    continue
                for workload in workloads.values():
                    for pod in workload["pods"]:
                        if _service_matches_pod(selector, pod):
                            svc_info = {
                                "name": svc["metadata"]["name"],
                                "type": svc.get("spec", {}).get("type"),
                                "cluster_ip": svc.get("spec", {}).get("clusterIP"),
                                "ports": [
                                    {
                                        "name": p.get("name"),
                                        "port": p.get("port"),
                                        "target_port": p.get("targetPort"),
                                        "protocol": p.get("protocol"),
                                    }
                                    for p in svc.get("spec", {}).get("ports", [])
                                ],
                            }
                            if not any(s["name"] == svc_info["name"] for s in workload["services"]):
                                workload["services"].append(svc_info)
                            service_to_workloads.setdefault(svc_info["name"], []).append(workload)
                            break

            def add_dependency(workload: dict, dep: dict):
                key = "|".join([dep.get("type", ""), dep.get("kind", ""), dep.get("name", ""), dep.get("service", "")])
                if not any(d.get("_key") == key for d in workload["dependencies"]):
                    dep["_key"] = key
                    workload["dependencies"].append(dep)

            for dep in ingress_deps + istio_deps:
                for workload in service_to_workloads.get(dep.get("service", ""), []):
                    add_dependency(workload, dep)

            for workload in workloads.values():
                for pod in workload["pods"]:
                    annotations = pod.get("annotations", {}) or {}
                    has_sidecar = any(c.get("name") in {"istio-proxy", "envoy"} for c in pod.get("containers", []))
                    if has_sidecar or annotations.get("sidecar.istio.io/status"):
                        add_dependency(workload, {
                            "kind": "Sidecar",
                            "type": "mesh",
                            "name": "istio-proxy",
                            "label": "Istio Sidecar",
                            "source": "pod_sidecar",
                            "confidence": 0.9,
                        })
                    for vol in pod.get("volumes", []):
                        claim = vol.get("persistent_volume_claim")
                        if claim and claim in pvc_index:
                            add_dependency(workload, _storage_dependency_for_claim(pvc_index[claim], pv_index))
                    for hint in pod.get("dependency_hints", []):
                        add_dependency(workload, {
                            "kind": "DependencyHint",
                            "type": hint.get("type", "external"),
                            "name": hint.get("name", ""),
                            "label": hint.get("label", "依赖线索"),
                            "source": hint.get("source", "pod_spec_hint"),
                            "confidence": hint.get("confidence", 0.5),
                        })

            workload_list = []
            for workload in workloads.values():
                total = len(workload["pods"])
                ready = sum(1 for p in workload["pods"] if p.get("ready"))
                completed = sum(1 for p in workload["pods"] if p.get("completed"))
                failed = sum(1 for p in workload["pods"] if p.get("failed"))
                workload["impact"] = _impact_for_workload(total, ready, workload["category"], completed, failed)
                for dep in workload.get("dependencies", []):
                    dep.pop("_key", None)
                    summary["dependencies"] += 1
                    if dep.get("type") == "ingress":
                        summary["ingress_dependencies"] += 1
                    elif dep.get("type") == "mesh":
                        summary["mesh_dependencies"] += 1
                    elif dep.get("type") == "storage":
                        summary["storage_dependencies"] += 1
                    elif dep.get("type") == "data":
                        summary["data_dependencies"] += 1
                if workload["impact"]["level"] == "critical":
                    summary["critical_risks"] += 1
                workload_list.append(workload)

            summary["namespaces"] += 1
            summary["workloads"] += len(workload_list)
            summary["pods"] += sum(len(w["pods"]) for w in workload_list)
            topology.append({"namespace": ns, "workloads": workload_list})

        return {"summary": summary, "topology": topology}
    except Exception as e:
        return {"error": str(e)}
    

@mcp.tool()
def get_external_traffic_candidates(namespace: str = "all") -> dict:
    """Collect raw K8s objects used to infer external/cross-cluster traffic.

    This tool is intentionally read-only. It does not read Secret values; Pod
    specs may contain env literals, and the API layer redacts sensitive fields
    before showing evidence to users.
    """
    try:
        ns = str(namespace or "all")
        if ns.lower() in {"", "all", "*", "所有", "所有namespace"}:
            pods = _safe_k8s_get("/api/v1/pods").get("items", [])
            services = _safe_k8s_get("/api/v1/services").get("items", [])
            endpoints = _safe_k8s_get("/api/v1/endpoints").get("items", [])
            endpoint_slices = _safe_k8s_get("/apis/discovery.k8s.io/v1/endpointslices").get("items", [])
            ingresses = _safe_k8s_get("/apis/networking.k8s.io/v1/ingresses").get("items", [])
            network_policies = _safe_k8s_get("/apis/networking.k8s.io/v1/networkpolicies").get("items", [])
        else:
            check_namespace(ns)
            qns = quote(ns, safe="")
            pods = _safe_k8s_get(f"/api/v1/namespaces/{qns}/pods").get("items", [])
            services = _safe_k8s_get(f"/api/v1/namespaces/{qns}/services").get("items", [])
            endpoints = _safe_k8s_get(f"/api/v1/namespaces/{qns}/endpoints").get("items", [])
            endpoint_slices = _safe_k8s_get(f"/apis/discovery.k8s.io/v1/namespaces/{qns}/endpointslices").get("items", [])
            ingresses = _safe_k8s_get(f"/apis/networking.k8s.io/v1/namespaces/{qns}/ingresses").get("items", [])
            network_policies = _safe_k8s_get(f"/apis/networking.k8s.io/v1/namespaces/{qns}/networkpolicies").get("items", [])
        cluster_name = os.getenv("CLUSTER_NAME", "local-cluster")
        resources = [{
            "cluster": {"id": cluster_name, "name": cluster_name, "source": "mcp"},
            "pods": pods,
            "services": services,
            "endpoints": endpoints,
            "endpoint_slices": endpoint_slices,
            "ingresses": ingresses,
            "network_policies": network_policies,
            "source": "mcp",
        }]
        return {
            "status": "ok",
            "resources": resources,
            "summary": {
                "pods": len(pods),
                "services": len(services),
                "endpoints": len(endpoints),
                "endpoint_slices": len(endpoint_slices),
                "ingresses": len(ingresses),
                "network_policies": len(network_policies),
            },
        }
    except Exception as e:
        return {"error": str(e)}


__all__ = [
    "kubernetes_access_status",
    "list_pods",
    "get_pod_events",
    "get_pod_logs",
    "get_pod_diagnostics",
    "restart_deployment",
    "scale_deployment",
    "recreate_pod",
    "evict_pod",
    "patch_hpa",
    "expand_pvc",
    "create_pvc",
    "create_persistent_volume",
    "cordon_node",
    "get_remediation_target_state",
    "list_nodes",
    "check_access",
    "patch_workload",
    "create_workload",
    "patch_service",
    "patch_pdb",
    "get_cluster_summary",
    "list_all_pods",
    "list_all_deployments",
    "list_namespaces",
    "list_pod_metrics",
    "get_resilience_topology",
    "get_external_traffic_candidates",
]


if __name__ == "__main__":
    mcp.run()
