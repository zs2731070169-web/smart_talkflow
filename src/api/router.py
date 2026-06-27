"""/chat 路由:认证 → runtime(已装配)轻量 run → SSE 流式回复。

``/chat`` 仅做 HTTP 适配:``Depends(get_current_operator)`` 认证拿 operator,
从 ``app.state.runtime`` 取**启动时装配好**的 :class:`runtime.runner.Runtime`,
其 :meth:`~runtime.runner.Runtime.run` 已流式产出 SSE ``data:`` 行,router 直接
``StreamingResponse`` 返回——SSE 序列化归 runtime,runtime 不在请求内重新装配。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from api.deps import get_current_operator
from api.schema import ChatRequest
from runtime.context import OperatorContext
from runtime.runner import Runtime

router = APIRouter()


@router.post("/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    operator: Annotated[OperatorContext, Depends(get_current_operator)],
) -> StreamingResponse:
    """接收用户输入 → runtime 意图解析 → SSE 流式回复。"""
    runtime: Runtime = request.app.state.runtime
    return StreamingResponse(
        runtime.run(operator, req.user_input),
        media_type="text/event-stream",
        headers={"X-Operator-Userid": operator.user_id},
    )
