from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Protocol

from engine.client.messages import ConversationMessage


@dataclass(frozen=True)
class ApiMessageRequest:
    """统一的 llm 调用入参"""

    model: str  # 使用的模型
    messages: list[ConversationMessage]  # 用户/ai消息列表
    system_prompt: str | None = None  # 提示词
    max_tokens: int = 4096
    tools: list[dict[str, Any]] = field(default_factory=list)  # 工具源信息


@dataclass(frozen=True)
class ApiTextDeltaEvent:
    """模型输出的文本片段."""

    text: str


@dataclass(frozen=True)
class ApiMessageCompleteEvent:
    """统一的 llm 调用响应,屏蔽底层 sdk 差异"""

    message: ConversationMessage  # ai回复的文本内容


ApiStreamEvent = ApiTextDeltaEvent | ApiMessageCompleteEvent


class SupportsStreamingMessages(Protocol):
    """llm 客户端协议,所有 llm 调用统一通过该接口"""

    def stream_message(self, request: ApiMessageRequest) -> AsyncGenerator[ApiStreamEvent, None]:
        """调用 llm 流式返回统一结构化响应"""
        ...
