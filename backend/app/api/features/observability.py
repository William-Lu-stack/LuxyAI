"""LLM 全链路观测、Loki、Tempo 与外部集成接口。"""

from ._registry import build_feature_router


def build_router(runtime):
    return build_feature_router(runtime, [
        ("GET", "/api/llm-observability", "llm_observability"),
        ("GET", "/api/integrations", "integrations_status"),
        ("POST", "/api/integrations/notify/test", "test_collaboration_notification"),
        ("POST", "/api/observability/logs", "query_loki_logs"),
        ("GET", "/api/observability/traces", "query_tempo_traces"),
        ("GET", "/api/dashboard", "dashboard"),
    ], tag="可观测与集成")

