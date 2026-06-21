from pydantic import ValidationError

from infra.idempotency import Action, IdempotencyChecker, IdempotencyCheckRequest, IdempotencyChecked
from infra.logger import setup_logging
from orchestrator.base import (
    WorkflowExecutionContext, WorkflowRegistry, WorkflowResult,
)
from runtime.context import get_operator, set_process_id
from utils.trace_id_util import get_trace_id

logger = setup_logging(__name__)


class WorkflowDispatcher:

    def __init__(self, registry: WorkflowRegistry) -> None:
        self._registry = registry

    async def dispatch(self, name: str,
                       arguments: dict,
                       context: WorkflowExecutionContext,
                       max_retry: int
                       ) -> WorkflowResult:
        """获取工作流实例进行调度, 调度顺序: 查找工作流 → 参数校验 → 幂等校验 → 执行 → 更新状态。

        :param
            name: 工作流名称
            arguments: 工作流业务入参
            context: 工作流上下文入参
            max_retry: 最大执行次数(含首次),超过则状态扭转为 reject

        :return
            WorkflowResult: 返回工作流执行结果
        """
        # 1. 认证校验:未认证(operator 缺失)直接拒绝,不进入流程。
        operator = get_operator()
        if operator is None:
            logger.warning("未认证请求,拒绝执行工作流: %s", name)
            return WorkflowResult(output="未认证:缺少合法执行人", is_error=True)

        # 2. 查找工作流
        workflow = self._registry.get_workflow(name)
        if workflow is None:
            logger.warning("未知工作流: %s", name)
            return WorkflowResult(output=f"未知工作流: {name}", is_error=True)

        # 3. 权限:operator 角色不在 workflow_role 配置的允许集合内则拒执行
        if not await workflow.is_allowed(operator):
            logger.warning("用户 %s 无权触发工作流 %s", operator.user_id, name)
            return WorkflowResult(output="您没有该操作的权限", is_error=True)

        # 4. 参数校验
        try:
            # 校验和反序列化参数
            input_arguments = workflow.input_model.model_validate(arguments)
        except ValidationError as e:
            logger.warning("工作流 %s 参数校验失败: %s", name, e)
            return WorkflowResult(output=str(e), is_error=True)

        # 5. 幂等校验
        checker: IdempotencyChecker | None = None
        checked: IdempotencyChecked | None = None

        business_key = workflow.business_key(input_arguments)  # 获取业务唯一键
        if business_key:
            # 获取幂等校验器, 同时把当前执行的工作流名称作为校验器当中的任务标识
            checker = IdempotencyChecker(workflow.name)
            # 进行幂等校验
            checked = await checker.check(
                IdempotencyCheckRequest(
                    business_key,
                    input_params=input_arguments.model_dump(),  # 序列化参数
                    trace_id=get_trace_id(),
                    created_by=operator.user_id,
                )
            )

            action = checked.action
            if action == Action.FAILED:
                # 命中 failed 任务: 按已执行次数(含首次)判定是否还能重跑
                attempt = int((checked.context or {}).get("attempt", 1))

                # 超过最大执行次数: 状态更新为reject, 直接返回, 避免对相同失败任务被重复发起执行
                if attempt >= max_retry:
                    await checker.reject(
                        checked.process,
                        f"已达到最大执行次数 {max_retry}, 判定为不可恢复任务, 拒绝执行"
                    )
                    logger.info("幂等命中 action=%s, process_key=%s business_key=%s",
                                action, workflow.name, business_key)
                    return WorkflowResult(
                        output=checked.message,
                        is_error=True,
                        metadata={
                            "idempotent_hit": True,
                            "action": action.value,
                            "process_id": checked.process.id if checked.process else None,
                            "rejected": True,
                            "attempt": attempt,
                            "error": checked.error,
                        },
                    )

                # 未超限: 累加重试执行次数
                await checker.increment_attempt(checked.process)

            # COMPLETED / REJECT:幂等命中,直接短路返回
            elif action not in (Action.NEW, Action.FAILED):
                logger.info("幂等命中 action=%s, process_key=%s business_key=%s",
                            action, workflow.name, business_key)
                return WorkflowResult(
                    output=checked.message,
                    is_error=False,
                    metadata={
                        "idempotent_hit": True,
                        "action": action.value,
                        "process_id": checked.process.id if checked.process else None,
                    },
                )

        # 回填 process_id 到请求上下文,供 adapter 落 adapter_call_logs 关联
        if checked is not None and checked.process is not None:
            set_process_id(checked.process.id)

        # 6. 执行工作流
        try:
            result = await workflow.execute(input_arguments, context)
        except Exception as e:
            logger.exception("工作流 %s 执行失败", name)
            # 8. 更新状态: 任务执行失败, 更新为失败状态
            if checker and checked and checked.process is not None:
                await checker.failed(checked.process, str(e))
            return WorkflowResult(output=f"工作流执行失败: {e}", is_error=True)

        # 7. 更新状态: 按执行结果判定 completed / failed
        if checker and checked and checked.process is not None:
            if result.is_error:
                # 业务失败(workflow 返回 is_error=True 而非抛异常)同样置 failed
                await checker.failed(checked.process, result.output)
            else:
                await checker.completed(checked.process, result.model_dump())
        return result
