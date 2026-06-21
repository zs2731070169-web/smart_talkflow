"""凭证抽象:把操作人打包成下游可识别的「代签委托书」。"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from conf import settings
from runtime.context import OperatorContext


@dataclass(frozen=True)
class Credential:
    """单次下游调用的凭证

    - ``headers``:注入下游 HTTP 请求的鉴权头(服务账号 api-key + operator 代签)。
    - ``principal``:真实操作人 userId(审计用,非服务账号)。
    - ``source``:凭证来源标记,如 ``service_account_delegated``。
    """

    scheme: str
    headers: dict[str, str] = field(default_factory=dict)
    principal: str = ""
    source: str = ""


class CredentialProvider(Protocol):
    """凭证供给协议:把 operator 打包为下游凭证。

    实现应做到「同一 provider、不同 operator → 不同凭证」(headers 随
    operator + nonce + 时间戳变化),并保证服务账号 token 与签名密钥不外泄。
    """

    async def resolve(self, operator: OperatorContext | None) -> Credential:
        ...


class DefaultCredentialProvider:
    """默认凭证供给者:服务账号 api-key + operator HMAC 代签。"""

    def __init__(self, api_key: str, delegation_secret: str) -> None:
        self._api_key = api_key
        self._delegation_secret = delegation_secret

    async def resolve(self, operator: OperatorContext | None) -> Credential:
        # 凭证(api-key / secret)在构造时已按 target_system 绑定,这里只需 operator。
        headers = _build_agent_headers(self._api_key, self._delegation_secret, operator)
        return Credential(
            scheme="agent_delegation",
            headers=headers,
            principal=operator.user_id if operator else "",
            source="service_account_delegated" if operator else "service_account_only",
        )


def _build_agent_headers(
        api_key: str,
        delegation_secret: str,
        operator: OperatorContext | None,
) -> dict[str, str]:
    """代签请求头。

    服务账号 api-key 做技术认证(``X-API-Key``);operator 经 HMAC 签名代签
    (``X-Operator-Userid`` + ``X-Agent-Signature``),供下游 ``AgentDelegationFilter``
    验签后改写当前用户为真实操作人。

    tenant-id 取自 ``operator.tenant_id``(请求级,随操作人而变)。
    operator 为 None 时只带 api-key(仅技术认证、不代签)。
    """
    # 无 operator 时返回只含 X-API-Key 的凭证
    headers: dict[str, str] = {"X-API-Key": api_key}
    if operator is None:
        return headers

    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time()))

    raw = f"{operator.user_id}|{operator.tenant_id}|{nonce}|{timestamp}"
    # 签名加密计算,唯一认证
    signature = hmac.new(delegation_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()

    headers.update({
        "tenant-id": operator.tenant_id,
        "X-Operator-Userid": operator.user_id,
        "X-Agent-Signature": signature,
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Nonce": nonce, # 一次性随机数
    })
    return headers


def default_credential_provider(target_system: str) -> CredentialProvider:
    """按 target_system 从 settings 加载服务账号凭证,构造默认 CredentialProvider。

    :param target_system: 下游业务系统标识
    :raises ValueError: 未配置该系统的服务账号凭证
    """
    if target_system == "oa":
        api_key, delegation_secret = settings.oa_api_key, settings.oa_delegation_secret
    else:
        raise ValueError(f"未配置 target_system 的服务账号凭证: {target_system}")
    return DefaultCredentialProvider(api_key, delegation_secret)
