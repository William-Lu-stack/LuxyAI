"""可插拔模型与运维能力测评接口的数据模型。"""

from pydantic import BaseModel, Field


class ModelProfileUpsertRequest(BaseModel):
    id: str = ""
    provider: str = "openai-compatible"
    model: str
    base_url: str
    auth_type: str = "api_key"
    role: str = "candidate"
    weight: int = 50
    max_tokens: int = 4096
    cost_input_per_1k: float = 0.0
    cost_output_per_1k: float = 0.0
    enabled: bool = True
    description: str = ""
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    api_key: str = ""
    headers: dict = Field(default_factory=dict)
    verify_ssl: bool = True
    set_active: bool = False


class ModelProfileActiveRequest(BaseModel):
    profile_id: str


class ModelBenchmarkRequest(BaseModel):
    model_profile_ids: list[str] = Field(default_factory=list)
    cluster: str = "all"
    namespace: str = "all"
    prompt: str = ""
    include_latest_findings: bool = True

