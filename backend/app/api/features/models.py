"""可插拔模型注册、连接测试与运维能力测评接口。"""

from ._registry import build_feature_router


def build_router(runtime):
    return build_feature_router(runtime, [
        ("GET", "/api/model-registry", "model_registry"),
        ("POST", "/api/model-registry", "save_model_profile"),
        ("POST", "/api/model-registry/active", "activate_model_profile"),
        ("DELETE", "/api/model-registry/{profile_id}", "remove_model_profile"),
        ("POST", "/api/model-registry/{profile_id}/test", "test_model_profile"),
        ("POST", "/api/model-benchmark/run", "run_model_benchmark"),
        ("GET", "/api/model-benchmark", "model_benchmark"),
    ], tag="模型实验室")

