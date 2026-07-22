"""Normalize eBPF/Hubble/Cilium network-flow payloads.

Flawless does not run a privileged packet-capture container by default. In
production, the platform consumes flow events emitted by an approved eBPF
observer such as Cilium Hubble or an enterprise flow collector, then converts
them into the same contract used by topology and external data-flow APIs.
"""

from __future__ import annotations

import hashlib
from typing import Any


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _first_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    for item in _as_list(value):
        if isinstance(item, dict):
            return item
    return {}


def _get(obj: dict, *path: str, default: Any = "") -> Any:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def _hash_id(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]


def _short_kind(kind: str) -> str:
    lower = str(kind or "").strip().lower()
    if lower.endswith("s"):
        lower = lower[:-1]
    return {
        "deployment": "Deployment",
        "statefulset": "StatefulSet",
        "daemonset": "DaemonSet",
        "replicaset": "ReplicaSet",
        "pod": "Pod",
        "service": "Service",
        "job": "Job",
        "cronjob": "CronJob",
        "external": "External",
    }.get(lower, kind or "Workload")


def _source_label(source_system: str) -> str:
    lowered = str(source_system or "").lower()
    if "beyla" in lowered:
        return "ebpf_beyla"
    if any(marker in lowered for marker in ("calico", "goldmane", "tigera")):
        return "ebpf_calico"
    if "canal" in lowered:
        return "ebpf_canal"
    if "flannel" in lowered:
        return "ebpf_flannel"
    if any(marker in lowered for marker in ("hubble", "cilium")):
        return "ebpf_hubble"
    if "ebpf" in lowered:
        return "ebpf_generic"
    return source_system or "observed_flow"


def _pick_ip(endpoint: dict, flow: dict, side: str) -> str:
    for key in ("ip", "pod_ip", "podIP", "host_ip", "hostIP"):
        if endpoint.get(key):
            return str(endpoint.get(key))
    ip = _get(flow, "IP", "source" if side == "source" else "destination", default="")
    return str(ip or "")


def _workload_from_hubble(endpoint: dict) -> dict:
    workload = _first_dict(endpoint.get("workloads"))
    kind = workload.get("kind") or endpoint.get("workload_kind") or endpoint.get("kind") or ""
    name = workload.get("name") or endpoint.get("workload_name") or endpoint.get("name") or ""
    if not name and endpoint.get("pod_name"):
        kind, name = "Pod", endpoint.get("pod_name")
    return {"kind": _short_kind(str(kind or "Pod")), "name": str(name or "")}


def _endpoint_from_hubble(endpoint: dict, flow: dict, side: str, *, cluster_hint: str, default_namespace: str) -> dict:
    workload = _workload_from_hubble(endpoint)
    pod_name = str(endpoint.get("pod_name") or endpoint.get("podName") or endpoint.get("pod") or "")
    namespace = str(endpoint.get("namespace") or endpoint.get("ns") or default_namespace or "")
    cluster = str(
        endpoint.get("cluster_name")
        or endpoint.get("cluster")
        or endpoint.get("cluster_id")
        or cluster_hint
        or ""
    )
    ip = _pick_ip(endpoint, flow, side)
    name = workload.get("name") or pod_name or ip
    is_internal = bool(namespace or pod_name or workload.get("name") or endpoint.get("identity"))
    kind = workload.get("kind") if is_internal else "External"
    return {
        "cluster": cluster,
        "cluster_id": str(endpoint.get("cluster_id") or cluster),
        "namespace": namespace,
        "kind": kind,
        "name": name or "external",
        "pod": pod_name,
        "ip": ip,
        "type": "kubernetes" if is_internal else "external_ip",
        "id": (
            f"{cluster or cluster_hint}:{namespace}:{kind}/{name}"
            if is_internal
            else f"external:{ip or name}"
        ),
        "internal": is_internal,
    }


