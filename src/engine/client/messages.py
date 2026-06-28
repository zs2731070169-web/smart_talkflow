from typing import Annotated, Any, Literal
from uuid import uuid4

from openai import BaseModel
from pydantic import Field, field_validator


class ToolUseBlock(BaseModel):
    """llm 输出执行一个工具的请求"""

    type: Literal["tool_use"] = "tool_use"
    id: str = Field(default_factory=lambda: f"tool_use_{uuid4().hex}")
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """工具执行结果返回给 llm"""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False
    result_metadata: dict[str, Any] = Field(default_factory=dict)


class TextBlock(BaseModel):
    """文本内容."""

    type: Literal["text"] = "text"
    text: str


# discriminator="type" 用  type  字段的值来区分当前数据应该匹配联合类型中的哪个子模型
ContentBlock = Annotated[TextBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")]


class ConversationMessage(BaseModel):
    """assistant 或 user 消息."""

    role: Literal["user", "assistant"]  # 角色
    # 如果创建对象时没有传  content  参数，自动调用 list() 生成一个独立的的空列表实例，互不干扰
    content: list[ContentBlock] = Field(default_factory=list)  # 用户查询/ai回答

    @field_validator("content", mode="before")
    @classmethod
    def _normalize_content(cls, value: Any) -> list[Any]:
        """llm返回内容为空直接返回[]"""
        if value is None:
            return []
        return value

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        """返回所有工具调用"""
        return [block for block in self.content if isinstance(block, ToolUseBlock)]
