"""smart_talkflow FastAPI 应用入口。

装配 FastAPI app、挂载路由;停机时释放数据库引擎与 redis 连接池。

运行(项目根)::

    PYTHONPATH=src uv run uvicorn main:app --port 8000 --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import infra.database as _db
from api.router import router
from infra.exceptions import ApiException
from infra.redis_client import close_redis
from runtime.runner import build_runtime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期:启动装配运行时一次;停机释放数据库引擎与 redis 连接池。"""
    app.state.runtime = build_runtime()
    try:
        yield
    finally:
        await _db.async_engine.dispose()
        await close_redis()


app = FastAPI(title="smart_talkflow", lifespan=lifespan)


@app.exception_handler(ApiException)
async def _api_exception_handler(_request: Request, exc: ApiException) -> JSONResponse:
    """统一把 ApiException(含 401/403/404 等子类)映射为其 status_code + detail。"""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(router)