def _endpoint_from_generic(raw: dict, side: str, *, cluster_hint: str, default_namespace: str) -> dict:
    endpoint = raw.get(side) if isinstance(raw.get(side), dict) else {}
    aliases = {
        "source": ("source", "src", "s"),
        "destination": ("destination", "dest", "dst", "d"),
    }.get(side, (side,))

    def pick(*suffixes: str) -> Any:
        for key in suffixes:
            if endpoint.get(key) not in (None, ""):
                return endpoint.get(key)
        for prefix in aliases:
            for key in suffixes:
                for candidate in (f"{prefix}_{key}", f"{prefix}{key[:1].upper()}{key[1:]}", f"{prefix}.{key}"):
                    if raw.get(candidate) not in (None, ""):
                        return raw.get(candidate)
        return ""

    namespace = str(pick("namespace", "ns") or raw.get("namespace") or default_namespace or "")
    cluster = str(pick("cluster", "cluster_id", "cluster_name") or raw.get("cluster") or cluster_hint or "")
    kind = _short_kind(str(pick("kind", "workload_kind", "type") or ("Workload" if namespace else "External")))
    pod = str(pick("pod", "pod_name", "podName") or "")
    name = str(
        pick("name", "workload", "workload_name", "service", "app")
        or pod
        or endpoint.get("address")
        or pick("ip", "ip_addr", "ipAddress")
        or ""
    )
    ip = str(pick("ip", "ip_addr", "ipAddress", "address") or "")
    is_internal = kind != "External" and bool(namespace or pod or pick("kind", "workload_kind") or endpoint.get("internal"))
    return {
        "cluster": cluster,
        "cluster_id": str(endpoint.get("cluster_id") or cluster),
        "namespace": namespace,
        "kind": kind if is_internal else "External",
        "name": name or ip or "external",
        "pod": pod,
        "ip": ip,
        "type": endpoint.get("type") or ("kubernetes" if is_internal else "external_ip"),
        "id": str(endpoint.get("id") or pick("id") or (f"{cluster}:{namespace}:{kind}/{name}" if is_internal else f"external:{ip or name}")),
        "internal": is_internal,
    }


def _port_protocol(flow: dict, raw: dict) -> tuple[int | None, str]:
    destination = raw.get("destination") if isinstance(raw.get("destination"), dict) else {}
    dest = raw.get("dest") if isinstance(raw.get("dest"), dict) else {}
    dst = raw.get("dst") if isinstance(raw.get("dst"), dict) else {}
    protocol = str(raw.get("protocol") or raw.get("proto") or raw.get("ip_protocol") or raw.get("network_protocol") or "")
    port = (
        raw.get("port")
        or raw.get("destination_port")
        or raw.get("dest_port")
        or raw.get("dst_port")
        or raw.get("dport")
        or destination.get("port")
        or dest.get("port")
        or dst.get("port")
    )
    l4 = flow.get("l4") if isinstance(flow.get("l4"), dict) else {}
    if isinstance(l4.get("TCP"), dict):
        protocol = protocol or "tcp"
        port = port or l4["TCP"].get("destination_port") or l4["TCP"].get("source_port")
    elif isinstance(l4.get("UDP"), dict):
        protocol = protocol or "udp"
        port = port or l4["UDP"].get("destination_port") or l4["UDP"].get("source_port")
    elif isinstance(flow.get("l7"), dict):
        protocol = protocol or "l7"
    try:
        parsed_port = int(port) if port not in (None, "") else None
    except Exception:
        parsed_port = None
    return parsed_port, protocol or "unknown"


def _infer_direction(source: dict, destination: dict) -> str:
    src_internal = bool(source.get("internal"))
    dst_internal = bool(destination.get("internal"))
    src_cluster = str(source.get("cluster") or source.get("cluster_id") or "")
    dst_cluster = str(destination.get("cluster") or destination.get("cluster_id") or "")
    if src_internal and dst_internal:
        if src_cluster and dst_cluster and src_cluster != dst_cluster:
            return "cross_cluster"
        return "internal"
    if src_internal and not dst_internal:
        return "egress"
    if not src_internal and dst_internal:
        return "ingress"
    return "external"


def _iter_flow_items(raw: Any) -> list[dict]:
    if isinstance(raw, list):
        return [item.get("_source") if isinstance(item, dict) and isinstance(item.get("_source"), dict) else item for item in raw if isinstance(item, dict)]
    if not isinstance(raw, dict):
        return []
    hits = raw.get("hits")
    if isinstance(hits, dict) and isinstance(hits.get("hits"), list):
        return _iter_flow_items(hits.get("hits"))
    for key in ("flows", "items", "data", "events", "result"):
        value = raw.get(key)
        if isinstance(value, list):
            return _iter_flow_items(value)
        if isinstance(value, dict):
            nested = _iter_flow_items(value)
            if nested:
                return nested
    if isinstance(raw.get("flow"), dict) or isinstance(raw.get("source"), dict) or isinstance(raw.get("destination"), dict):
        return [raw]
    return []


def _looks_like_hubble_flow(flow: dict) -> bool:
    if not isinstance(flow, dict):
        return False
    if any(key in flow for key in ("IP", "l4", "l7", "verdict", "event_type", "node_name", "Summary")):
        return True
    for side in ("source", "destination"):
        endpoint = flow.get(side)
        if isinstance(endpoint, dict) and any(key in endpoint for key in ("pod_name", "workloads", "identity", "labels", "cluster_name")):
            return True
    return False


