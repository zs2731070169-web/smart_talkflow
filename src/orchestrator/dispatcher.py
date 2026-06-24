from pydantic import ValidationError

from orchestrator.idempotency import Action, IdempotencyChecker, IdempotencyCheckRequest, IdempotencyChecked
from infra.logger import setup_logging
from orchestrator.base import (
    WorkflowExecutionContext, WorkflowRegistry, WorkflowResult,
)
from permission.permission import workflow_role_checker
from runtime.context import OperatorContext, get_operator, set_process_id
from utils.trace_id_util import get_trace_id

logger = setup_logging(__name__)


async def _check_idempotency(
        workflow, inputs, operator: OperatorContext, max_retry: int,
):
    """幂等校验。返回 ``(checker, checked, short_circuit_result)``。

    ``short_circuit_result`` 非 None 表示命中终态(COMPLETED / REJECT 或失败超限),
    dispatch 应直接返回它;为 None 表示允许执行(NEW 或 FAILED 未超限重跑)。
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
        )
    )

    # 命中 failed:按已执行次数(含首次)判定是否还能重跑
    if checked.action == Action.FAILED:
        attempt = int((checked.context or {}).get("attempt", 1))
        if attempt >= max_retry:
            # 超过最大执行次数:状态扭转为 reject,避免对相同失败任务被重复发起
            await checker.reject(
                checked.process,
                f"已达到最大执行次数 {max_retry}, 判定为不可恢复任务, 拒绝执行",
            )
            logger.info("幂等命中 action=%s, process_key=%s business_key=%s",
                        checked.action, workflow.name, business_key)
            return checker, checked, WorkflowResult(
                output=checked.message,
                is_error=True,
                metadata={
                    "idempotent_hit": True,
                    "action": checked.action.value,
                    "process_id": checked.process.id if checked.process else None,
                    "rejected": True,
                    "attempt": attempt,
                    "error": checked.error,
                },
            )
        # 未超限:累加重试执行次数,允许重跑
        await checker.increment_attempt(checked.process)
        return checker, checked, None

    # 命中 COMPLETED / REJECT:短路返回历史结果
    if checked.action not in (Action.NEW, Action.FAILED):
        logger.info("幂等命中 action=%s, process_key=%s business_key=%s",
                    checked.action, workflow.name, business_key)
        return checker, checked, WorkflowResult(
            output=checked.message,
            is_error=False,
            metadata={
                "idempotent_hit": True,
                "action": checked.action.value,
                "process_id": checked.process.id if checked.process else None,
            },
        )

    # NEW:首次执行
    return checker, checked, None


async def _finalize(checker, checked, result: WorkflowResult) -> None:
    """按执行结果更新流程状态(completed / failed)。"""
    if checker is None or checked is None or checked.process is None:
        return
    if result.is_error:
        # 业务失败(workflow 返回 is_error=True)同样置 failed
        await checker.failed(checked.process, result.output)
    else:
        await checker.completed(checked.process, result.model_dump())


class WorkflowDispatcher:
    """工作流调度入口:编排 用户认证 → 路由工作流 → 权限校验 → 参数校验 → 幂等校验 → 执行 → 状态更新。

    各环节委托专门组件(get_operator / workflow_role_checker / IdempotencyChecker /
    workflow.execute),自身只做编排与短路决策,不实现业务。
    """

    def __init__(self, registry: WorkflowRegistry) -> None:
        self._registry = registry

    async def dispatch(
            self, name: str, arguments: dict,
            context: WorkflowExecutionContext, max_retry: int,
    ) -> WorkflowResult:
        """调度入口

        :param name: 工作流名称
        :param arguments: 工作流业务入参
        :param context: 工作流上下文入参
        :param max_retry: 最大执行次数(含首次),超过则状态扭转为 reject
        :return: 工作流执行结果
        """
        # 1. 认证:operator 由 api/deps 注入
        operator = get_operator()
        if operator is None:
            logger.warning("未认证请求,拒绝执行工作流: %s", name)
            return WorkflowResult(output="未认证:缺少合法执行人", is_error=True)

        # 2. 路由:查找工作流
        workflow = self._registry.get_workflow(name)
        if workflow is None:
            logger.warning("未知工作流: %s", name)
            return WorkflowResult(output=f"未知工作流: {name}", is_error=True)

        # 3. 权限:operator 角色不在 workflow_role 配置的白名单里则拒执行
        if not await workflow_role_checker.is_allowed(workflow.name, operator):
            logger.warning("用户 %s 无权触发工作流 %s", operator.user_id, name)
            return WorkflowResult(output="您没有该操作的权限", is_error=True)

        # 4. 参数校验
        try:
            inputs = workflow.input_model.model_validate(arguments)
        except ValidationError as e:
            logger.warning("工作流 %s 参数校验失败: %s", name, e)
            return WorkflowResult(output=str(e), is_error=True)

        # 5. 幂等校验(命中终态时短路返回)
        checker, checked, short_circuit = await _check_idempotency(
            workflow, inputs, operator, max_retry
        )
        if short_circuit is not None:
            return short_circuit

        # 6. 回填 process_id 到请求上下文,供 adapter 落 adapter_call_logs 关联
        if checked is not None and checked.process is not None:
            set_process_id(checked.process.id)

        # 7. 执行(execute 不抛:next 异常已归一, 补偿内部也已兜底)
        result = await workflow.execute(inputs, context)

        # 8. 状态更新:按执行结果判定 completed / failed
        await _finalize(checker, checked, result)

        return result
