"""Redis 客户端(异步)。

进程级单例(参照 :mod:`infra.database`),供 JWKS 公钥缓存等需要 KV 缓存的场景复用。
连接由 :func:`init_redis` 显式装配(在 ``build_runtime`` 启动装配时调用一次)

用法::

    from infra.redis_client import get_redis

    redis = get_redis()
    await redis.set("jwks:xxx", json_str, ex=3600)
    cached = await redis.get("jwks:xxx")
"""

from __future__ import annotations

import redis.asyncio as redis

from conf.config import settings

# 进程级单例:由 init_redis() 装配,初始为 None(装配前 get_redis 不可用)。
_redis: redis.Redis | None = None


def init_redis(
    *,
    redis_url: str = settings.redis_url,  # redis://host:port/db
    decode_responses: bool = True,  # 返回 str 而非 bytes,JWKS JSON 可直接用
) -> None:
    """装配进程级 Redis 连接池(build_runtime 启动装配时调用一次)。"""
    global _redis
    _redis = redis.from_url(redis_url, decode_responses=decode_responses)


def get_redis() -> redis.Redis:
    """返回已装配的进程级 Redis 单例;未装配时抛 RuntimeError。"""
    if _redis is None:
        raise RuntimeError("Redis 未装配,请先初始化 init_redis()")
    return _redis


async def close_redis() -> None:
    """关闭连接池(应用停机时调用一次)。"""
    if _redis is not None:
        await _redis.aclose()
