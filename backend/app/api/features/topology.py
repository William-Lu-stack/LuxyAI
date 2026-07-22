"""拓扑影响、爆炸半径与灰度发布门禁接口。"""

from ._registry import build_feature_router


def build_router(runtime):
    return build_feature_router(runtime, [
        ("POST", "/api/topology/impact", "analyze_topology_impact"),
        ("POST", "/api/network/external-flows", "external_traffic_flows"),
        ("POST", "/api/release-gate/evaluate", "evaluate_gray_release_gate"),
    ], tag="拓扑与变更风险")
