"""流程级幂等校验(状态机判定层)。

职责:对「同一流程(``process_key``)+ 同一业务唯一键(``business_key``)」
保证只执行一次。幂等键约定 ``{process_key}_{business_key}``,与 ``process`` 表的
``uk_process_business (process_key, business_key)`` / ``uk_idempotency_key`` 唯一索引对齐。

判定流程(经 :func:`acquire_or_create` 拿到占位/命中记录后分流;首次 ``is_new=True`` 直接执行)::

    completed -> 返回历史结果(跳过重复执行)
    running   -> 拒绝(并发重入执行)
    failed    -> 返回失败(交上层决策是否重跑)
    None      -> 拒绝(并发冲突回查未命中)
    其它中间态 -> 拒绝
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from repository.models import Process
from repository.process_tracker import (
    acquire_or_create,
    transition_status,
)

# 幂等键最大长度
_MAX_IDEM_KEY_LEN = 160


class Status(StrEnum):
    """process.status 取值(DB 状态)。"""

    RUNNING = "running"  # 执行中(含新建占位)
    COMPLETED = "completed"  # 完成
    FAILED = "failed"  # 失败


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
    if len(key) > _MAX_IDEM_KEY_LEN:
        key = key[:_MAX_IDEM_KEY_LEN]
    return key


@dataclass(frozen=True)
class IdempotencyCheckRequest:
    """幂等校验请求参数"""

    business_key: str  # 业务唯一键
    input_params: dict | None = None  # 工作流业务入参
    context: dict | None = None  # 工作流传递上下文
    created_by: str | None = None  # 该工作任务创建人
    trace_id: str | None = None  # 全链路追踪id
    request_log_id: int | None = None  # 关联的请求日志id
    operator_context: dict | None = None  # 操作人身份(失联重跑重建代签用)


@dataclass(frozen=True)
class IdempotencyChecked:
    """幂等校验决策结果。"""

    process: Process | None = None  # 执行的任务
    is_new: bool = False  # 首次(本次 check 新建占位);dispatcher 据此放行执行
    complete_result: dict | None = None  # 命中 completed 时的历史结果
    error: str | None = None  # 命中 failed 时的错误信息
    context: dict | None = None  # 任务执行的上下文参数
    message: str = ""  # 决策消息


class IdempotencyChecker:
    """流程级幂等校验器。

    一个 checker 绑定一个 :attr:`process_key`(对应一个 workflow);:meth:`check`
    为幂等准入闸门,持久化部分(先查后插 + 并发兜底)委托
    :mod:`repository.process_tracker`。
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

        持久化(先查后插 + 并发兜底)由 :func:`acquire_or_create` 完成,
        本方法只做参数校验与命中分流决策。

        :param check_request: 幂等校验请求
        """
        business_key = (check_request.business_key or "").strip()
        if not business_key:
            raise ValueError("business_key 不能为空")

        idem_key = build_idempotency_key(self._process_key, business_key)

        context = dict(check_request.context or {})

        acquired = await acquire_or_create(
            process_key=self._process_key,
            business_key=business_key,
            idempotency_key=idem_key,
            status=Status.RUNNING,
            input_params=check_request.input_params,
            context=context,
            created_by=check_request.created_by,
            trace_id=check_request.trace_id,
            request_log_id=check_request.request_log_id,
            operator_context=check_request.operator_context,
        )
        if acquired.is_new:
            return IdempotencyChecked(
                process=acquired.process,
                is_new=True,
                message="首次执行,已新建 process 记录",
            )
        return self._check_process(acquired.process)

    @staticmethod
    async def completed(process: Process, result: dict) -> None:
        """任务执行成功后更新状态为 completed(仅可由 running 推进)。"""
        await transition_status(process.id, Status.COMPLETED, Status.RUNNING, {"result": result})

    @staticmethod
    async def failed(process: Process, error: str) -> None:
        """任务执行失败后状态更新为 failed(仅可由 running 推进)。"""
        await transition_status(process.id, Status.FAILED, Status.RUNNING, {"error_message": error})

    @staticmethod
    def _check_process(process: Process | None) -> IdempotencyChecked:
        """按已存在任务的 status 生成决策(纯逻辑,不碰 DB)。"""
        if process is None:
            # 并发事务情况下出现唯一键冲突异常: IntegrityError 回查又未命中。
            return IdempotencyChecked(
                process=None,
                message="任务并发冲突且回查未命中, 拒绝执行",
            )
        status = process.status
        if status == Status.COMPLETED:
            return IdempotencyChecked(
                process=process,
                complete_result=process.result,
                message=f"命中已完成流程(id={process.id}),无需重新执行",
            )
        if status == Status.RUNNING:
            return IdempotencyChecked(
                process=process,
                message=f"流程进行中(id={process.id}),拒绝执行",
            )
        if status == Status.FAILED:
            return IdempotencyChecked(
                process=process,
                error=process.error_message,
                context=process.context,
            )
        # pending 等其它中间态: 直接拒绝执行
        return IdempotencyChecked(
            process=process,
            message=f"流程处于非终态 {status}(id={process.id}),拒绝执行",
        )
