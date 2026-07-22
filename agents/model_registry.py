"""Pluggable AI model registry for production AIOps deployments.

The registry still supports immutable env/configmap profiles, but it also has a
small runtime store so operators can add, test, switch, and compare models from
the UI. Secret fields are kept server-side and never returned by the public
payload.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SECRET_FIELDS = {"api_key", "client_secret", "headers"}
STORE_PATH = Path(os.getenv("MODEL_PROFILES_STORE", "/tmp/flawless-model-profiles.json"))
_STORE_LOCK = threading.RLock()


@dataclass(frozen=True)
class ModelProfile:
    id: str
    provider: str
    model: str
    base_url: str
    auth_type: str = "api_key"
    role: str = "primary"
    weight: int = 100
    max_tokens: int = 4096
    cost_input_per_1k: float = 0.0
    cost_output_per_1k: float = 0.0
    enabled: bool = True
    description: str = ""
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    api_key: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    verify_ssl: bool = True
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self, redact: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if redact:
            for key in SECRET_FIELDS:
                value = data.get(key)
                if isinstance(value, dict):
                    data[key] = {item_key: "***" for item_key in value} if value else {}
                elif value:
                    data[key] = "***"
        auth_labels = {
            "oauth_client_credentials": "Token URL + client credentials",
            "none": "No auth / local endpoint",
        }
        data["auth_label"] = (
            "Base URL + runtime credential"
            if self.auth_type == "api_key"
            else auth_labels.get(self.auth_type, self.auth_type)
        )
        return data


def _bool(value: str | bool | None, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_id(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", (value or "").strip()).strip("-")
    return text[:80] or f"model-{int(datetime.now(timezone.utc).timestamp())}"


def _runtime_store_enabled() -> bool:
    return _bool(os.getenv("MODEL_PROFILE_RUNTIME_WRITE_ENABLED"), True)


def _load_runtime_store() -> dict[str, Any]:
    with _STORE_LOCK:
        if not STORE_PATH.exists():
            return {"active_profile_id": "", "profiles": []}
        try:
            data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"active_profile_id": "", "profiles": []}
            profiles = data.get("profiles")
            if not isinstance(profiles, list):
                data["profiles"] = []
            return data
        except Exception:
            return {"active_profile_id": "", "profiles": []}


def _write_runtime_store(data: dict[str, Any]) -> None:
    if not _runtime_store_enabled():
        raise RuntimeError("MODEL_PROFILE_RUNTIME_WRITE_ENABLED=false")
    with _STORE_LOCK:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE_PATH.with_suffix(STORE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(STORE_PATH)


def _dict_to_profile(item: dict[str, Any]) -> ModelProfile | None:
    if not isinstance(item, dict):
        return None
    model = item.get("model") or item.get("name")
    base_url = item.get("base_url") or item.get("api_base") or ""
    if not model or not base_url:
        return None
    provider = str(item.get("provider") or "openai-compatible")
    profile_id = _clean_id(str(item.get("id") or f"{provider}:{model}"))
    headers = item.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}
    return ModelProfile(
        id=profile_id,
        provider=provider,
        model=str(model),
        base_url=str(base_url),
        auth_type=str(item.get("auth_type") or item.get("auth") or "api_key"),
        role=str(item.get("role") or "candidate"),
        weight=int(item.get("weight") or 0),
        max_tokens=int(item.get("max_tokens") or 4096),
        cost_input_per_1k=float(item.get("cost_input_per_1k") or 0),
        cost_output_per_1k=float(item.get("cost_output_per_1k") or 0),
        enabled=_bool(item.get("enabled"), True),
        description=str(item.get("description") or ""),
        token_url=str(item.get("token_url") or item.get("oauth_token_url") or ""),
        client_id=str(item.get("client_id") or ""),
        client_secret=str(item.get("client_secret") or ""),
        api_key=str(item.get("api_key") or item.get("key") or ""),
        headers={str(k): str(v) for k, v in headers.items() if v is not None},
        verify_ssl=_bool(item.get("verify_ssl"), _bool(os.getenv("LLM_VERIFY_SSL"), True)),
        created_at=str(item.get("created_at") or ""),
        updated_at=str(item.get("updated_at") or ""),
    )


def model_profile_from_payload(payload: dict[str, Any]) -> ModelProfile | None:
    """Build a profile from an internal trusted payload."""
    return _dict_to_profile(payload)


def _profiles_from_json(raw: str) -> list[ModelProfile]:
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    profiles = []
    for item in payload:
        profile = _dict_to_profile(item)
        if profile:
            profiles.append(profile)
    return profiles


def _env_profiles() -> list[ModelProfile]:
    """Return profiles defined by env/configmap."""

    raw = os.getenv("MODEL_PROFILES_JSON", "").strip()
    profiles = _profiles_from_json(raw) if raw else []
    if profiles:
        return profiles
    return [ModelProfile(
        id=os.getenv("LLM_PROFILE_ID", "primary"),
        provider=os.getenv("LLM_PROVIDER", "openai-compatible"),
        model=os.getenv("LLM_MODEL", "qwen2.5:7b"),
        base_url=os.getenv("LLM_API_BASE") or os.getenv("LLM_GATEWAY_BASE", ""),
        auth_type=os.getenv("LLM_AUTH_TYPE", "oauth_client_credentials" if os.getenv("OAUTH_TOKEN_URL") else "api_key"),
        role="primary",
        weight=100,
        max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
        description="Default production diagnosis model",
        token_url=os.getenv("OAUTH_TOKEN_URL", ""),
        client_id=os.getenv("OAUTH_CLIENT_ID", ""),
        client_secret=os.getenv("OAUTH_CLIENT_SECRET", ""),
        api_key=os.getenv("LLM_API_KEY", ""),
        verify_ssl=_bool(os.getenv("LLM_VERIFY_SSL"), True),
    )]


def get_model_profiles(include_disabled: bool = False) -> list[ModelProfile]:
    """Return model profiles from env plus runtime UI store.

    MODEL_PROFILES_JSON example:
    [
      {"id":"primary","provider":"oauth-gateway","model":"your-model",
       "base_url":"https://gateway/engines/x","auth_type":"oauth_client_credentials",
       "role":"primary","weight":100}
    ]
    """
    merged: dict[str, ModelProfile] = {profile.id: profile for profile in _env_profiles()}
    for item in _load_runtime_store().get("profiles", []):
        profile = _dict_to_profile(item)
        if profile:
            merged[profile.id] = profile
    profiles = list(merged.values())
    if not include_disabled:
        profiles = [p for p in profiles if p.enabled]
    return profiles


def get_active_model_profile_id() -> str:
    env_active = os.getenv("ACTIVE_MODEL_PROFILE_ID") or os.getenv("MODEL_ACTIVE_PROFILE_ID")
    if env_active:
        return env_active
    store_active = str(_load_runtime_store().get("active_profile_id") or "")
    if store_active:
        return store_active
    return os.getenv("LLM_PROFILE_ID", "")


def select_model_profile(profile_id: str | None = None, role: str = "primary") -> ModelProfile:
    profiles = get_model_profiles()
    selected_id = profile_id or get_active_model_profile_id()
    if selected_id:
        for profile in profiles:
            if profile.id == selected_id:
                return profile
    role_matches = [p for p in profiles if p.role == role]
    if role_matches:
        return sorted(role_matches, key=lambda p: p.weight, reverse=True)[0]
    if profiles:
        return sorted(profiles, key=lambda p: p.weight, reverse=True)[0]
    raise RuntimeError("No enabled model profiles configured")


def _existing_runtime_profile(profile_id: str) -> dict[str, Any] | None:
    for item in _load_runtime_store().get("profiles", []):
        if isinstance(item, dict) and str(item.get("id")) == profile_id:
            return item
    return None


def upsert_model_profile(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("profile payload must be an object")
    existing_id = str(payload.get("id") or "")
    existing = _existing_runtime_profile(existing_id) if existing_id else None
    now = _now()
    merged = {**(existing or {}), **payload}
    for key in ("api_key", "client_secret"):
        if payload.get(key) in {"", "***", None} and existing:
            merged[key] = existing.get(key, "")
    headers_value = payload.get("headers")
    if (headers_value is None or headers_value == "" or headers_value == "***") and existing:
        merged["headers"] = existing.get("headers", {})
    merged.setdefault("created_at", (existing or {}).get("created_at") or now)
    merged["updated_at"] = now
    profile = _dict_to_profile(merged)
    if not profile:
        raise ValueError("model and base_url are required")
    data = _load_runtime_store()
    profiles = [item for item in data.get("profiles", []) if isinstance(item, dict) and str(item.get("id")) != profile.id]
    profiles.append(profile.to_dict(redact=False))
    data["profiles"] = profiles
    if payload.get("set_active") or not data.get("active_profile_id"):
        data["active_profile_id"] = profile.id
    _write_runtime_store(data)
    return profile.to_dict(redact=True)


def set_active_model_profile(profile_id: str) -> dict[str, Any]:
    profile = select_model_profile(profile_id)
    data = _load_runtime_store()
    data["active_profile_id"] = profile.id
    _write_runtime_store(data)
    return profile.to_dict(redact=True)


def delete_model_profile(profile_id: str) -> dict[str, Any]:
    data = _load_runtime_store()
    before = len(data.get("profiles", []))
    data["profiles"] = [item for item in data.get("profiles", []) if isinstance(item, dict) and str(item.get("id")) != profile_id]
    if len(data["profiles"]) == before:
        raise KeyError(profile_id)
    if data.get("active_profile_id") == profile_id:
        data["active_profile_id"] = ""
    _write_runtime_store(data)
    return {"status": "deleted", "id": profile_id}


def registry_payload() -> dict[str, Any]:
    profiles = [p.to_dict(redact=True) for p in get_model_profiles(include_disabled=True)]
    enabled = [p for p in profiles if p.get("enabled", True)]
    return {
        "status": "ok" if enabled else "empty",
        "active_profile_id": get_active_model_profile_id() or (enabled[0]["id"] if enabled else ""),
        "runtime_store": {
            "enabled": _runtime_store_enabled(),
            "path": str(STORE_PATH),
        },
        "profiles": profiles,
        "routing": {
            "strategy": os.getenv("MODEL_ROUTING_STRATEGY", "primary-with-shadow-eval"),
            "shadow_eval_enabled": _bool(os.getenv("MODEL_SHADOW_EVAL_ENABLED"), False),
            "judge_model": os.getenv("MODEL_JUDGE_PROFILE_ID", ""),
        },
    }
