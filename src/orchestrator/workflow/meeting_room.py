"""会议室预订流程编排。

顺序执行::

    Step 1 提交     submit_booking      -> bookingId
    Step 2 审批     approve_booking     -> (依赖 bookingId)
    Step 3 更新状态 update_use_status   -> (依赖 bookingId)
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from adapters import AdapterResponse
from adapters.oa_adapter.oa_client import client
from infra.logger import setup_logging
from orchestrator.base import (
    BaseWorkflow,
    WorkflowExecutionContext,
    WorkflowResult,
)
from runtime.context import get_operator

logger = setup_logging(__name__)


class MeetingRoomBookingInput(BaseModel):
    """会议室预订流程入参。"""

    room_id: int = Field(description="会议室ID")
    meeting_title: str = Field(description="会议名称")
    meeting_start_time: str = Field(description="会议开始时间(yyyy-MM-dd HH:mm:ss)")
    meeting_end_time: str = Field(description="会议结束时间(yyyy-MM-dd HH:mm:ss)")
    moderator_id: int = Field(description="主持人ID")
    use_status: int = Field(default=1, description="最终使用状态(0待使用/1使用中/2已完成/3已取消)")


class MeetingRoomBookingWorkflow(BaseWorkflow):
    """会议室预订流程编排(submit → approve → update-use-status)。"""

    name: str = "meeting_room_booking"
    description: str = "调用OA系统执行会议室预订:提交→审批→更新使用状态"
    input_model: type[BaseModel] = MeetingRoomBookingInput

    def business_key(self, arguments: MeetingRoomBookingInput) -> str | None:
        """会议室预订业务唯一键:操作人 + 会议室 + 起止时间。"""
        operator = get_operator()
        return (
            f"{operator.user_id}_{arguments.room_id}_"
            f"{arguments.meeting_start_time}_{arguments.meeting_end_time}"
        )

    async def execute(
            self, arguments: MeetingRoomBookingInput, context: WorkflowExecutionContext
    ) -> WorkflowResult:

        steps: list[dict] = []

        # creator 取自当前 operator(dispatcher 已保证存在),代签头同步透传给下游
        creator = get_operator().user_id

        try:
            # Step 1 提交预订
            submitted = await client.submit_booking(
                room_id=arguments.room_id,
                meeting_title=arguments.meeting_title,
                meeting_start_time=arguments.meeting_start_time,
                meeting_end_time=arguments.meeting_end_time,
                creator=creator,
                moderator_id=arguments.moderator_id,
            )
            steps.append(self._record_step(1, "submit_booking", "提交预订", submitted))
            if submitted.is_error:
                return self._failure(steps, submitted)
            booking_id = submitted.result.get("value")

            # Step 2 审批(依赖 bookingId)
            approved = await client.approve_booking(booking_id)
            steps.append(self._record_step(2, "approve_booking", "审批通过", approved))
            if approved.is_error:
                return self._failure(steps, approved)

            # Step 3 更新使用状态(依赖 bookingId)
            updated = await client.update_use_status(booking_id, arguments.use_status)
            steps.append(self._record_step(3, "update_use_status", "更新使用状态", updated))
            if updated.is_error:
                return self._failure(steps, updated)
        except Exception as exc:
            logger.exception("会议室预订流程执行异常,已完成 %d 步", len(steps))
            return WorkflowResult(
                output=f"会议室预订流程执行异常(已完成 {len(steps)} 步):{exc}",
                is_error=True,
                metadata={"completed_steps": len(steps), "steps": steps},
            )

        logger.info("会议室预订完成: booking_id=%s", booking_id)

        return WorkflowResult(
            output=(
                f"会议室预订完成:预订单号 {booking_id},已审批,"
                f"使用状态已更新为 {arguments.use_status}"
            ),
            metadata={
                "booking_id": booking_id,
                "use_status": arguments.use_status,
                "steps": steps,
            },
        )

    @staticmethod
    def _record_step(
            step_no: int, step_key: str, step_name: str, resp: AdapterResponse
    ) -> dict:
        """把单步 :class:`AdapterResponse` 转成步骤记录(字段对齐 ProcessStep)。"""
        return {
            "step_no": step_no,
            "step_key": step_key,
            "step_name": step_name,
            "adapter": resp.adapter,
            "action": resp.action,
            "status": "success" if not resp.is_error else "failed",
            "input": resp.request_payload,
            "output": resp.response_payload,
            "error_message": resp.error_message,
            "duration_ms": resp.duration,
        }

    @staticmethod
    def _failure(steps: list[dict], resp: AdapterResponse) -> WorkflowResult:
        """某步外部调用失败:该步已并入 steps(含完整留痕),据此返回错误结果。"""
        return WorkflowResult(
            output=f"会议室预订在「{steps[-1]['step_name']}」步骤失败:{resp.error_message}",
            is_error=True,
            metadata={"completed_steps": len(steps) - 1, "steps": steps},
        )