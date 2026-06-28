"""会议室预订流程编排"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from adapters.oa_adapter.oa_meeting_room import room_booking_adapter
from orchestrator.base import BaseWorkflow
from orchestrator.workflow_engine import Compensate, step
from runtime.context import get_operator


class MeetingRoomBookingInput(BaseModel):
    """会议室预订流程入参。"""

    room_id: int = Field(description="会议室ID")
    meeting_title: str = Field(description="会议名称")
    meeting_start_time: str = Field(description="会议开始时间(yyyy-MM-dd HH:mm:ss)")
    meeting_end_time: str = Field(description="会议结束时间(yyyy-MM-dd HH:mm:ss)")
    moderator_id: int = Field(description="主持人ID")
    use_status: int = Field(default=1, description="最终使用状态(0待使用/1使用中/2已完成/3已取消)")


class Steps(StrEnum):
    """# 每一步的 step_key 和 step_name 常量"""

    SUBMIT_BOOKING = "提交预订"
    APPROVE_BOOKING = "审批通过"
    UPDATE_USE_STATUS = "更新使用状态"


class MeetingRoomBookingWorkflow(BaseWorkflow):
    """会议室预订(submit → approve → update-use-status,失败统一补偿 cancel)。"""

    name: str = "meeting_room_booking"
    description: str = "调用OA系统执行会议室预订:提交→审批→更新使用状态"
    input_model: type[BaseModel] = MeetingRoomBookingInput

    def business_key(self, arguments: MeetingRoomBookingInput) -> str | None:
        """会议室预订业务唯一键:操作人 + 会议室 + 起止时间。"""
        operator = get_operator()
        return f"{operator.user_id}_{arguments.room_id}_{arguments.meeting_start_time}_{arguments.meeting_end_time}"

    def create(self, arguments: MeetingRoomBookingInput):
        operator = get_operator()
        booking_id = None
        try:
            booking_id = yield step(
                room_booking_adapter.submit_booking,
                room_id=arguments.room_id,
                meeting_title=arguments.meeting_title,
                meeting_start_time=arguments.meeting_start_time,
                meeting_end_time=arguments.meeting_end_time,
                creator=operator.user_id,
                moderator_id=arguments.moderator_id,
                name=Steps.SUBMIT_BOOKING,
            )
            yield step(
                room_booking_adapter.approve_booking,
                booking_id=booking_id,
                name=Steps.APPROVE_BOOKING,
            )
            yield step(
                room_booking_adapter.update_use_status,
                booking_id=booking_id,
                use_status=arguments.use_status,
                name=Steps.UPDATE_USE_STATUS,
            )
            return f"已为您预订会议室:{arguments.meeting_title}(预订号 {booking_id})"
        except Compensate:
            if booking_id:
                yield step(room_booking_adapter.cancel_booking, booking_id, name="取消预订")
            return "会议室预订失败,已取消"
