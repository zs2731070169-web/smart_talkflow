"""OA 业务域适配器集合。

按业务域组织,当前为会议室预订域(:class:`MeetingRoomBookingAdapter`),
由 :class:`adapters.oa_adapter.oa_client.OAClient` 聚合对外。
"""
from adapters.oa_adapter.oa_meeting_room import MeetingRoomBookingAdapter

__all__ = ["MeetingRoomBookingAdapter"]