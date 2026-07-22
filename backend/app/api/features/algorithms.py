"""算法目录、实时输入输出和决策证据接口。"""

from ._registry import build_feature_router


def build_router(runtime):
    return build_feature_router(runtime, [
        ("GET", "/api/algorithms", "algorithm_registry"),
        ("GET", "/api/algorithms/workbench", "algorithm_workbench"),
    ], tag="算法决策")

