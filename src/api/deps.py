"""认证依赖:在请求入口确立操作人并注入请求级上下文。

身份只自来自企业 SSO 登录态(开发态用请求头模拟)。本模块在请求入口解析 operator

- 核心解析逻辑 :func:`resolve_operator` 是框架无关纯函数,可单测、可独立调用;
- :func:`get_current_operator` 是 FastAPI 依赖封装;待 api 层接线、安装 fastapi 后
  即作为 ``Depends(get_current_operator)`` 生效。

"""
from __future__ import annotations

from typing import Mapping

import jwt
from fastapi import Request

from conf import settings
from infra.exceptions import UnauthorizedException
from runtime.context import (
    OperatorContext,
)
from security.jwks_client import jwks_resolver

# 开发态信任的操作人请求头
_HEADER_USERID = "X-Operator-Userid"
_HEADER_TENANT = "X-Operator-Tenant"
_HEADER_ROLES = "X-Operator-Roles"


def resolve_operator(headers: Mapping[str, str]) -> OperatorContext | None:
    """从请求头解析操作人(开发态)。

    开发态(``settings.auth_dev_mode=True``)直接信任以下请求头:

    - ``X-Operator-Userid``(必填):真实操作人标识。
    - ``X-Operator-Tenant``:所属租户。
    - ``X-Operator-Roles``:平台 RBAC 角色,逗号分隔(如 ``hr_admin,employee``)。

    缺 ``X-Operator-Userid`` 视为未认证,返回 ``None``。

    :param headers: 请求头映射(Starlette 的 ``request.headers`` 大小写不敏感)。
    """

    user_id = (headers.get(_HEADER_USERID) or "").strip()
    if not user_id:
        return None

    roles_raw = headers.get(_HEADER_ROLES) or ""
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()]

    return OperatorContext(
        user_id=user_id,
        roles=roles,
        tenant_id=(headers.get(_HEADER_TENANT) or "").strip(),
    )


async def resolve_operator_from_sso(token: str) -> OperatorContext | None:
    """从 SSO JWT(RS256)解析操作人信息(生产态)。"""

    # 提取令牌
    token = (token or "").removeprefix("Bearer ").strip()
    if not token:
        return None

    try:
        # 获取公钥
        sign_key = await jwks_resolver.get_sign_key(token)

        # 解析jwt令牌
        payload = jwt.decode(
            token,
            sign_key,
            algorithms=["RS256"],
            issuer=settings.sso_issuer,
            audience=settings.sso_audience or None,
        )
    except jwt.PyJWTError as e:
        raise UnauthorizedException(f"SSO token 无效或已过期: {e}") from e

    # 获取令牌的 sub claim (令牌的身份主体)
    user_id = payload.get("sub")
    if not user_id:
        raise UnauthorizedException("SSO token 缺少 sub claim")

    # 获取角色
    roles = payload.get("roles") or []
    if isinstance(roles, str):
        roles = [r.strip() for r in roles.split(",") if r.strip()]

    return OperatorContext(
        user_id=str(user_id),
        roles=list(roles),
        tenant_id=str(payload.get("tenant_id", "")),
    )


async def get_current_operator(request: "Request") -> OperatorContext:
    """FastAPI 依赖:解析登录态 → 注入请求级上下文 → 返回操作人。"""
    if settings.auth_dev_mode:
        operator = resolve_operator(request.headers)
    else:
        operator = await resolve_operator_from_sso(request.headers.get("Authorization", ""))

    if operator is None:
        raise UnauthorizedException("未认证:缺少合法操作人")

    return operator
