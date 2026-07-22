"""知识库导入、重建和问答接口的数据模型。"""

from pydantic import BaseModel, Field


class KnowledgeAskRequest(BaseModel):
    question: str
    domain: str = "app"
    model_profile_id: str = ""
    include_principle: bool = False
    use_vector: bool = True


class KnowledgeDocumentRequest(BaseModel):
    title: str = ""
    content: str
    domain: str = "auto"
    tags: list[str] | str = Field(default_factory=list)
    source: str = "ui"
    document_type: str = "text"
    embed: bool = True


class KnowledgeReindexRequest(BaseModel):
    domain: str = "all"
    document_id: str = ""
    force: bool = False

