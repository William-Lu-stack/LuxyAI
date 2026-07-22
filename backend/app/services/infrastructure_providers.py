"""全栈基础设施资源适配器。

这里定义 Kubernetes 之外的资源入口：数据库、虚拟机、中间件、存储和云资源。
适配器只负责发现资源、采集低风险健康证据和生成标准化 finding；真实变更必须
继续经过 OpsJob、动作目录、外部受控执行器、人工确认和审计。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx


RESOURCE_TYPE_CATALOG: list[dict[str, Any]] = [
    {
        "id": "database",
        "name": "数据库",
        "description": "MySQL、PostgreSQL、Oracle、Redis、MongoDB、Elasticsearch 等数据服务。",
        "evidence": ["db_connectivity", "db_slow_queries", "db_locks", "db_replication", "db_capacity", "db_backup_status"],
        "typical_actions": ["db_restart_instance", "db_kill_session", "db_expand_storage", "db_failover", "db_apply_parameter"],
    },
    {
        "id": "virtual_machine",
        "name": "虚拟机 / 主机",
        "description": "VMware、OpenStack、Harvester、ECS、裸机 Linux/Windows 等计算节点。",
        "evidence": ["vm_agent_status", "vm_system_metrics", "vm_service_status", "vm_disk_usage", "vm_security_baseline"],
        "typical_actions": ["vm_restart_service", "vm_reboot", "vm_expand_disk", "vm_run_approved_script", "vm_snapshot"],
    },
    {
        "id": "middleware",
        "name": "中间件",
        "description": "Kafka、RabbitMQ、Nacos、Redis Cluster、ELK 等跨系统依赖。",
        "evidence": ["middleware_cluster_health", "middleware_lag", "middleware_topic_status", "middleware_client_errors"],
        "typical_actions": ["middleware_rebalance", "middleware_restart_broker", "middleware_expand_partition"],
    },
    {
        "id": "storage",
        "name": "企业存储",
        "description": "Generic CSI Storage、Virtualization Platform、NFS、SAN、NAS、Ceph 等存储后端。",
        "evidence": ["storage_pool_capacity", "storage_latency", "storage_snapshot", "storage_path_acl"],
        "typical_actions": ["storage_expand_volume", "storage_fix_acl", "storage_restore_snapshot"],
    },
    {
        "id": "cloud_service",
        "name": "云资源",
        "description": "阿里云、通用云、私有云和组织云服务资源。",
        "evidence": ["cloud_instance_status", "cloud_quota", "cloud_security_group", "cloud_billing_risk"],
        "typical_actions": ["cloud_scale_instance", "cloud_attach_disk", "cloud_adjust_security_group"],
    },
]


DEFAULT_THRESHOLDS = {
    "cpu_percent": 85,
    "memory_percent": 88,
    "disk_percent": 85,
    "connections_percent": 85,
    "replication_lag_seconds": 60,
    "slow_query_count": 20,
    "lock_wait_seconds": 30,
}

SECRET_KEYS = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key|credential)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: str, fallback: str) -> str:
    raw = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(value or fallback).strip()).strip("-")
    return raw[:96] or fallback


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return value.get("resources") or value.get("items") or value.get("targets") or []
    return []


def _json_env(name: str, default: str = "[]") -> Any:
    raw = os.getenv(name, default) or default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if SECRET_KEYS.search(str(key)):
                result[key] = "***"
            else:
                result[key] = redact_sensitive(item)
        return result
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(password|token|secret|api[_-]?key)=([^\\s&]+)", r"\\1=***", value)
        return value[:3000]
    return value


def _normalize_resource(raw: dict[str, Any], *, default_type: str) -> dict[str, Any]:
    rtype = str(raw.get("type") or raw.get("resource_type") or default_type).strip().lower()
    if rtype in {"db", "mysql", "postgres", "postgresql", "oracle", "redis", "mongodb", "elasticsearch"}:
        subtype = rtype if rtype != "db" else str(raw.get("engine") or raw.get("provider") or "database").lower()
        rtype = "database"
    elif rtype in {"vm", "host", "server", "ecs", "virtualmachine"}:
        subtype = str(raw.get("provider") or raw.get("os") or "vm").lower()
        rtype = "virtual_machine"
    else:
        subtype = str(raw.get("subtype") or raw.get("engine") or raw.get("provider") or rtype).strip().lower()
    host = str(raw.get("host") or raw.get("hostname") or "").strip()
    endpoint = str(raw.get("endpoint") or raw.get("url") or raw.get("dsn") or "").strip()
    if endpoint and not host:
        parsed = urlparse(endpoint if "://" in endpoint else f"tcp://{endpoint}")
        host = parsed.hostname or ""
    port = raw.get("port")
    if port is None and endpoint:
        parsed = urlparse(endpoint if "://" in endpoint else f"tcp://{endpoint}")
        port = parsed.port
    name = str(raw.get("name") or raw.get("display_name") or raw.get("id") or host or endpoint or f"{rtype}-resource").strip()
    resource_id = _safe_id(
        str(raw.get("id") or raw.get("resource_id") or name),
        f"{rtype}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:10]}",
    )
    return {
        "id": resource_id,
        "name": name,
        "type": rtype,
        "subtype": subtype or rtype,
        "provider": str(raw.get("provider") or subtype or rtype),
        "environment": str(raw.get("environment") or raw.get("env") or "unknown"),
        "cluster": str(raw.get("cluster") or raw.get("cluster_id") or raw.get("region") or "external"),
        "namespace": str(raw.get("namespace") or raw.get("project") or ""),
        "business_service": str(raw.get("business_service") or raw.get("service") or raw.get("app") or ""),
        "owner": str(raw.get("owner") or raw.get("team") or ""),
        "criticality": str(raw.get("criticality") or raw.get("tier") or "medium"),
        "host": host,
        "port": int(port) if str(port or "").isdigit() else None,
        "endpoint": endpoint,
        "health_url": str(raw.get("health_url") or raw.get("probe_url") or ""),
        "metrics": raw.get("metrics") or raw.get("current_metrics") or {},
        "thresholds": {**DEFAULT_THRESHOLDS, **(raw.get("thresholds") or {})},
        "cmdb_id": str(raw.get("cmdb_id") or raw.get("asset_id") or ""),
        "tags": [str(item) for item in raw.get("tags") or []],
        "enabled": bool(raw.get("enabled", True)),
        "actions_enabled": bool(raw.get("actions_enabled", False)),
        "raw": redact_sensitive(raw),
    }


def load_resources() -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for item in _as_list(_json_env("INFRASTRUCTURE_RESOURCES_JSON", "[]")):
        if isinstance(item, dict):
            resources.append(_normalize_resource(item, default_type=str(item.get("type") or "external")))
    for item in _as_list(_json_env("DATABASE_TARGETS_JSON", "[]")):
        if isinstance(item, dict):
            resources.append(_normalize_resource(item, default_type="database"))
    for item in _as_list(_json_env("VM_TARGETS_JSON", "[]")):
        if isinstance(item, dict):
            resources.append(_normalize_resource(item, default_type="virtual_machine"))
    for item in _as_list(_json_env("MIDDLEWARE_TARGETS_JSON", "[]")):
        if isinstance(item, dict):
            resources.append(_normalize_resource(item, default_type="middleware"))
    for item in _as_list(_json_env("STORAGE_TARGETS_JSON", "[]")):
        if isinstance(item, dict):
            resources.append(_normalize_resource(item, default_type="storage"))

    dedup: dict[str, dict[str, Any]] = {}
    for item in resources:
        if item.get("enabled", True):
            dedup[item["id"]] = item
    return sorted(dedup.values(), key=lambda item: (item["type"], item["cluster"], item["name"]))


def providers_payload() -> dict[str, Any]:
    resources = load_resources()
    counts: dict[str, int] = {}
    for item in resources:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    return {
        "status": "ok",
        "catalog": deepcopy(RESOURCE_TYPE_CATALOG),
        "resources": [redact_sensitive(item) for item in resources],
        "summary": {
            "total": len(resources),
            "by_type": counts,
            "configured": bool(resources),
            "action_webhook_configured": bool(os.getenv("INFRASTRUCTURE_ACTION_WEBHOOK_URL", "").strip()),
        },
        "configuration": {
            "unified": "INFRASTRUCTURE_RESOURCES_JSON",
            "database": "DATABASE_TARGETS_JSON",
            "virtual_machine": "VM_TARGETS_JSON",
            "action_webhook": "INFRASTRUCTURE_ACTION_WEBHOOK_URL",
        },
    }


async def _tcp_probe(host: str, port: int | None, timeout: float = 2.5) -> dict[str, Any]:
    if not host or not port:
        return {"status": "skipped", "message": "host/port 未配置"}
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port), family=socket.AF_UNSPEC), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return {"status": "ok", "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


async def _http_probe(url: str, timeout: float = 4.0) -> dict[str, Any]:
    if not url:
        return {"status": "skipped", "message": "health_url 未配置"}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=os.getenv("INFRASTRUCTURE_VERIFY_SSL", "true").lower() in {"1", "true", "yes", "on"}) as client:
            response = await client.get(url)
        return {
            "status": "ok" if response.status_code < 500 else "failed",
            "http_status": response.status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _metric_findings(resource: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = resource.get("metrics") or {}
    thresholds = resource.get("thresholds") or DEFAULT_THRESHOLDS
    findings = []

    def add_if(metric: str, category: str, title: str, unit: str = "%"):
        value = metrics.get(metric)
        threshold = thresholds.get(metric)
        if value is None or threshold is None:
            return
        try:
            if float(value) >= float(threshold):
                findings.append({
                    "category": category,
                    "severity": "P1" if float(value) >= float(threshold) * 1.15 else "P2",
                    "title": title,
                    "summary": f"{resource['name']} {title}：{value}{unit}，阈值 {threshold}{unit}",
                    "evidence": {"metric": metric, "value": value, "threshold": threshold},
                })
        except (TypeError, ValueError):
            return

    for metric in ("cpu_percent", "memory_percent", "disk_percent"):
        add_if(metric, f"{resource['type']}_capacity", {"cpu_percent": "CPU 使用率过高", "memory_percent": "内存使用率过高", "disk_percent": "磁盘使用率过高"}[metric])
    add_if("connections_percent", "database_connection", "数据库连接池接近耗尽")
    add_if("replication_lag_seconds", "database_replication", "数据库复制延迟过高", "s")
    add_if("slow_query_count", "database_slow_query", "慢 SQL 数量异常", "")
    add_if("lock_wait_seconds", "database_lock", "锁等待时间过长", "s")
    return findings


def _default_steps(resource: dict[str, Any], finding: dict[str, Any]) -> list[dict[str, Any]]:
    if resource["type"] == "database":
        return [
            {"title": "读取数据库连接与实例状态", "description": "检查连接数、活跃会话、复制角色、只读状态、错误日志和最近变更。", "probe": "db_connectivity"},
            {"title": "定位数据库热点", "description": "分析慢 SQL、锁等待、长事务、复制延迟、表空间和磁盘使用率。", "probe": "db_runtime_evidence"},
            {"title": "评估业务影响和回滚条件", "description": "结合 CMDB 调用关系确认受影响应用、读写路径、主从切换风险和恢复判据。", "probe": "dependency_topology"},
        ]
    if resource["type"] == "virtual_machine":
        return [
            {"title": "读取主机健康证据", "description": "检查 Agent、CPU、内存、磁盘、IO、网络、系统日志和关键服务状态。", "probe": "vm_system_metrics"},
            {"title": "定位进程与系统层根因", "description": "确认是否为服务异常、磁盘满、文件句柄耗尽、内核错误、网络不可达或安全基线变更。", "probe": "vm_runtime_evidence"},
            {"title": "选择最小修复动作", "description": "优先选择重启故障服务、扩容磁盘或恢复配置；整机重启必须作为高风险动作二次确认。", "probe": "vm_change_guard"},
        ]
    return [
        {"title": "读取资源健康证据", "description": "检查状态、指标、日志、依赖和最近变更。", "probe": "infra_health"},
        {"title": "判断影响范围", "description": "结合 CMDB、业务服务和上下游依赖评估爆炸半径。", "probe": "dependency_topology"},
        {"title": "生成受控修复计划", "description": "只选择动作目录中可审计、可回滚、可验证的动作。", "probe": "infra_action_guard"},
    ]


def _candidate_changes(resource: dict[str, Any], finding: dict[str, Any]) -> list[dict[str, Any]]:
    category = str(finding.get("category") or "")
    rtype = resource["type"]
    base = {
        "resource_id": resource["id"],
        "resource_type": rtype,
        "resource_name": resource["name"],
        "provider": resource.get("provider"),
        "requires_external_executor": True,
        "reason": finding.get("summary") or finding.get("title"),
    }
    if rtype == "database":
        if "capacity" in category or "disk" in str(finding.get("title", "")).lower():
            return [{**base, "type": "db_expand_storage", "risk": "high", "rollback": "存储扩容通常不可逆，执行前必须确认备份和容量策略。"}]
        if "connection" in category:
            return [{**base, "type": "db_kill_session", "risk": "high", "rollback": "重新建立被终止会话；执行前必须确认会话来源和 SQL。"}]
        if "replication" in category:
            return [{**base, "type": "db_failover", "risk": "high", "rollback": "按数据库 HA 预案回切或重新同步。"}]
        if "slow_query" in category or "lock" in category:
            return [{**base, "type": "db_apply_parameter", "risk": "high", "rollback": "恢复参数快照或回滚变更窗口。"}]
        return [{**base, "type": "db_restart_instance", "risk": "high", "rollback": "按数据库启动前快照和 HA 预案恢复。"}]
    if rtype == "virtual_machine":
        if "disk" in str(finding.get("summary", "")).lower() or category.endswith("capacity"):
            return [{**base, "type": "vm_expand_disk", "risk": "high", "rollback": "磁盘扩容通常不可逆，执行前确认快照和文件系统扩容步骤。"}]
        if "connectivity" in category:
            return [{**base, "type": "vm_reboot", "risk": "high", "rollback": "无法原地回滚，执行前必须确认业务冗余和窗口。"}]
        return [{**base, "type": "vm_restart_service", "risk": "medium", "rollback": "恢复服务原启动参数或回退配置。"}]
    return [{**base, "type": "infra_run_approved_action", "risk": "high", "rollback": "按外部执行器返回的回滚计划处理。"}]


async def scan_resources(resource_type: str = "all", resource_id: str = "", *, include_probe: bool = True) -> dict[str, Any]:
    resources = [
        item for item in load_resources()
        if resource_type in {"", "all", "*"} or item["type"] == resource_type
    ]
    if resource_id:
        resources = [item for item in resources if item["id"] == resource_id]
    findings: list[dict[str, Any]] = []
    for resource in resources:
        probes: dict[str, Any] = {}
        if include_probe:
            probes["tcp"] = await _tcp_probe(resource.get("host", ""), resource.get("port"))
            probes["http"] = await _http_probe(resource.get("health_url", ""))
        probe_failed = any((p or {}).get("status") == "failed" for p in probes.values())
        if probe_failed:
            findings.append({
                "id": f"{resource['id']}:connectivity",
                "resource": redact_sensitive(resource),
                "resource_id": resource["id"],
                "resource_type": resource["type"],
                "category": f"{resource['type']}_connectivity",
                "severity": "P1" if resource.get("criticality") in {"high", "core", "p0", "p1"} else "P2",
                "title": f"{resource['name']} 连通性异常",
                "summary": f"{resource['name']} 健康探测失败，需要确认网络、实例状态、Agent 或访问凭据。",
                "evidence": {"probes": redact_sensitive(probes), "resource": redact_sensitive(resource)},
            })
        for metric_finding in _metric_findings(resource):
            metric_finding.update({
                "id": f"{resource['id']}:{metric_finding['category']}",
                "resource": redact_sensitive(resource),
                "resource_id": resource["id"],
                "resource_type": resource["type"],
                "evidence": {**metric_finding.get("evidence", {}), "probes": redact_sensitive(probes), "resource": redact_sensitive(resource)},
            })
            findings.append(metric_finding)

    for finding in findings:
        resource = next((item for item in resources if item["id"] == finding.get("resource_id")), finding.get("resource") or {})
        finding["ops_plan"] = {
            "id": f"infra-plan-{finding['id'].replace(':', '-')}",
            "title": f"AI SRE 预演：{finding['title']}",
            "summary": finding["summary"],
            "source": "infrastructure",
            "resource_type": finding.get("resource_type"),
            "resource_id": finding.get("resource_id"),
            "target": f"{finding.get('resource_type')}/{finding.get('resource_id')}",
            "cluster": resource.get("cluster", "external"),
            "namespace": resource.get("namespace", ""),
            "evidence": finding.get("evidence") or {},
            "steps": _default_steps(resource, finding),
            "changes": _candidate_changes(resource, finding),
            "requires_confirmation": True,
            "requires_high_risk_confirmation": True,
            "success_criteria": ["infra_probe_healthy", "business_probe_ok", "error_rate_recovered"],
            "risk_note": "数据库/虚拟机变更必须通过外部受控执行器或企业批准脚本提交，平台不会把 LLM 输出当作任意命令执行。",
        }
    return {
        "status": "ok",
        "timestamp": _now(),
        "resource_count": len(resources),
        "finding_count": len(findings),
        "resources": [redact_sensitive(item) for item in resources],
        "findings": findings,
        "summary": {
            "total": len(findings),
            "p1": sum(1 for item in findings if item.get("severity") == "P1"),
            "p2": sum(1 for item in findings if item.get("severity") == "P2"),
            "configured": bool(resources),
        },
    }
