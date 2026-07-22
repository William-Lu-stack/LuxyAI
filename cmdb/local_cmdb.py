"""
Local CMDB service for k8s-agent.

This service turns live Kubernetes objects into an application/data-flow topology
that the frontend can consume via /api/cmdb/topology.
"""
from __future__ import annotations

import os
import re
import ssl
import json
import asyncio
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException

TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
K8S_HOST = os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
K8S_PORT = os.getenv("KUBERNETES_SERVICE_PORT", "443")
API_BASE = f"https://{K8S_HOST}:{K8S_PORT}"

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
INFRA_KEYWORDS = {
    "coredns",
    "kube-proxy",
    "calico",
    "flannel",
    "cilium",
    "ingress",
    "prometheus",
    "grafana",
    "alertmanager",
    "node-exporter",
    "kube-state-metrics",
    "cert-manager",
    "istio",
    "envoy",
    "jaeger",
    "otel",
}
DATA_KEYWORDS = {
    "mysql",
    "postgres",
    "postgresql",
    "redis",
    "mongodb",
    "mongo",
    "kafka",
    "zookeeper",
    "elastic",
    "clickhouse",
    "minio",
    "etcd",
    "rabbitmq",
    "nacos",
}

app = FastAPI(title="k8s-agent Local CMDB", version="1.0")


def _allowed_namespaces() -> set[str] | None:
    # Topology is read-only. Do not reuse the remediation allowlist here, otherwise
    # a safe mutation boundary silently hides most clusters from CMDB analysis.
    raw = os.getenv("CMDB_ALLOWED_NAMESPACES", "all")
    items = {item.strip() for item in raw.split(",") if item.strip()}
    if not items or "all" in items or "*" in items:
        return None
    return items


def _token() -> str:
    with open(TOKEN_FILE, encoding="utf-8") as f:
        return f.read().strip()


def _ssl_context() -> ssl.SSLContext:
    if os.path.exists(CA_FILE):
        return ssl.create_default_context(cafile=CA_FILE)
    return ssl.create_default_context()


