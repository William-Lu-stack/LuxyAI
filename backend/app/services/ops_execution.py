"""长时间运维任务使用的有界异步执行、心跳与超时原语。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


class StageTimeoutError(TimeoutError):
    """单个可观测执行阶段超过硬超时时间。"""

    def __init__(self, stage: str, timeout_seconds: float):
        self.stage = stage
        self.timeout_seconds = timeout_seconds
        super().__init__(f"stage '{stage}' exceeded {timeout_seconds:.0f}s hard timeout")


async def run_with_heartbeat(
    operation: Awaitable[Any],
    *,
    stage: str,
    timeout_seconds: float,
    heartbeat_seconds: float = 5.0,
    cancel_event: asyncio.Event | None = None,
    on_heartbeat: Callable[[float, float], Awaitable[None]] | None = None,
) -> Any:
    """Run one stage with a deadline and visible liveness heartbeats.

    A stage can no longer leave an operation job permanently in ``running``.
    Cancellation and timeout both cancel the child task and wait for cleanup.
    """

    timeout_seconds = max(0.05, float(timeout_seconds))
    heartbeat_seconds = max(0.01, min(float(heartbeat_seconds), timeout_seconds))
    started = time.monotonic()
    task = asyncio.create_task(operation)
    try:
        while True:
            if cancel_event and cancel_event.is_set():
                task.cancel()
                raise asyncio.CancelledError
            elapsed = time.monotonic() - started
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                task.cancel()
                raise StageTimeoutError(stage, timeout_seconds)
            done, _ = await asyncio.wait({task}, timeout=min(heartbeat_seconds, remaining))
            if task in done:
                return task.result()
            if on_heartbeat:
                await on_heartbeat(time.monotonic() - started, max(0.0, timeout_seconds - (time.monotonic() - started)))
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
