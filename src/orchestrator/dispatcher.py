from pydantic import BaseModel

from infra.logger import setup_logging
from orchestrator.base import (
    WorkflowResult,
)
from orchestrator.idempotency import IdempotencyChecked, IdempotencyChecker, IdempotencyCheckRequest, Status
from permission.permission import workflow_role_checker
from runtime.context import OperatorContext, get_operator, set_process_id
from utils.trace_id_util import get_trace_id

logger = setup_logging(__name__)


async def _check_idempotency(
        workflow,
        inputs,
        operator: OperatorContext,
) -> tuple[IdempotencyChecker | None, IdempotencyChecked | None, WorkflowResult | None]:
    """幂等校验。返回 ``(checker, checked, workflow_result)``

    ``workflow_result`` 非 None 表示命中已存在记录(短路:completed/failed/拒绝并发),
    execute 应直接返回它;为 None 表示首次(``is_new``),允许执行
    """
    business_key = workflow.business_key(inputs)
    if not business_key:
        # 无业务唯一键,不做幂等
        return None, None, None

    checker = IdempotencyChecker(workflow.name)
    checked: IdempotencyChecked = await checker.check(
        IdempotencyCheckRequest(
            business_key,
            input_params=inputs.model_dump(),
            trace_id=get_trace_id(),
            created_by=operator.user_id,
            operator_context=operator.to_operator_context(),
        )
    )

    # 首次(is_new):放行执行
    if checked.is_new:
        return checker, checked, None

    # 命中已存在记录:按 status 短路(failed → 返回失败; completed/running/非终态 → 返回 message)
    status = checked.process.status if checked.process else None
    is_failed = status == Status.FAILED
    logger.info("幂等命中 status=%s, process_key=%s business_key=%s", status, workflow.name, business_key)
    return (
        checker,
        checked,
        WorkflowResult(
            output=(checked.error or checked.message or "流程之前执行失败") if is_failed else checked.message,
            is_error=is_failed,
            metadata={
                "idempotent_hit": True,
                "status": status,
                "process_id": checked.process.id if checked.process else None,
                "error": checked.error,
            },
        ),
    )


async def execute(
        workflow,
        inputs: BaseModel
) -> WorkflowResult:
    """工作流执行入口:编排 认证 → 权限校验 → 幂等校验 → 执行 → 状态更新

    :param workflow: 已路由的 workflow(IntentParser._route)
    :param inputs: 已校验的入参(IntentParser._validate)
    :param context: 额外的上下文参数
    """
    # 1. 认证:operator 由 api/deps 注入
    operator = get_operator()
    if operator is None:
        logger.warning("未认证请求,拒绝执行工作流: %s", workflow.name)
        return WorkflowResult(output="未认证:缺少合法执行人", is_error=True)

    # 2. 权限:operator 角色不在 workflow_role 配置的白名单里则拒执行
    if not await workflow_role_checker.is_allowed(workflow.name, operator):
        logger.warning("用户 %s 无权触发工作流 %s", operator.user_id, workflow.name)
        return WorkflowResult(output="您没有该操作的权限", is_error=True)

    # 3. 幂等校验(命中终态时直接短路返回)
    checker, checked, workflow_result = await _check_idempotency(workflow, inputs, operator)
    if workflow_result is not None:
        return workflow_result

    # 4. 回填 process_id 到请求上下文,供 adapter 落 adapter_call_logs 关联
    if checked is not None and checked.process is not None:
        set_process_id(checked.process.id)

    # 5. 执行(execute 不抛:next 异常已归一, 补偿内部也已兜底)
    result = await workflow.execute(inputs)

    # 6. 状态更新:按执行结果判定 completed / failed
    if checker is not None and checked is not None and checked.process is not None:
        if result.is_error:
            # 业务失败(workflow 返回 is_error=True)同样置 failed
            await checker.failed(checked.process, result.output)
        else:
            await checker.completed(checked.process, result.model_dump())

    return result
