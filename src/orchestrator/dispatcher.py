"""工作流执行编排:认证 → 权限 → 幂等 → drive → finalize。

:class:`WorkflowDispatcher` 是工作流执行的统一入口,串联认证/权限/幂等/引擎驱动/终态收尾。
路由(``get_workflow``)与参数校验(``model_validate``)由 IntentParser 前移处理,
本类只接收已校验输入,串联各环节、自身只编排。
"""

from __future__ import annotations

from pydantic import BaseModel

from infra.logger import setup_logging
from orchestrator.base import BaseWorkflow, WorkflowResult
from orchestrator.idempotency import IdempotencyChecked, IdempotencyChecker, IdempotencyCheckRequest, Status
from permission.permission import workflow_role_checker
from runtime.context import OperatorContext, get_operator
from utils.trace_id_util import get_trace_id

logger = setup_logging(__name__)


class WorkflowDispatcher:
    """工作流执行编排器:认证 → 权限 → 幂等 → drive → finalize。

    进程级单例(由 ``build_runtime`` 装配一次),无状态(每请求从 ContextVar 取 operator)。
    """

    async def execute(self, workflow: BaseWorkflow, inputs: BaseModel) -> WorkflowResult:
        """工作流执行入口。

        :param workflow: 已路由的 workflow(IntentParser._route)
        :param inputs: 已校验的入参(IntentParser._validate)
        """
        # 1. 认证:operator 由 api/deps 注入 ContextVar
        operator = get_operator()
        if operator is None:
            logger.warning("未认证请求,拒绝执行工作流: %s", workflow.name)
            return WorkflowResult(output="未认证:缺少合法执行人", is_error=True)

        # 2. 权限:operator 角色不在 workflow_role 白名单则拒执行
        if not await workflow_role_checker.is_allowed(workflow.name, operator):
            logger.warning("用户 %s 无权触发工作流 %s", operator.user_id, workflow.name)
            return WorkflowResult(output="您没有该操作的权限", is_error=True)

        # 3. 幂等校验(命中终态时直接短路返回)
        checker, checked, workflow_result = await self._check_idempotency(workflow, inputs, operator)
        if workflow_result is not None:
            return workflow_result

        # 4. 取 process_id(任务追踪)
        process_id = checked.process.id if (checked is not None and checked.process is not None) else None

        # 5. 执行(execute)
        result = await workflow.execute(inputs, process_id)

        # 6. 状态更新:按执行结果判定 completed / failed
        await self._finalize(checker, checked, result)

        return result

    async def _check_idempotency(
        self,
        workflow,
        inputs: BaseModel,
        operator: OperatorContext,
    ) -> tuple[IdempotencyChecker | None, IdempotencyChecked | None, WorkflowResult | None]:
        """幂等校验。返回 ``(checker, checked, workflow_result)``。

        ``workflow_result`` 非 None 表示命中已存在记录(短路:completed/failed/拒绝并发),
        execute 应直接返回它;为 None 表示首次(``is_new``),允许执行。
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

    async def _finalize(
        self, checker: IdempotencyChecker | None, checked: IdempotencyChecked | None, result: WorkflowResult
    ) -> None:
        """按执行结果更新流程状态(completed / failed)。"""
        if checker is None or checked is None or checked.process is None:
            return
        if result.is_error:
            await checker.failed(checked.process, result.output)
        else:
            await checker.completed(checked.process, result.model_dump())
