"""声明式步骤执行引擎:步骤声明 + 顺序执行 + 留痕 + 失败逆序补偿。

从 :mod:`orchestrator.base` 抽出,职责单一——只管「步骤怎么跑」。
:func:`run_steps` 由 :meth:`orchestrator.base.BaseWorkflow.execute` 委托调用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from pydantic import BaseModel

from adapters import AdapterResponse
from infra.logger import setup_logging
from orchestrator.base import WorkflowResult
from repository.step_tracker import (
    CompensationStatus,
    StepStatus,
    create_step,
    finish_step,
    update_compensation,
)
from runtime.context import set_step_id

logger = setup_logging(__name__)


@dataclass
class StepContext:
    """步骤执行上下文: 内部累积的步骤状态,供后续步与补偿步访问提供入参"""

    results: dict[str, AdapterResponse] = field(default_factory=dict)


@dataclass
class WorkflowStep:
    """工作流单步声明"""

    step_no: int
    step_key: str
    step_name: str
    adapter: str
    action: str
    next: Callable[[StepContext], Awaitable[AdapterResponse]]  # 执行步骤
    compensate: Callable[[StepContext], Awaitable[AdapterResponse]] | None = None  # saga补偿(可选)
    input_params: dict | None = None  # 入参快照


@dataclass
class DoneStep:
    """已成功步(供saga补偿使用)"""

    step: WorkflowStep
    step_id: int | None


class StepMeta(BaseModel):
    """单步执行留痕"""

    step_no: int
    step_key: str
    step_name: str
    adapter: str
    action: str
    status: str
    input: dict
    output: dict
    error_message: str | None
    duration_ms: int


async def _exec_step(
        process_id, step: WorkflowStep, ctx: StepContext, steps_meta: list[StepMeta],
):
    """执行单步:建占位 → set_step_id → forward → 落结果 → 记录 meta。"""

    # 新建执行步骤记录落库
    step_id = await create_step(
        process_id, step.step_no, step.step_key, step.step_name,
        step.adapter, step.action, step.input_params,
    )

    # 回填当前执行步骤id到运行时上下文
    set_step_id(step_id)

    try:
        # 执行当前步骤, 并把前面步骤的结果 ctx 作为当前步输入参数
        resp: AdapterResponse = await step.next(ctx)
    except (KeyError, ValueError, TypeError) as exc:
        # next 抛异常时归一为失败,交 run_steps 失败分支补偿
        logger.exception("步骤 %s 执行异常", step.step_key)
        resp = AdapterResponse(
            adapter=step.adapter,
            target_system=step.adapter,
            action=step.action,
            method="",
            is_error=True,
            error_message=f"步骤执行异常: {exc}",
        )
    finally:
        set_step_id(None)

    # 更新当前步执行状态
    await finish_step(
        step_id,
        status=StepStatus.FAILED if resp.is_error else StepStatus.COMPLETED,
        output_result=resp.response_payload,
        error_message=resp.error_message,
        duration_ms=resp.duration,
    )

    # 步骤留痕: 添加每个执行步骤完以后的状态
    steps_meta.append(StepMeta(
        step_no=step.step_no,
        step_key=step.step_key,
        step_name=step.step_name,
        adapter=resp.adapter,
        action=resp.action,
        status="success" if not resp.is_error else "failed",
        input=resp.request_payload,
        output=resp.response_payload,
        error_message=resp.error_message,
        duration_ms=resp.duration,
    ))

    return resp, step_id


async def _compensate(done_step_list: list[DoneStep], ctx: StepContext):
    """对失败步骤的补偿操作

    :param done_step_list: 已执行过的步骤列表
    :param ctx: 上一步的执行结果(当前步的执行入参)
    """
    for done in reversed(done_step_list):
        # 跳过不需要补偿的步骤
        if done.step.compensate is None:
            continue

        try:
            # 对需要补偿的步骤执行补偿操作
            cancel_resp = await done.step.compensate(ctx)
        except (KeyError, ValueError, TypeError):
            logger.exception("补偿异常(step_id=%s key=%s)", done.step_id, done.step.step_key)
            # 如果补偿执行异常, 更新补偿记录的状态为失败, 并跳过该步补偿
            if done.step_id is not None:
                await update_compensation(done.step_id, CompensationStatus.FAILED)
            continue

        # 如果补偿成功, 更新补偿记录的状态为成功/失败
        if done.step_id is not None:
            await update_compensation(
                done.step_id,
                CompensationStatus.DONE if cancel_resp.ok else CompensationStatus.FAILED,
            )


async def run_steps(process_id: int | None, steps: list[WorkflowStep]) -> WorkflowResult:
    """顺序执行声明的步骤,任一步失败逆序补偿,返回流程结果。"""
    ctx = StepContext()  # 每个执行步骤上下文, 当前步的输出做为下一步的输入
    done_step_list: list[DoneStep] = []  # 已成功步(供补偿回退, 保证一致性)
    steps_meta: list[StepMeta] = []  # 步骤留痕, 每一步可观测

    for step in steps:
        # 按顺序执行步骤
        resp, step_id = await _exec_step(process_id, step, ctx, steps_meta)

        # 如果某步出错, 对前面成功的步骤 (done_step_list) 执行saga补偿操作
        if resp.is_error:
            await _compensate(done_step_list, ctx)
            return WorkflowResult(
                output=(
                    f"流程在 [{steps_meta[-1].step_name if steps_meta else ""}] 步骤失败:{resp.error_message}"
                    f"(已自动补偿 {sum(1 for done in done_step_list if done.step.compensate is not None)} 个可补偿步骤)"
                ),
                is_error=True,
                retryable=True,
                metadata={"completed_steps": len(steps_meta) - 1, "steps": steps_meta},
            )

        # 若当前步执行成功, 就添加当前步骤的执行结果到步骤执行上下文, 供后续步骤和saga补偿作为入参
        ctx.results[step.step_key] = resp

        # 成功执行的步骤添加已完成步骤列表, 供后续步骤的saga补偿
        done_step_list.append(DoneStep(step, step_id))

    return WorkflowResult(
        output="流程执行完成",
        is_error=False,
        metadata={"steps": steps_meta},
    )