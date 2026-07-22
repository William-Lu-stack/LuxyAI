"""供运维和发布治理共同使用的 SLO 与错误预算策略。"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_error_budget(objective: dict[str, Any]) -> dict[str, Any]:
    """根据请求失败率或停机证据计算可用性错误预算。"""

    target_percent = min(99.999, max(50.0, _number(objective.get("target_percent"), 99.9)))
    window_days = min(365, max(1, int(_number(objective.get("window_days"), 30))))
    window_minutes = window_days * 24 * 60
    budget_fraction = max(0.00001, 1.0 - target_percent / 100.0)
    allowed_downtime_minutes = window_minutes * budget_fraction
    observed_minutes = min(window_minutes, max(1.0, _number(objective.get("observed_minutes"), window_minutes)))

    total_requests = max(0.0, _number(objective.get("total_requests")))
    failed_requests = max(0.0, _number(objective.get("failed_requests")))
    if total_requests > 0:
        allowed_bad_events = total_requests * budget_fraction
        used = failed_requests
        allowed = max(0.00001, allowed_bad_events)
        evidence_mode = "request_ratio"
        actual_availability = max(0.0, 100.0 * (1.0 - failed_requests / total_requests))
        used_downtime_minutes = observed_minutes * (1.0 - actual_availability / 100.0)
    else:
        availability = min(100.0, max(0.0, _number(objective.get("observed_availability_percent"), 100.0)))
        explicit_downtime = objective.get("downtime_minutes")
        used_downtime_minutes = (
            max(0.0, _number(explicit_downtime))
            if explicit_downtime is not None
            else observed_minutes * (1.0 - availability / 100.0)
        )
        used = used_downtime_minutes
        allowed = max(0.00001, allowed_downtime_minutes)
        evidence_mode = "availability_time"
        actual_availability = max(0.0, 100.0 * (1.0 - used_downtime_minutes / observed_minutes))
        allowed_bad_events = 0.0

    consumed_ratio = max(0.0, used / allowed)
    remaining_ratio = max(0.0, 1.0 - consumed_ratio)
    actual_error_fraction = max(0.0, 1.0 - actual_availability / 100.0)
    burn_rate = actual_error_fraction / budget_fraction
    exhausted = consumed_ratio >= 1.0
    state = "exhausted" if exhausted else "at_risk" if consumed_ratio >= 0.7 or burn_rate >= 2.0 else "healthy"

    return {
        "objective_id": objective.get("id"),
        "service": objective.get("service"),
        "cluster": objective.get("cluster", "all"),
        "namespace": objective.get("namespace", "all"),
        "target_percent": round(target_percent, 4),
        "window_days": window_days,
        "error_budget_percent": round(budget_fraction * 100.0, 4),
        "allowed_downtime_minutes": round(allowed_downtime_minutes, 3),
        "used_downtime_minutes": round(used_downtime_minutes, 3),
        "remaining_downtime_minutes": round(max(0.0, allowed_downtime_minutes - used_downtime_minutes), 3),
        "allowed_bad_events": round(allowed_bad_events, 3),
        "failed_requests": round(failed_requests, 3),
        "actual_availability_percent": round(actual_availability, 5),
        "consumed_ratio": round(consumed_ratio, 5),
        "remaining_ratio": round(remaining_ratio, 5),
        "burn_rate": round(burn_rate, 4),
        "state": state,
        "freeze_changes": exhausted,
        "freeze_reason": "错误预算已耗尽：冻结新功能和常规变更，优先恢复稳定性。" if exhausted else "",
        "evidence_mode": evidence_mode,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def default_objective(service: str = "platform-default") -> dict[str, Any]:
    target = _number(os.getenv("SLO_DEFAULT_TARGET_PERCENT"), 99.9)
    window_days = max(1, int(_number(os.getenv("SLO_DEFAULT_WINDOW_DAYS"), 30)))
    return {
        "id": service,
        "service": service,
        "cluster": "all",
        "namespace": "all",
        "target_percent": target,
        "window_days": window_days,
        "observed_availability_percent": 100.0,
        "observed_minutes": window_days * 24 * 60,
    }
