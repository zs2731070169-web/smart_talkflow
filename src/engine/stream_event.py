from dataclasses import dataclass
from typing import Any


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


StreamEvent = AssistantTextDelta | ToolExecutionStarted | ToolExecutionCompleted
