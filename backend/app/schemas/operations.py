"""拓扑、巡检、告警与受控运维接口的数据模型。"""

from pydantic import BaseModel, Field, SecretStr


class KubeconfigContextsRequest(BaseModel):
    kubeconfig: str = Field(min_length=20, max_length=2_000_000)


class ManagedClusterUpsertRequest(KubeconfigContextsRequest):
    name: str = Field(default="", max_length=120)
    context: str = Field(default="", max_length=253)
    cluster_id: str = Field(default="", max_length=128)
    bearer_token: SecretStr | None = Field(default=None, max_length=200_000)


class ManagedClusterTokenRefreshRequest(BaseModel):
    bearer_token: SecretStr = Field(min_length=1, max_length=200_000)


class MCPToolRequest(BaseModel):
    tool: str
    arguments: dict = Field(default_factory=dict)


class AlertScanRequest(BaseModel):
    intent: str
    cluster: str = "all"
    namespace: str = "default"
    severity: str = "auto"
    auto_healing_enabled: bool = False


class TopologyImpactRequest(BaseModel):
    selected: dict
    graph: dict = Field(default_factory=dict)
    scenario: str = "pod_change"


class ExternalTrafficFlowRequest(BaseModel):
    """外部/跨集群数据流查询条件。"""

    cluster: str = "all"
    cluster_id: str = ""
    namespace: str = "all"
    workload: str = ""
    window: str = "30m"
    source: str = "auto"
    include_static_inference: bool = True
    include_cmdb: bool = True


class ReleaseGateRequest(BaseModel):
    change: dict = Field(default_factory=dict)
    graph: dict = Field(default_factory=dict)
    runtime: dict = Field(default_factory=dict)
    history: list[dict] = Field(default_factory=list)
    candidates: list[dict] = Field(default_factory=list)
    observation: dict = Field(default_factory=dict)


class OpsExecuteRequest(BaseModel):
    plan: dict
    confirm: bool = False


class OpsJobCreateRequest(BaseModel):
    plan: dict
    confirm: bool = False
    autonomous: bool = False
    high_risk_confirmed: bool = False
    operator_force_execute: bool = False
    allow_high_risk_after_confirmation: bool = False
    operator_override_reason: str = ""
    stepwise_confirmation: bool = False


class OpsStepApprovalRequest(BaseModel):
    change_index: int = Field(ge=1)
    approval_id: str = Field(min_length=8, max_length=128)
    change_fingerprint: str = Field(min_length=16, max_length=128)
    confirm: bool = True
    comment: str = ""


class OpsSkillScriptPolicy(BaseModel):
    """Skill 的可审批脚本策略。"""

    enabled: bool = False
    script_id: str = ""
    trigger_conditions: list[str] = Field(default_factory=list)
    trigger_description: str = ""
    timeout_seconds: int = Field(default=120, ge=10, le=600)
    require_confirmation: bool = True


class OpsSkillDefinition(BaseModel):
    """运维人员确认后发布的可匹配、可执行 Skill。"""

    id: str = ""
    name: str
    category: str = "custom"
    summary: str = ""
    symptoms: list[str] = Field(default_factory=list)
    applies_to: list[str] = Field(default_factory=list)
    evidence_required: list[str] = Field(default_factory=list)
    diagnostic_steps: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    risk: str = "medium"
    rollback: str = ""
    owner: str = ""
    enabled: bool = True
    script_policy: OpsSkillScriptPolicy = Field(default_factory=OpsSkillScriptPolicy)


class OpsSkillMatchRequest(BaseModel):
    """用于单独测试 AI/规则是否能把问题匹配到正确 Skill。"""

    question: str = ""
    alert: dict = Field(default_factory=dict)
    evidence: dict = Field(default_factory=dict)
    cluster: str = "all"
    namespace: str = "all"
    workload: str = ""
    top_k: int = 5


class InspectionRequest(BaseModel):
    auto_ops: bool = False
    cluster: str = "all"
    namespace: str = "all"
    model_profile_id: str = ""
    production_mode: bool = False


class InspectionPreviewRequest(BaseModel):
    """基于最近一次巡检 finding 重新采集实时证据并生成预演。"""

    finding_id: str
    model_profile_id: str = ""


class InfrastructureScanRequest(BaseModel):
    """数据库、虚拟机、中间件等非 K8s 基础设施巡检请求。"""

    resource_type: str = "all"
    resource_id: str = ""
    model_profile_id: str = ""
    production_mode: bool = True
    include_probe: bool = True
