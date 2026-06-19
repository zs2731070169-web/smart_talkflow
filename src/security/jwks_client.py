"""JWKS 公钥解析器:从 SSO 的 JWKS 端点取公钥、redis 缓存,供 JWT RS256 验签。

消费方(smart_talkflow)不持有 SSO 公钥,而是按需从 ``sso_jwks_uri`` 拉取 JWKS、
按 token header 的 ``kid`` 定位公钥。JWKS 经 redis 缓存(TTL),减少对 SSO 的请求;
``kid`` 未命中时强制刷新一次(应对密钥轮换)。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from conf.config import settings
from infra.exceptions import UnauthorizedException
from infra.redis_client import get_redis


class JwksKeyResolver:
    """按 token 解析 RS256 验签公钥(JWKS + redis 缓存)。"""

    def __init__(self, jwks_uri: str, cache_ttl: int) -> None:
        self._jwks_uri = jwks_uri
        self._cache_ttl = cache_ttl
        self._jwks_key = f"jwks:{jwks_uri}"

    async def _load_jwks(self, force_refresh: bool = False) -> dict[str, Any]:
        """加载公钥集(JSON)

        取 JWKS:redis 命中即用, 否则使用 http 获取并回写缓存;
        当force_refresh=True, 强行从 http 重新获取 共享对象集.
        """
        redis = get_redis()

        if not force_refresh:
            jwks = await redis.get(self._jwks_key)
            if jwks:
                return json.loads(jwks)

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(self._jwks_uri)
                resp.raise_for_status()
                jwks = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise UnauthorizedException(f"拉取 JWKS 失败: {e}") from e

        await redis.set(self._jwks_key, json.dumps(jwks), ex=self._cache_ttl)

        return jwks

    @staticmethod
    def _find_key(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
        """根据kid从公钥集获取对应的公钥

        jwks = {
            "keys": [
                {"kid": "key-1", "kty": "RSA", "n": "..."},
                {"kid": "key-2", "kty": "RSA", "n": "..."}
            ]
        }
        """
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                return key
        return None

    async def get_sign_key(self, token: str):
        """根据 token header 的 kid (钥匙ID) 获取公钥集对应验签公钥(RSA)。

        :raises UnauthorizedException: token 无 kid、或 JWKS 中找不到对应公钥。
        """
        try:
            kid = jwt.get_unverified_header(token)["kid"]
        except (jwt.PyJWTError, KeyError) as e:
            raise UnauthorizedException("token 缺少 kid") from e

        # 获取公钥JSON对象集合
        jwks = await self._load_jwks()

        # 从 公钥集 中根据对应 钥匙ID 获取对应的 公钥JSON对象
        jwk = self._find_key(jwks, kid)

        # kid 未命中:可能是密钥轮换,强制刷新一次 JWKS, 并重新获取kid对应的 公钥JSON对象
        if jwk is None:
            jwks = await self._load_jwks(force_refresh=True)
            jwk = self._find_key(jwks, kid)

        if jwk is None:
            raise UnauthorizedException(f"JWKS 未找到 kid={kid} 的公钥")

        # 转换成: public_key, 获得真正的公钥对象
        return RSAAlgorithm.from_jwk(json.dumps(jwk))


# 模块级单例(从 settings 配置构造)。开发态 sso_jwks_uri 可为空——此时不会走 SSO 分支。
jwks_resolver = JwksKeyResolver(
    jwks_uri=settings.sso_jwks_uri or "",
    cache_ttl=settings.sso_jwks_cache_ttl,
)
