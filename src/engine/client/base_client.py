from typing import Protocol
from dataclasses import dataclass, field
from typing import Any

from engine.messages import ConversationMessage


@dataclass(frozen=True)
class ApiMessageRequest:
    """统一的 llm 调用入参"""

    model: str # 使用的模型
    message: ConversationMessage # 用户/ai消息
    system_prompt: str | None = None # 提示词
    max_tokens: int = 4096
    workflows: list[dict[str, Any]] = field(default_factory=list) # 工作流源信息


@dataclass(frozen=True)
class ApiMessageResponse:
    """统一的 llm 调用响应,屏蔽底层 sdk 差异"""

    content: str # ai回复的文本内容
    model: str # 使用的模型
    finish_reason: str | None = None # llm执行完成时返回的任务标识
    workflow_calls: list[dict[str, Any]] = field(default_factory=list) # 调用的工作流列表
    raw: Any = None # ai回复的原始消息


class SupportsInvokeMessages(Protocol):
    """llm 客户端协议,所有 llm 调用统一通过该接口"""

    async def ainvoke_message(self, request: ApiMessageRequest) -> ApiMessageResponse:
        """调用 llm 返回统一结构化响应"""
        ...
