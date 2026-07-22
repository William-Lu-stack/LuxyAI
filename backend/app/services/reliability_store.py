"""SLO 目标与发布审计记录的轻量持久化仓库。"""

from __future__ import annotations

import json
import os
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.app.domain.slo import default_objective, evaluate_error_budget


class ReliabilityStore:
    def __init__(self, path: str | None = None):
        self.primary_path = Path(path or os.getenv("RELIABILITY_STORE_PATH", "/var/lib/flawless/reliability-state.json"))
        self.path = self.primary_path
        self.fallback_path = Path(os.getenv("RELIABILITY_STORE_FALLBACK_PATH", "/tmp/flawless/reliability-state.json"))
        self.emergency_path = Path(os.getenv("RELIABILITY_STORE_EMERGENCY_PATH", "/dev/shm/flawless/reliability-state.json"))
        self.loaded_from: Path | None = None
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"objectives": {}, "releases": []}
        self._load()

    def _load(self) -> None:
        with self._lock:
            for candidate in (self.primary_path, self.fallback_path, self.emergency_path):
                try:
                    payload = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        self._data["objectives"] = payload.get("objectives") or {}
                        self._data["releases"] = payload.get("releases") or []
                        self.loaded_from = candidate
                        return
                except FileNotFoundError:
                    continue
                except Exception:
                    continue

    def _save(self) -> None:
        def write_to(path: Path) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)

        errors: list[str] = []
        candidates = list(dict.fromkeys((self.primary_path, self.fallback_path, self.emergency_path)))
        for candidate in candidates:
            try:
                write_to(candidate)
                self.path = candidate
                return
            except OSError as exc:
                errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
        raise OSError("all reliability audit paths are unavailable; " + " | ".join(errors))

    def storage_status(self) -> dict[str, Any]:
        return {
            "active_path": str(self.path),
            "primary_path": str(self.primary_path),
            "fallback_path": str(self.fallback_path),
            "emergency_path": str(self.emergency_path),
            "loaded_from": str(self.loaded_from) if self.loaded_from else "",
            "durable": self.path == self.primary_path,
        }

    def objectives(self) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._data["objectives"].values())
            if not values:
                values = [default_objective()]
            return [{**deepcopy(item), "budget": evaluate_error_budget(item)} for item in values]

    def upsert_objective(self, value: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            service = str(value.get("service") or "").strip()
            if not service:
                raise ValueError("service is required")
            objective_id = str(value.get("id") or f"{value.get('cluster','all')}:{value.get('namespace','all')}:{service}")
            item = {**value, "id": objective_id, "service": service, "updated_at": datetime.now(timezone.utc).isoformat()}
            self._data["objectives"][objective_id] = item
            self._save()
            return {**deepcopy(item), "budget": evaluate_error_budget(item)}

    def objective_for(self, service: str, cluster: str = "all", namespace: str = "all") -> dict[str, Any]:
        with self._lock:
            values = list(self._data["objectives"].values())
            exact = next((item for item in values if item.get("service") == service and item.get("cluster", "all") in {cluster, "all"} and item.get("namespace", "all") in {namespace, "all"}), None)
            return deepcopy(exact or default_objective(service))

    def add_release(self, value: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            item = {**value, "id": value.get("id") or f"rel-{uuid.uuid4().hex[:12]}", "created_at": now, "updated_at": now}
            self._data["releases"].append(item)
            self._data["releases"] = self._data["releases"][-500:]
            self._save()
            return deepcopy(item)

    def releases(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(list(reversed(self._data["releases"])))

    def release(self, release_id: str) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(next((item for item in self._data["releases"] if item.get("id") == release_id), None))

    def update_release(self, release_id: str, **values: Any) -> dict[str, Any] | None:
        with self._lock:
            item = next((item for item in self._data["releases"] if item.get("id") == release_id), None)
            if not item:
                return None
            item.update(values)
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save()
            return deepcopy(item)
