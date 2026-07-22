"""功能路由注册工具。

迁移期间复用经过生产验证的处理函数，仅把 URL 所有权拆到独立文件。
后续移动业务实现时，只需替换 runtime 中同名处理函数，不改变前端契约。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastapi import APIRouter


RouteSpec = tuple[str, str, str]


def build_feature_router(
    runtime: Mapping[str, Any],
    routes: Sequence[RouteSpec],
    *,
    tag: str,
) -> APIRouter:
    """根据功能清单注册路由，并在启动时校验处理函数是否存在。"""
    router = APIRouter(tags=[tag])
    for method, path, handler_name in routes:
        handler: Callable[..., Any] | None = runtime.get(handler_name)
        if handler is None:
            raise RuntimeError(f"功能路由 {tag} 缺少处理函数：{handler_name}")
        router.add_api_route(path, handler, methods=[method])
    return router

