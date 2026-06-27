"""运行时:启动装配工厂 + 每请求轻量执行。

两层语义:

- :func:`build_runtime`:**启动装配一次**(app lifespan 调用),组装进程级组件
  并接好依赖,产出长期存活的 :class:`Runtime`。
- :meth:`Runtime.run`:**每请求轻量执行**,构建请求级上下文 + 编排(parser →
  dispatcher),流式产出 **SSE ``data:`` 行**(已序列化),router 直接
  ``StreamingResponse(runtime.run(...))``。

当前装配的进程级组件(随阶段演进扩充):

- :class:`IntentParser`(意图解析,当前骨架);
- :class:`WorkflowRegistry` + 已注册 workflow(会议室预订);
- :class:`WorkflowDispatcher`(调度,持 registry)。

LLM client / DB 引擎 / redis / credential / OAClient 等基础设施目前为模块级单例,
parser / workflow 按需引用;后续可逐步收拢到本工厂统一装配注入。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel

from engine.client.messages import ConversationMessage, TextBlock
from engine.parser import IntentParser
from engine.stream_event import (
    AssistantTextDelta,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from orchestrator.base import WorkflowRegistry
from orchestrator.dispatcher import WorkflowDispatcher
from orchestrator.workflow.meeting_room import MeetingRoomBookingWorkflow
from runtime.context import OperatorContext, RequestContext, set_request_context
from utils.trace_id_util import new_trace_id


# ---- SSE 事件结构(对应 StreamEvent 各子类的序列化形式)----
class TextSseEvent(BaseModel):
    """文本片段。"""

    type: Literal["text"] = "text"
    text: str


class ToolStartedSseEvent(BaseModel):
    """工具开始执行。"""

    type: Literal["tool_started"] = "tool_started"
    tool: str
    input: dict[str, Any] = {}


class ToolCompletedSseEvent(BaseModel):
    """工具执行完成。"""

    type: Literal["tool_completed"] = "tool_completed"
    tool: str
    output: str
    is_error: bool = False


class UnknownSseEvent(BaseModel):
    """未知事件兜底(防御:StreamEvent 新增子类未覆盖时)。"""

    type: Literal["unknown"] = "unknown"


#: SSE 事件联合类型
type SseEvent = TextSseEvent | ToolStartedSseEvent | ToolCompletedSseEvent | UnknownSseEvent


def _serialize_event(event: StreamEvent) -> SseEvent:
    """StreamEvent → 结构化 SSE 事件对象(经 ``.model_dump()`` 即可 JSON 序列化)。"""
    if isinstance(event, AssistantTextDelta):
        return TextSseEvent(text=event.text)
    if isinstance(event, ToolExecutionStarted):
        return ToolStartedSseEvent(tool=event.tool_name, input=event.tool_input)
    if isinstance(event, ToolExecutionCompleted):
        return ToolCompletedSseEvent(tool=event.tool_name, output=event.output, is_error=event.is_error)
    return UnknownSseEvent()


class Runtime:
    """运行时:持有进程级组件,每请求执行编排。"""

    def __init__(
        self,
        parser: IntentParser,
        dispatcher: WorkflowDispatcher,
        registry: WorkflowRegistry,
    ) -> None:
        self._parser = parser  # 解析器
        self._dispatcher = dispatcher  # 分发器
        self._registry = registry  # 工作流注册器

    async def run(self, operator: OperatorContext, user_input: str) -> AsyncIterator[str]:
        """构建请求级上下文 → 编排执行,流式产出 SSE ``data:`` 行。

        1. 构建运行时用户上下文 RequestContext;
        2. 构建用户消息列表 messages
        3. 执行 IntentParser.run() 意图解析, 并使用SSE协议流式产出;
        4. (后续)解析器 parser 输出 workflow 调用决策 → 交 dispatcher 调度器进行路由分发
        """
        context = RequestContext(operator=operator, trace_id=new_trace_id())
        set_request_context(context)

        messages = [ConversationMessage(role="user", content=[TextBlock(text=user_input)])]

        async for event in await self._parser.run(context, messages):
            sse = _serialize_event(event)
            yield f"data: {json.dumps(sse.model_dump(), ensure_ascii=False)}\n\n"

    @property
    def registry(self) -> WorkflowRegistry:
        return self._registry


def build_runtime() -> Runtime:
    """装配工厂:启动时调用一次,组装进程级组件并接好依赖。"""
    registry = WorkflowRegistry()
    registry.register(MeetingRoomBookingWorkflow())
    dispatcher = WorkflowDispatcher()
    parser = IntentParser(registry, dispatcher)
    return Runtime(parser=parser, dispatcher=dispatcher, registry=registry)
