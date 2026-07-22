from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class A2AMessage(BaseModel):
    id: str
    source_agent: str
    target_agent: str
    task_type: str
    payload: dict[str, Any]
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    correlation_id: Optional[str] = None


class A2AResponse(BaseModel):
    id: str
    source_agent: str
    target_agent: str
    status: Literal["accepted", "running", "completed", "failed"]
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
