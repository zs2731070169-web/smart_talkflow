"""流程步骤留痕(``process_step`` 表)的读写封装。

职责:把 workflow 每一步的执行过程结构化落库,支撑「全程可追溯」与 Saga 补偿状态机。
与 :mod:`orchestrator.idempotency`(流程级幂等)同款风格——纯 DB 操作、显式 ``flush``、
落库失败只记日志不阻断主流程(留痕丢失不应让业务调用失败)。

调用方约定:workflow 在**每步执行前**调 :func:`create_step` 拿到 ``step_id``
(并 ``set_step_id(step_id)`` 供 adapter 审计留痕关联),执行后调 :func:`finish_step`
写结果;补偿时调 :func:`mark_compensation` 标记补偿状态。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy.exc import SQLAlchemyError

from infra.database import db_session
from infra.logger import setup_logging
from repository.models import ProcessStep

logger = setup_logging(__name__)


class StepStatus(str, Enum):
    """:class:`ProcessStep` 的执行状态(对齐 ``db/schema_diagram.md``)。"""

    RUNNING = "running"  # 执行中(create_step 占位)
    COMPLETED = "completed"  # 执行成功
    FAILED = "failed"  # 执行失败


class CompensationStatus(str, Enum):
    """:class:`ProcessStep` 的补偿状态。"""

    NONE = "none"  # 未补偿(成功步或尚无需补偿)
    DONE = "done"  # 补偿成功
    FAILED = "failed"  # 补偿失败(交人工/对账)


def _datetime_now() -> datetime:
    """当前 UTC 时间(naive,与 MySQL DATETIME 列对齐)。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_step(
        process_id: int,
        step_no: int,
        step_key: str,
        step_name: str,
        adapter: str,
        action: str,
        input_params: dict | None = None,
) -> int | None:
    """插入一条 ``status=running`` 的 :class:`ProcessStep` 占位,返回自增主键 id。

    显式 ``await session.flush()`` 拿 id
    落库失败返回 ``None``,调用方据此不 ``set_step_id``(该步留痕丢失但流程继续)。

    :param process_id: 所属流程实例 id
    :param step_no: 步骤序号(从 1 开始)
    :param step_key: 步骤标识(如 ``submit_booking``)
    :param step_name: 步骤中文名
    :param adapter: 适配器名(如 ``oa``)
    :param action: 动作名(如 ``submit_booking``)
    :param input_params: 步骤入参(落 JSON 列)
    """
    try:
        async with db_session() as session:
            step = ProcessStep(
                process_id=process_id,
                step_no=step_no,
                step_key=step_key,
                step_name=step_name,
                adapter=adapter,
                action=action,
                status=StepStatus.RUNNING.value,
                input_params=input_params,
                started_at=_datetime_now(),
            )
            session.add(step)
            # autoflush=False:必须显式 flush 才能拿到自增主键
            await session.flush()
            return step.id
    except SQLAlchemyError:
        logger.exception("创建 ProcessStep 失败(step_no=%s key=%s)", step_no, step_key)
        return None


async def finish_step(
        step_id: int,
        *,
        status: StepStatus,
        output_result: dict | None = None,
        external_ref: str | None = None,
        error_message: str | None = None,
        duration_ms: int | None = None,
) -> None:
    """更新步骤执行结果(:attr:`StepStatus.COMPLETED` / :attr:`StepStatus.FAILED`)与产出。

    :param step_id: :func:`create_step` 返回的步骤 id
    :param status: 终态枚举
    :param output_result: 步骤产出(落 JSON 列,如下游响应体)
    :param external_ref: 外部业务键(如 yudao 返回的 bookingId,补偿时回取)
    :param error_message: 失败原因
    :param duration_ms: 执行耗时(毫秒)
    """
    try:
        async with db_session() as session:
            row = await session.get(ProcessStep, step_id)
            if row is None:
                return
            row.status = status.value
            row.output_result = output_result
            if external_ref is not None:
                row.external_ref = str(external_ref)
            row.error_message = error_message
            row.duration_ms = duration_ms
            row.finished_at = _datetime_now()
            await session.flush()
    except SQLAlchemyError:
        logger.exception("更新 ProcessStep 失败(step_id=%s)", step_id)


async def update_compensation(step_id: int, status: CompensationStatus) -> None:
    """更新步骤的补偿状态(:attr:`CompensationStatus.DONE` / :attr:`CompensationStatus.FAILED`)。

    Saga 逆序补偿时,对每个已成功步调用:补偿成功标 ``done``,补偿失败标 ``failed``
    交人工(不吞错——补偿失败意味着 yudao 侧可能残留,需可见)。
    """
    try:
        async with db_session() as session:
            row = await session.get(ProcessStep, step_id)
            if row is None:
                return
            row.compensation_status = status.value
            await session.flush()
    except SQLAlchemyError:
        logger.exception("标记 ProcessStep 补偿状态失败(step_id=%s status=%s)", step_id, status)