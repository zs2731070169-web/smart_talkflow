"""流程级幂等校验(阶段一:DB 表查 + 唯一索引兜底)。

职责:对「同一流程(``process_key``)+ 同一业务唯一键(``business_key``)」
保证只执行一次。幂等键约定 ``{process_key}_{business_key}``,与 ``process`` 表的
``uk_process_business (process_key, business_key)`` / ``uk_idempotency_key`` 唯一索引对齐。

机制(先查后插 + 冲突回查)::

    1. 先按 idempotency_key 查 process 表,命中则按 status 分流:
         completed -> 跳过执行
         running   -> 拒绝并发重入执行
         reject    -> 达到最大重试次数拒绝执行
         failed    -> 失败允许重新执行
         其他状态   -> 拒绝执行
    2. 未命中 -> 插入一条 status=running 的 Process 占位, 返回状态 new -> 允许执行
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from infra.database import db_session
from repository.models import Process

# 幂等键最大长度
_MAX_IDEM_KEY_LEN = 160

# Process.status 取值
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_REJECT = "reject"

# 状态转换映射, 避免跨态跳跃
_FROM_COMPLETED = {STATUS_RUNNING}
_FROM_FAILED = {STATUS_RUNNING}
_FROM_REJECT = {STATUS_FAILED, STATUS_RUNNING}


def _datetime_now() -> datetime:
    """当前 UTC 时间(naive,与 MySQL DATETIME 列对齐)。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def build_idempotency_key(process_key: str, business_key: str) -> str:
    """生成幂等键:``{process_key}_{business_key}``。

    :param process_key: 流程标识(``workflow.name``, 如 ``onboarding``)
    :param business_key: 业务唯一键
    :raises ValueError: ``process_key`` / ``business_key`` 为空或纯空白
    """
    # 先 strip 再判空,避免纯空白生成无效幂等键
    process_key, business_key = process_key.strip(), business_key.strip()
    if not process_key or not business_key:
        raise ValueError("process_key / business_key 均不能为空")
    # 构建唯一键
    key = f"{process_key}_{business_key}"
    # 超过最大长度直接截取
    if len(key) > _MAX_IDEM_KEY_LEN: key = key[:_MAX_IDEM_KEY_LEN]
    return key


class Action(str, Enum):
    """幂等判定后的下一步动作信号"""

    NEW = "new"  # 首次 new: 未命中,首次执行
    COMPLETED = "completed"  # 命中 completed: 返回历史结果,跳过重复执行
    REJECT = "reject"  # 命中 running: 拒绝重执行
    FAILED = "failed"  # 命中 failed: 交上层 llm 决策是否重跑


@dataclass(frozen=True)
class IdempotencyCheckRequest:
    """幂等校验请求参数"""

    business_key: str  # 业务唯一键
    input_params: dict | None = None  # 工作流业务入参
    context: dict | None = None  # 工作流传递上下文
    created_by: str | None = None  # 该工作任务创建人
    trace_id: str | None = None  # 全链路追踪id
    request_log_id: int | None = None  # 关联的请求日志id


@dataclass(frozen=True)
class IdempotencyChecked:
    """幂等校验决策结果。"""

    action: Action  # 当前任务的状态
    process: Process | None = None  # 执行的任务
    complete_result: dict | None = None  # 任务执行完成的结果
    error: str | None = None # 任务执行失败的错误信息
    context: dict | None = None # 任务执行的上下文参数
    message: str = "" # 决策消息


