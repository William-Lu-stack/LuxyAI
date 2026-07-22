"""事件、告警、复盘和 Agent 调用记录接口。"""

from ._registry import build_feature_router


def build_router(runtime):
    return build_feature_router(runtime, [
        ("GET", "/api/incidents", "list_incidents"),
        ("POST", "/api/incidents", "create_incident"),
        ("GET", "/api/postmortems", "list_postmortems"),
        ("GET", "/api/alerts", "list_alerts"),
        ("GET", "/api/traces", "list_traces"),
    ], tag="事件与复盘")

