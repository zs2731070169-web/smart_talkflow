from dataclasses import dataclass
from typing import Any

from engine.client.messages import ConversationMessage


@dataclass(frozen=True)
class AssistantTurnComplete:
    """ai 完成回复"""

    message: ConversationMessage


@dataclass(frozen=True)
class AssistantTextDelta:
    """llm 输出文本片段"""

    text: str


@dataclass(frozen=True)
class ToolExecutionStarted:
    """llm 开始执行工具."""

    tool_name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True)
class ToolExecutionCompleted:
    """llm 完成工具执行."""

    tool_name: str
    output: str
    is_error: bool = False
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolProgress:
    """工具(工作流)单步执行进度(步级流式)。"""

    tool_name: str
    step_name: str
    is_error: bool
    error: str | None = None
    step_id: int | None = None
    is_compensation: bool = False


StreamEvent = AssistantTextDelta | ToolExecutionStarted | ToolExecutionCompleted | ToolProgress | AssistantTurnComplete
