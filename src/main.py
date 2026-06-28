"""smart_talkflow FastAPI 应用入口。

装配 FastAPI app、挂载路由;停机时释放数据库引擎与 redis 连接池。

运行(项目根)::

    PYTHONPATH=src uv run uvicorn main:app --port 8000 --reload
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import infra.database as _db
from api.router import router
from infra.exceptions import ApiException
from infra.redis_client import close_redis
from runtime.heartbeat import heartbeat_watchdog
from runtime.runner import RuntimeBundle, build_runtime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期: 启动装配运行时 + 后台看门狗"""
    runtime = build_runtime(RuntimeBundle())
    app.state.runtime = runtime
    watchdog_task = asyncio.create_task(heartbeat_watchdog(runtime.registry))
    try:
        yield
    finally:
        # 发送任务取消信号, cancel()向任务抛入一个 CancelledError 异常
        watchdog_task.cancel()
        # 等待任务真正结束, return_exceptions=True  把 CancelledError 吞掉，避免抛到外层导致报错
        await asyncio.gather(watchdog_task, return_exceptions=True)
        await _db.dispose_engine()
        await close_redis()


app = FastAPI(title="smart_talkflow", lifespan=lifespan)


@app.exception_handler(ApiException)
async def _api_exception_handler(_request: Request, exc: ApiException) -> JSONResponse:
    """统一把 ApiException(含 401/403/404 等子类)映射为其 status_code + detail。"""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(router)
