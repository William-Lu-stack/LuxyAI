"""Cloud adapter registry.

Production deployments can implement each adapter with the relevant SDK. The
contract is intentionally stable: discover accounts/clusters, normalize topology,
and expose safety boundaries for remediation.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class CloudAdapterSpec:
    id: str
    provider: str
    display_name: str
    enabled: bool
    capabilities: list[str]
    auth_mode: str
    regions: list[str]
    inventory_scope: str
    safety_boundary: str


DEFAULT_ADAPTERS = [
    CloudAdapterSpec("rancher", "private-cloud", "Rancher Multi-Cluster", True, ["kubernetes", "topology", "remediation"], "token", ["local"], "all-clusters", "namespace/workload allowlist + human approval"),
    CloudAdapterSpec("generic-storage", "csi-storage", "Generic CSI Storage", False, ["storage", "csi", "metrics", "topology"], "secret-ref-or-service-account", ["example-region"], "storage-pool/cluster", "read-only discovery + explicit PVC/PV change approval"),
    CloudAdapterSpec("virtualization-platform", "virtualization-platform", "Virtualization Platform", False, ["hci", "virtualization", "network", "storage", "security", "topology"], "api-token-or-service-account", ["example-dc"], "tenant/resource-pool", "tenant-scoped token + manual approval for infrastructure changes"),
    CloudAdapterSpec("aliyun-ack", "aliyun", "Alibaba Cloud ACK", False, ["ack", "ecs", "slb", "arms", "sls", "cms"], "cloud-role-or-secret-ref", ["region-a", "region-b"], "resource-group", "RAM least privilege + change window"),
    CloudAdapterSpec("aws-eks", "aws", "Amazon EKS", False, ["eks", "ec2", "elb", "cloudwatch", "xray"], "iam-role", ["ap-southeast-1"], "account/region", "IAM role + SCP guardrails"),
    CloudAdapterSpec("azure-aks", "azure", "Azure AKS", False, ["aks", "vmss", "monitor", "app-insights"], "managed-identity", ["eastasia"], "subscription/resource-group", "Azure RBAC + policy"),
    CloudAdapterSpec("gcp-gke", "gcp", "Google GKE", False, ["gke", "gce", "cloud-monitoring", "cloud-trace"], "workload-identity", ["asia-east1"], "project/region", "IAM + org policy"),
    CloudAdapterSpec("openstack", "private-cloud", "OpenStack / VMware", False, ["compute", "network", "storage", "kubernetes"], "service-account", ["dc-1"], "tenant/project", "tenant-scoped account"),
]


def _bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def _custom_adapters() -> list[CloudAdapterSpec]:
    raw = os.getenv("CLOUD_ADAPTERS_JSON", "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    adapters = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        adapters.append(CloudAdapterSpec(
            id=str(item.get("id") or item.get("provider")),
            provider=str(item.get("provider") or "custom"),
            display_name=str(item.get("display_name") or item.get("name") or item.get("provider")),
            enabled=_bool(item.get("enabled"), True),
            capabilities=list(item.get("capabilities") or []),
            auth_mode=str(item.get("auth_mode") or "external-secret"),
            regions=list(item.get("regions") or []),
            inventory_scope=str(item.get("inventory_scope") or "account"),
            safety_boundary=str(item.get("safety_boundary") or "least privilege + approval"),
        ))
    return adapters


def cloud_adapters_payload() -> dict[str, Any]:
    adapters = {item.id: item for item in DEFAULT_ADAPTERS}
    for item in _custom_adapters():
        adapters[item.id] = item
    values = [asdict(item) for item in adapters.values()]
    return {
        "status": "ok",
        "enabled": [x for x in values if x["enabled"]],
        "available": values,
        "contract": {
            "discover": "accounts/projects/clusters/namespaces/workloads/cloud resources",
            "topology": "normalize resources into typed graph nodes and dependency edges",
            "observe": "metrics/logs/traces/events through provider-native observability APIs",
            "remediate": "guarded changes with dry-run, approval, audit and rollback metadata",
        },
    }
