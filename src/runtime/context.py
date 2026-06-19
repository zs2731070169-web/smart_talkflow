"""请求级运行时上下文。

每请求在 api 层(deps.py)构建一个 :class:`RequestContext` 实例,聚合该请求的
操作人身份(operator)、trace_id 等请求级状态,在请求生命周期内有状态、用完即弃
(不持久化)。深层组件(adapter 等)通过 :func:`get_operator` 经 :class:`ContextVar`
取到当前请求的 operator,无需层层透传。

这是平台「请求级执行上下文」的归宿(见 CLAUDE.md「runtime/」层):api 层构建,
串联 parse → resolve → 幂等 → orchestrator 全链,承载意图 / 参数 / 幂等键 / 步骤
中间产物等。本次仅落地 operator(认证代签所需),其余字段随阶段演进补全。
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class OperatorContext:
    """操作人身份(来自认证层 / SSO 登录态,请求级确立)。

    下游调用时作为「代签」身份透传给 yudao ``AgentDelegationFilter``,由其改写
    当前用户为本操作人,使 ``@PreAuthorize`` 按真实用户权限判定、审计归属真实用户。

    严禁来自请求体 / LLM 参数(可伪造)——只来自可信的认证来源。
    """

    user_id: str  # 真实操作人标识
    roles: list[str] = field(default_factory=list)  # 平台 RBAC 角色(层 A 授权用)
    tenant_id: str = ""  # 所属租户


@dataclass
class RequestContext:
    """请求级有状态上下文。

    每请求在 api 层构建一个实例,承载该请求的用户身份与中间产物,请求结束即弃。
    未来可扩展:意图、参数、幂等键、步骤中间产物等请求级状态。
    """

    operator: OperatorContext
    trace_id: str | None = None
    # 预留:意图 / 参数 / 幂等键 / 步骤中间产物等请求级状态


# ContextVar:持有当前请求的 RequestContext。
_request_context: ContextVar[RequestContext | None] = ContextVar(
    "request_context", default=None
)


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