from typing import Literal, Any

from pydantic import Field, field_validator

from openai import BaseModel

class ConversationMessage(BaseModel):
    """A single assistant or user message."""

    role: Literal["user", "assistant"] # 角色
    content: list[str] = Field(default_factory=list) # 用户查询/ai回答

    @field_validator("content", mode="before")
    @classmethod
    def _normalize_content(cls, value: Any) -> list[Any]:
        """llm返回内容为空直接返回[]"""
        if value is None:
            return []
        return value
