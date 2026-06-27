"""generator-based 工作流引擎。

业务 workflow 用同步 generator 声明步骤序列(``yield step(...)``),引擎 :func:`drive`
驱动:每步 :func:`_exec_step`(心跳/留痕/执行/异常处理),成功 ``gen.send(result.data)``
回灌闭包,失败 ``gen.throw(Compensate)`` 触发业务 ``except Compensate`` 补偿分支。
降级靠 :func:`_replay_steps` 重建 generator + 回填已成功步 ``result_data``。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Generator
from dataclasses import asdict, dataclass, field, replace
from time import perf_counter
from typing import Any

from orchestrator.base import WorkflowResult
from repository.process_tracker import flush_heartbeat
from repository.step_tracker import (
    StepStatus,
    create_step,
    finish_step,
    list_completed_steps,
)
from runtime.context import get_process_id, set_step_id

logger = logging.getLogger(__name__)


class Compensate(Exception):
    """引擎↔业务的补偿协议信号。

    一步失败时,drive 向 generator ``gen.throw(Compensate)``;业务的 create 用
    ``except Compensate`` 捕获进入补偿分支(``yield`` 补偿步,直接用闭包变量)。
    """


@dataclass(frozen=True)
class StepResult:
    """单步结果(引擎与副作用之间唯一的契约)。

    前 4 个字段由 adapter action 的 func 返回时填好 / 异常时由 ``_exec_step`` 归一;
    ``name``/``step_id`` 由引擎 ``_exec_step`` 用 ``replace`` 补齐。
    """

    ok: bool = True
    data: Any = None  # 成功产出(供 gen.send 回灌 generator 变量 + 落 result_data);失败时 None
    error: str | None = None
    name: str = ""
    step_id: int | None = None


@dataclass
class Step:
    """一步对外调用(声明式):func 直接返回 StepResult(adapter 层已把 AdapterResponse 转好)。"""

    func: Callable[..., Awaitable[StepResult]]
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    name: str = ""

    async def execute(self) -> StepResult:
        return await self.func(*self.args, **self.kwargs)


def step(func: Callable[..., Awaitable[StepResult]], *args: Any, name: str = "", **kwargs: Any) -> Step:
    """yield 工厂:``yield step(adapter.submit_booking, room_id=..., name="提交预订")``。"""
    return Step(func=func, args=args, kwargs=kwargs, name=name or getattr(func, "__name__", ""))


def _arg_names(func: Callable) -> list[str]:
    """取 func 的位置参数和关键字参数的形参名"""
    try:
        params = inspect.signature(func).parameters.values()
        return [
            param.name
            for param in params
            if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (ValueError, TypeError):
        return []


def _step_meta(step: Step) -> tuple[str, str, dict]:
    """从 adapter action callable 推断 (adapter, action, input_params)。

    func 形如 ``room_booking_adapter.submit_booking`` —— 通过 ``__self__``/``__name__`` 取。
    """
    instance = getattr(step.func, "__self__", None)
    adapter = getattr(instance, "adapter_name", None) or getattr(instance, "target_system", "") or "unknown"
    action = getattr(step.func, "__name__", "call")
    input_params = {**dict(zip(_arg_names(step.func), step.args, strict=False)), **step.kwargs}
    return adapter, action, input_params


async def _exec_step(step: Step, step_no: int) -> StepResult:
    """单步执行:心跳 → 留痕 → 执行(StepResult) → 写终态。"""
    process_id = get_process_id()

    # ① 续心跳(失联判定基准)
    await flush_heartbeat(process_id)

    # ② 推断元信息
    adapter, action, input_params = _step_meta(step)

    # ③ 留痕占位
    step_id = await create_step(
        process_id,
        step_no,
        step_key=step.name or action,
        step_name=step.name or action,
        adapter=adapter,
        action=action,
        input_params=input_params,
    )

    # ④ 回填,供 adapter_call_logs 关联
    if step_id is not None:
        set_step_id(step_id)

        # ⑤ 执行(func 返回 StepResult);
    logger.info("步 %d 开始:%s.%s", step_no, adapter, action)
    started = perf_counter()
    try:
        result = await step.execute()
    except Exception as exc:
        logger.warning("步 %d 异常归一:%s", step_no, exc)
        result = StepResult(ok=False, error=str(exc) or f"引擎未捕获异常: {exc!r}")
    else:
        result = replace(result, name=step.name, step_id=step_id)  # 引擎补声明元信息
    finally:
        set_step_id(None)

    # ⑥ 写终态(每步存 result_data,供降级回溯)
    duration_ms = int((perf_counter() - started) * 1000)
    await finish_step(
        step_id,
        status=StepStatus.COMPLETED if result.ok else StepStatus.FAILED,
        result_data=result.data if result.ok else None,
        error_message=result.error,
        duration_ms=duration_ms,
    )
    logger.info("步 %d 完成:ok=%s, %dms", step_no, result.ok, duration_ms)
    return result


async def drive(
    workflow, arguments, *, on_step: Callable[[StepResult], Awaitable[None]] | None = None
) -> WorkflowResult:
    """驱动 workflow.create generator:send 推进, 失败 throw Compensate 补偿。"""
    # 创建定义好的工作流编排, 返回生成器
    gen: Generator = workflow.create(arguments)

    step_results: list[StepResult] = []
    send_value: Any = None  # 首次 send(None) 启动

    try:
        while True:
            try:
                # send: 赋值当前yield表达式左边变量, 并且恢复下一个step执行
                step = gen.send(send_value)
            except StopIteration as stop:
                return WorkflowResult(
                    output=stop.value or "执行完成",
                    metadata={"steps": [asdict(step_result) for step_result in step_results]},
                )
            if not isinstance(step, Step):
                raise TypeError(f"create 必须 yield Step,得到 {type(step)}")

            result = await _exec_step(step, len(step_results) + 1)

            # 添加步骤结果
            step_results.append(result)

            # 输出步骤钩子
            if on_step:
                await on_step(result)

            # 执行结果赋值给 send_value, send给当前yield表达式
            send_value = result.data

            if not result.ok:
                # 失败:throw Compensate 触发补偿分支, 驱动补偿步直到 StopIteration
                logger.info("drive:步 %d 失败(%s),转补偿", len(step_results), result.error)
                return await compensate(gen, step_results, result, on_step)
    except Exception as exc:
        logger.exception("drive 异常:workflow=%s", getattr(workflow, "name", "?"))
        return WorkflowResult(
            output=f"流程异常: {exc!r}",
            is_error=True,
            metadata={"steps": [asdict(r) for r in step_results]},
        )


async def compensate(
    gen: Generator,
    step_results: list[StepResult],
    fail_step_result: StepResult,
    on_step: Callable[[StepResult], Awaitable[None]] | None,
) -> WorkflowResult:
    """gen.throw(Compensate) 触发 create 补偿分支,驱动补偿步直到 StopIteration。"""
    logger.info("补偿:throw Compensate(失败步=%s)", fail_step_result.name)

    try:
        # 失败 yield 处抛 → create 进 except → 返回首个补偿步
        step = gen.throw(Compensate(fail_step_result.error or "执行失败"))
    except StopIteration as stop:  # except 内无 yield,直接 return
        return WorkflowResult(
            output=stop.value or fail_step_result.error or "执行失败",
            is_error=True,
            metadata={"steps": [asdict(step_result) for step_result in step_results], "compensated": True},
        )
    except Compensate:  # create 没 except Compensate(无补偿)→ 直接失败
        return WorkflowResult(
            output=(fail_step_result and fail_step_result.error) or "执行失败",
            is_error=True,
            metadata={"steps": [asdict(result_step) for result_step in step_results]},
        )

    step_no = len(step_results) + 1

    while True:  # 驱动补偿步(终态/逆序都由 create 的 except 体决定)
        result = await _exec_step(step, step_no)
        logger.info("补偿步执行:%s ok=%s", result.name, result.ok)
        step_results.append(result)
        if on_step:
            await on_step(result)
        step_no += 1
        try:
            step = gen.send(None)  # 补偿步产出不赋值左边变量(create 闭包已有所需变量)
        except StopIteration as stop:
            return WorkflowResult(
                output=stop.value or fail_step_result.error or "执行失败,已补偿",
                is_error=True,
                metadata={"steps": [asdict(step_result) for step_result in step_results], "compensated": True},
            )


async def replay_steps(workflow, arguments, process_id: int) -> tuple[list[StepResult], Step | None, Generator]:
    """
    重建 generator
    replay 已成功步 result_data 回灌闭包变量
    定位失败步
    返回 generator 供补偿续跑
    """

    # ① 从 DB 取已成功步的 result_data,按 step_no 索引(供按步号查)
    completed = await list_completed_steps(process_id)  # [(step_no, step_key, result_data)]
    result_data_by_step = {step_no: result_data for step_no, _, result_data in completed}

    # ② 重建 generator(未启动、无副作用;create 只依赖 arguments)
    generator = workflow.create(arguments)

    # ③ replay 循环:send 推进 generator,用 DB 的 result_data 回灌闭包变量,直到失败点
    step_results, send_value, step_no = [], None, 0
    fail_step = None

    logger.info("replay:开始恢复闭包变量(已成功 %d 步)", len(completed))
    while True:
        try:
            step = generator.send(send_value)  # 推进到下一个 yield,拿到 step 声明
        except StopIteration:
            break

        step_no += 1
        if step_no not in result_data_by_step:  # DB 无此步 = 当初没成功 = 失败点
            fail_step = step
            logger.info("replay:定位失败步=%s", step.name)
            break

        result_data = result_data_by_step[step_no]
        step_results.append(StepResult(name=step.name, data=result_data, ok=True))

        send_value = result_data  # 下一轮回灌,闭包变量逐一恢复
    return step_results, fail_step, generator
