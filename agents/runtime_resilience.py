"""Runtime resilience primitives for high-concurrency AIOps services."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import os
import time
from typing import Any


class BulkheadRejected(RuntimeError):
    """Raised when a concurrency bulkhead cannot admit more work."""


class AsyncBulkhead:
    """Bound concurrent work and fail fast instead of exhausting the process."""

    def __init__(self, limit: int, acquire_timeout: float = 1.5):
        self.limit = max(1, int(limit or 1))
        self.acquire_timeout = max(0.05, float(acquire_timeout or 1.5))
        self._sem = asyncio.Semaphore(self.limit)
        self.in_flight = 0
        self.rejected = 0

    @asynccontextmanager
    async def slot(self):
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.acquire_timeout)
        except asyncio.TimeoutError as exc:
            self.rejected += 1
            raise BulkheadRejected(f"bulkhead full: limit={self.limit}") from exc
        self.in_flight += 1
        try:
            yield
        finally:
            self.in_flight -= 1
            self._sem.release()

    def snapshot(self) -> dict[str, Any]:
        return {"limit": self.limit, "in_flight": self.in_flight, "rejected": self.rejected}


class TTLCache:
    """Small async-safe TTL cache for expensive inventory calls."""

    def __init__(self, ttl_seconds: int = 30, max_items: int = 128):
        self.ttl_seconds = max(1, int(ttl_seconds or 30))
        self.max_items = max(1, int(max_items or 128))
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str):
        async with self._lock:
            item = self._items.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at < time.time():
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None):
        async with self._lock:
            ttl = self.ttl_seconds if ttl_seconds is None else max(1, int(ttl_seconds))
            self._items[key] = (time.time() + ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

    async def invalidate(self, prefix: str = ""):
        async with self._lock:
            if not prefix:
                self._items.clear()
            else:
                for key in list(self._items.keys()):
                    if key.startswith(prefix):
                        self._items.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        live = sum(1 for expires_at, _ in self._items.values() if expires_at >= now)
        return {"ttl_seconds": self.ttl_seconds, "max_items": self.max_items, "live_items": live}


def bounded_append(store: list[dict], item: dict, max_items: int):
    store.append(item)
    overflow = len(store) - max(1, int(max_items or 1))
    if overflow > 0:
        del store[:overflow]


@dataclass
class SelfHealDecision:
    status: str
    severity: str
    reason: str
    actions: list[dict[str, Any]]
    requires_confirmation: bool = True
    cooldown_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _core_services() -> set[str]:
    raw = os.getenv("PLATFORM_CORE_SERVICES", "observability,healing,incident,postmortem,adapter,mcp")
    return {x.strip() for x in raw.split(",") if x.strip()}


def build_self_heal_decision(health_payload: dict[str, Any], last_action_at: float = 0.0) -> dict[str, Any]:
    """Build a guarded plan for the platform to repair its own agent stack."""
    services = health_payload.get("services") or {}
    core = _core_services()
    down = [
        {"name": name, **value}
        for name, value in services.items()
        if name in core and isinstance(value, dict) and value.get("status") != "up"
    ]
    cooldown_seconds = int(os.getenv("PLATFORM_SELF_HEAL_COOLDOWN_SECONDS", "300"))
    cooldown_active = bool(last_action_at and (time.time() - last_action_at) < cooldown_seconds)
    namespace = os.getenv("PLATFORM_NAMESPACE", os.getenv("POD_NAMESPACE", "k8s-agent"))
    workload = os.getenv("PLATFORM_WORKLOAD_NAME", "flawless")
    actions: list[dict[str, Any]] = []

    for item in down:
        actions.append({
            "type": "diagnose_service",
            "service": item["name"],
            "reason": item.get("error", "")[:500],
            "evidence_url": item.get("url") or item.get("configured_url"),
        })

    if down and not cooldown_active:
        actions.append({
            "type": "restart",
            "namespace": namespace,
            "workload_type": "Deployment",
            "workload_name": workload,
            "reason": "one or more core agents are unhealthy; restart the platform deployment after approval",
            "patch": {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "<now>"}}}}},
        })

    if not down:
        decision = SelfHealDecision(
            status="healthy",
            severity="P3",
            reason="all core platform services are healthy",
            actions=[],
            requires_confirmation=False,
        )
    elif cooldown_active:
        decision = SelfHealDecision(
            status="hold",
            severity="P2",
            reason=f"core services unhealthy but self-heal cooldown is active ({cooldown_seconds}s)",
            actions=actions,
            cooldown_active=True,
        )
    else:
        severity = "P1" if len(down) >= 2 else "P2"
        decision = SelfHealDecision(
            status="repairable",
            severity=severity,
            reason=f"{len(down)} core service(s) unhealthy: {', '.join(x['name'] for x in down)}",
            actions=actions,
        )

    payload = decision.to_dict()
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    payload["core_services"] = sorted(core)
    payload["unhealthy_services"] = down
    payload["self_heal_enabled"] = os.getenv("PLATFORM_SELF_HEAL_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    return payload
