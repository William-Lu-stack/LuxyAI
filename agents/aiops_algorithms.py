from __future__ import annotations

import importlib.util
import math
import os
import sys
from pathlib import Path
from typing import Any


def _load_private_module():
    if os.getenv("FLAWLESS_DISABLE_CUSTOM_ALGORITHMS", "").lower() in {"1", "true", "yes", "on"}:
        return None
    candidates = [
        os.getenv("FLAWLESS_CUSTOM_ALGORITHM_PATH", ""),
        str(Path(__file__).resolve().parents[1] / ".local" / "custom_algorithms" / "aiops_algorithms_custom.py"),
        "/var/lib/flawless-custom/aiops_algorithms_custom.py",
        "/var/lib/flawless/private/aiops_algorithms_custom.py",
    ]
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.exists() or not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("_flawless_custom_aiops_algorithms", path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    return None


_PRIVATE = _load_private_module()


def _clamp(value: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        number = float(value)
    except Exception:
        number = 0.0
    return max(lo, min(hi, number))


def _public_node(node: dict[str, Any] | None) -> dict[str, Any]:
    node = node or {}
    return {
        "id": node.get("id"),
        "type": node.get("type"),
        "title": node.get("title") or node.get("name"),
        "category": node.get("category"),
        "risk": node.get("risk"),
        "status": node.get("status"),
        "meta": node.get("meta") or {},
    }


def _edge_lists(graph: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    outgoing: dict[str, list[dict[str, Any]]] = {}
    incoming: dict[str, list[dict[str, Any]]] = {}
    for edge in graph.get("edges", []) or []:
        src = str(edge.get("from") or "")
        dst = str(edge.get("to") or "")
        if not src or not dst:
            continue
        outgoing.setdefault(src, []).append(edge)
        incoming.setdefault(dst, []).append(edge)
    return outgoing, incoming


def _walk(graph: dict[str, Any], start_id: str, direction: str, max_depth: int = 3) -> list[dict[str, Any]]:
    nodes = {str(n.get("id")): n for n in graph.get("nodes", []) or [] if n.get("id")}
    outgoing, incoming = _edge_lists(graph)
    frontier = [(start_id, 0)]
    seen = {start_id}
    result: list[dict[str, Any]] = []
    while frontier:
        current, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        edges = outgoing.get(current, []) if direction == "downstream" else incoming.get(current, [])
        for edge in edges:
            nxt = str(edge.get("to") if direction == "downstream" else edge.get("from"))
            if not nxt or nxt in seen:
                continue
            seen.add(nxt)
            node = _public_node(nodes.get(nxt, {"id": nxt}))
            score = _score_node(node, edge=edge, depth=depth + 1)
            item = {"direction": direction, "depth": depth + 1, "node": node, "score": score, "edge_count": depth + 1}
            result.append(item)
            frontier.append((nxt, depth + 1))
    return result


def _risk_value(value: Any) -> float:
    table = {"critical": 1.0, "err": 0.9, "high": 0.78, "warning": 0.58, "medium": 0.5, "low": 0.3, "normal": 0.18, "ok": 0.12}
    return table.get(str(value or "").lower(), 0.28)


def _score_node(node: dict[str, Any], *, edge: dict[str, Any] | None = None, depth: int = 1) -> float:
    meta = node.get("meta") or {}
    edge = edge or {}
    business = _clamp(meta.get("business_weight") or meta.get("criticality_weight") or 1.0, 0.2, 2.0)
    traffic = _clamp(edge.get("traffic_ratio") or edge.get("traffic") or 0.45, 0.05, 1.0)
    risk = _risk_value(node.get("risk") or node.get("status"))
    return round((business * (0.4 + traffic) * (0.65 + risk)) / math.sqrt(max(depth, 1)), 4)


def _impact_level(score: float) -> str:
    if score >= 0.82:
        return "critical"
    if score >= 0.62:
        return "high"
    if score >= 0.38:
        return "medium"
    return "low"


def analyze_blast_radius(
    selected: dict[str, Any],
    graph: dict[str, Any],
    scenario: str = "pod_change",
    change: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if _PRIVATE and hasattr(_PRIVATE, "analyze_blast_radius"):
        return _PRIVATE.analyze_blast_radius(selected, graph, scenario, change, runtime)

    selected = selected or {}
    graph = graph or {}
    runtime = runtime or {}
    start_id = str(selected.get("id") or "")
    upstream = _walk(graph, start_id, "upstream", int(runtime.get("max_depth", 3) or 3)) if start_id else []
    downstream = _walk(graph, start_id, "downstream", int(runtime.get("max_depth", 3) or 3)) if start_id else []
    critical_paths = sorted(upstream + downstream, key=lambda item: item["score"], reverse=True)[:8]
    amp = round(sum(item["score"] for item in critical_paths), 4)
    impact_score = _clamp((amp / 4.0) + _risk_value(selected.get("risk") or selected.get("status")) * 0.25)
    downstream_nodes = [item["node"] for item in downstream]
    upstream_nodes = [item["node"] for item in upstream]
    return {
        "source": "public-fallback",
        "algorithm": {
            "name": "OpenBlastRadiusBaseline",
            "version": "0.1",
            "semantic_operator": str((change or {}).get("operator") or scenario or "change"),
            "formula": "Public baseline scoring. Private formula is loaded only from a local path when configured.",
        },
        "selected": _public_node(selected),
        "impact_level": _impact_level(impact_score),
        "impact_score": round(impact_score, 4),
        "amplification_factor": amp,
        "context_risk": round(_risk_value(selected.get("risk") or selected.get("status")), 4),
        "summary": "Open-source baseline impact estimate. Mount the custom algorithm module to enable production scoring.",
        "blast_radius": {
            "upstream": upstream[:8],
            "downstream": downstream[:12],
            "impacted_services": [n for n in downstream_nodes if n.get("type") == "service"],
            "impacted_pods": [n for n in downstream_nodes if n.get("type") == "pod"],
            "parent_workloads": [n for n in upstream_nodes if n.get("type") == "workload"],
            "related_dependencies": [n for n in downstream_nodes + upstream_nodes if n.get("type") == "dependency" or n.get("category") in {"data", "middleware", "storage", "observability"}][:8],
            "critical_paths": critical_paths,
        },
        "aiops_value": [
            "Ranks nearby topology nodes for triage.",
            "Provides a stable contract for the UI and release gate.",
            "Allows the public project to run without exposing custom scoring logic.",
        ],
        "recommended_actions": [
            "Validate the highest-scored upstream and downstream nodes first.",
            "Review Service endpoints, Pod readiness, events, recent logs, and shared dependencies.",
            "Require preview, rollback, and operator approval before mutating high-impact workloads.",
        ],
    }


def evaluate_release_gate(
    change: dict[str, Any],
    graph: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if _PRIVATE and hasattr(_PRIVATE, "evaluate_release_gate"):
        return _PRIVATE.evaluate_release_gate(change, graph, runtime, history, candidates, observation)

    runtime = runtime or {}
    observation = observation or {}
    selected = (change or {}).get("selected") or {"id": (change or {}).get("target"), "type": (change or {}).get("kind"), "title": (change or {}).get("target")}
    blast = analyze_blast_radius(selected, graph or {}, "release_gate", change, runtime)
    remaining_budget = _clamp(runtime.get("remaining_budget", 0.5))
    burn = _clamp(runtime.get("budget_burn_rate", 0.0))
    diff_error = _clamp(float(observation.get("diff_error_rate", 0) or 0) / 0.01)
    amp = _clamp(float(blast.get("amplification_factor", 0) or 0) / 4.0)
    risk_score = round(_clamp(0.35 * amp + 0.35 * burn + 0.30 * diff_error), 4)
    if remaining_budget <= 0.05 or risk_score >= 0.78:
        verdict, action = "manual_approval", "deny_or_minimal_canary"
        reason = "Open-source baseline detected high release risk or low error budget."
    elif risk_score >= 0.52:
        verdict, action = "hold", "pause"
        reason = "Open-source baseline recommends holding the rollout and collecting more evidence."
    else:
        verdict, action = "pass", "advance_canary"
        reason = "Open-source baseline risk is within the default release envelope."
    strategy = {
        "first_ratio": 0.01,
        "max_ratio": 0.10 if risk_score >= 0.35 else 0.30,
        "step_ratio": 0.02,
        "observation_window_min": 30,
        "batches": 5,
        "predicted_budget_cost": round(risk_score * 0.25, 4),
        "violation_probability": risk_score,
        "risk_score": risk_score,
        "within_envelope": verdict == "pass",
    }
    return {
        "status": "ok",
        "algorithm": {
            "name": "OpenReleaseGateBaseline",
            "version": "0.1",
            "semantic_operator": str((change or {}).get("operator") or "release"),
            "formulas": ["Public baseline scoring. Private gate algorithm is loaded only from a local path when configured."],
        },
        "verdict": verdict,
        "action": action,
        "reason": reason,
        "selected_strategy": strategy if verdict == "pass" else None,
        "safety_envelope": {
            "remaining_budget": remaining_budget,
            "safety_factor": 0.35,
            "budget_cost_limit": round(remaining_budget * 0.35, 4),
            "violation_threshold": 0.32,
            "risk_threshold": 0.58,
            "first_ratio_limit": 0.05,
            "max_ratio_limit": 0.5,
            "step_ratio_limit": 0.1,
            "pause_threshold": 0.48,
            "resume_threshold": 0.28,
            "rollback_threshold": 0.78,
        },
        "risk": {
            "amplification_factor": blast.get("amplification_factor", 0),
            "history_risk": 0.0,
            "diff_risk": diff_error,
            "risk_score": risk_score,
        },
        "candidate_strategies": [strategy],
        "blast_radius": blast,
    }


def prioritize_inspection_findings(findings: list[dict[str, Any]]) -> dict[str, Any]:
    if _PRIVATE and hasattr(_PRIVATE, "prioritize_inspection_findings"):
        return _PRIVATE.prioritize_inspection_findings(findings)

    severity_weight = {"P0": 1.0, "P1": 0.86, "P2": 0.62, "P3": 0.34}
    issue_weight = {"crashloop": 0.9, "image_pull": 0.78, "storage_config": 0.82, "network": 0.8, "scheduling": 0.68, "node": 0.74, "capacity": 0.58, "high_cpu": 0.56}
    ranked = []
    for idx, finding in enumerate(findings or []):
        workload = finding.get("workload") or {}
        pods = workload.get("pods") or []
        replicas = float(workload.get("replicas") or len(pods) or 1)
        ready = float(workload.get("ready_replicas") or sum(1 for pod in pods if pod.get("ready")) or 0)
        redundancy = _clamp(1.0 - ready / max(replicas, 1.0))
        evidence = finding.get("evidence") or {}
        evidence_score = 0.3 + (0.25 if evidence.get("events") else 0) + (0.2 if evidence.get("state_text") or evidence.get("pod") else 0)
        score = _clamp(
            0.38 * severity_weight.get(str(finding.get("severity")), 0.42)
            + 0.24 * issue_weight.get(str(finding.get("category")), 0.48)
            + 0.20 * redundancy
            + 0.18 * _clamp(evidence_score)
        )
        level = _impact_level(score)
        finding["aiops_priority"] = {
            "score": round(score, 4),
            "level": level,
            "method": "open-source baseline severity/category/redundancy/evidence scoring",
            "factors": {
                "severity": finding.get("severity"),
                "category": finding.get("category"),
                "redundancy_risk": round(redundancy, 4),
                "evidence_confidence": round(_clamp(evidence_score), 4),
            },
        }
        ranked.append((score, -idx, finding))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    ordered = [item[2] for item in ranked]
    return {
        "algorithm": {
            "name": "OpenInspectionPriorityBaseline",
            "version": "0.1",
            "formula": "Public baseline scoring. Private prioritization is loaded only from a local path when configured.",
        },
        "findings": ordered,
        "top_risks": [finding.get("aiops_priority") for finding in ordered[:5]],
    }