def normalize_observed_flow_payload(
    raw: Any,
    *,
    source_system: str = "observed_flow",
    cluster_hint: str = "",
    default_namespace: str = "",
) -> list[dict]:
    """Return flow dictionaries compatible with external_traffic.py.

    Supported inputs:
    - Cilium Hubble JSON events: {"flow": {"source": ..., "destination": ...}}
    - Hubble UI/Relay batches: {"flows": [...]}, {"data": [...]}
    - Calico/Goldmane/Flow Log style records with src_* and dst_* fields.
    - flannel/canal clusters through a generic enterprise eBPF collector.
    - Generic normalized flow arrays already containing source/destination maps.
    """
    flows: list[dict] = []
    label = _source_label(source_system)
    for raw_item in _iter_flow_items(raw):
        flow = raw_item.get("flow") if isinstance(raw_item.get("flow"), dict) else raw_item
        if not isinstance(flow, dict):
            continue
        if _looks_like_hubble_flow(flow):
            source = _endpoint_from_hubble(flow.get("source") or {}, flow, "source", cluster_hint=cluster_hint, default_namespace=default_namespace)
            destination = _endpoint_from_hubble(flow.get("destination") or {}, flow, "destination", cluster_hint=cluster_hint, default_namespace=default_namespace)
        else:
            source = _endpoint_from_generic(raw_item, "source", cluster_hint=cluster_hint, default_namespace=default_namespace)
            destination = _endpoint_from_generic(raw_item, "destination", cluster_hint=cluster_hint, default_namespace=default_namespace)
        if not (source.get("name") or source.get("ip")) or not (destination.get("name") or destination.get("ip")):
            continue
        port, protocol = _port_protocol(flow, raw_item)
        direction = str(raw_item.get("direction") or flow.get("direction") or _infer_direction(source, destination))
        destination_ref = {
            "type": "cross_cluster" if direction == "cross_cluster" else (destination.get("type") or "external_observed"),
            "kind": destination.get("kind"),
            "name": destination.get("name") or destination.get("ip") or "destination",
            "address": destination.get("ip") or destination.get("name") or "",
            "port": port,
            "protocol": protocol,
            "cluster": destination.get("cluster") or "",
            "cluster_id": destination.get("cluster_id") or destination.get("cluster") or "",
            "namespace": destination.get("namespace") or "",
            "id": destination.get("id"),
        }
        verdict = str(flow.get("verdict") or raw_item.get("verdict") or raw_item.get("action") or raw_item.get("policy_action") or "")
        summary = str(flow.get("Summary") or flow.get("summary") or raw_item.get("summary") or "")
        event_type = flow.get("event_type") or raw_item.get("event_type") or {}
        evidence = [
            item
            for item in [
                f"{label}: {source.get('namespace') or '-'}:{source.get('pod') or source.get('name')} -> {destination.get('namespace') or '-'}:{destination.get('pod') or destination.get('name')}",
                f"verdict={verdict}" if verdict else "",
                f"policy={raw_item.get('policy') or raw_item.get('policy_name')}" if raw_item.get("policy") or raw_item.get("policy_name") else "",
                f"event_type={event_type}" if event_type else "",
                summary,
            ]
            if item
        ]
        byte_count = (
            raw_item.get("bytes")
            or raw_item.get("bytes_total")
            or raw_item.get("num_bytes")
            or flow.get("bytes")
            or flow.get("bytes_total")
        )
        if byte_count in (None, "") and (raw_item.get("bytes_in") is not None or raw_item.get("bytes_out") is not None):
            try:
                byte_count = int(raw_item.get("bytes_in") or 0) + int(raw_item.get("bytes_out") or 0)
            except Exception:
                byte_count = None
        flows.append({
            "direction": direction,
            "source": {key: value for key, value in source.items() if key != "internal"},
            "destination": destination_ref,
            "protocol": protocol,
            "port": port,
            "bytes": byte_count,
            "rps": raw_item.get("rps") or raw_item.get("qps") or raw_item.get("flow_rate") or raw_item.get("num_flows"),
            "confidence": 0.98 if label == "ebpf_hubble" else float(raw_item.get("confidence") or 0.94),
            "source_system": label,
            "observed": True,
            "evidence": evidence or ["Observed by configured network-flow source"],
            "raw_ref": {
                "time": flow.get("time") or raw_item.get("time") or raw_item.get("timestamp"),
                "node": flow.get("node_name") or raw_item.get("node"),
                "uuid": flow.get("uuid") or raw_item.get("id") or _hash_id(source.get("id"), destination.get("id"), port, protocol),
            },
        })
    return flows