def _k8s_get(path: str) -> dict[str, Any]:
    req = Request(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    with urlopen(req, context=_ssl_context(), timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _safe_get(path: str) -> dict[str, Any]:
    try:
        return _k8s_get(path)
    except Exception as exc:
        return {"items": [], "_error": f"{type(exc).__name__}: {exc}"}


def _items(path: str) -> list[dict[str, Any]]:
    return _safe_get(path).get("items") or []


def _items_with_diagnostics(path: str, label: str, diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _safe_get(path)
    items = payload.get("items") or []
    diagnostics["resources"][label] = len(items)
    if payload.get("_error"):
        diagnostics["errors"][label] = payload["_error"]
    return items


def _rancher_enabled() -> bool:
    return bool(os.getenv("RANCHER_URL", "").strip() and os.getenv("RANCHER_TOKEN", "").strip())


def _rancher_ssl_context() -> ssl.SSLContext:
    verify = os.getenv("RANCHER_VERIFY_SSL", "true").lower() in {"1", "true", "yes", "on"}
    if not verify:
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def _rancher_get(path: str) -> dict[str, Any]:
    base = os.getenv("RANCHER_URL", "").strip().rstrip("/")
    for marker in ("/dashboard", "/v3", "/v1", "/k8s/clusters"):
        if marker in base:
            base = base.split(marker, 1)[0]
    base = base.rstrip("/")
    token = os.getenv("RANCHER_TOKEN", "").strip()
    url = path if str(path).startswith(("http://", "https://")) else f"{base}/{path.lstrip('/')}"
    req = Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urlopen(req, context=_rancher_ssl_context(), timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _with_rancher_limit(path_or_url: str, limit: int = 1000) -> str:
    parsed = urlparse(path_or_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("limit", str(limit))
    return urlunparse(parsed._replace(query=urlencode(query)))


def _rancher_collect(path_or_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = _with_rancher_limit(path_or_url)
    pages = 0
    while page and pages < 50:
        payload = _rancher_get(page)
        data = payload.get("data") or [] if isinstance(payload, dict) else []
        if isinstance(data, list):
            items.extend(x for x in data if isinstance(x, dict))
        pagination = payload.get("pagination") or {} if isinstance(payload, dict) else {}
        links = payload.get("links") or {} if isinstance(payload, dict) else {}
        next_page = pagination.get("next") or links.get("next")
        if not next_page or next_page == page:
            break
        page = next_page
        pages += 1
    return items


def _wanted_rancher_clusters() -> set[str] | None:
    raw = os.getenv("RANCHER_CLUSTER_IDS", "all")
    items = {item.strip() for item in raw.split(",") if item.strip()}
    lowered = {item.lower() for item in items}
    if not items or {"all", "*", "所有"} & lowered:
        return None
    return items


def _rancher_clusters() -> list[dict[str, str]]:
    endpoints: list[tuple[str, str]] = []
    try:
        root = _rancher_get("/v3")
        cluster_link = ((root.get("links") or {}).get("clusters") if isinstance(root, dict) else "") or ""
        if cluster_link:
            endpoints.append((cluster_link, "v3/root.links.clusters"))
    except Exception:
        pass
    endpoints.extend([
        ("/v3/clusters", "v3/clusters"),
        ("/v1/management.cattle.io.clusters?limit=-1", "v1/management.cattle.io.clusters"),
        ("/v1/provisioning.cattle.io.clusters?limit=-1", "v1/provisioning.cattle.io.clusters"),
    ])
    wanted = _wanted_rancher_clusters()
    clusters_by_id: dict[str, dict[str, str]] = {}
    for path, source in endpoints:
        try:
            data = _rancher_collect(path)
        except Exception:
            continue
        for item in data:
            meta = item.get("metadata") or {}
            spec = item.get("spec") or {}
            status = item.get("status") or {}
            raw_id = str(item.get("id") or meta.get("name") or item.get("name") or "")
            cid = str(status.get("clusterName") or raw_id)
            name = str(
                item.get("displayName")
                or spec.get("displayName")
                or item.get("name")
                or meta.get("name")
                or cid
            )
            if not cid:
                continue
            if wanted and cid not in wanted and name not in wanted:
                continue
            clusters_by_id.setdefault(cid, {"id": cid, "name": name, "source": source})
    clusters = list(clusters_by_id.values())
    clusters.sort(key=lambda c: (c["name"] == "local", c["name"]))
    return clusters


def _rancher_k8s_get(cluster_id: str, path: str) -> dict[str, Any]:
    return _rancher_get(f"/k8s/clusters/{cluster_id}{path}")


def _ns_allowed(namespace: str, allowed: set[str] | None) -> bool:
    return allowed is None or namespace in allowed


def _labels(obj: dict[str, Any]) -> dict[str, str]:
    return (obj.get("metadata") or {}).get("labels") or {}


def _annotations(obj: dict[str, Any]) -> dict[str, str]:
    return (obj.get("metadata") or {}).get("annotations") or {}


def _name(obj: dict[str, Any]) -> str:
    return (obj.get("metadata") or {}).get("name") or ""


def _namespace(obj: dict[str, Any]) -> str:
    return (obj.get("metadata") or {}).get("namespace") or "default"


def _node_id(kind: str, namespace: str, name: str) -> str:
    return f"{kind}:{namespace}:{name}"


def _classify(namespace: str, name: str, labels: dict[str, str]) -> str:
    text = " ".join([namespace, name, *labels.values(), *labels.keys()]).lower()
    if namespace in INFRA_NAMESPACES or any(key in text for key in INFRA_KEYWORDS):
        return "infrastructure"
    if any(key in text for key in DATA_KEYWORDS):
        return "data"
    if "job" in text or "cron" in text or "batch" in text:
        return "batch"
    return "application"


def _selector_matches(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return bool(selector) and all(labels.get(key) == value for key, value in selector.items())


def _workload_from_owner(pod: dict[str, Any], replica_owner: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    owners = (pod.get("metadata") or {}).get("ownerReferences") or []
    if not owners:
        return None
    owner = owners[0]
    kind = owner.get("kind") or ""
    name = owner.get("name") or ""
    if kind == "ReplicaSet":
        return replica_owner.get(name) or ("ReplicaSet", name)
    if kind:
        return kind, name
    return None


def _workload_node(kind: str, obj: dict[str, Any]) -> dict[str, Any]:
    namespace = _namespace(obj)
    name = _name(obj)
    labels = _labels(obj)
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    replicas = spec.get("replicas", status.get("replicas", 1))
    ready = status.get("readyReplicas", status.get("numberReady", 0))
    return {
        "id": _node_id(kind.lower(), namespace, name),
        "name": name,
        "type": _classify(namespace, name, labels),
        "kind": kind,
        "namespace": namespace,
        "tier": labels.get("app.kubernetes.io/part-of") or labels.get("tier") or kind,
        "owner": labels.get("app.kubernetes.io/managed-by") or labels.get("team") or "",
        "replicas": replicas,
        "ready_replicas": ready,
        "raw": {
            "labels": labels,
            "annotations": _annotations(obj),
        },
    }


def _service_node(obj: dict[str, Any]) -> dict[str, Any]:
    namespace = _namespace(obj)
    name = _name(obj)
    labels = _labels(obj)
    spec = obj.get("spec") or {}
    return {
        "id": _node_id("service", namespace, name),
        "name": name,
        "type": "service",
        "kind": "Service",
        "namespace": namespace,
        "tier": spec.get("type", "ClusterIP"),
        "owner": labels.get("team", ""),
        "ports": spec.get("ports") or [],
        "raw": {
            "selector": spec.get("selector") or {},
            "cluster_ip": spec.get("clusterIP", ""),
            "labels": labels,
            "annotations": _annotations(obj),
        },
    }


def _dependency_edges_from_env(source_id: str, obj: dict[str, Any], known_services: dict[tuple[str, str], str]) -> list[dict[str, Any]]:
    namespace = _namespace(obj)
    spec = obj.get("spec") or {}
    tmpl = ((spec.get("template") or {}).get("spec") or {})
    containers = tmpl.get("containers") or []
    edges: list[dict[str, Any]] = []
    for container in containers:
        for env in container.get("env") or []:
            value = str(env.get("value") or "")
            name = str(env.get("name") or "")
            candidates = set(re.findall(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value.lower()))
            if name.endswith("_SERVICE_HOST"):
                svc = name.removesuffix("_SERVICE_HOST").lower().replace("_", "-")
                candidates.add(svc)
            if any(key in name.lower() for key in ("database", "redis", "mysql", "postgres", "kafka", "mongo")):
                dep_name = value.split(":")[0].split("/")[0] if value else name.lower()
                dep_id = f"external:{namespace}:{dep_name}"
                edges.append({"source": source_id, "target": dep_id, "type": "config_dependency", "evidence": name})
            for candidate in candidates:
                svc_id = known_services.get((namespace, candidate))
                if svc_id:
                    edges.append({"source": source_id, "target": svc_id, "type": "env_dependency", "evidence": name})
    return edges


def _collect_cluster_topology(
    cluster_id: str,
    cluster_name: str,
    fetcher,
    allowed: set[str] | None,
) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {
        "cluster_id": cluster_id,
        "cluster": cluster_name,
        "allowed_namespaces": "all" if allowed is None else sorted(allowed),
        "resources": {},
        "errors": {},
    }

    def items(path: str, label: str) -> list[dict[str, Any]]:
        try:
            payload = fetcher(path)
            result = payload.get("items") or []
            diagnostics["resources"][label] = len(result)
            return result
        except Exception as exc:
            diagnostics["resources"][label] = 0
            diagnostics["errors"][label] = f"{type(exc).__name__}: {exc}"
            return []

    deployments = [x for x in items("/apis/apps/v1/deployments", "deployments") if _ns_allowed(_namespace(x), allowed)]
    statefulsets = [x for x in items("/apis/apps/v1/statefulsets", "statefulsets") if _ns_allowed(_namespace(x), allowed)]
    daemonsets = [x for x in items("/apis/apps/v1/daemonsets", "daemonsets") if _ns_allowed(_namespace(x), allowed)]
    replicasets = [x for x in items("/apis/apps/v1/replicasets", "replicasets") if _ns_allowed(_namespace(x), allowed)]
    services = [x for x in items("/api/v1/services", "services") if _ns_allowed(_namespace(x), allowed)]
    pods = [x for x in items("/api/v1/pods", "pods") if _ns_allowed(_namespace(x), allowed)]
    ingresses = [x for x in items("/apis/networking.k8s.io/v1/ingresses", "ingresses") if _ns_allowed(_namespace(x), allowed)]

    replica_owner: dict[str, tuple[str, str]] = {}
    for rs in replicasets:
        owners = (rs.get("metadata") or {}).get("ownerReferences") or []
        if owners:
            owner = owners[0]
            replica_owner[_name(rs)] = (owner.get("kind", "Deployment"), owner.get("name", _name(rs)))

    workloads: list[tuple[str, dict[str, Any]]] = [
        *[("Deployment", x) for x in deployments],
        *[("StatefulSet", x) for x in statefulsets],
        *[("DaemonSet", x) for x in daemonsets],
    ]
    workload_by_key: dict[tuple[str, str, str], str] = {}
    for kind, obj in workloads:
        node = _workload_node(kind, obj)
        nodes[node["id"]] = node
        workload_by_key[(node["namespace"], kind, node["name"])] = node["id"]

    pod_workload: dict[tuple[str, str], str] = {}
    for pod in pods:
        namespace = _namespace(pod)
        pod_name = _name(pod)
        owner = _workload_from_owner(pod, replica_owner)
        workload_id = ""
        if owner:
            kind, name = owner
            workload_id = workload_by_key.get((namespace, kind, name))
            if workload_id:
                pod_workload[(namespace, pod_name)] = workload_id
        status = pod.get("status") or {}
        phase = str(status.get("phase") or "Unknown")
        container_statuses = status.get("containerStatuses") or []
        ready = bool(container_statuses) and all(bool(item.get("ready")) for item in container_statuses)
        reasons: list[str] = []
        restart_count = 0
        for container in container_statuses:
            restart_count += int(container.get("restartCount") or 0)
            state = container.get("state") or {}
            last_state = container.get("lastState") or {}
            for detail in [state.get("waiting") or {}, state.get("terminated") or {}, last_state.get("terminated") or {}]:
                reason = str(detail.get("reason") or "").strip()
                if reason and reason not in reasons:
                    reasons.append(reason)
        completed = phase == "Succeeded"
        healthy = completed or (phase == "Running" and ready)
        pod_id = _node_id("pod", namespace, pod_name)
        nodes[pod_id] = {
            "id": pod_id,
            "name": pod_name,
            "type": "pod",
            "kind": "Pod",
            "namespace": namespace,
            "tier": "one-shot" if completed else _classify(namespace, pod_name, _labels(pod)),
            "owner": workload_id,
            "phase": phase,
            "ready": ready,
            "restart_count": restart_count,
            "risk": "normal" if healthy else "high" if any(reason in {"CrashLoopBackOff", "OOMKilled", "ImagePullBackOff", "ErrImagePull"} for reason in reasons) else "warning",
            "raw": {
                "labels": _labels(pod),
                "annotations": _annotations(pod),
                "phase": phase,
                "ready": ready,
                "reasons": reasons,
                "restart_count": restart_count,
                "completed_job": completed,
            },
        }
        if workload_id:
            edges.append({"source": workload_id, "target": pod_id, "type": "owns"})

    known_services: dict[tuple[str, str], str] = {}
    for svc in services:
        node = _service_node(svc)
        nodes[node["id"]] = node
        known_services[(node["namespace"], node["name"])] = node["id"]
        selector = ((svc.get("spec") or {}).get("selector") or {})
        matched = set()
        for pod in pods:
            if _namespace(pod) != node["namespace"] or not _selector_matches(selector, _labels(pod)):
                continue
            workload_id = pod_workload.get((node["namespace"], _name(pod)))
            if workload_id and workload_id not in matched:
                matched.add(workload_id)
                edges.append({"source": node["id"], "target": workload_id, "type": "routes_to"})

    for kind, obj in workloads:
        namespace = _namespace(obj)
        source_id = workload_by_key.get((namespace, kind, _name(obj)))
        if source_id:
            edges.extend(_dependency_edges_from_env(source_id, obj, known_services))

    for ingress in ingresses:
        namespace = _namespace(ingress)
        ingress_id = _node_id("ingress", namespace, _name(ingress))
        nodes[ingress_id] = {
            "id": ingress_id,
            "name": _name(ingress),
            "type": "ingress",
            "kind": "Ingress",
            "namespace": namespace,
            "tier": "north-south",
            "owner": "",
            "raw": {"labels": _labels(ingress), "annotations": _annotations(ingress)},
        }
        for rule in (ingress.get("spec") or {}).get("rules") or []:
            http = rule.get("http") or {}
            for path in http.get("paths") or []:
                svc_name = (((path.get("backend") or {}).get("service") or {}).get("name") or "")
                svc_id = known_services.get((namespace, svc_name))
                if svc_id:
                    edges.append({"source": ingress_id, "target": svc_id, "type": "ingress_route", "host": rule.get("host", "")})

    for edge in list(edges):
        if str(edge.get("target", "")).startswith("external:") and edge["target"] not in nodes:
            _, namespace, name = edge["target"].split(":", 2)
            dep_type = _classify(namespace, name, {})
            nodes[edge["target"]] = {
                "id": edge["target"],
                "name": name,
                "type": dep_type if dep_type == "data" else "external",
                "kind": "ExternalDependency",
                "namespace": namespace,
                "tier": "external",
                "owner": "",
                "raw": {},
            }

    local_edges = {}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in nodes and target in nodes and source != target:
            local_edges[(source, target, edge.get("type", "dependency"))] = edge

    prefix = f"cluster:{cluster_id}:"
    cluster_node_id = f"{prefix}cluster"
    out_nodes: list[dict[str, Any]] = [{
        "id": cluster_node_id,
        "name": cluster_name,
        "type": "cluster",
        "kind": "RancherCluster" if cluster_id != "local" else "KubernetesCluster",
        "namespace": "",
        "cluster": cluster_name,
        "cluster_id": cluster_id,
        "tier": "cluster",
        "owner": "rancher" if cluster_id != "local" else "local",
        "raw": {},
    }]
    out_edges: list[dict[str, Any]] = []
    id_map = {old_id: f"{prefix}{old_id}" for old_id in nodes}
    namespaces = sorted({str(n.get("namespace") or "") for n in nodes.values() if n.get("namespace")})
    ns_ids: dict[str, str] = {}
    for namespace in namespaces:
        ns_id = f"{prefix}namespace:{namespace}"
        ns_ids[namespace] = ns_id
        out_nodes.append({
            "id": ns_id,
            "name": namespace,
            "type": "namespace",
            "kind": "Namespace",
            "namespace": namespace,
            "cluster": cluster_name,
            "cluster_id": cluster_id,
            "tier": "namespace",
            "owner": "",
            "raw": {},
        })
        out_edges.append({"source": cluster_node_id, "target": ns_id, "type": "contains"})
    for old_id, node in nodes.items():
        new_node = {
            **node,
            "id": id_map[old_id],
            "cluster": cluster_name,
            "cluster_id": cluster_id,
        }
        out_nodes.append(new_node)
        ns_id = ns_ids.get(str(node.get("namespace") or ""))
        if ns_id:
            out_edges.append({"source": ns_id, "target": new_node["id"], "type": "contains"})
    for edge in local_edges.values():
        out_edges.append({
            **edge,
            "source": id_map[edge["source"]],
            "target": id_map[edge["target"]],
            "cluster": cluster_name,
            "cluster_id": cluster_id,
        })
    return {"nodes": out_nodes, "edges": out_edges, "diagnostics": diagnostics}


def _keyword_node_score(node: dict[str, Any], keywords: set[str], namespace: str = "", name: str = "", cluster: str = "") -> int:
    haystack = " ".join(
        str(node.get(key) or "")
        for key in ("id", "name", "type", "kind", "namespace", "cluster", "cluster_id", "tier")
    ).lower()
    score = 0
    if any(keyword in haystack for keyword in keywords):
        score += 20
    if namespace and str(node.get("namespace") or "").lower() == namespace.lower():
        score += 8
    if name and name.lower() in haystack:
        score += 10
    if cluster and cluster.lower() in haystack:
        score += 6
    if str(node.get("kind") or "").lower() in keywords or str(node.get("type") or "").lower() in keywords:
        score += 10
    return score


def _find_existing_node(nodes: list[dict[str, Any]], keywords: set[str], namespace: str = "", name: str = "", cluster: str = "") -> dict[str, Any] | None:
    scored = [
        (_keyword_node_score(node, keywords, namespace, name, cluster), node)
        for node in nodes
    ]
    scored = [(score, node) for score, node in scored if score >= 20]
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _add_logging_backbone(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    middleware_cluster = os.getenv("MIDDLEWARE_CLUSTER_NAME", "middleware-cluster")
    kafka_ns = os.getenv("LOGGING_KAFKA_NAMESPACE", "middleware")
    kafka_name = os.getenv("LOGGING_KAFKA_SERVICE", "kafka")
    elk_ns = os.getenv("ELK_NAMESPACE", "logging")
    elk_name = os.getenv("ELK_SERVICE", "elk")
    kafka_node = _find_existing_node(nodes, {"kafka", "strimzi"}, kafka_ns, kafka_name, middleware_cluster)
    elk_node = _find_existing_node(nodes, {"elk", "elastic", "elasticsearch", "logstash", "kibana"}, elk_ns, elk_name, middleware_cluster)
    kafka_id = str(kafka_node.get("id")) if kafka_node else f"middleware:{middleware_cluster}:kafka:{kafka_ns}:{kafka_name}"
    elk_id = str(elk_node.get("id")) if elk_node else f"observability:{middleware_cluster}:elk:{elk_ns}:{elk_name}"
    existing = {n.get("id") for n in nodes}
    if kafka_node:
        kafka_node["type"] = kafka_node.get("type") or "middleware"
        kafka_node["kind"] = kafka_node.get("kind") or "Kafka"
        kafka_node.setdefault("raw", {})
        kafka_node["raw"]["cmdb_role"] = "discovered cross-cluster logging kafka"
    elif kafka_id not in existing:
        nodes.append({
            "id": kafka_id,
            "name": kafka_name,
            "type": "middleware",
            "kind": "Kafka",
            "namespace": kafka_ns,
            "cluster": middleware_cluster,
            "cluster_id": middleware_cluster,
            "tier": "cross-cluster-logging",
            "owner": "platform",
            "raw": {"role": "synthetic fallback; receives logging components from all clusters", "discovered": False},
        })
    if elk_node:
        elk_node["type"] = elk_node.get("type") or "observability"
        elk_node["kind"] = elk_node.get("kind") or "ELK"
        elk_node.setdefault("raw", {})
        elk_node["raw"]["cmdb_role"] = "discovered log analytics backend"
    elif elk_id not in existing:
        nodes.append({
            "id": elk_id,
            "name": elk_name,
            "type": "observability",
            "kind": "ELK",
            "namespace": elk_ns,
            "cluster": middleware_cluster,
            "cluster_id": middleware_cluster,
            "tier": "log-analytics",
            "owner": "platform",
            "raw": {"role": "synthetic fallback; log search and analysis", "discovered": False},
        })
    edge_keys = {(e.get("source"), e.get("target"), e.get("type")) for e in edges}
    workload_count = 0
    for node in list(nodes):
        node_id = node.get("id")
        if node.get("type") == "cluster" and (node_id, kafka_id, "cluster_logging") not in edge_keys:
            edges.append({"source": node_id, "target": kafka_id, "type": "cluster_logging", "traffic_ratio": 0.28, "propagation_coef": 0.42})
            edge_keys.add((node_id, kafka_id, "cluster_logging"))
        if node.get("kind") in {"Deployment", "StatefulSet", "DaemonSet"} and workload_count < 200:
            edges.append({"source": node_id, "target": kafka_id, "type": "workload_log_stream", "traffic_ratio": 0.22, "propagation_coef": 0.36})
            workload_count += 1
    if (kafka_id, elk_id, "kafka_to_elk") not in edge_keys:
        edges.append({"source": kafka_id, "target": elk_id, "type": "kafka_to_elk", "traffic_ratio": 0.9, "propagation_coef": 0.74})


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "local-cmdb",
        "kubernetes_api": "not_checked",
        "message": "Process is alive. Use /ready or /topology for Kubernetes API diagnostics.",
    }


@app.get("/ready")
async def ready():
    try:
        if _rancher_enabled():
            clusters = _rancher_clusters()
            return {
                "status": "ok",
                "service": "local-cmdb",
                "mode": "rancher",
                "rancher_api": "ok",
                "clusters": [c["name"] for c in clusters],
            }
        namespaces = _k8s_get("/api/v1/namespaces?limit=1").get("items") or []
        return {
            "status": "ok",
            "service": "local-cmdb",
            "mode": "local",
            "kubernetes_api": "ok",
            "allowed_namespaces": os.getenv("ALLOWED_NAMESPACES", "all"),
            "sample_namespaces": [(_name(ns) or "-") for ns in namespaces[:1]],
        }
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "failed",
                "service": "local-cmdb",
                "kubernetes_api": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


@app.get("/topology")
@app.get("/api/topology")
async def topology():
    allowed = _allowed_namespaces()
    diagnostics: dict[str, Any] = {
        "mode": "rancher" if _rancher_enabled() else "local",
        "allowed_namespaces": "all" if allowed is None else sorted(allowed),
        "clusters": [],
        "errors": {},
    }
    all_nodes: list[dict[str, Any]] = []
    all_edges: list[dict[str, Any]] = []

    if _rancher_enabled():
        try:
            clusters = await asyncio.to_thread(_rancher_clusters)
        except Exception as exc:
            clusters = []
            diagnostics["errors"]["rancher_clusters"] = f"{type(exc).__name__}: {exc}"
        # Each Rancher cluster is independent. Collect them concurrently so one
        # slow downstream cluster does not multiply the topology response time.
        results = await asyncio.gather(*[
            asyncio.to_thread(
                _collect_cluster_topology,
                cluster["id"],
                cluster["name"],
                lambda path, cid=cluster["id"]: _rancher_k8s_get(cid, path),
                allowed,
            )
            for cluster in clusters
        ])
        for result in results:
            diagnostics["clusters"].append(result["diagnostics"])
            all_nodes.extend(result["nodes"])
            all_edges.extend(result["edges"])
    else:
        result = await asyncio.to_thread(
            _collect_cluster_topology,
            "local",
            os.getenv("LOCAL_CLUSTER_NAME", "local-cluster"),
            _k8s_get,
            allowed,
        )
        diagnostics["clusters"].append(result["diagnostics"])
        all_nodes.extend(result["nodes"])
        all_edges.extend(result["edges"])

    _add_logging_backbone(all_nodes, all_edges)

    node_ids = {n.get("id") for n in all_nodes}
    unique_edges = {}
    for edge in all_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in node_ids and target in node_ids and source != target:
            unique_edges[(source, target, edge.get("type", "dependency"))] = edge

    status = "ok"
    message = "CMDB topology built from Rancher clusters." if _rancher_enabled() else "CMDB topology built from Kubernetes resources."
    cluster_errors = {
        item.get("cluster", item.get("cluster_id", "cluster")): item.get("errors", {})
        for item in diagnostics["clusters"]
        if item.get("errors")
    }
    if diagnostics["errors"] or cluster_errors:
        status = "degraded"
        message = "CMDB 部分 Kubernetes API 读取失败，请查看 diagnostics.errors。"
        diagnostics["errors"]["clusters"] = cluster_errors
    if not all_nodes and not diagnostics["errors"]:
        status = "empty"
        message = "CMDB 已连接 Kubernetes API，但当前权限范围内没有可建模的 Workload/Service/Ingress。"

    return {
        "status": status,
        "message": message,
        "source": "rancher-cmdb" if _rancher_enabled() else "kubernetes-local-cmdb",
        "nodes": all_nodes,
        "edges": list(unique_edges.values()),
        "diagnostics": diagnostics,
        "summary": {
            "clusters": sum(1 for n in all_nodes if n.get("type") == "cluster"),
            "applications": sum(1 for n in all_nodes if n.get("type") == "application"),
            "infrastructure": sum(1 for n in all_nodes if n.get("type") == "infrastructure"),
            "data": sum(1 for n in all_nodes if n.get("type") == "data"),
            "services": sum(1 for n in all_nodes if n.get("type") == "service"),
            "middleware": sum(1 for n in all_nodes if n.get("type") in {"middleware", "observability"}),
            "relations": len(unique_edges),
        },
    }
