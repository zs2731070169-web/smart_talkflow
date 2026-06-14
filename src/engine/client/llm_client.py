from typing import Any

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from engine.client.base_client import ApiMessageRequest, ApiMessageResponse


class OpenAIClient:
    """openai 兼容协议的 llm 客户端"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: int = 120):
        if not api_key:
            raise ValueError("OPENAI_API_KEY 未配置")
        if not base_url:
            raise ValueError("OPENAI_API_BASE 未配置")
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    async def ainvoke_message(self, request: ApiMessageRequest) -> ApiMessageResponse:
        # 组装 messages:可选 system_prompt + 当前会话消息
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({
            "role": request.message.role,
            "content": "\n".join(request.message.content),
        })

        # workflows 透传为 OpenAI tools(为空则不传,避免无谓参数)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }
        if request.workflows:
            kwargs["tools"] = request.workflows

        completion = await self._client.chat.completions.create(**kwargs)
        choice = completion.choices[0]
        assistant = choice.message

        # tool_calls 映射为统一的 workflow_calls,屏蔽底层 sdk 差异
        workflow_calls = [
            tool_call.model_dump()
            for tool_call in (assistant.tool_calls or [])
        ]

        return ApiMessageResponse(
            content=assistant.content or "",
            model=completion.model,
            finish_reason=choice.finish_reason,
            workflow_calls=workflow_calls,
            raw=completion,
        )


class AnthropicApiClient:
    """anthropic 兼容协议的 llm 客户端"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: int = 120):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY 未配置")
        self._client = AsyncAnthropic(api_key=api_key, base_url=base_url, timeout=timeout)

    async def stream_message(self, request: ApiMessageRequest) -> ApiMessageResponse:
        # anthropic 的 system 是顶级参数,与 openai 放进 messages 的做法不同
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": request.message.role, "content": "\n".join(request.message.content)},
            ],
        }
        if request.system_prompt:
            kwargs["system"] = request.system_prompt
        # workflows 透传为 anthropic tools,需符合 name/description/input_schema 结构
        if request.workflows:
            kwargs["tools"] = request.workflows

        # 流式拉取后用 get_final_message 聚合完整响应,屏蔽底层事件遍历
        async with self._client.messages.stream(**kwargs) as stream:
            final = await stream.get_final_message()

        # 拼接 text 块;tool_use 块映射为统一的 workflow_calls,屏蔽 sdk 差异
        workflow_calls = [
            block.model_dump() for block in final.content if block.type == "tool_use"
        ]
        content = "".join(block.text for block in final.content if block.type == "text")

        return ApiMessageResponse(
            content=content,
            model=final.model,
            finish_reason=final.stop_reason,
            workflow_calls=workflow_calls,
            raw=final,
        )


if __name__ == '__main__':
    import asyncio

    from conf.config import settings
    from engine.messages import ConversationMessage


    async def main() -> None:
        # 最小连通性自测:用 settings 里的 llm 配置发一次普通对话,打印回复
        client = OpenAIClient(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout,
        )
        request = ApiMessageRequest(
            model=settings.llm_model,
            message=ConversationMessage(role="user", content=["用一句话介绍你自己"]),
        )
        response = await client.ainvoke_message(request)
        print(f"[{response.model}] {response.content}")


    asyncio.run(main())
