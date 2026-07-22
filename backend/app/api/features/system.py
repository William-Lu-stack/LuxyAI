"""系统入口、健康检查与平台自愈接口。"""

from ._registry import build_feature_router


def build_router(runtime):
    return build_feature_router(runtime, [
        ("GET", "/api/build", "build_info"),
        ("GET", "/api/session", "console_session"),
        ("GET", "/", "index"),
        ("GET", "/legacy", "legacy_index"),
        ("GET", "/favicon.svg", "favicon"),
        ("GET", "/health", "process_health"),
        ("GET", "/api/health", "health"),
        ("GET", "/api/llm/health", "proxy_llm_health"),
        ("GET", "/api/aiops/status", "aiops_status"),
        ("GET", "/api/platform/resilience", "platform_resilience"),
        ("GET", "/api/platform/self-heal/status", "platform_self_heal_status"),
        ("POST", "/api/platform/self-heal/run", "platform_self_heal_run"),
        ("GET", "/api/cloud/adapters", "cloud_adapters"),
        ("GET", "/api/effectiveness", "ai_effectiveness"),
    ], tag="系统状态")
