"""API 请求 / 响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """``POST /chat`` 请求。"""

    user_input: str = Field(description="用户自然语言输入")