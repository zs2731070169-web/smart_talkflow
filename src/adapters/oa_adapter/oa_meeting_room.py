"""会议室预订业务域适配器(yudao ``/admin-api/oa/meeting-room-booking/*``)。

封装会议室预订正向链路(submit → approve → update-use-status)+ 补偿(cancel)。
继承 :class:`OAAdapter`,复用 yudao 系统级协议解析(``is_success`` / ``extract_result``)与 ``target_system = "oa"``。
action 经 ``_step_call`` 返回 :class:`StepResult`(adapter 层转 AdapterResponse,引擎不认 AdapterResponse)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.base import AdapterRequest
from adapters.oa_adapter.oa_base import OAAdapter
from conf import settings

if TYPE_CHECKING:
    from orchestrator.workflow_engine import ProcessContext, StepResult


class MeetingRoomBookingAdapter(OAAdapter):
    """会议室预订业务域适配器(yudao ``/admin-api/oa/meeting-room-booking/*``)。"""

    adapter_name = "oa_adapter_meeting_room"

    async def submit_booking(
        self,
        process_ctx: ProcessContext,
        room_id: int,
        meeting_title: str,
        meeting_start_time: str,
        meeting_end_time: str,
        creator: str,
        moderator_id: int,
    ) -> StepResult:
        """Step 1 提交会议室预订(发起审批),返回 bookingId。"""
        return await self.step_call(
            process_ctx,
            AdapterRequest(
                action="submit_booking",
                method="POST",
                path="/admin-api/oa/meeting-room-booking/submit",
                payload={
                    "roomId": room_id,
                    "meetingTitle": meeting_title,
                    "meetingStartTime": meeting_start_time,
                    "meetingEndTime": meeting_end_time,
                    "creator": creator,
                    "moderatorId": moderator_id,
                },
            ),
        )

    async def approve_booking(self, process_ctx: ProcessContext, booking_id: int) -> StepResult:
        """Step 2 审批通过会议室预订。"""
        return await self.step_call(
            process_ctx,
            AdapterRequest(
                action="approve_booking",
                method="PUT",
                path="/admin-api/oa/meeting-room-booking/approve",
                params={"id": booking_id},
            ),
        )

    async def update_use_status(self, process_ctx: ProcessContext, booking_id: int, use_status: int) -> StepResult:
        """Step 3 更新使用状态(0待使用/1使用中/2已完成/3已取消)。"""
        return await self.step_call(
            process_ctx,
            AdapterRequest(
                action="update_use_status",
                method="PUT",
                path="/admin-api/oa/meeting-room-booking/update-use-status",
                params={"id": booking_id, "useStatus": use_status},
            ),
        )

    async def cancel_booking(self, process_ctx: ProcessContext, booking_id: int) -> StepResult:
        """补偿动作:取消会议室预订。

        yudao ``PUT /cancel`` 是终态覆盖(一次性置 ``useStatus=3`` + ``processStatus=4``),
        故无论预订当前处在 submit 后(审批中)还是 approve 后(已通过),都能用它回退。
        yudao 不校验源状态、纯状态覆盖,故重复调用同一 booking_id 幂等无害——适合 Saga
        逆序补偿多次重试。
        """
        return await self.step_call(
            process_ctx,
            AdapterRequest(
                action="cancel_booking",
                method="PUT",
                path="/admin-api/oa/meeting-room-booking/cancel",
                params={"id": booking_id},
            ),
        )


room_booking_adapter = MeetingRoomBookingAdapter(base_url=settings.oa_base_url)
