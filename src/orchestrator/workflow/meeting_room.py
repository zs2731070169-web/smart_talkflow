"""会议室预订流程编排(声明式 Saga:整笔原子 + 失败逆序补偿)。

继承 :class:`BaseWorkflow`,只声明三步序列(submit → approve → update-use-status);
留痕、顺序执行、失败逆序 ``cancel`` 补偿由基类统一驱动。

一致性策略:任一步失败即逆序 cancel 已成功步(yudao ``PUT /cancel`` 终态覆盖、幂等),
整笔原子——失败无副作用残留,故重试从头跑即可(不做断点续跑,断点续跑会去操作已
cancel 的预订、与 yudao 宽松状态机冲突)。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from adapters.oa_adapter.oa_client import client
from orchestrator.base import BaseWorkflow, WorkflowExecutionContext
from orchestrator.workflow_engine import WorkflowStep
from runtime.context import get_operator


class MeetingRoomBookingInput(BaseModel):
    """会议室预订流程入参。"""

    room_id: int = Field(description="会议室ID")
    meeting_title: str = Field(description="会议名称")
    meeting_start_time: str = Field(description="会议开始时间(yyyy-MM-dd HH:mm:ss)")
    meeting_end_time: str = Field(description="会议结束时间(yyyy-MM-dd HH:mm:ss)")
    moderator_id: int = Field(description="主持人ID")
    use_status: int = Field(default=1, description="最终使用状态(0待使用/1使用中/2已完成/3已取消)")


class MeetingRoomBookingWorkflow(BaseWorkflow):
    """会议室预订(submit → approve → update-use-status,失败补偿 cancel)。"""

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

    def steps(
            self, arguments: MeetingRoomBookingInput, context: WorkflowExecutionContext
    ) -> list[WorkflowStep]:
        """声明三步预订序列;任一步失败时,此前已成功步按 compensate 逆序 cancel。"""
        # creator 取自当前 operator
        creator = get_operator().user_id
        # submit 步产出 bookingId(其 result.data),经 ctx.results 回取
        booking_id = lambda ctx: int(ctx.results["submit_booking"].result.data)

        return [
            WorkflowStep(
                step_no=1,
                step_key="submit_booking",
                step_name="提交预订",
                adapter="oa",
                action="submit_booking",
                next=lambda ctx: client.submit_booking(
                    room_id=arguments.room_id,
                    meeting_title=arguments.meeting_title,
                    meeting_start_time=arguments.meeting_start_time,
                    meeting_end_time=arguments.meeting_end_time,
                    creator=creator,
                    moderator_id=arguments.moderator_id,
                ),
                compensate=lambda ctx: client.cancel_booking(booking_id(ctx)),
                input_params={
                    "roomId": arguments.room_id,
                    "meetingTitle": arguments.meeting_title,
                    "meetingStartTime": arguments.meeting_start_time,
                    "meetingEndTime": arguments.meeting_end_time,
                    "moderatorId": arguments.moderator_id,
                },
            ),
            WorkflowStep(
                step_no=2,
                step_key="approve_booking",
                step_name="审批通过",
                adapter="oa",
                action="approve_booking",
                next=lambda ctx: client.approve_booking(booking_id(ctx)),
                compensate=lambda ctx: client.cancel_booking(booking_id(ctx)),
            ),
            WorkflowStep(
                step_no=3,
                step_key="update_use_status",
                step_name="更新使用状态",
                adapter="oa",
                action="update_use_status",
                next=lambda ctx: client.update_use_status(booking_id(ctx), arguments.use_status),
                compensate=lambda ctx: client.cancel_booking(booking_id(ctx)),
            ),
        ]