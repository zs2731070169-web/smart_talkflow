"""数据库连接与会话管理。

基于 SQLAlchemy 2.x 异步引擎 + 内置连接池(``QueuePool``),提供进程级
``async_engine``、会话工厂 ``AsyncSessionLocal`` 以及异步上下文管理器
``db_session``。

引擎与会话工厂由 :func:`init_engine` 显式装配(在 ``build_runtime`` 启动装配时
调用一次),不再「导入即创建」——``db_session`` 须待装配完成方可使用。

业务代码推荐用法::

    from infra.database import db_session

    async with db_session() as session:
        result = await session.execute(select(...))
        session.add(obj)
    # 正常退出自动提交;异常自动回滚,连接归还连接池
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from conf.config import settings

# 进程级单例:由 init_engine() 装配,初始为 None(装配前 db_session 不可用)。
async_engine: AsyncEngine | None = None
AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None


def create_engine(
    *,
    db_url: str = settings.mysql_conf,  # mysql+asyncmy://user:pwd@host:port/db?charset=utf8mb4
    pool_size: int = settings.pool_size,
    max_overflow: int = settings.max_size,
    pool_recycle: int = settings.keep_alive,
    pool_pre_ping: bool = True,  # 取连接前先 ping,丢弃已失效连接
    echo: bool = settings.sql_log,
) -> AsyncEngine:
    """根据配置构建异步引擎(含连接池参数)"""
    return create_async_engine(
        db_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=pool_recycle,
        pool_pre_ping=pool_pre_ping,
        echo=echo,
    )


def init_engine(
    *,
    db_url: str = settings.mysql_conf,  # mysql+asyncmy://user:pwd@host:port/db?charset=utf8mb4
    pool_size: int = settings.pool_size,
    max_overflow: int = settings.max_size,
    pool_recycle: int = settings.keep_alive,
    pool_pre_ping: bool = True,  # 取连接前先 ping,丢弃已失效连接
    echo: bool = settings.sql_log,
) -> None:
    """装配进程级引擎与会话工厂(build_runtime 启动装配时调用一次)"""
    global async_engine, AsyncSessionLocal
    async_engine = create_engine(
        db_url=db_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=pool_recycle,
        pool_pre_ping=pool_pre_ping,
        echo=echo,
    )
    AsyncSessionLocal = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,  # 提交事务后，不将对象标记为"过期", 否则异步场景下会报错. commit() 后对象仍可直接访问，无需重新查询
        autoflush=False,  # 禁止自动 flush，改为手动控制. 数据写入完全由你控制，必须通过  await session.flush()  显式执行sql
    )


def _require_session_factory() -> async_sessionmaker[AsyncSession]:
    """取已装配的会话工厂;未装配时抛 RuntimeError。"""
    if AsyncSessionLocal is None:
        raise RuntimeError("DB 引擎未装配,请先在 build_runtime 中调用 init_engine()")
    return AsyncSessionLocal


@asynccontextmanager
async def db_session() -> AsyncIterator[AsyncSession]:
    """异步会话上下文管理器:自动管理提交 / 回滚与连接归还。

    正常退出 -> ``commit``;抛异常 -> ``rollback`` 并重新抛出。无论哪种情况,
    会话都会被关闭、底层连接归还连接池。
    """
    async with _require_session_factory()() as session:
        try:
            yield session  # 暂停, 把 session 交给业务代码执行
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """关闭引擎、释放连接池(应用停机时调用一次)。"""
    if async_engine is not None:
        await async_engine.dispose()
