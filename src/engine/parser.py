from typing import AsyncGenerator

from engine.client.messages import ConversationMessage
from engine.stream_event import StreamEvent
from runtime.context import RequestContext


class IntentParser:
    """用户意图解析, 参数提取, 工具/工作流调用决策, 反馈执行结果"""

    def __init__(self) -> None:
        pass

    async def run(self, context: RequestContext, messages: list[ConversationMessage]) -> AsyncGenerator[StreamEvent, None]:
        """获取用户查询, 执行解析

        Args:
            context: QueryContext 查询上下文信息
            messages: list 用户消息

        :return
        AsyncGenerator[StreamEvent, None]
        """
