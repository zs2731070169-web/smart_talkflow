"""OA 业务域适配器集合。

按业务域组织,当前为会议室预订域(:class:`MeetingRoomBookingAdapter`),
各业务域 adapter 经自身模块单例对外(如会议室预订域的 ``room_booking_adapter``)。
"""

from adapters.oa_adapter.oa_meeting_room import MeetingRoomBookingAdapter

__all__ = ["MeetingRoomBookingAdapter"]
