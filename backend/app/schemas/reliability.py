"""SLO、错误预算和发布治理接口的数据契约。"""

from typing import Any

from pydantic import BaseModel, Field


class ObjectiveRequest(BaseModel):
    """创建或更新服务可用性目标。"""

    id: str = ""
    service: str = Field(min_length=1, max_length=160)
    cluster: str = "all"
    namespace: str = "all"
    target_percent: float = Field(default=99.9, ge=50.0, lt=100.0)
    window_days: int = Field(default=30, ge=1, le=365)
    observed_availability_percent: float = Field(default=100.0, ge=0.0, le=100.0)
    observed_minutes: float = Field(default=43200, gt=0)
    downtime_minutes: float | None = Field(default=None, ge=0)
    total_requests: float = Field(default=0, ge=0)
    failed_requests: float = Field(default=0, ge=0)


class ReleaseRequest(BaseModel):
    """提交生产发布风险判定所需的完整变更信息。"""

    service: str = Field(min_length=1, max_length=160)
    cluster: str = Field(default="local", max_length=160)
    namespace: str = Field(default="default", max_length=160)
    workload_kind: str = Field(default="Deployment", pattern="^(Deployment|StatefulSet|DaemonSet)$")
    workload_name: str = Field(default="", max_length=253)
    release_mode: str = Field(default="existing", pattern="^(existing|new)$")
    change_channel: str = Field(default="standard", pattern="^(standard|emergency_recovery)$")
    emergency_action: str = Field(default="", pattern="^(|rollback|restore_config|restart_component)$")
    emergency_reason: str = Field(default="", max_length=2000)
    container_name: str = Field(default="", max_length=253)
    image: str = Field(default="", max_length=1000)
    change_summary: str = Field(default="", max_length=2000)
    manifest_yaml: str = Field(default="", max_length=120000)
    patch: dict[str, Any] = Field(default_factory=dict)
    graph: dict[str, Any] = Field(default_factory=dict)
    history: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    observation: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    """确认或拒绝已经完成风险判定的发布。"""

    confirm: bool = False
    comment: str = Field(default="", max_length=1000)