class IdempotencyChecker:
    """流程级幂等校验器。

    一个 checker 绑定一个 :attr:`process_key`(对应一个 workflow),:meth:`check` 。
    """

    def __init__(self, process_key: str) -> None:
        process_key = (process_key or "").strip()
        if not process_key:
            raise ValueError("process_key 不能为空")
        self._process_key = process_key

    @property
    def process_key(self) -> str:
        return self._process_key

    async def check(self, check_request: IdempotencyCheckRequest) -> IdempotencyChecked:
        """幂等准入闸门:判定本次执行是否放行,返回 :class:`IdempotencyChecked`。

        :param check_request:
        """
        business_key = (check_request.business_key or "").strip()
        if not business_key:
            raise ValueError("business_key 不能为空")

        idem_key = build_idempotency_key(self._process_key, business_key)

        async with db_session() as session:
            # 1. 先根据key查这个执行任务是否存在
            process = await self._select_process(session, idem_key)

            # 2. 核心校验逻辑: 命中 -> 分状态处理
            if process is not None:
                return self._check_process(process)

            # context 初始化 attempt=1(已执行次数,含首次),供 FAILED 重试计数
            context = dict(check_request.context or {})
            context.setdefault("attempt", 1)

            # 3. 未命中 -> 新建 running 状态
            process = Process(
                process_key=self._process_key,
                business_key=business_key,
                idempotency_key=idem_key,
                status=STATUS_RUNNING,
                input_params=check_request.input_params,
                context=context,
                created_by=check_request.created_by,
                trace_id=check_request.trace_id,
                request_log_id=check_request.request_log_id,
                started_at=_datetime_now(),
            )
            session.add(process)
            try:
                # autoflush=False:必须显式 flush 执行sql
                # mysql在执行sql阶段会进行唯一性校验, 可能抛出唯一索引冲突 IntegrityError;
                await session.flush()
            except IntegrityError:
                # 并发异常处理, flush 失败的事物会重新查询进程执行状态, 并返回对应动作
                # 这里必须重新进行查询和状态匹配, 而不是直接REJECT, 因为事物状态会动态更新
                await session.rollback()
                # 查询以后的process可能为 None、completed、failed、running...
                process = await self._select_process(session, idem_key)
                return self._check_process(process)

            # 正常退出 with 块时 db_session() 自动 commit 落库
            return IdempotencyChecked(
                action=Action.NEW,
                process=process,
                message="首次执行,已新建 process 记录",
            )

    @staticmethod
    async def _transition_status(
            process: Process,
            target_status: str,
            from_status: set[str],
            extra: dict | None = None,
    ) -> None:
        async with db_session() as session:
            row = await session.get(Process, process.id)
            if row is not None and row.status in from_status:
                row.status = target_status
                for field, value in (extra or {}).items():
                    setattr(row, field, value)
                row.finished_at = _datetime_now()
                await session.flush()

    @staticmethod
    async def completed(process: Process, result: dict):
        """任务执行成功后更新状态为 completed"""
        await IdempotencyChecker._transition_status(
            process, STATUS_COMPLETED, _FROM_COMPLETED, {"result": result}
        )

    @staticmethod
    async def failed(process: Process, error: str):
        """任务执行失败后状态更新为 failed"""
        await IdempotencyChecker._transition_status(
            process, STATUS_FAILED, _FROM_FAILED, {"error_message": error}
        )

    @staticmethod
    async def reject(process: Process, reason: str):
        """超过最大执行次数更新为 reject"""
        await IdempotencyChecker._transition_status(
            process, STATUS_REJECT, _FROM_REJECT, {"error_message": reason}
        )

    @staticmethod
    async def increment_attempt(process: Process):
        """累加已执行次数"""
        async with db_session() as session:
            row = await session.get(Process, process.id)
            if row is None:
                return
            ctx = dict(row.context or {})
            ctx["attempt"] = int(ctx.get("attempt", 1)) + 1
            row.context = ctx
            await session.flush()

    @staticmethod
    async def _select_process(session, idem_key: str) -> Process | None:
        stmt = select(Process).where(Process.idempotency_key == idem_key)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    def _check_process(process: Process | None) -> IdempotencyChecked:
        """按已存在任务的 status 生成决策"""
        if process is None:
            # 并发事物情况下出现唯一键冲突异常: IntegrityError 回查又未命中。
            return IdempotencyChecked(
                action=Action.REJECT,
                process=None,
                message="任务并发冲突且回查未命中, 拒绝执行",
            )
        status = process.status
        if status == STATUS_COMPLETED:
            return IdempotencyChecked(
                action=Action.COMPLETED,
                process=process,
                complete_result=process.result,
                message=f"命中已完成流程(id={process.id}),无需重新执行",
            )
        if status == STATUS_RUNNING:
            return IdempotencyChecked(
                action=Action.REJECT,
                process=process,
                message=f"流程进行中(id={process.id}),拒绝执行",
            )
        if status == STATUS_REJECT:
            # 超过最大执行次数被拒绝
            return IdempotencyChecked(
                action=Action.REJECT,
                process=process,
                error=process.error_message,
                context=process.context,
                message=f"该任务失败次数过多, 判定为不可恢复任务(id={process.id}), 拒绝执行, 需交人工处理",
            )
        if status == STATUS_FAILED:
            return IdempotencyChecked(
                action=Action.FAILED,
                process=process,
                error=process.error_message,
                context=process.context,
            )
        # pending 等其它中间态: 直接拒绝执行
        return IdempotencyChecked(
            action=Action.REJECT,
            process=process,
            message=f"流程处于非终态 {status}(id={process.id}),拒绝执行",
        )
