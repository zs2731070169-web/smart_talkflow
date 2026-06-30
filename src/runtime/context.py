"""请求级运行时上下文。

api 层每请求构建一个 :class:`RequestContext` 实例,聚合该请求的操作人身份(operator)、
trace_id 等请求级**只读不变量**,请求生命周期内不变、用完即弃(不持久化)。深层组件通过
:func:`get_operator` 经 :class:`ContextVar` 只读取到当前请求的 operator,无需层层透传。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

from engine.client.base_client import SupportsStreamingMessages
from engine.client.messages import ConversationMessage
from orchestrator.base import WorkflowRegistry


@dataclass
class OperatorContext:
    """操作人身份(来自认证层 / SSO 登录)"""

    user_id: str  # 真实操作人标识
    roles: list[str] = field(default_factory=list)  # 平台 RBAC 角色(层 A 授权用)
    tenant_id: str = ""  # 所属租户
    name: str = ""  # 操作人显示名(代签 X-Operator-Name 头,审计用)

    def to_operator_context(self) -> dict:
        """序列化 OperatorContext, 重建操作人身份"""
        return {
            "user_id": self.user_id,
            "roles": list(self.roles),
            "tenant_id": self.tenant_id,
            "name": self.name,
        }

    @classmethod
    def from_operator_context(cls, operator_context: dict | None) -> OperatorContext | None:
        """反序列化为 OperatorContext"""
        if not operator_context:
            return None
        return cls(
            user_id=operator_context.get("user_id", ""),
            roles=list(operator_context.get("roles") or []),
            tenant_id=operator_context.get("tenant_id", ""),
            name=operator_context.get("name", ""),
        )


@dataclass
class ModelContext:
    """LLM 调用配置快照(由 runner 从全局配置 settings 读取并注入)。"""

    provider: str | None = None  # 厂商(openai / anthropic)
    model: str | None = None  # 模型名
    temperature: float = 0.3  # 采样温度
    max_tokens: int | None = 4096  # 最大输出token数


@dataclass(frozen=True)
class RequestContext:
    """请求级只读不变量上下文。

    api 层每请求构建一个实例,承载该请求的用户身份与 LLM 配置,请求结束即弃。
    LLM 专用字段(intent_model / api_client / system_prompt / messages)在非 LLM 路径(如降级)可不填。
    """

    # 操作人信息
    operator: OperatorContext
    # 意图理解 model 配置(意图理解)
    intent_model: ModelContext | None = None
    # 回复生成 model 配置
    reply_model: ModelContext | None = None
    # llm客户端
    api_client: SupportsStreamingMessages | None = None
    # 意图解析标准系统提示词
    intent_system_prompt: str | None = None
    # 回复生成标准系统提示词
    reply_system_prompt: str | None = None
    # 用户查询提示词
    messages: list[ConversationMessage] | None = None
    # 工作流注册器
    workflow_registry: WorkflowRegistry | None = None
    # 流程追踪id
    trace_id: str | None = None


# ContextVar:持有当前请求的 RequestContext
_request_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def set_request_context(ctx: RequestContext | None) -> None:
    """设置当前请求的上下文(api 层每请求调用一次)。"""
    _request_context.set(ctx)


def get_request_context() -> RequestContext | None:
    """读取当前请求的上下文,可能为 ``None``(未设置)。"""
    return _request_context.get()


def get_operator() -> OperatorContext | None:
    """读取当前请求的操作人,可能为 ``None``(未认证或未设置)。"""
    ctx = _request_context.get()
    return ctx.operator if ctx else None
