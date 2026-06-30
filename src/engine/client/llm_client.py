import json
from collections.abc import AsyncGenerator
from typing import Any

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from engine.client.base_client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiStreamEvent,
    ApiTextDeltaEvent,
)
from engine.client.messages import ConversationMessage, ToolUseBlock


class OpenAIClient:
    """openai 兼容协议的 llm 客户端"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: int = 120):
        if not api_key:
            raise ValueError("OPENAI_API_KEY 未配置")
        if not base_url:
            raise ValueError("OPENAI_API_BASE 未配置")
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    async def stream_message(self, request: ApiMessageRequest) -> AsyncGenerator[ApiStreamEvent, None]:
        # 组装 messages:system_prompt + 当前会话消息
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        for msg in request.messages:
            text = "".join(block.text for block in msg.content if block.type == "text")
            messages.append({"role": msg.role, "content": text})

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "stream": True,
        }

        # 工具 透传为 OpenAI tools(为空则不传)
        if request.tools:
            kwargs["tools"] = request.tools

        # 流式拉取:逐 chunk 吐出文本片段;
        # tool_calls 为增量协议,按 index 聚合 id / name / arguments
        tool_calls: dict[int, dict[str, Any]] = {}
        async with await self._client.chat.completions.create(**kwargs) as stream:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta.content:
                    yield ApiTextDeltaEvent(text=delta.content)

                for piece in delta.tool_calls or []:
                    slot = tool_calls.setdefault(piece.index, {"id": "", "name": "", "arguments": ""})
                    if piece.id:
                        slot["id"] = piece.id
                    if piece.function:
                        if piece.function.name:
                            slot["name"] = piece.function.name
                        if piece.function.arguments:
                            slot["arguments"] += piece.function.arguments

        # 聚合结果映射为统一的 TextBlock / ToolUseBlock,屏蔽 sdk 差异
        blocks: list[Any] = []
        for slot in tool_calls.values():
            blocks.append(
                ToolUseBlock(
                    id=slot["id"],
                    name=slot["name"],
                    input=json.loads(slot["arguments"] or "{}"),
                )
            )

        yield ApiMessageCompleteEvent(message=ConversationMessage(role="assistant", content=blocks))


class AnthropicApiClient:
    """anthropic 兼容协议的 llm 客户端"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: int = 120):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY 未配置")
        self._client = AsyncAnthropic(api_key=api_key, base_url=base_url, timeout=timeout)

    @staticmethod
    def _build_messages(request: ApiMessageRequest) -> tuple[str | None, list[dict[str, Any]]]:
        """统一对话结构, 三类 content block 映射到 text / tool_use / tool_result。"""

        def build_block(block: Any) -> dict[str, Any]:
            # 用户消息
            if block.type == "text":
                return {"type": "text", "text": block.text}
            # 请求工具调用
            if block.type == "tool_use":
                return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            # 工具调用结果
            return {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content,
            }

        messages = [
            {"role": msg.role, "content": [build_block(block) for block in msg.content]}
            for msg in request.messages
            if msg.content  # 跳过空内容消息
        ]
        return request.system_prompt, messages

    async def stream_message(self, request: ApiMessageRequest) -> AsyncGenerator[ApiStreamEvent, None]:
        # 构建系统提示词和消息列表
        system_prompt, messages = self._build_messages(request)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if request.tools:
            kwargs["tools"] = request.tools

        # 流式拉取:text_stream 逐片段吐出,get_final_message 聚合完整响应(含 tool_use)
        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield ApiTextDeltaEvent(text=text)
            final_message = await stream.get_final_message()

        # tool_use 块映射为 ToolUseBlock, 屏蔽 sdk 差异
        blocks: list[Any] = []
        for block in final_message.content:
            if block.type == "tool_use":
                blocks.append(ToolUseBlock(id=block.id, name=block.name, input=block.input))

        yield ApiMessageCompleteEvent(message=ConversationMessage(role="assistant", content=blocks))
