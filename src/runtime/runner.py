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
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from conf import settings
from engine.client.base_client import SupportsStreamingMessages
from engine.client.llm_client import AnthropicApiClient, OpenAIClient
from engine.client.messages import ConversationMessage, TextBlock
from engine.query import Query
from engine.stream_event import (
    AssistantTextDelta,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from infra.database import init_engine
from infra.redis_client import init_redis
from orchestrator.base import WorkflowRegistry
from orchestrator.dispatcher import WorkflowDispatcher
from orchestrator.workflow.meeting_room import MeetingRoomBookingWorkflow
from prompts.envirement import EnvironmentInfo
from prompts.system_prompt import build_system_prompt
from runtime.context import ModelContext, OperatorContext, RequestContext, set_request_context
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


@dataclass
class RuntimeBundle:
    """运行时配置绑定"""

    provider: str | None = settings.llm_provider  # 厂商(openai / anthropic)
    api_key: str | None = settings.llm_api_key  # 凭证(api-key)
    model: str | None = settings.llm_model  # 模型名
    base_url: str | None = settings.llm_base_url  # API 端点
    timeout: int = settings.llm_timeout  # 调用超时秒
    temperature: float = settings.llm_temperature  # 采样温度
    max_tokens: int | None = settings.max_tokens  # 最大输出token数
    db_url: str = settings.mysql_conf  # mysql+asyncmy://user:pwd@host:port/db?charset=utf8mb4
    pool_size: int = settings.pool_size
    max_overflow: int = settings.max_size
    pool_recycle: int = settings.keep_alive
    pool_pre_ping: bool = True  # 取连接前先 ping,丢弃已失效连接
    echo: bool = settings.sql_log
    redis_url: str = settings.redis_url  # redis://host:port/db


class Runtime:
    """运行时:持有进程级组件,每请求执行编排。"""

    def __init__(
            self,
            query: Query,
            registry: WorkflowRegistry,
            model_context: ModelContext,
            api_client: SupportsStreamingMessages,
    ) -> None:
        self._query = query  # 解析器
        self._registry = registry  # 工作流注册器
        self._model_context = model_context  # llm上下文信息
        self._api_client = api_client  # llm客户端

    async def run(self, operator: OperatorContext, user_input: str) -> AsyncIterator[str]:
        """构建请求级上下文 → 编排执行,流式产出 SSE ``data:`` 行。

        1. 构建运行时用户上下文 RequestContext;
        2. 构建用户消息列表 messages
        3. 执行 IntentParser.run() 意图解析, 并使用SSE协议流式产出;
        4. (后续)解析器 parser 输出 workflow 调用决策 → 交 dispatcher 调度器进行路由分发
        """
        # 注入系统提示词:build_system_prompt 按远程仓库 > 自定义 > 默认降级生成
        system_prompt = await build_system_prompt(EnvironmentInfo.get_environment())

        # 用户查询提示词
        messages = [ConversationMessage(role="user", content=[TextBlock(text=user_input)])]

        # 运行时请求上下文构建
        context = RequestContext(
            operator=operator,
            model=self._model_context,
            api_client=self._api_client,
            system_prompt=system_prompt,
            messages=messages,
            trace_id=new_trace_id(),
            workflow_registry=self._registry,
        )
        set_request_context(context)

        async for event in self._query.run(context):
            if isinstance(event, AssistantTextDelta):
                sse_event = TextSseEvent(text=event.text)
            elif isinstance(event, ToolExecutionStarted):
                sse_event = ToolStartedSseEvent(tool=event.tool_name, input=event.tool_input)
            elif isinstance(event, ToolExecutionCompleted):
                sse_event = ToolCompletedSseEvent(tool=event.tool_name, output=event.output, is_error=event.is_error)
            else:
                sse_event = UnknownSseEvent()
            yield f"data: {json.dumps(sse_event.model_dump(), ensure_ascii=False)}\n\n"

    @property
    def registry(self) -> WorkflowRegistry:
        return self._registry


def _resolve_api_client(runtime_bundle: RuntimeBundle) -> SupportsStreamingMessages:
    """按 provider 构建对应的 LLM 客户端

    :param runtime_bundle: 运行时配置绑定(含 provider / api_key / base_url / timeout)。
    :returns: 满足 :class:`SupportsStreamingMessages` 协议的厂商客户端。
    :raises ValueError: provider 缺失或非支持的厂商(启动即失败)。
    """
    # 大小写 / 空白归一,容忍 OPENAI 这类全大写环境变量取值
    provider = (runtime_bundle.provider or "").strip().lower()
    # provider → 客户端构造器。注解为协议工厂类型
    clients: dict[str, Callable[..., SupportsStreamingMessages]] = {
        "openai": OpenAIClient,
        "anthropic": AnthropicApiClient,
    }
    client = clients.get(provider)
    if client is None:
        raise ValueError(f"不支持的 llm_provider={runtime_bundle.provider!r},当前支持:{' / '.join(clients)}")
    return client(
        api_key=runtime_bundle.api_key,
        base_url=runtime_bundle.base_url,
        timeout=runtime_bundle.timeout,
    )


def build_runtime(runtime_bundle: RuntimeBundle) -> Runtime:
    """装配工厂:启动时调用一次,组装进程级组件并接好依赖。

    :param runtime_bundle: 运行时配置绑定(环境变量快照),由调用方
        (app lifespan)从 ``settings`` 装配后显式注入;内部据此初始化
    """
    # 初始化数据库引擎
    init_engine(
        db_url=runtime_bundle.db_url,
        pool_size=runtime_bundle.pool_size,
        max_overflow=runtime_bundle.max_overflow,
        pool_recycle=runtime_bundle.pool_recycle,
        pool_pre_ping=runtime_bundle.pool_pre_ping,
        echo=runtime_bundle.echo,
    )
    # 初始化 redis 连接池
    init_redis(redis_url=runtime_bundle.redis_url)
    # 创建工作流注册器
    registry = WorkflowRegistry()
    # 注册工作流
    workflows = [MeetingRoomBookingWorkflow()]
    registry.register_workflows(workflows)
    # 创建工作流路由器
    dispatcher = WorkflowDispatcher()
    # 创建意图解析器
    query = Query(registry, dispatcher)
    # 提取 LLM 配置
    model_context = ModelContext(
        provider=runtime_bundle.provider,
        model=runtime_bundle.model,
        temperature=runtime_bundle.temperature,
        max_tokens=runtime_bundle.max_tokens,
    )
    # llm客户端:按 provider 构建对应厂商客户端
    api_client = _resolve_api_client(runtime_bundle)
    # 返回运行时实例
    return Runtime(query=query, registry=registry, model_context=model_context, api_client=api_client)
