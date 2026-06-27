"""engine.client.llm_client 流式调用测试。

迁移自 ``src/engine/client/llm_client.py`` 的 ``__main__`` 段,拆成
OpenAI 与 Anthropic 两个用例,各自走一遍流式拉取并打印文本片段与工具调用。
异步测试使用 ``IsolatedAsyncioTestCase``。

运行::

    python -m unittest tests.test_llm_client
"""

import unittest

from conf.config import settings
from engine.client.base_client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiTextDeltaEvent,
)
from engine.client.llm_client import AnthropicApiClient, OpenAIClient
from engine.client.messages import ConversationMessage, TextBlock


class LlmClientTest(unittest.IsolatedAsyncioTestCase):
    """OpenAI / Anthropic 两个客户端的流式调用冒烟测试。"""

    async def test_openai(self):
        """OpenAI 兼容协议:workflow 采用 function 包装格式。"""
        print("\n--- OpenAI ---", flush=True)
        client = OpenAIClient(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout,
        )
        request = ApiMessageRequest(
            model=settings.llm_model,
            message=[ConversationMessage(role="user", content=[TextBlock(text="明天杭州的天气是什么")])],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "weather_search",
                        "description": "天气查询",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "city": {"type": "string", "description": "城市名称"},
                                "time": {"type": "string", "description": "查询时间"},
                            },
                            "required": [],
                        },
                    },
                }
            ],
        )
        async for event in client.stream_message(request):
            if isinstance(event, ApiTextDeltaEvent):
                print(event.text, end="", flush=True)
            elif isinstance(event, ApiMessageCompleteEvent):
                print("\n--- 完成 ---")
                for block in event.message.content:
                    if block.type == "tool_use":
                        print(f"工具调用: {block.name}({block.input})")

    async def test_anthropic(self):
        """Anthropic 兼容协议:workflow 采用 input_schema 格式。"""
        print("\n--- Anthropic ---", flush=True)
        anthropic_client = AnthropicApiClient(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout,
        )
        anthropic_request = ApiMessageRequest(
            model=settings.llm_model,
            message=[ConversationMessage(role="user", content=[TextBlock(text="明天杭州的天气是什么")])],
            tools=[
                {
                    "name": "weather_search",
                    "description": "天气查询",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "城市名称"},
                            "time": {"type": "string", "description": "查询时间"},
                        },
                        "required": [],
                    },
                }
            ],
        )
        async for event in anthropic_client.stream_message(anthropic_request):
            if isinstance(event, ApiTextDeltaEvent):
                print(event.text, end="", flush=True)
            elif isinstance(event, ApiMessageCompleteEvent):
                print("\n--- 完成 ---")
                for block in event.message.content:
                    if block.type == "tool_use":
                        print(f"工具调用: {block.name}({block.input})")


if __name__ == "__main__":
    unittest.main()
