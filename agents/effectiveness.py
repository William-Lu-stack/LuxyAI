"""AI 巡检与运维成效记录，默认持久化到运行时 PVC。"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any
import uuid


@dataclass
class InspectionRun:
    id: str
    timestamp: str
    scope_cluster: str
    scope_namespace: str
    source: str
    findings_total: int
    critical_findings: int
    affected_pods: int
    affected_workloads: int
    model_id: str = ""


@dataclass
class RemediationOutcome:
    id: str
    timestamp: str
    plan_id: str
    cluster: str
    namespace: str
    target: str
    model_id: str
    changes_total: int
    changes_succeeded: int
    changes_failed: int
    pods_recovered: int
    risk_reduced: bool
    safety_state: str
    status: str = "unknown"
    error: str = ""
    recovered_pods: list[str] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    alternative_plans: int = 0
    summary: str = ""
    lineage_id: str = ""
    parent_job_id: str = ""
    lineage_attempt: int = 0
    attempted_strategies: list[dict[str, Any]] = field(default_factory=list)


INSPECTION_RUNS: list[InspectionRun] = []
REMEDIATION_OUTCOMES: list[RemediationOutcome] = []
_STORE_LOCK = threading.RLock()
_STORE_LOADED_FROM = ""
_STORE_ACTIVE_PATH = ""


def _store_candidates() -> list[Path]:
    configured = os.getenv("EFFECTIVENESS_STORE_PATH", "/var/lib/flawless/effectiveness-state.json")
    fallback = os.getenv("EFFECTIVENESS_STORE_FALLBACK_PATH", "/tmp/flawless-effectiveness-state.json")
    paths: list[Path] = []
    for value in (configured, fallback):
        path = Path(value).expanduser()
        if path not in paths:
            paths.append(path)
    return paths


def _construct_dataclass(model, payload: dict[str, Any]):
    allowed = set(model.__dataclass_fields__)
    return model(**{key: value for key, value in payload.items() if key in allowed})


def _ensure_store_loaded() -> None:
    global _STORE_LOADED_FROM, _STORE_ACTIVE_PATH
    signature = "|".join(str(path) for path in _store_candidates())
    if _STORE_LOADED_FROM == signature:
        return
    INSPECTION_RUNS.clear()
    REMEDIATION_OUTCOMES.clear()
    _STORE_ACTIVE_PATH = ""
    for path in _store_candidates():
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            INSPECTION_RUNS.extend(
                _construct_dataclass(InspectionRun, item)
                for item in (payload.get("inspection_runs") or [])[-500:]
                if isinstance(item, dict)
            )
            REMEDIATION_OUTCOMES.extend(
                _construct_dataclass(RemediationOutcome, item)
                for item in (payload.get("remediation_outcomes") or [])[-500:]
                if isinstance(item, dict)
            )
            _STORE_ACTIVE_PATH = str(path)
            break
        except Exception:
            continue
    _STORE_LOADED_FROM = signature


def _persist_state() -> dict[str, Any]:
    global _STORE_ACTIVE_PATH
    payload = {
        "version": 1,
        "updated_at": _now(),
        "inspection_runs": [asdict(item) for item in INSPECTION_RUNS[-500:]],
        "remediation_outcomes": [asdict(item) for item in REMEDIATION_OUTCOMES[-500:]],
    }
    errors: list[str] = []
    preferred = [Path(_STORE_ACTIVE_PATH)] if _STORE_ACTIVE_PATH else []
    candidates = preferred + [path for path in _store_candidates() if path not in preferred]
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temporary.replace(path)
            _STORE_ACTIVE_PATH = str(path)
            return {"durable": str(path).startswith("/var/lib/"), "path": str(path), "error": ""}
        except Exception as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
    return {"durable": False, "path": "", "error": "; ".join(errors)}


def _storage_status() -> dict[str, Any]:
    path = _STORE_ACTIVE_PATH or str(_store_candidates()[0])
    return {
        "path": path,
        "durable": path.startswith("/var/lib/"),
        "loaded": bool(_STORE_LOADED_FROM),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        masked = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(word in lowered for word in ("secret", "password", "token", "authorization", "client_secret", "apikey", "api_key")):
                masked[key] = "***"
            else:
                masked[key] = _redact(item)
        return masked
    if isinstance(value, list):
        return [_redact(item) for item in value[:40]]
    return value


def _change_target(change: dict[str, Any], plan: dict[str, Any]) -> str:
    ctype = change.get("type") or "change"
    if ctype in {"recreate_pod", "evict_pod"}:
        return f"Pod/{change.get('pod_name') or plan.get('pod_name') or '-'}"
    if ctype == "patch_hpa":
        return f"HPA/{change.get('hpa_name') or '-'}"
    if ctype == "expand_pvc":
        return f"PVC/{change.get('pvc_name') or '-'}"
    if ctype == "cordon_node":
        return f"Node/{change.get('node_name') or '-'}"
    return f"{change.get('workload_type') or 'Workload'}/{change.get('workload_name') or plan.get('target') or '-'}"


def _compact_changes(plan: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for idx, change in enumerate(plan.get("changes") or []):
        result = results[idx] if idx < len(results) else {}
        compacted.append({
            "type": change.get("type") or "change",
            "target": _change_target(change, plan),
            "namespace": change.get("namespace") or plan.get("namespace") or "default",
            "reason": change.get("reason") or "",
            "risk": change.get("risk") or "medium",
            "status": result.get("status") or "pending",
            "patch": _redact(change.get("patch") or {}),
            "payload": _redact({k: v for k, v in change.items() if k not in {"patch", "reason"}}),
        })
    return compacted


def _compact_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "status": item.get("status") or "unknown",
        "change_type": (item.get("change") or {}).get("type") or "",
        "target": _change_target(item.get("change") or {}, {}),
        "result": _redact(item.get("result") or {}),
    } for item in results[:40]]


def _extract_recovered_pods(plan: dict[str, Any], verification: dict[str, Any]) -> list[str]:
    if verification.get("recovered") is not True:
        return []
    pods = [str(x) for x in (verification.get("recovered_pods") or []) if x]
    if pods:
        return sorted(set(pods))
    pod_name = plan.get("pod_name") or ((plan.get("evidence") or {}).get("pod") or {}).get("name")
    return [pod_name] if pod_name else []


def _record_inspection_in_memory(scope_cluster: str, scope_namespace: str, payload: dict[str, Any], model_id: str = "") -> dict[str, Any]:
    findings = payload.get("findings") or []
    affected_pods = {f.get("name") for f in findings if f.get("name")}
    affected_workloads = {
        f"{(f.get('workload') or {}).get('kind', '')}/{(f.get('workload') or {}).get('name', '')}"
        for f in findings
        if (f.get("workload") or {}).get("name")
    }
    run = InspectionRun(
        id=str(uuid.uuid4())[:12],
        timestamp=_now(),
        scope_cluster=scope_cluster or "all",
        scope_namespace=scope_namespace or "all",
        source=payload.get("source", "unknown"),
        findings_total=len(findings),
        critical_findings=sum(1 for f in findings if f.get("severity") in {"P0", "P1"}),
        affected_pods=len(affected_pods),
        affected_workloads=len(affected_workloads),
        model_id=model_id,
    )
    INSPECTION_RUNS.append(run)
    del INSPECTION_RUNS[:-500]
    return asdict(run)


def record_inspection(scope_cluster: str, scope_namespace: str, payload: dict[str, Any], model_id: str = "") -> dict[str, Any]:
    with _STORE_LOCK:
        _ensure_store_loaded()
        recorded = _record_inspection_in_memory(scope_cluster, scope_namespace, payload, model_id)
        recorded["storage"] = _persist_state()
        return recorded


def _record_remediation_in_memory(plan: dict[str, Any], result: dict[str, Any], model_id: str = "") -> dict[str, Any]:
    changes = plan.get("changes") or []
    results = result.get("results") or []
    failed = [x for x in results if x.get("status") in {"failed", "blocked"}]
    succeeded = [x for x in results if x.get("status") not in {"failed", "blocked"}]
    status = result.get("status", "unknown")
    verification = result.get("verification") or {}
    continuation = result.get("continuation_context") if isinstance(result.get("continuation_context"), dict) else {}
    recovered_pods = _extract_recovered_pods(plan, verification)
    pods_recovered = int(
        result.get("pods_recovered")
        or len(recovered_pods)
        or (1 if status == "completed" and not failed and changes else 0)
    )
    outcome = RemediationOutcome(
        id=str(uuid.uuid4())[:12],
        timestamp=_now(),
        plan_id=plan.get("id", ""),
        cluster=plan.get("cluster", "local-cluster"),
        namespace=plan.get("namespace", "default"),
        target=plan.get("target", ""),
        model_id=model_id,
        changes_total=len(changes),
        changes_succeeded=len(succeeded),
        changes_failed=len(failed),
        pods_recovered=pods_recovered,
        risk_reduced=status == "completed" and not failed,
        safety_state="human-approved" if changes else "diagnosis-only",
        status=status,
        error="; ".join(str((x.get("result") or {}).get("error", "")) for x in failed if x.get("result")),
        recovered_pods=recovered_pods,
        changes=_compact_changes(plan, results),
        results=_compact_results(results),
        verification=_redact(verification),
        alternative_plans=len(result.get("alternative_plans") or []),
        summary=str(result.get("message") or ""),
        lineage_id=str(continuation.get("lineage_id") or plan.get("_lineage_id") or ""),
        parent_job_id=str(continuation.get("parent_job_id") or plan.get("_parent_job_id") or ""),
        lineage_attempt=int(continuation.get("attempt_count") or plan.get("_prior_attempt_count") or 0),
        attempted_strategies=_redact(continuation.get("attempts") or plan.get("_prior_attempts") or [])[-12:],
    )
    REMEDIATION_OUTCOMES.append(outcome)
    del REMEDIATION_OUTCOMES[:-500]
    return asdict(outcome)


def record_remediation(plan: dict[str, Any], result: dict[str, Any], model_id: str = "") -> dict[str, Any]:
    with _STORE_LOCK:
        _ensure_store_loaded()
        recorded = _record_remediation_in_memory(plan, result, model_id)
        recorded["storage"] = _persist_state()
        return recorded


def _summary_in_memory() -> dict[str, Any]:
    inspections = [asdict(x) for x in INSPECTION_RUNS]
    outcomes = [asdict(x) for x in REMEDIATION_OUTCOMES]
    total_findings = sum(x["findings_total"] for x in inspections)
    total_critical = sum(x["critical_findings"] for x in inspections)
    changes_total = sum(x["changes_total"] for x in outcomes)
    changes_succeeded = sum(x["changes_succeeded"] for x in outcomes)
    pods_recovered = sum(x["pods_recovered"] for x in outcomes)
    by_model: dict[str, dict[str, Any]] = {}
    for row in inspections:
        key = row.get("model_id") or "default"
        by_model.setdefault(key, {
            "model_id": key,
            "inspection_runs": 0,
            "findings": 0,
            "critical": 0,
            "remediation_runs": 0,
            "changes_total": 0,
            "successful_changes": 0,
            "failed_changes": 0,
            "pods_recovered": 0,
            "recovered_pods": set(),
            "records": [],
        })
        by_model[key]["inspection_runs"] += 1
        by_model[key]["findings"] += row["findings_total"]
        by_model[key]["critical"] += row["critical_findings"]
    for row in outcomes:
        key = row.get("model_id") or "default"
        by_model.setdefault(key, {
            "model_id": key,
            "inspection_runs": 0,
            "findings": 0,
            "critical": 0,
            "remediation_runs": 0,
            "changes_total": 0,
            "successful_changes": 0,
            "failed_changes": 0,
            "pods_recovered": 0,
            "recovered_pods": set(),
            "records": [],
        })
        by_model[key]["remediation_runs"] += 1
        by_model[key]["changes_total"] += row["changes_total"]
        by_model[key]["failed_changes"] += row["changes_failed"]
        by_model[key]["pods_recovered"] += row["pods_recovered"]
        by_model[key]["successful_changes"] += row["changes_succeeded"]
        by_model[key]["recovered_pods"].update(row.get("recovered_pods") or [])
        by_model[key]["records"].append({
            "id": row["id"],
            "timestamp": row["timestamp"],
            "cluster": row["cluster"],
            "namespace": row["namespace"],
            "target": row["target"],
            "status": row.get("status", "unknown"),
            "changes_total": row["changes_total"],
            "changes_succeeded": row["changes_succeeded"],
            "changes_failed": row["changes_failed"],
            "pods_recovered": row["pods_recovered"],
            "recovered_pods": row.get("recovered_pods") or [],
            "lineage_id": row.get("lineage_id") or "",
            "lineage_attempt": row.get("lineage_attempt") or 0,
            "attempted_strategies": row.get("attempted_strategies") or [],
        })
    models = []
    for item in by_model.values():
        recovered_names = sorted(item.pop("recovered_pods"))
        records = sorted(item.get("records") or [], key=lambda x: x.get("timestamp", ""), reverse=True)
        item["recovered_pods"] = recovered_names[:20]
        item["records"] = records[:10]
        item["change_success_rate"] = round(item["successful_changes"] / item["changes_total"], 4) if item["changes_total"] else 0
        models.append(item)
    return {
        "status": "ok",
        "summary": {
            "inspection_runs": len(inspections),
            "findings_total": total_findings,
            "critical_findings": total_critical,
            "remediation_runs": len(outcomes),
            "changes_total": changes_total,
            "changes_succeeded": changes_succeeded,
            "change_success_rate": round(changes_succeeded / changes_total, 4) if changes_total else 0,
            "pods_recovered": pods_recovered,
            "risk_reduction_rate": round(sum(1 for x in outcomes if x["risk_reduced"]) / len(outcomes), 4) if outcomes else 0,
        },
        "by_model": sorted(models, key=lambda x: (x["pods_recovered"], x["successful_changes"]), reverse=True),
        "recent_inspections": inspections[-20:],
        "recent_remediations": outcomes[-20:],
    }


def summary() -> dict[str, Any]:
    with _STORE_LOCK:
        _ensure_store_loaded()
        payload = _summary_in_memory()
        payload["storage"] = _storage_status()
        return payload
