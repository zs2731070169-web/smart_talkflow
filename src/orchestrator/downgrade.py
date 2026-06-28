"""降级处理:工作流失联(心跳超时)后,回填已成功步 + 跑补偿 + 标 failed/completed。

回滚已成功步的副作用(except Compensate 补偿),
然后把 process 标 failed/completed。由 heartbeat 看门狗触发。
"""

from __future__ import annotations

from conf.config import settings
from infra.logger import setup_logging
from orchestrator.base import WorkflowRegistry
from orchestrator.idempotency import Status
from orchestrator.workflow_engine import ProcessContext, StepResult, compensate, replay_steps
from repository.process_tracker import acquire_recovery_lock, detect, transition_status
from runtime.context import OperatorContext, RequestContext, set_request_context
from utils.trace_id_util import trace_id_context

logger = setup_logging(__name__)


async def handle_processes(registry: WorkflowRegistry):
    """扫描心跳超时的 process, 逐个降级(补偿 + 标 failed)

    process:抢权 → 重建上下文 → replay → throw 补偿 → 标 failed
    """
    process_list = await detect(settings.process_heartbeat_timeout)
    if process_list:
        logger.info("降级:检测到 %d 个失活流程", len(process_list))

    for process in process_list:
        # ① 抢权(多实例防重:DB 原子条件 UPDATE,失败=别实例已处理)
        if not await acquire_recovery_lock(process.id, settings.process_heartbeat_timeout):
            logger.info("降级:process=%s 被别的实例抢占,跳过", process.id)
            continue

        # ② 重建 process 的请求上下文(operator / trace_id,供代签 + 审计关联)
        operator = OperatorContext.from_operator_context(process.operator_context)
        set_request_context(
            RequestContext(
                operator=operator,
                workflow_registry=registry,
                trace_id=process.trace_id,
            )
        )
        trace_id_context.set(process.trace_id)

        # ③ 重建 workflow + 业务入参
        workflow = registry.get_workflow(process.process_key)
        if workflow is None:
            logger.warning("降级:未注册 workflow %s, 重建工作流失败", process.process_key)
            await transition_status(
                process.id, Status.FAILED, Status.RUNNING, {"error_message": f"未注册 workflow: {process.process_key}"}
            )
            continue
        arguments = workflow.input_model.model_validate(process.input_params or {})

        # ④ replay 已成功步 result_data,定位失败点(generator 已推进到失败点就绪态)
        step_results, fail_step, generator = await replay_steps(workflow, arguments, process.id)

        # ⑤ 在失败点 throw 补偿(与正常失败同路径);fail_step=None 说明 replay 全成功 → 标 completed
        if fail_step is not None:
            fail_step_result = StepResult(name="downgrade", error="心跳超时中断")
            process_ctx = ProcessContext(process_id=process.id)
            await compensate(generator, step_results, fail_step_result, process_ctx, on_step=None)  # 执行补偿流程
            # 失败:标 failed
            await transition_status(
                process.id,
                Status.FAILED,
                Status.RUNNING,
                extra={"error_message": f"失活流程降级,失败步: {fail_step.name}"},
            )
            logger.info("降级:process=%s 已善后(失败步=%s)", process.id, fail_step.name)
        else:
            # replay 全成功, 标 completed
            await transition_status(
                process.id,
                Status.COMPLETED,
                Status.RUNNING,
                extra={"result": "失活流程恢复,流程实际已完成"},
            )
            logger.info("降级:process=%s replay 全成功,标 completed", process.id)
