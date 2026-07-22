"""Unified Langfuse observability helpers for Flawless AIOps.

The rest of the platform should treat Langfuse as an optional telemetry sink:
when the SDK or credentials are unavailable, every helper degrades to a no-op
object while still returning local trace ids for UI audit and model comparison.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

try:  # pragma: no cover - exercised in the production image.
    from langfuse import Langfuse
except Exception:  # pragma: no cover - local dev may not install langfuse.
    Langfuse = None  # type: ignore[assignment]


SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|cookie|client_secret|api[_-]?key|access[_-]?key)",
    re.I,
)
SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I),
    re.compile(r"(client_secret=)[^&\s]+", re.I),
    re.compile(r"(password=)[^&\s]+", re.I),
    re.compile(r"(token=)[^&\s]+", re.I),
    re.compile(r"(sk-lf-)[A-Za-z0-9-]+", re.I),
    re.compile(r"(pk-lf-)[A-Za-z0-9-]+", re.I),
]


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _clamp(value: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        numeric = float(value)
    except Exception:
        numeric = 0.0
    return max(lo, min(hi, numeric))


def redact_text(value: str) -> str:
    """Mask common secret-like values before writing LLM traces or logs."""
    redacted = value
    for pattern in SENSITIVE_VALUE_PATTERNS:
        redacted = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", redacted)
    return redacted


def redact_sensitive(value: Any, depth: int = 0) -> Any:
    """Recursively redact secrets and bound payload size for observability sinks."""
    if depth > 8:
        return "[REDACTED:MAX_DEPTH]"
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if SENSITIVE_KEY_RE.search(str(key)) else redact_sensitive(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item, depth + 1) for item in value[:200]]
    if isinstance(value, str):
        return redact_text(value)
    return value


class _NoopObservation:
    """Langfuse-compatible observation object used when telemetry is disabled."""

    def __init__(self, obs_id: str | None = None):
        self.id = obs_id or uuid.uuid4().hex

    def span(self, *args, **kwargs):
        return _NoopObservation()

    def generation(self, *args, **kwargs):
        return _NoopObservation()

    def event(self, *args, **kwargs):
        return _NoopObservation()

    def update(self, *args, **kwargs):
        return self

    def end(self, *args, **kwargs):
        return None

    def score(self, *args, **kwargs):
        return None


_LANGFUSE_ENABLED = _env_bool("LANGFUSE_ENABLED", "true")
_LANGFUSE = None
_LANGFUSE_ERROR = ""

if _LANGFUSE_ENABLED and Langfuse and os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
    try:  # pragma: no cover - depends on runtime credentials.
        _LANGFUSE = Langfuse(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
            host=os.getenv("LANGFUSE_HOST", "http://localhost:3000"),
        )
    except Exception as exc:  # pragma: no cover - defensive runtime guard.
        _LANGFUSE = None
        _LANGFUSE_ERROR = f"{type(exc).__name__}: {exc}"


def langfuse_status() -> dict[str, Any]:
    """Return configuration state without exposing credentials."""
    return {
        "enabled": _LANGFUSE_ENABLED,
        "configured": bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")),
        "sdk_available": Langfuse is not None,
        "active": _LANGFUSE is not None,
        "host": os.getenv("LANGFUSE_HOST", ""),
        "error": _LANGFUSE_ERROR,
        "capabilities": [
            "session_trace",
            "span_tool_call",
            "generation_token_usage",
            "quality_score",
            "cost_estimation",
            "model_profile_compare",
            "secret_redaction",
        ],
    }


def new_trace_id(prefix: str = "flawless") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def observation_id(observation: Any, fallback: str = "") -> str:
    return str(
        getattr(observation, "id", "")
        or getattr(observation, "trace_id", "")
        or getattr(observation, "observation_id", "")
        or fallback
    )


def start_trace(
    name: str,
    *,
    trace_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    input: Any | None = None,
    tags: list[str] | None = None,
) -> Any:
    trace_id = trace_id or new_trace_id()
    payload = _drop_none({
        "id": trace_id,
        "name": name,
        "user_id": user_id,
        "session_id": session_id,
        "metadata": redact_sensitive(metadata or {}),
        "input": redact_sensitive(input),
        "tags": tags,
    })
    if _LANGFUSE is None:
        return _NoopObservation(trace_id)
    try:  # pragma: no cover - depends on SDK/runtime.
        return _LANGFUSE.trace(**payload)
    except TypeError:  # Older SDK builds can be stricter about kwargs.
        try:
            return _LANGFUSE.trace(name=name, metadata=payload.get("metadata"))
        except Exception:
            return _NoopObservation(trace_id)
    except Exception:
        return _NoopObservation(trace_id)


def start_span(trace: Any, name: str, *, input: Any | None = None, metadata: dict[str, Any] | None = None) -> Any:
    try:
        return trace.span(name=name, input=redact_sensitive(input), metadata=redact_sensitive(metadata or {}))
    except TypeError:
        try:
            return trace.span(name=name, metadata=redact_sensitive(metadata or {}))
        except Exception:
            return _NoopObservation()
    except Exception:
        return _NoopObservation()


def start_generation(
    trace: Any,
    name: str,
    *,
    model: str = "",
    input: Any | None = None,
    metadata: dict[str, Any] | None = None,
    prompt_name: str = "",
) -> Any:
    payload = _drop_none({
        "name": name,
        "model": model or None,
        "input": redact_sensitive(input),
        "metadata": redact_sensitive({**(metadata or {}), **({"prompt_name": prompt_name} if prompt_name else {})}),
    })
    try:
        return trace.generation(**payload)
    except TypeError:
        try:
            return trace.generation(name=name, model=model)
        except Exception:
            return _NoopObservation()
    except Exception:
        return _NoopObservation()


def end_observation(
    observation: Any,
    *,
    output: Any | None = None,
    status_message: str | None = None,
    usage: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    level: str | None = None,
) -> None:
    payload = _drop_none({
        "output": redact_sensitive(output),
        "status_message": redact_text(status_message) if status_message else None,
        "usage": usage,
        "metadata": redact_sensitive(metadata or {}) if metadata else None,
        "level": level,
    })
    try:
        observation.end(**payload)
        return
    except TypeError:
        pass
    except Exception:
        return
    try:
        observation.update(**payload)
        observation.end()
    except Exception:
        return


def update_trace(trace: Any, *, output: Any | None = None, metadata: dict[str, Any] | None = None) -> None:
    try:
        trace.update(**_drop_none({"output": redact_sensitive(output), "metadata": redact_sensitive(metadata or {}) if metadata else None}))
    except Exception:
        return


def score_observation(
    trace: Any,
    *,
    name: str,
    value: float | int | bool,
    comment: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    score_value = float(value) if not isinstance(value, bool) else (1.0 if value else 0.0)
    payload = _drop_none({
        "name": name,
        "value": score_value,
        "comment": redact_text(comment)[:500] if comment else None,
        "metadata": redact_sensitive(metadata or {}) if metadata else None,
    })
    try:
        trace.score(**payload)
        return
    except Exception:
        pass
    if _LANGFUSE is None:
        return
    try:  # pragma: no cover - depends on SDK/runtime.
        _LANGFUSE.score(trace_id=observation_id(trace), **payload)
    except Exception:
        return


def estimate_llm_cost_usd(usage: dict[str, Any] | None, *, model_profile_id: str = "", model: str = "") -> float:
    """Estimate cost from profile rates or env defaults; returns USD."""
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    input_rate = float(os.getenv("LLM_COST_INPUT_PER_1K", "0") or 0)
    output_rate = float(os.getenv("LLM_COST_OUTPUT_PER_1K", "0") or 0)
    if model_profile_id:
        try:
            from agents.model_registry import select_model_profile

            profile = select_model_profile(model_profile_id)
            input_rate = float(profile.cost_input_per_1k or input_rate)
            output_rate = float(profile.cost_output_per_1k or output_rate)
        except Exception:
            pass
    return round((input_tokens / 1000.0 * input_rate) + (output_tokens / 1000.0 * output_rate), 6)


def quality_score_from_diagnosis(diagnosis: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, float]:
    """Score SRE answer quality for Langfuse eval dashboards."""
    context = context or {}
    signals = diagnosis.get("signals") if isinstance(diagnosis.get("signals"), list) else []
    actions = diagnosis.get("immediate_actions") if isinstance(diagnosis.get("immediate_actions"), list) else []
    changes = diagnosis.get("proposed_changes") if isinstance(diagnosis.get("proposed_changes"), list) else []
    metadata = diagnosis.get("diagnosis_metadata") or {}
    has_root = bool(str(diagnosis.get("root_cause") or "").strip())
    has_impact = bool(str(diagnosis.get("impact") or diagnosis.get("blast_radius") or "").strip())
    confidence = _clamp(diagnosis.get("confidence", 0.0))
    evidence_count = len(signals) + len((diagnosis.get("evidence") or {}).get("events", []) if isinstance(diagnosis.get("evidence"), dict) else [])
    log_bonus = 1 if (context.get("logs") or (context.get("diagnostics") or {}).get("logs")) else 0
    evidence_completeness = _clamp((0.2 if has_root else 0) + min(0.55, evidence_count * 0.12) + (0.15 if has_impact else 0) + log_bonus * 0.1)
    actionability = _clamp(min(0.7, len(actions) * 0.12) + (0.25 if changes else 0.1 if diagnosis.get("suggested_action") in {"investigate", "answer"} else 0))
    safety_gate = 1.0
    if changes and diagnosis.get("need_human_approval") is False:
        safety_gate = 0.82
    if diagnosis.get("risk_level") in {"critical", "high"} and changes and diagnosis.get("need_human_approval") is False:
        safety_gate = 0.65
    fallback_penalty = 0.18 if metadata.get("source") == "fallback" or diagnosis.get("llm_error") else 0.0
    root_cause_quality = _clamp(confidence * 0.5 + evidence_completeness * 0.35 + (0.15 if has_root else 0) - fallback_penalty)
    overall = _clamp(root_cause_quality * 0.34 + evidence_completeness * 0.24 + actionability * 0.24 + safety_gate * 0.18)
    return {
        "overall": round(overall, 3),
        "root_cause_quality": round(root_cause_quality, 3),
        "evidence_completeness": round(evidence_completeness, 3),
        "actionability": round(actionability, 3),
        "safety_gate": round(safety_gate, 3),
    }


def trace_hierarchy_schema() -> dict[str, Any]:
    """Stable UI/PPT contract for how Flawless maps Agent calls into Langfuse."""
    return {
        "user": "operator / automation / alertmanager",
        "session": "one incident, inspection, remediation run, or model evaluation",
        "trace": "one SRE workflow execution",
        "observations": [
            {"type": "span", "name": "collect_context", "captures": "Rancher/K8s/CMDB/Prometheus evidence"},
            {"type": "generation", "name": "llm_diagnosis", "captures": "prompt, model, token usage, cost, answer"},
            {"type": "span", "name": "decision_gate", "captures": "risk gate, approval, action vocabulary"},
            {"type": "span", "name": "healing_agent", "captures": "tool call payload, dry-run, execution result"},
            {"type": "score", "name": "quality/effectiveness", "captures": "evidence, root cause, actionability, safety, recovery"},
        ],
    }


def flush() -> None:
    if _LANGFUSE is None:
        return
    try:  # pragma: no cover - depends on SDK/runtime.
        _LANGFUSE.flush()
    except Exception:
        return
