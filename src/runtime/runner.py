"""运行时:启动装配工厂 + 每请求轻量执行。

两层语义:

- :func:`build_runtime`:**启动装配一次**(app lifespan 调用),组装进程级组件
  并接好依赖,产出长期存活的 :class:`Runtime`。
- :meth:`Runtime.run`:**每请求轻量执行**,构建请求级上下文 + 编排(query →
  dispatcher),流式产出 **SSE ``data:`` 行**(已序列化),router 直接
  ``StreamingResponse(runtime.run(...))``。

当前装配的进程级组件(随阶段演进扩充):

- :class:`Query`(对话编排器:意图理解 / 并发执行 workflow / 回复生成);
- :class:`WorkflowRegistry` + 已注册 workflow(会议室预订);
- :class:`WorkflowDispatcher`(执行编排,持 registry)。

LLM client / DB 引擎 / redis / credential / OAClient 等基础设施目前为模块级单例,
由 Runtime 持有或按需引用;后续可逐步收拢到本工厂统一装配注入。
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
    AssistantTurnComplete,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    ToolProgress,
)
from infra.database import init_engine
from infra.logger import setup_logging
from infra.redis_client import init_redis
from orchestrator.base import WorkflowRegistry
from orchestrator.dispatcher import WorkflowDispatcher
from orchestrator.workflow.meeting_room import MeetingRoomBookingWorkflow
from prompts import EnvironmentInfo, PromptType, build_runtime_system_prompt
from runtime.context import ModelContext, OperatorContext, RequestContext, set_request_context
from utils.trace_id_util import new_trace_id

logger = setup_logging(__name__)


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


class ToolProgressSseEvent(BaseModel):
    """工具单步执行进度(步级流式)。"""

    type: Literal["tool_progress"] = "tool_progress"
    tool: str
    step: str
    is_error: bool
    step_id: int | None = None
    is_compensation: bool = False
    error: str | None = None


class TurnCompleteSseEvent(BaseModel):
    """对话回合完成。"""

    type: Literal["turn_complete"] = "turn_complete"


class UnknownSseEvent(BaseModel):
    """未知事件兜底(防御:StreamEvent 新增子类未覆盖时)。"""

    type: Literal["unknown"] = "unknown"


#: SSE 事件联合类型
type SseEvent = (
    TextSseEvent
    | ToolStartedSseEvent
    | ToolCompletedSseEvent
    | ToolProgressSseEvent
    | TurnCompleteSseEvent
    | UnknownSseEvent
)


@dataclass
class RuntimeBundle:
    """运行时配置绑定"""

    provider: str | None = settings.llm_provider  # 厂商(openai / anthropic)
    api_key: str | None = settings.llm_api_key  # 凭证(api-key)
    base_url: str | None = settings.llm_base_url  # API 端点
    timeout: int = settings.llm_timeout  # 调用超时秒
    # 意图理解 LLM 配置
    intent_llm_name: str | None = settings.intent_llm_name  # 模型名
    intent_llm_temperature: float = settings.intent_llm_temperature  # 采样温度
    intent_llm_max_tokens: int | None = settings.intent_llm_max_tokens  # 最大输出token数
    # 回复生成 LLM 配置
    reply_llm_name: str | None = settings.reply_llm_name
    reply_llm_temperature: float = settings.reply_llm_temperature
    reply_llm_max_tokens: int | None = settings.reply_llm_max_tokens
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
        reply_model_context: ModelContext,
        api_client: SupportsStreamingMessages,
    ) -> None:
        self._query = query  # 解析器
        self._registry = registry  # 工作流注册器
        self._model_context = model_context  # 意图理解
        self._reply_model_context = reply_model_context  # 回复生成
        self._api_client = api_client  # llm客户端

    async def run(self, operator: OperatorContext, user_input: str) -> AsyncIterator[str]:
        """构建请求级上下文 → 编排执行,流式产出 SSE ``data:`` 行。

        1. 构建运行时用户上下文 RequestContext(注入 intent/reply 系统提示词);
        2. 构建用户消息列表 messages;
        3. 执行 Query.run() 两段式编排(意图理解带 tools → 并发执行 workflow → 回复生成),
           并使用 SSE 协议流式产出。
        """
        # 注入系统提示词:build_runtime_system_prompt 按远程 > 本地 .prompt/custom > 默认降级
        env = EnvironmentInfo.get_environment()
        intent_system_prompt = await build_runtime_system_prompt(PromptType.INTENT, env=env)
        reply_system_prompt = await build_runtime_system_prompt(PromptType.REPLY, env=env)

        # 用户查询提示词
        messages = [ConversationMessage(role="user", content=[TextBlock(text=user_input)])]

        # 运行时请求上下文构建
        context = RequestContext(
            operator=operator,
            intent_model=self._model_context,
            reply_model=self._reply_model_context,
            api_client=self._api_client,
            intent_system_prompt=intent_system_prompt,
            reply_system_prompt=reply_system_prompt,
            messages=messages,
            trace_id=new_trace_id(),
            workflow_registry=self._registry,
        )
        set_request_context(context)
        logger.info(
            "runtime.run: start trace_id=%s operator=%s user_input=%s",
            context.trace_id,
            operator.user_id,
            user_input[:80],
        )

        # 异步执行查询流程编排, 实时输出流式片段
        async for event in self._query.run(context):
            if isinstance(event, AssistantTextDelta):
                sse_event = TextSseEvent(text=event.text)
            elif isinstance(event, ToolExecutionStarted):
                sse_event = ToolStartedSseEvent(tool=event.tool_name, input=event.tool_input)
            elif isinstance(event, ToolProgress):
                sse_event = ToolProgressSseEvent(
                    tool=event.tool_name,
                    step=event.step_name,
                    is_error=event.is_error,
                    step_id=event.step_id,
                    is_compensation=event.is_compensation,
                    error=event.error,
                )
            elif isinstance(event, ToolExecutionCompleted):
                sse_event = ToolCompletedSseEvent(tool=event.tool_name, output=event.output, is_error=event.is_error)
            elif isinstance(event, AssistantTurnComplete):
                sse_event = TurnCompleteSseEvent()
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
    # LLM 配置(意图理解)
    model_context = ModelContext(
        provider=runtime_bundle.provider,
        model=runtime_bundle.intent_llm_name,
        temperature=runtime_bundle.intent_llm_temperature,
        max_tokens=runtime_bundle.intent_llm_max_tokens,
    )
    # LLM 配置(回复生成)
    reply_model_context = ModelContext(
        provider=runtime_bundle.provider,
        model=runtime_bundle.reply_llm_name or runtime_bundle.intent_llm_name,
        temperature=runtime_bundle.reply_llm_temperature,
        max_tokens=runtime_bundle.reply_llm_max_tokens,
    )
    # llm客户端:按 provider 构建对应厂商客户端
    api_client = _resolve_api_client(runtime_bundle)
    # 返回运行时实例
    return Runtime(
        query=query,
        registry=registry,
        model_context=model_context,
        reply_model_context=reply_model_context,
        api_client=api_client,
    )
