"""SRE 对话与流式回答接口。"""

from ._registry import build_feature_router


def build_router(runtime):
    return build_feature_router(runtime, [
        ("POST", "/api/chat", "proxy_chat"),
        ("POST", "/api/chat/stream", "proxy_chat_stream"),
        ("POST", "/api/chat/risk-rank", "rank_chat_risks"),
    ], tag="SRE 对话")
