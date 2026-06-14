from __future__ import annotations

import uuid
from contextvars import ContextVar

# 全局链路追踪 ID。缺省 None 表示尚未生成(此时日志里输出占位符 "-")。
trace_id_context: ContextVar[str | None] = ContextVar("trace_id", default=None)


def get_trace_id() -> str | None:
    """读取当前上下文的 trace_id,可能为 ``None``。"""
    return trace_id_context.get()

def new_trace_id():
    """生成一个新的 trace_id 并写入当前上下文,返回该值。"""
    trace_id = uuid.uuid4().hex
    trace_id_context.set(trace_id)