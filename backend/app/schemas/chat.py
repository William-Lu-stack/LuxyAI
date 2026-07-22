"""SRE 对话接口的数据模型。"""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str
    original_message: str = ""
    model_profile_id: str = ""
    cluster: str = "all"
    cluster_id: str = "all"
    namespace: str = "default"
    deployment: str = ""
    workload_type: str = "Deployment"
    pod: str = ""
    target_id: str = ""
    severity: str = "P2"
    auto_healing_enabled: bool = False


class ChatResponse(BaseModel):
    answer: str
    raw: dict = Field(default_factory=dict)
    postmortem: dict | None = None


class ChatRiskRankRequest(BaseModel):
    risks: list[dict] = Field(default_factory=list)
    cluster: str = "all"
    namespace: str = "all"
    model_profile_id: str = ""
