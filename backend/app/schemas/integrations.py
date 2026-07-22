"""协作通知接口的数据模型。"""

from pydantic import BaseModel


class CollaborationNotificationRequest(BaseModel):
    channel: str
    message: str = "Flawless 通道连通性测试：配置有效，可以接收告警、诊断和审批通知。"

