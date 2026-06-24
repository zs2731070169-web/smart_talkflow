"""OA 系统统一对接客户端。

聚合 OA 会议室预订业务域,对编排层暴露业务语义化方法。编排层只与本客户端打交道,
不感知底层 adapter 的拆分与协议细节。

服务账号凭证(api-key / delegation-secret)由 ``BaseAdapter`` 按
:attr:`~BaseAdapter.target_system` 自动加载(adapter 只声明系统身份);tenant-id 与
operator 则每请求随上下文获取,由 ``_call_action`` 经 provider 组装代签头。
"""
from __future__ import annotations

from adapters.base import AdapterResponse
from adapters.oa_adapter.oa_meeting_room import (
    MeetingRoomBookingAdapter,
    SubmitBookingRequest,
)
from conf import settings


class OAClient:
    """OA 系统对接客户端(聚合会议室预订域)。"""

    def __init__(self) -> None:
        self.meeting_room = MeetingRoomBookingAdapter(base_url=settings.oa_base_url)

    async def submit_booking(
            self,
            room_id: int,
            meeting_title: str,
            meeting_start_time: str,
            meeting_end_time: str,
            creator: str,
            moderator_id: int,
    ) -> AdapterResponse:
        """Step 1 提交会议室预订(发起审批),返回 bookingId。"""
        return await self.meeting_room.submit_booking(SubmitBookingRequest(
            room_id=room_id,
            meeting_title=meeting_title,
            meeting_start_time=meeting_start_time,
            meeting_end_time=meeting_end_time,
            creator=creator,
            moderator_id=moderator_id,
        ))

    async def approve_booking(self, booking_id: int) -> AdapterResponse:
        """Step 2 审批通过。"""
        return await self.meeting_room.approve_booking(booking_id)

    async def update_use_status(self, booking_id: int, use_status: int) -> AdapterResponse:
        """Step 3 更新使用状态(0待使用/1使用中/2已完成/3已取消)。"""
        return await self.meeting_room.update_use_status(booking_id, use_status)

    async def cancel_booking(self, booking_id: int) -> AdapterResponse:
        """补偿:取消会议室预订(submit/approve/update 后皆可用以回退)。"""
        return await self.meeting_room.cancel_booking(booking_id)


client = OAClient()