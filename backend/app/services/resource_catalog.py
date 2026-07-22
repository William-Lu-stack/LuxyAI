"""统一资源目录。

把 Rancher/Kubernetes 与数据库、虚拟机、中间件、存储、云资源适配器的输出
归一成同一份只读契约。业务模块只依赖该契约，不直接耦合具体基础设施 API。
"""

from __future__ import annotations

from collections import Counter
from typing import Any


def _text(value: Any, fallback: str = "") -> str:
    return str(value if value is not None and value != "" else fallback)


def _k8s_status(kind: str, item: dict[str, Any]) -> str:
    if kind == "pod":
        if item.get("issue") or (item.get("phase") not in {"Succeeded"} and not item.get("ready")):
            return "degraded"
        return "healthy"
    if kind == "workload":
        desired = int(item.get("replicas") or 0)
        ready = int(item.get("ready_replicas") or 0)
        return "healthy" if ready >= desired else "degraded"
    if kind == "node":
        return "healthy" if item.get("ready") and not item.get("problems") else "degraded"
    return "healthy" if _text(item.get("status"), "Active").lower() in {"active", "ready", "running"} else "unknown"


def _k8s_item(kind: str, item: dict[str, Any], cluster: dict[str, Any]) -> dict[str, Any]:
    cluster_id = _text(item.get("cluster_id") or cluster.get("id"), "local")
    cluster_name = _text(item.get("cluster") or cluster.get("name"), cluster_id)
    namespace = _text(item.get("namespace"))
    resource_kind = _text(item.get("kind") or item.get("workload_kind"), kind.title())
    name = _text(item.get("name"), "unknown")
    return {
        "id": f"k8s:{cluster_id}:{namespace or '_'}:{resource_kind.lower()}:{name}",
        "source": "rancher",
        "provider": "kubernetes",
        "resource_type": kind,
        "kind": resource_kind,
        "name": name,
        "cluster": cluster_name,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "status": _k8s_status(kind, item),
        "criticality": _text(item.get("criticality"), "unknown"),
        "owner": _text(item.get("owner")),
        "business_service": _text(item.get("business_service")),
        "summary": {
            key: item.get(key)
            for key in ("phase", "ready", "restart_count", "replicas", "ready_replicas", "health", "problems", "classification")
            if item.get(key) is not None and item.get(key) != ""
        },
        "raw": item,
    }


def _normalize_kubernetes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for cluster_inventory in payload.get("inventory") or []:
        if not isinstance(cluster_inventory, dict):
            continue
        cluster = cluster_inventory.get("cluster") or {}
        for key, kind in (("namespaces", "namespace"), ("nodes", "node"), ("workloads", "workload"), ("pods", "pod")):
            for item in cluster_inventory.get(key) or []:
                if isinstance(item, dict):
                    resources.append(_k8s_item(kind, item, cluster))
    return resources


def _normalize_infrastructure(payload: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for item in payload.get("resources") or []:
        if not isinstance(item, dict):
            continue
        resource_type = _text(item.get("type"), "external")
        resource_id = _text(item.get("id"), item.get("name") or "unknown")
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        resources.append({
            "id": f"infra:{resource_type}:{resource_id}",
            "source": "infrastructure_adapter",
            "provider": _text(item.get("provider"), resource_type),
            "resource_type": resource_type,
            "kind": _text(item.get("subtype"), resource_type),
            "name": _text(item.get("name"), resource_id),
            "cluster": _text(item.get("cluster"), "external"),
            "cluster_id": _text(item.get("cluster"), "external"),
            "namespace": _text(item.get("namespace")),
            "status": _text(item.get("health") or item.get("status"), "unknown"),
            "criticality": _text(item.get("criticality"), "medium"),
            "owner": _text(item.get("owner")),
            "business_service": _text(item.get("business_service")),
            "summary": {"environment": item.get("environment"), "metrics": metrics, "actions_enabled": bool(item.get("actions_enabled"))},
            "raw": item,
        })
    return resources


def build_resource_catalog(
    kubernetes_payload: dict[str, Any],
    infrastructure_payload: dict[str, Any],
    *,
    resource_type: str = "all",
    cluster: str = "all",
    namespace: str = "all",
    limit: int = 500,
    cursor: str = "",
) -> dict[str, Any]:
    """返回稳定、分页、可筛选的统一资源契约。"""
    resources = _normalize_kubernetes(kubernetes_payload) + _normalize_infrastructure(infrastructure_payload)
    if resource_type not in {"", "all", "*"}:
        resources = [item for item in resources if item["resource_type"] == resource_type or item["kind"].lower() == resource_type.lower()]
    if cluster not in {"", "all", "*"}:
        resources = [item for item in resources if cluster in {item["cluster"], item["cluster_id"]}]
    if namespace not in {"", "all", "*"}:
        resources = [item for item in resources if item["namespace"] == namespace]
    resources.sort(key=lambda item: (item["source"], item["cluster"], item["namespace"], item["resource_type"], item["name"]))

    try:
        offset = max(0, int(cursor or 0))
    except (TypeError, ValueError):
        offset = 0
    bounded_limit = max(1, min(int(limit or 500), 2000))
    page = resources[offset:offset + bounded_limit]
    next_offset = offset + len(page)
    by_type = Counter(item["resource_type"] for item in resources)
    by_status = Counter(item["status"] for item in resources)
    by_source = Counter(item["source"] for item in resources)
    return {
        "status": "ok",
        "contract": "flawless.resource.v1",
        "items": page,
        "pagination": {
            "limit": bounded_limit,
            "cursor": str(offset),
            "next_cursor": str(next_offset) if next_offset < len(resources) else "",
            "returned": len(page),
            "total": len(resources),
        },
        "filters": {"resource_type": resource_type, "cluster": cluster, "namespace": namespace},
        "summary": {"total": len(resources), "by_type": dict(by_type), "by_status": dict(by_status), "by_source": dict(by_source)},
        "sources": {
            "kubernetes": kubernetes_payload.get("status") or "unknown",
            "infrastructure": infrastructure_payload.get("status") or "unknown",
        },
    }
