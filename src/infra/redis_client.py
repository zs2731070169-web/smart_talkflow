"""Redis 客户端(异步)。

进程级单例(参照 :mod:`infra.database`),供 JWKS 公钥缓存等需要 KV 缓存的场景复用。
连接串来自 :attr:`settings.redis_url`(默认 ``redis://127.0.0.1:6379/0``)。

用法::

    from infra.redis_client import get_redis

    redis = get_redis()
    await redis.set("jwks:xxx", json_str, ex=3600)
    cached = await redis.get("jwks:xxx")
"""

from __future__ import annotations

import redis.asyncio as redis

from conf.config import settings

# 进程级单例:import 时建连接池(延迟连接,首次命令才真正连)。
# decode_responses=True:返回 str 而非 bytes,JWKS JSON 可直接用。
_redis: redis.Redis = redis.from_url(settings.redis_url, decode_responses=True)


def get_redis() -> redis.Redis:
    """返回进程级 Redis 单例。"""
    return _redis


async def close_redis() -> None:
    """关闭连接池(应用停机时调用一次)。"""
    await _redis.aclose()
