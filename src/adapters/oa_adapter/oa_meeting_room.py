"""会议室预订业务域适配器。

封装 yudao OA「会议室预订」的 HTTP 调用,对应会议室预订正向链路:
``submit → approve → update-use-status``。

yudao 统一响应 ``CommonResult{code, msg, data}``,``code == 0`` 视为业务成功,
``data`` 为业务结果(submit 返回 bookingId,approve/update 返回 true)。
"""
from dataclasses import dataclass

from adapters.base import AdapterRequest, AdapterResponse, BaseAdapter


@dataclass
class SubmitBookingRequest:
    """提交会议室预订请求(submit 的 JSON body)。

    字段名与 yudao ``MeetingRoomBookingSaveReqVO`` 对齐(非 curl 示例的简化名)。
    ``creator`` 必传(yudao 用其发起 BPM 审批流程),取自当前 operator.user_id。
    """

    room_id: int  # 会议室ID
    meeting_title: str  # 会议名称
    meeting_start_time: str  # 会议开始时间(yyyy-MM-dd HH:mm:ss)
    meeting_end_time: str  # 会议结束时间
    creator: str  # 创建人(用户ID字符串,发起 BPM 用)
    moderator_id: int  # 主持人ID


class MeetingRoomBookingAdapter(BaseAdapter):
    """会议室预订业务域适配器(yudao ``/admin-api/oa/meeting-room-booking/*``)。"""

    adapter_name = "oa_adapter_meeting_room"
    target_system = "oa"

    def is_success(self, http_status: int, response_payload: dict) -> tuple[bool, str | None]:
        """yudao 判定:HTTP 2xx 且 body ``code == 0`` 视为业务成功。"""
        if not (200 <= http_status < 300):
            return False, None
        code = response_payload.get("code")
        if code == 0:
            return True, None
        return False, response_payload.get("msg") or f"业务失败 code={code}"

    def extract_result(self, payload: dict) -> dict:
        """提取 yudao 响应的 ``data``。

        data 可能是标量(submit 的 bookingId、approve/update 的 true),
        统一包成 dict 便于编排层按 key 消费。
        """
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return {"value": data}

    async def submit_booking(self, request: SubmitBookingRequest) -> AdapterResponse:
        """Step 1 提交会议室预订(发起审批),返回 bookingId。"""
        return await self._call_action(
            AdapterRequest(
                action="submit_booking",
                method="POST",
                path="/admin-api/oa/meeting-room-booking/submit",
                payload={
                    "roomId": request.room_id,
                    "meetingTitle": request.meeting_title,
                    "meetingStartTime": request.meeting_start_time,
                    "meetingEndTime": request.meeting_end_time,
                    "creator": request.creator,
                    "moderatorId": request.moderator_id,
                },
            )
        )

    async def approve_booking(self, booking_id: int) -> AdapterResponse:
        """Step 2 审批通过会议室预订。"""
        return await self._call_action(
            AdapterRequest(
                action="approve_booking",
                method="PUT",
                path="/admin-api/oa/meeting-room-booking/approve",
                params={"id": booking_id},
            )
        )

    async def update_use_status(self, booking_id: int, use_status: int) -> AdapterResponse:
        """Step 3 更新使用状态(0待使用/1使用中/2已完成/3已取消)。"""
        return await self._call_action(
            AdapterRequest(
                action="update_use_status",
                method="PUT",
                path="/admin-api/oa/meeting-room-booking/update-use-status",
                params={"id": booking_id, "useStatus": use_status},
            )
        )