"""process 表(流程实例)的持久化封装。

与 :mod:`repository.step_tracker`(``process_step`` 表)并列——这两张孪生表的
所有读写操作统一收口在 repository 层。本模块只做「持久化 + 事务/并发兜底」,
**不含幂等状态机判定**:`status` 的合法取值、状态迁移的前驱约束(``_FROM_*``)、
命中分流决策属于编排领域逻辑,见 :mod:`orchestrator.idempotency`。

风格同 step_tracker:显式 ``flush``(``autoflush=False``)、唯一索引冲突回查兜底、
落库异常不向上抛(由调用方按需处理)。

调用方约定:

    - :func:`acquire_or_create`:幂等闸门的持久化部分——先查后插 + 冲突回查,
      返回 :class:`AcquiredProcess`(命中已存在记录 / 本次新建 / 冲突回查未命中)。
    - :func:`transition_status`:带前驱约束的单次状态迁移(completed/failed/reject)。
    - :func:`increment_attempt`:累加重试计数。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from infra.database import db_session
from repository.models import Process


@dataclass(frozen=True)
class AcquiredProcess:
    """幂等占位的获取结果。

    :ivar process: 命中的已存在记录,或本次新建的 ``running`` 占位;
        并发冲突且回查未命中时为 ``None``(此时调用方应判定为 REJECT)。
    :ivar is_new: 是否本次新建。命中已存在记录(含冲突回查命中)时为 ``False``。
    """

    process: Process | None
    is_new: bool


def _datetime_now() -> datetime:
    """当前 UTC 时间(naive,与 MySQL DATETIME 列对齐)。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def acquire_or_create(
        *,
        process_key: str,
        business_key: str,
        idempotency_key: str,
        status: str,
        input_params: dict | None,
        context: dict | None,
        created_by: str | None,
        trace_id: str | None,
        request_log_id: int | None,
) -> AcquiredProcess:
    """幂等占位获取:先查后插 + 唯一索引冲突回查(并发兜底)。

    流程::

        1. 按 idempotency_key 查 process 表;命中 -> 直接返回(is_new=False)
        2. 未命中 -> 插入 status 占位并 flush:
             - 正常 -> 返回新建记录(is_new=True)
             - IntegrityError(并发同 key 撞唯一索引)-> rollback 回查
               -> 返回回查结果(is_new=False)

    :param status: 新占位的初始状态(由编排层决定,幂等场景为 ``running``)。
    :returns: :class:`AcquiredProcess`;``process`` 可能为 ``None``
        (并发冲突回查仍未命中),调用方据此判定 REJECT。
    """
    async with db_session() as session:
        process = await _find_by_key(session, idempotency_key)
        if process is not None:
            return AcquiredProcess(process=process, is_new=False)

        process = Process(
            process_key=process_key,
            business_key=business_key,
            idempotency_key=idempotency_key,
            status=status,
            input_params=input_params,
            context=context,
            created_by=created_by,
            trace_id=trace_id,
            request_log_id=request_log_id,
            started_at=_datetime_now(),
        )
        session.add(process)
        try:
            # autoflush=False:必须显式 flush 才会触发 MySQL 唯一性校验
            await session.flush()
        except IntegrityError:
            # 并发同 key:flush 撞 uk_idempotency_key。必须回查而非直接判定,
            # 因为事务状态会动态变化(对方可能已推进终态)。
            await session.rollback()
            process = await _find_by_key(session, idempotency_key)
            return AcquiredProcess(process=process, is_new=False)

        # 正常退出 with 块时 db_session() 自动 commit 落库
        return AcquiredProcess(process=process, is_new=True)


async def transition_status(
        process_id: int,
        target_status: str,
        from_status: set[str],
        extra: dict | None = None,
) -> bool:
    """带前驱约束的状态迁移:仅当当前 status ∈ from_status 时才推进到 target。

    用于幂等状态机的终态收尾(completed/failed/reject)。前驱集合 ``from_status``
    由编排层传入,承载「避免跨态跳跃」的业务规则(见 ``orchestrator.idempotency``
    的 ``_FROM_*`` 常量)。

    :returns: 是否实际推进(前驱不满足则静默跳过,返回 ``False``)。
    """
    async with db_session() as session:
        row = await session.get(Process, process_id)
        if row is None or row.status not in from_status:
            return False
        row.status = target_status
        for field, value in (extra or {}).items():
            setattr(row, field, value)
        row.finished_at = _datetime_now()
        await session.flush()
        return True


async def increment_attempt(process_id: int) -> None:
    """累加 ``context.attempt``(FAILED 重试计数,供超 ``max_retry`` 判 reject)。"""
    async with db_session() as session:
        row = await session.get(Process, process_id)
        if row is None:
            return
        ctx = dict(row.context or {})
        ctx["attempt"] = int(ctx.get("attempt", 1)) + 1
        row.context = ctx
        await session.flush()


async def _find_by_key(session, idempotency_key: str) -> Process | None:
    stmt = select(Process).where(Process.idempotency_key == idempotency_key)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
