"""SSO JWT 验签解析测试(resolve_operator_from_sso)。

用本地生成的 RSA 密钥对签发测试 JWT,JWKS 经 fakeredis 注入缓存(不依赖真 redis
与 smart_sso),验证:

- 合法 token → 正确 :class:`OperatorContext`;
- 空 token → ``None``;
- 过期 / 签名不匹配 / issuer 不符 / 未知 kid → :class:`UnauthorizedException`。

运行(项目根)::

    PYTHONPATH=src python -m unittest tests.test_sso
"""

import json
import time
import unittest

import fakeredis.aioredis
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from api.deps import resolve_operator_from_sso
from conf.config import settings
from infra import redis_client
from infra.exceptions import UnauthorizedException
from security.jwks_client import jwks_resolver


def _gen_keypair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def _private_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class ResolveOperatorFromSsoTest(unittest.IsolatedAsyncioTestCase):
    """resolve_operator_from_sso:RS256 验签 + claims 解析。"""

    async def asyncSetUp(self) -> None:
        self.private, self.public = _gen_keypair()
        self.kid = "test-kid"
        jwk = json.loads(RSAAlgorithm.to_jwk(self.public))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        self.jwks = {"keys": [jwk]}

        # fakeredis 替换模块级单例
        self._orig_redis = redis_client._redis
        redis_client._redis = fakeredis.aioredis.FakeRedis()
        # 预置 JWKS 缓存,使 jwks_resolver 命中缓存(不回源 httpx)
        await redis_client._redis.set(jwks_resolver._jwks_key, json.dumps(self.jwks))

        # issuer 与 token 一致(jwt.decode 会校验)
        self._orig_issuer = settings.sso_issuer
        self._orig_audience = settings.sso_audience
        settings.sso_issuer = "test-iss"
        settings.sso_audience = None

    async def asyncTearDown(self) -> None:
        redis_client._redis = self._orig_redis
        settings.sso_issuer = self._orig_issuer
        settings.sso_audience = self._orig_audience

    def _make_token(self, *, expired: bool = False, iss: str = "test-iss") -> str:
        now = int(time.time())
        payload = {
            "sub": "9527",
            "tenant_id": "1",
            "roles": ["employee"],
            "iss": iss,
            "iat": now,
            "exp": now - 10 if expired else now + 3600,
        }
        return jwt.encode(
            payload,
            _private_pem(self.private),
            algorithm="RS256",
            headers={"kid": self.kid},
        )

    async def test_valid_token_parses_operator(self):
        """合法 token → OperatorContext(user_id / tenant / roles 正确)。"""
        op = await resolve_operator_from_sso(self._make_token())
        self.assertEqual(op.user_id, "9527")
        self.assertEqual(op.tenant_id, "1")
        self.assertEqual(op.roles, ["employee"])

    async def test_empty_token_returns_none(self):
        """空 / 无 Bearer → None(交上层判 401)。"""
        self.assertIsNone(await resolve_operator_from_sso(""))
        self.assertIsNone(await resolve_operator_from_sso("Bearer "))

    async def test_expired_token_raises(self):
        """过期 token → UnauthorizedException。"""
        with self.assertRaises(UnauthorizedException):
            await resolve_operator_from_sso(self._make_token(expired=True))

    async def test_wrong_issuer_raises(self):
        """issuer 不符 → UnauthorizedException。"""
        with self.assertRaises(UnauthorizedException):
            await resolve_operator_from_sso(self._make_token(iss="other-iss"))

    async def test_bad_signature_raises(self):
        """用另一密钥签发(kid 相同但签名不匹配)→ 验签失败。"""
        other_priv, _ = _gen_keypair()
        now = int(time.time())
        token = jwt.encode(
            {"sub": "x", "iss": "test-iss", "iat": now, "exp": now + 3600},
            _private_pem(other_priv),
            algorithm="RS256",
            headers={"kid": self.kid},
        )
        with self.assertRaises(UnauthorizedException):
            await resolve_operator_from_sso(token)

    async def test_unknown_kid_raises(self):
        """token kid 不在 JWKS → UnauthorizedException(回源失败或未找到)。"""
        now = int(time.time())
        token = jwt.encode(
            {"sub": "x", "iss": "test-iss", "iat": now, "exp": now + 3600},
            _private_pem(self.private),
            algorithm="RS256",
            headers={"kid": "unknown-kid"},
        )
        with self.assertRaises(UnauthorizedException):
            await resolve_operator_from_sso(token)


if __name__ == "__main__":
    unittest.main()
