"""External traffic and cross-cluster data-flow inference.

The Kubernetes API does not expose byte-level network flow by itself. This
service therefore merges three evidence classes into one stable contract:

- observed flows from eBPF/service-mesh/log systems when configured;
- CMDB relationships when one side is outside the selected cluster scope;
- Kubernetes static hints from Pod specs, Services, Endpoints and Ingresses.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import Any
from urllib.parse import urlparse


URL_RE = re.compile(
    r"(?:(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://)?"
    r"(?P<host>(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}|(?:\d{1,3}\.){3}\d{1,3})"
    r"(?::(?P<port>\d{2,5}))?"
)

SENSITIVE_KEY_RE = re.compile(r"(token|secret|password|passwd|authorization|cookie|api[_-]?key|client[_-]?secret)", re.I)

DEFAULT_INTERNAL_SUFFIXES = (
    ".svc",
    ".svc.cluster.local",
    ".cluster.local",
    ".local",
)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _meta(item: dict) -> dict:
    return item.get("metadata") or {}


def _spec(item: dict) -> dict:
    return item.get("spec") or {}


def _status(item: dict) -> dict:
    return item.get("status") or {}


def _name(item: dict) -> str:
    return str(_meta(item).get("name") or item.get("name") or "")


def _namespace(item: dict) -> str:
    return str(_meta(item).get("namespace") or item.get("namespace") or "default")


def _hash_id(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]


def _short_kind(kind: str) -> str:
    lower = str(kind or "").lower()
    if lower.endswith("s"):
        lower = lower[:-1]
    return {
        "deployment": "Deployment",
        "statefulset": "StatefulSet",
        "daemonset": "DaemonSet",
        "replicaset": "ReplicaSet",
        "pod": "Pod",
        "service": "Service",
        "ingress": "Ingress",
    }.get(lower, kind or "Workload")


def _workload_from_pod(pod: dict) -> dict:
    owners = _meta(pod).get("ownerReferences") or pod.get("owner_references") or []
    if not owners:
        return {"kind": "Pod", "name": _name(pod)}
    owner = owners[0] or {}
    kind = str(owner.get("kind") or "Pod")
    name = str(owner.get("name") or _name(pod))
    if kind == "ReplicaSet" and "-" in name:
        left, suffix = name.rsplit("-", 1)
        if len(suffix) >= 5:
            return {"kind": "Deployment", "name": left, "via": name}
    return {"kind": kind, "name": name}


def _workload_label(workload: dict) -> str:
    return f"{_short_kind(workload.get('kind'))}/{workload.get('name') or '-'}"


def _source_from_pod(cluster: dict, pod: dict) -> dict:
    workload = _workload_from_pod(pod)
    namespace = _namespace(pod)
    return {
        "cluster": cluster.get("name") or cluster.get("id") or "local-cluster",
        "cluster_id": cluster.get("id") or cluster.get("name") or "local-cluster",
        "namespace": namespace,
        "kind": _short_kind(workload.get("kind")),
        "name": workload.get("name") or _name(pod),
        "pod": _name(pod),
        "ip": (_status(pod) or {}).get("podIP") or "",
        "id": f"{cluster.get('id') or cluster.get('name')}:{namespace}:{_workload_label(workload)}",
    }


def _source_from_service(cluster: dict, service: dict) -> dict:
    namespace = _namespace(service)
    return {
        "cluster": cluster.get("name") or cluster.get("id") or "local-cluster",
        "cluster_id": cluster.get("id") or cluster.get("name") or "local-cluster",
        "namespace": namespace,
        "kind": "Service",
        "name": _name(service),
        "pod": "",
        "ip": _spec(service).get("clusterIP") or "",
        "id": f"{cluster.get('id') or cluster.get('name')}:{namespace}:Service/{_name(service)}",
    }


def _external_endpoint(host: str, *, port: str | int = "", protocol: str = "unknown", cluster_hint: str = "") -> dict:
    endpoint_type = "external_ip" if _is_ip(host) else "external_domain"
    if cluster_hint:
        endpoint_type = "cross_cluster"
    return {
        "type": endpoint_type,
        "name": host,
        "address": host,
        "port": int(port) if str(port or "").isdigit() else None,
        "protocol": protocol or "unknown",
        "cluster": cluster_hint,
    }


def _service_selector_matches(selector: dict, pod: dict) -> bool:
    labels = _meta(pod).get("labels") or pod.get("labels") or {}
    return bool(selector) and all(labels.get(key) == value for key, value in selector.items())


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(str(value))
        return True
    except Exception:
        return False


def _is_loopback_or_unspecified(host: str) -> bool:
    if not _is_ip(host):
        return host.lower() in {"localhost"}
    ip = ipaddress.ip_address(host)
    return ip.is_loopback or ip.is_unspecified or ip.is_multicast


def _internal_suffixes(options: dict | None = None) -> tuple[str, ...]:
    raw = (options or {}).get("internal_domains") or []
    suffixes = list(DEFAULT_INTERNAL_SUFFIXES)
    for item in raw:
        item = str(item or "").strip().lower()
        if item and item not in suffixes:
            suffixes.append(item if item.startswith(".") else f".{item}")
    return tuple(suffixes)


def _known_internal_addresses(resources: list[dict]) -> set[str]:
    known: set[str] = set()
    for scope in resources:
        for pod in _as_list(scope.get("pods")):
            for key in ("podIP", "hostIP"):
                value = (_status(pod) or {}).get(key)
                if value:
                    known.add(str(value))
        for service in _as_list(scope.get("services")):
            spec = _spec(service)
            for value in [spec.get("clusterIP"), *(_as_list(spec.get("clusterIPs")))]:
                if value and value != "None":
                    known.add(str(value))
            for ip in _as_list(spec.get("externalIPs")):
                # externalIPs are intentionally not internal.
                if ip in known:
                    known.remove(ip)
        for endpoint in _as_list(scope.get("endpoints")):
            for subset in _as_list(endpoint.get("subsets")):
                for address in [*_as_list(subset.get("addresses")), *_as_list(subset.get("notReadyAddresses"))]:
                    ip = address.get("ip")
                    if ip:
                        known.add(str(ip))
        for endpoint_slice in _as_list(scope.get("endpoint_slices")):
            for endpoint in _as_list(endpoint_slice.get("endpoints")):
                for address in _as_list(endpoint.get("addresses")):
                    if address:
                        known.add(str(address))
    return known


def _known_internal_hosts(resources: list[dict]) -> set[str]:
    hosts: set[str] = set()
    for scope in resources:
        cluster_domain = "cluster.local"
        for service in _as_list(scope.get("services")):
            namespace = _namespace(service)
            name = _name(service)
            if not name:
                continue
            hosts.update({
                name.lower(),
                f"{name}.{namespace}".lower(),
                f"{name}.{namespace}.svc".lower(),
                f"{name}.{namespace}.svc.{cluster_domain}".lower(),
            })
    return hosts


def _is_internal_host(host: str, internal_hosts: set[str], internal_addresses: set[str], suffixes: tuple[str, ...]) -> bool:
    normalized = str(host or "").strip().lower().rstrip(".")
    if not normalized:
        return True
    if _is_loopback_or_unspecified(normalized):
        return True
    if normalized in internal_hosts or normalized in internal_addresses:
        return True
    return any(normalized.endswith(suffix) for suffix in suffixes)


def _cluster_hint_for_host(host: str, known_clusters: list[dict], options: dict | None = None) -> str:
    normalized = str(host or "").lower()
    for item in (options or {}).get("cross_cluster_domains") or []:
        domain = str(item.get("domain") or item.get("suffix") or "").strip().lower()
        if domain and normalized.endswith(domain.lstrip("*")):
            return str(item.get("cluster") or item.get("cluster_id") or item.get("name") or "")
    for cluster in known_clusters:
        for key in ("id", "name"):
            value = str(cluster.get(key) or "").lower()
            if value and value in normalized:
                return str(cluster.get("name") or cluster.get("id") or "")
    return ""


def _redact_evidence(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"([?&](?:token|password|passwd|secret|api[_-]?key|client_secret)=)[^&\s]+", r"\1[REDACTED]", text, flags=re.I)
    cleaned = re.sub(r"(://[^:/\s]+:)[^@\s]+@", r"\1[REDACTED]@", cleaned)
    cleaned = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}", "Bearer [REDACTED]", cleaned, flags=re.I)
    return cleaned[:420]


def _extract_endpoints_from_text(text: str, *, context: str) -> list[dict]:
    result = []
    for match in URL_RE.finditer(str(text or "")):
        host = match.group("host") or ""
        if not host:
            continue
        scheme = (match.group("scheme") or "").lower()
        port = match.group("port") or ""
        protocol = scheme or ("tcp" if port else "unknown")
        result.append({
            "host": host.strip().lower().rstrip("."),
            "port": port,
            "protocol": protocol,
            "context": context,
            "evidence": _redact_evidence(match.group(0)),
        })
    return result


def _container_text_sources(container: dict) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    for field in ("image",):
        value = container.get(field)
        if value:
            sources.append((field, str(value)))
    for field in ("args", "command"):
        value = container.get(field)
        if isinstance(value, list):
            sources.extend((field, str(item)) for item in value)
        elif value:
            sources.append((field, str(value)))
    for env in _as_list(container.get("env")):
        name = str(env.get("name") or "")
        if "value" not in env:
            continue
        value = str(env.get("value") or "")
        if not value:
            continue
        label = f"env:{name or '-'}"
        evidence_value = "[REDACTED]" if SENSITIVE_KEY_RE.search(name) else value
        sources.append((label, evidence_value))
    return sources


def _add_flow(flows: dict[str, dict], flow: dict) -> None:
    src = flow.get("source") or {}
    dst = flow.get("destination") or {}
    flow_id = flow.get("id") or _hash_id(
        flow.get("direction"),
        src.get("cluster_id") or src.get("cluster"),
        src.get("namespace"),
        src.get("kind"),
        src.get("name"),
        dst.get("address") or dst.get("name"),
        dst.get("port"),
        flow.get("source_system"),
    )
    existing = flows.get(flow_id)
    if existing:
        existing["confidence"] = max(float(existing.get("confidence") or 0), float(flow.get("confidence") or 0))
        existing["evidence"] = sorted(set(_as_list(existing.get("evidence")) + _as_list(flow.get("evidence"))))[:8]
        return
    flow["id"] = flow_id
    flow["evidence"] = _as_list(flow.get("evidence"))[:8]
    flows[flow_id] = flow


def _service_to_workloads(cluster: dict, services: list[dict], pods: list[dict]) -> dict[str, list[dict]]:
    mapping: dict[str, list[dict]] = {}
    for service in services:
        selector = _spec(service).get("selector") or {}
        if not selector:
            continue
        key = f"{_namespace(service)}/{_name(service)}"
        seen = set()
        for pod in pods:
            if _namespace(pod) != _namespace(service) or not _service_selector_matches(selector, pod):
                continue
            source = _source_from_pod(cluster, pod)
            source_key = f"{source['kind']}/{source['name']}"
            if source_key in seen:
                continue
            seen.add(source_key)
            mapping.setdefault(key, []).append(source)
    return mapping


def _service_ports(service: dict) -> list[dict]:
    ports = []
    for port in _as_list(_spec(service).get("ports")):
        ports.append({
            "name": port.get("name") or "",
            "port": port.get("port"),
            "target_port": port.get("targetPort"),
            "protocol": str(port.get("protocol") or "tcp").lower(),
            "node_port": port.get("nodePort"),
        })
    return ports


def _flow_direction(source: dict, destination: dict) -> str:
    if destination.get("type") == "cross_cluster":
        return "cross_cluster"
    if source.get("kind") == "External":
        return "ingress"
    return "egress"


def _endpoint_matches_workload(endpoint: dict, workload_filter: str) -> bool:
    wanted = str(workload_filter or "").strip().lower()
    if not wanted:
        return True
    values = {
        str(endpoint.get("name") or ""),
        str(endpoint.get("pod") or ""),
        str(endpoint.get("id") or ""),
        str(endpoint.get("title") or ""),
        f"{endpoint.get('kind') or ''}/{endpoint.get('name') or ''}",
    }
    normalized = {value.strip().lower() for value in values if value}
    wanted_tail = wanted.split("/", 1)[-1] if "/" in wanted else wanted
    return any(
        wanted == value
        or wanted_tail == value
        or wanted in value
        or wanted_tail in value
        for value in normalized
    )


def _scan_pod_spec_flows(
    flows: dict[str, dict],
    *,
    cluster: dict,
    pods: list[dict],
    internal_hosts: set[str],
    internal_addresses: set[str],
    suffixes: tuple[str, ...],
    known_clusters: list[dict],
    scope: dict,
    options: dict,
) -> None:
    workload_filter = str(scope.get("workload") or "").strip()
    for pod in pods:
        source = _source_from_pod(cluster, pod)
        workload_label = f"{source['kind']}/{source['name']}"
        if workload_filter and workload_filter not in {source["name"], workload_label, source.get("pod")}:
            continue
        containers = _spec(pod).get("containers") or []
        for container in containers:
            cname = container.get("name") or "container"
            for label, text in _container_text_sources(container):
                for endpoint in _extract_endpoints_from_text(text, context=label):
                    host = endpoint["host"]
                    if _is_internal_host(host, internal_hosts, internal_addresses, suffixes):
                        continue
                    cluster_hint = _cluster_hint_for_host(host, known_clusters, options)
                    destination = _external_endpoint(
                        host,
                        port=endpoint.get("port") or "",
                        protocol=endpoint.get("protocol") or "unknown",
                        cluster_hint=cluster_hint,
                    )
                    _add_flow(flows, {
                        "direction": _flow_direction(source, destination),
                        "source": source,
                        "destination": destination,
                        "protocol": destination["protocol"],
                        "port": destination.get("port"),
                        "bytes": None,
                        "rps": None,
                        "confidence": 0.62 if label.startswith("env:") else 0.48,
                        "source_system": "k8s_static_inference",
                        "observed": False,
                        "evidence": [f"Pod spec {source.get('pod')} container/{cname} {label} -> {endpoint.get('evidence') or host}"],
                    })


def _scan_service_flows(
    flows: dict[str, dict],
    *,
    cluster: dict,
    services: list[dict],
    endpoints: list[dict],
    endpoint_slices: list[dict],
    service_workloads: dict[str, list[dict]],
    internal_hosts: set[str],
    internal_addresses: set[str],
    suffixes: tuple[str, ...],
    known_clusters: list[dict],
    scope: dict,
    options: dict,
) -> None:
    workload_filter = str(scope.get("workload") or "").strip()
    endpoint_by_service: dict[str, set[str]] = {}
    for endpoint in endpoints:
        key = f"{_namespace(endpoint)}/{_name(endpoint)}"
        for subset in _as_list(endpoint.get("subsets")):
            for address in [*_as_list(subset.get("addresses")), *_as_list(subset.get("notReadyAddresses"))]:
                if address.get("ip"):
                    endpoint_by_service.setdefault(key, set()).add(str(address["ip"]))
    for endpoint_slice in endpoint_slices:
        labels = _meta(endpoint_slice).get("labels") or {}
        service_name = labels.get("kubernetes.io/service-name")
        if not service_name:
            continue
        key = f"{_namespace(endpoint_slice)}/{service_name}"
        for endpoint in _as_list(endpoint_slice.get("endpoints")):
            for address in _as_list(endpoint.get("addresses")):
                endpoint_by_service.setdefault(key, set()).add(str(address))

    for service in services:
        spec = _spec(service)
        service_key = f"{_namespace(service)}/{_name(service)}"
        service_source = _source_from_service(cluster, service)
        target_sources = service_workloads.get(service_key) or [service_source]
        if workload_filter:
            filtered_sources = [source for source in target_sources if _endpoint_matches_workload(source, workload_filter)]
            if filtered_sources:
                target_sources = filtered_sources
            elif not _endpoint_matches_workload(service_source, workload_filter):
                continue
        ports = _service_ports(service)
        service_type = str(spec.get("type") or "ClusterIP")

        if service_type == "ExternalName" and spec.get("externalName"):
            host = str(spec.get("externalName")).lower().rstrip(".")
            if not _is_internal_host(host, internal_hosts, internal_addresses, suffixes):
                cluster_hint = _cluster_hint_for_host(host, known_clusters, options)
                destination = _external_endpoint(host, protocol="dns", cluster_hint=cluster_hint)
                for source in target_sources:
                    _add_flow(flows, {
                        "direction": _flow_direction(source, destination),
                        "source": source,
                        "destination": destination,
                        "protocol": "dns",
                        "port": None,
                        "bytes": None,
                        "rps": None,
                        "confidence": 0.86,
                        "source_system": "k8s_service",
                        "observed": False,
                        "evidence": [f"Service/{_name(service)} ExternalName -> {host}"],
                    })

        for external_ip in _as_list(spec.get("externalIPs")):
            if not external_ip:
                continue
            for port in ports or [{}]:
                _add_flow(flows, {
                    "direction": "ingress",
                    "source": {"kind": "External", "name": str(external_ip), "address": str(external_ip), "id": f"external:{external_ip}"},
                    "destination": service_source,
                    "protocol": port.get("protocol") or "tcp",
                    "port": port.get("port"),
                    "bytes": None,
                    "rps": None,
                    "confidence": 0.88,
                    "source_system": "k8s_service",
                    "observed": False,
                    "evidence": [f"Service/{_name(service)} externalIPs includes {external_ip}"],
                })

        lb_ingress = _as_list((_status(service).get("loadBalancer") or {}).get("ingress"))
        if service_type in {"LoadBalancer", "NodePort"} or lb_ingress:
            incoming = [
                str(item.get("hostname") or item.get("ip") or "")
                for item in lb_ingress
                if item.get("hostname") or item.get("ip")
            ] or (["private-network"] if service_type == "NodePort" else [])
            for incoming_host in incoming:
                for port in ports or [{}]:
                    _add_flow(flows, {
                        "direction": "ingress",
                        "source": {"kind": "External", "name": incoming_host, "address": incoming_host, "id": f"external:{incoming_host}"},
                        "destination": service_source,
                        "protocol": port.get("protocol") or "tcp",
                        "port": port.get("port") or port.get("node_port"),
                        "bytes": None,
                        "rps": None,
                        "confidence": 0.82,
                        "source_system": "k8s_service",
                        "observed": False,
                        "evidence": [f"Service/{_name(service)} type={service_type} exposes external traffic"],
                    })

        for address in sorted(endpoint_by_service.get(service_key, set())):
            if _is_internal_host(address, internal_hosts, internal_addresses, suffixes):
                continue
            cluster_hint = _cluster_hint_for_host(address, known_clusters, options)
            destination = _external_endpoint(address, protocol="tcp", cluster_hint=cluster_hint)
            for source in target_sources:
                _add_flow(flows, {
                    "direction": _flow_direction(source, destination),
                    "source": source,
                    "destination": destination,
                    "protocol": "tcp",
                    "port": ports[0].get("port") if ports else None,
                    "bytes": None,
                    "rps": None,
                    "confidence": 0.74,
                    "source_system": "k8s_endpoints",
                    "observed": False,
                    "evidence": [f"Service/{_name(service)} Endpoints/EndpointSlice points to non-cluster address {address}"],
                })


def _scan_ingress_flows(
    flows: dict[str, dict],
    *,
    cluster: dict,
    ingresses: list[dict],
    services: list[dict],
    service_workloads: dict[str, list[dict]],
    internal_hosts: set[str],
    internal_addresses: set[str],
    suffixes: tuple[str, ...],
    scope: dict,
) -> None:
    workload_filter = str(scope.get("workload") or "").strip()
    services_by_key = {f"{_namespace(service)}/{_name(service)}": service for service in services}
    for ingress in ingresses:
        namespace = _namespace(ingress)
        spec = _spec(ingress)
        backend_services: set[str] = set()
        hosts: list[str] = []
        for rule in _as_list(spec.get("rules")):
            host = str(rule.get("host") or "").lower().rstrip(".")
            if host:
                hosts.append(host)
            for path in _as_list(((rule.get("http") or {}).get("paths"))):
                service_name = (((path.get("backend") or {}).get("service") or {}).get("name"))
                if service_name:
                    backend_services.add(str(service_name))
        default_service = (((spec.get("defaultBackend") or {}).get("service") or {}).get("name"))
        if default_service:
            backend_services.add(str(default_service))
        external_hosts = [host for host in hosts if not _is_internal_host(host, internal_hosts, internal_addresses, suffixes)]
        for service_name in backend_services:
            service = services_by_key.get(f"{namespace}/{service_name}")
            destination = _source_from_service(cluster, service) if service else {
                "cluster": cluster.get("name") or cluster.get("id"),
                "cluster_id": cluster.get("id") or cluster.get("name"),
                "namespace": namespace,
                "kind": "Service",
                "name": service_name,
                "id": f"{cluster.get('id') or cluster.get('name')}:{namespace}:Service/{service_name}",
            }
            workload_targets = service_workloads.get(f"{namespace}/{service_name}") or []
            if workload_filter:
                workload_targets = [target for target in workload_targets if _endpoint_matches_workload(target, workload_filter)]
                if not workload_targets and not _endpoint_matches_workload(destination, workload_filter):
                    continue
            for host in external_hosts or ["external-client"]:
                _add_flow(flows, {
                    "direction": "ingress",
                    "source": {"kind": "External", "name": host, "address": host, "id": f"external:{host}"},
                    "destination": destination,
                    "protocol": "http",
                    "port": 80,
                    "bytes": None,
                    "rps": None,
                    "confidence": 0.92 if external_hosts else 0.72,
                    "source_system": "k8s_ingress",
                    "observed": False,
                    "evidence": [f"Ingress/{_name(ingress)} host {host} routes to Service/{service_name}"],
                })
                for workload in workload_targets:
                    _add_flow(flows, {
                        "direction": "ingress",
                        "source": {"kind": "External", "name": host, "address": host, "id": f"external:{host}"},
                        "destination": workload,
                        "protocol": "http",
                        "port": 80,
                        "bytes": None,
                        "rps": None,
                        "confidence": 0.84,
                        "source_system": "k8s_ingress",
                        "observed": False,
                        "evidence": [f"Ingress/{_name(ingress)} -> Service/{service_name} -> {workload.get('kind')}/{workload.get('name')}"],
                    })


def _scan_cmdb_flows(flows: dict[str, dict], cmdb_topology: dict, scope: dict, known_clusters: list[dict]) -> None:
    if not isinstance(cmdb_topology, dict):
        return
    nodes = {str(node.get("id") or node.get("name")): node for node in _as_list(cmdb_topology.get("nodes")) if isinstance(node, dict)}
    selected_cluster = str(scope.get("cluster") or "all")
    selected_namespace = str(scope.get("namespace") or "all")
    selected_workload = str(scope.get("workload") or "").strip()

    def in_scope(node: dict) -> bool:
        cluster = str(node.get("cluster") or node.get("cluster_id") or "")
        namespace = str(node.get("namespace") or "")
        cluster_ok = selected_cluster in {"", "all", "*", "所有"} or selected_cluster in {cluster, str(node.get("cluster_id") or "")}
        namespace_ok = selected_namespace in {"", "all", "*", "所有"} or not namespace or namespace == selected_namespace
        workload_ok = not selected_workload or _endpoint_matches_workload(node, selected_workload)
        return cluster_ok and namespace_ok and workload_ok

    def node_ref(node: dict) -> dict:
        cluster = str(node.get("cluster") or node.get("cluster_id") or "")
        return {
            "cluster": cluster or "external",
            "cluster_id": str(node.get("cluster_id") or cluster),
            "namespace": str(node.get("namespace") or ""),
            "kind": _short_kind(str(node.get("kind") or node.get("type") or "Service")),
            "name": str(node.get("name") or node.get("title") or node.get("id") or "-"),
            "id": str(node.get("id") or node.get("name") or ""),
        }

    known_cluster_values = {str(c.get("id") or "") for c in known_clusters} | {str(c.get("name") or "") for c in known_clusters}
    for edge in _as_list(cmdb_topology.get("edges")):
        if edge.get("observed") or str(edge.get("source_system") or "") in {"ebpf_beyla", "ebpf_hubble", "ebpf_calico", "ebpf_canal", "ebpf_flannel", "ebpf_generic", "observed_flow", "hubble", "cilium_hubble"}:
            continue
        source_node = nodes.get(str(edge.get("source") or edge.get("from") or edge.get("src") or ""))
        target_node = nodes.get(str(edge.get("target") or edge.get("to") or edge.get("dst") or ""))
        if not source_node or not target_node:
            continue
        source_in = in_scope(source_node)
        target_in = in_scope(target_node)
        if source_in and target_in:
            s_cluster = str(source_node.get("cluster") or source_node.get("cluster_id") or "")
            t_cluster = str(target_node.get("cluster") or target_node.get("cluster_id") or "")
            if s_cluster and t_cluster and s_cluster != t_cluster:
                direction = "cross_cluster"
            else:
                continue
        elif source_in:
            direction = "egress"
        elif target_in:
            direction = "ingress"
        else:
            continue
        source = node_ref(source_node)
        destination = node_ref(target_node)
        if direction == "ingress":
            source = node_ref(source_node)
        if direction in {"egress", "cross_cluster"}:
            destination_cluster = str(target_node.get("cluster") or target_node.get("cluster_id") or "")
            destination["type"] = "cross_cluster" if destination_cluster in known_cluster_values and destination_cluster else "external_cmdb_node"
            destination["address"] = destination.get("name")
        _add_flow(flows, {
            "direction": direction,
            "source": source,
            "destination": destination,
            "protocol": str(edge.get("protocol") or edge.get("type") or "unknown"),
            "port": edge.get("port"),
            "bytes": edge.get("bytes"),
            "rps": edge.get("rps") or edge.get("qps"),
            "confidence": 0.9,
            "source_system": "cmdb",
            "observed": False,
            "evidence": [f"CMDB edge {source.get('name')} -> {destination.get('name')} type={edge.get('type') or 'dependency'}"],
        })


def _scan_observed_flows(flows: dict[str, dict], observed_flows: list[dict], scope: dict, options: dict | None = None) -> None:
    selected_cluster = str(scope.get("cluster") or "all")
    selected_namespace = str(scope.get("namespace") or "all")
    selected_workload = str(scope.get("workload") or "").strip()
    include_internal = bool((options or {}).get("include_internal_observed"))
    for item in observed_flows:
        if not isinstance(item, dict):
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        destination = item.get("destination") if isinstance(item.get("destination"), dict) else {}
        src_cluster = str(source.get("cluster") or item.get("source_cluster") or item.get("cluster") or "")
        src_namespace = str(source.get("namespace") or item.get("source_namespace") or item.get("namespace") or "")
        if selected_cluster not in {"", "all", "*", "所有"} and selected_cluster not in {src_cluster, str(source.get("cluster_id") or "")}:
            continue
        if selected_namespace not in {"", "all", "*", "所有"} and src_namespace and src_namespace != selected_namespace:
            continue
        dest_addr = str(destination.get("address") or destination.get("name") or item.get("destination") or item.get("destination_ip") or "")
        if not dest_addr:
            continue
        source_ref = {
            "cluster": src_cluster or "observed",
            "cluster_id": str(source.get("cluster_id") or src_cluster),
            "namespace": src_namespace,
            "kind": _short_kind(str(source.get("kind") or item.get("source_kind") or "Workload")),
            "name": str(source.get("name") or item.get("source_name") or item.get("source_pod") or "-"),
            "pod": str(source.get("pod") or item.get("source_pod") or ""),
            "id": str(source.get("id") or item.get("source_id") or _hash_id(src_cluster, src_namespace, item.get("source_pod"))),
        }
        destination_ref = {
            "type": str(destination.get("type") or item.get("destination_type") or "external_observed"),
            "kind": str(destination.get("kind") or item.get("destination_kind") or ""),
            "name": str(destination.get("name") or dest_addr),
            "address": dest_addr,
            "port": destination.get("port") or item.get("destination_port"),
            "protocol": str(destination.get("protocol") or item.get("protocol") or "unknown"),
            "cluster": str(destination.get("cluster") or item.get("destination_cluster") or ""),
            "cluster_id": str(destination.get("cluster_id") or destination.get("cluster") or item.get("destination_cluster") or ""),
            "namespace": str(destination.get("namespace") or item.get("destination_namespace") or ""),
            "id": str(destination.get("id") or item.get("destination_id") or ""),
        }
        if selected_workload and not (
            _endpoint_matches_workload(source_ref, selected_workload)
            or _endpoint_matches_workload(destination_ref, selected_workload)
        ):
            continue
        direction = str(item.get("direction") or ("cross_cluster" if destination_ref.get("cluster") else "egress"))
        if direction == "internal" and not include_internal:
            continue
        if direction == "external":
            continue
        _add_flow(flows, {
            "direction": direction,
            "source": source_ref,
            "destination": destination_ref,
            "protocol": destination_ref.get("protocol") or "unknown",
            "port": destination_ref.get("port"),
            "bytes": item.get("bytes") or item.get("bytes_total"),
            "rps": item.get("rps") or item.get("qps"),
            "confidence": float(item.get("confidence") or 0.96),
            "source_system": str(item.get("source_system") or item.get("observer") or "observed_flow"),
            "observed": True,
            "evidence": _as_list(item.get("evidence")) or ["Observed by configured network-flow source"],
        })


def _build_graph(flows: list[dict]) -> dict:
    nodes: dict[str, dict] = {}
    edges = []
    for flow in flows:
        source = flow.get("source") or {}
        destination = flow.get("destination") or {}
        src_id = source.get("id") or _hash_id("src", source.get("cluster"), source.get("namespace"), source.get("kind"), source.get("name"))
        dst_id = destination.get("id") or _hash_id("dst", destination.get("type"), destination.get("cluster"), destination.get("address") or destination.get("name"))
        nodes.setdefault(src_id, {
            "id": src_id,
            "type": str(source.get("kind") or "workload").lower(),
            "title": f"{source.get('kind') or 'Source'}/{source.get('name') or '-'}",
            "cluster": source.get("cluster") or "",
            "namespace": source.get("namespace") or "",
            "external": source.get("kind") == "External",
        })
        nodes.setdefault(dst_id, {
            "id": dst_id,
            "type": str(destination.get("type") or destination.get("kind") or "external").lower(),
            "title": destination.get("name") or destination.get("address") or "external",
            "cluster": destination.get("cluster") or "",
            "namespace": destination.get("namespace") or "",
            "external": flow.get("direction") not in {"internal", "cross_cluster"} and not destination.get("namespace"),
        })
        edges.append({
            "id": flow.get("id"),
            "source": src_id if flow.get("direction") != "ingress" else dst_id,
            "target": dst_id if flow.get("direction") != "ingress" else src_id,
            "direction": flow.get("direction"),
            "protocol": flow.get("protocol"),
            "port": flow.get("port"),
            "source_system": flow.get("source_system"),
            "confidence": flow.get("confidence"),
        })
    return {"nodes": list(nodes.values()), "edges": edges}


def build_external_traffic_payload(
    resources: list[dict],
    *,
    cmdb_topology: dict | None = None,
    observed_flows: list[dict] | None = None,
    scope: dict | None = None,
    options: dict | None = None,
) -> dict:
    """Return external ingress, egress and cross-cluster flows for UI/API use."""
    scope = scope or {}
    options = options or {}
    flows_by_id: dict[str, dict] = {}
    resources = [item for item in resources if isinstance(item, dict)]
    known_clusters = [item.get("cluster") or {} for item in resources if isinstance(item.get("cluster"), dict)]
    internal_addresses = _known_internal_addresses(resources)
    internal_hosts = _known_internal_hosts(resources)
    suffixes = _internal_suffixes(options)

    for resource_scope in resources:
        cluster = resource_scope.get("cluster") or {"id": "local-cluster", "name": "local-cluster"}
        pods = _as_list(resource_scope.get("pods"))
        services = _as_list(resource_scope.get("services"))
        service_workloads = _service_to_workloads(cluster, services, pods)
        _scan_pod_spec_flows(
            flows_by_id,
            cluster=cluster,
            pods=pods,
            internal_hosts=internal_hosts,
            internal_addresses=internal_addresses,
            suffixes=suffixes,
            known_clusters=known_clusters,
            scope=scope,
            options=options,
        )
        _scan_service_flows(
            flows_by_id,
            cluster=cluster,
            services=services,
            endpoints=_as_list(resource_scope.get("endpoints")),
            endpoint_slices=_as_list(resource_scope.get("endpoint_slices")),
            service_workloads=service_workloads,
            internal_hosts=internal_hosts,
            internal_addresses=internal_addresses,
            suffixes=suffixes,
            known_clusters=known_clusters,
            scope=scope,
            options=options,
        )
        _scan_ingress_flows(
            flows_by_id,
            cluster=cluster,
            ingresses=_as_list(resource_scope.get("ingresses")),
            services=services,
            service_workloads=service_workloads,
            internal_hosts=internal_hosts,
            internal_addresses=internal_addresses,
            suffixes=suffixes,
            scope=scope,
        )

    _scan_cmdb_flows(flows_by_id, cmdb_topology or {}, scope, known_clusters)
    _scan_observed_flows(flows_by_id, observed_flows or [], scope, options)

    flows = sorted(
        flows_by_id.values(),
        key=lambda item: (
            {"ebpf_beyla": 0, "ebpf_hubble": 0, "ebpf_calico": 0, "ebpf_canal": 0, "ebpf_flannel": 0, "ebpf_generic": 0, "hubble": 0, "cilium_hubble": 0, "observed_flow": 1, "cmdb": 2, "k8s_ingress": 3, "k8s_service": 4, "k8s_endpoints": 5, "k8s_static_inference": 6}.get(str(item.get("source_system")), 9),
            str(item.get("direction") or ""),
            str((item.get("source") or {}).get("cluster") or ""),
            str((item.get("source") or {}).get("namespace") or ""),
        ),
    )
    summary = {
        "total": len(flows),
        "ingress": sum(1 for flow in flows if flow.get("direction") == "ingress"),
        "egress": sum(1 for flow in flows if flow.get("direction") == "egress"),
        "cross_cluster": sum(1 for flow in flows if flow.get("direction") == "cross_cluster"),
        "internal": sum(1 for flow in flows if flow.get("direction") == "internal"),
        "observed": sum(1 for flow in flows if flow.get("observed")),
        "ebpf_observed": sum(1 for flow in flows if str(flow.get("source_system") or "").startswith("ebpf_")),
        "inferred": sum(1 for flow in flows if not flow.get("observed")),
        "clusters": len({(flow.get("source") or {}).get("cluster_id") or (flow.get("source") or {}).get("cluster") for flow in flows}),
        "external_endpoints": len({(flow.get("destination") or {}).get("address") or (flow.get("destination") or {}).get("name") for flow in flows}),
    }
    sources = sorted({str(flow.get("source_system") or "unknown") for flow in flows})
    return {
        "status": "ok",
        "mode": "observed" if summary["observed"] and not summary["inferred"] else ("inferred" if not summary["observed"] else "mixed"),
        "scope": scope,
        "summary": summary,
        "flows": flows,
        "graph": _build_graph(flows),
        "data_sources": sources,
        "explain": "只展示集群内对象与集群外或跨集群对象之间的数据流；observed 为真实观测，inferred 为基于 K8s/CMDB 配置的推断。",
    }
