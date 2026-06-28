"""会议室预订工作流编排单测(声明式 call/ref/yields + 统一 compensate)。

mock 下游 client 与步骤留痕(避免真实 yudao / DB),驱动 BaseWorkflow.execute 验证:
三步顺序执行、bookingId 经 ref 流转、approve 失败触发统一补偿 cancel。

运行(项目根目录)::

    PYTHONPATH=src python -m unittest tests.test_meeting_room_workflow
"""

import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from orchestrator.workflow.meeting_room import (
    MeetingRoomBookingInput,
    MeetingRoomBookingWorkflow,
)
from orchestrator.workflow_engine import StepResult
from runtime.context import OperatorContext, RequestContext, set_request_context


def _resp(action, *, is_error=False, data=None, error=None):
    """构造 StepResult(mock adapter action 返回值)。"""
    return StepResult(ok=not is_error, data=data, error=error)


class MeetingRoomWorkflowTest(unittest.IsolatedAsyncioTestCase):
    """会议室预订 execute:全成功 / 步骤失败触发统一补偿。"""

    def _args(self) -> MeetingRoomBookingInput:
        return MeetingRoomBookingInput(
            room_id=1,
            meeting_title="周会",
            meeting_start_time="2026-06-24 10:00:00",
            meeting_end_time="2026-06-24 11:00:00",
            moderator_id=10,
            use_status=1,
        )

    async def asyncSetUp(self):
        set_request_context(
            RequestContext(
                operator=OperatorContext(user_id="9527", tenant_id="1", name="王五"),
                trace_id="test",
            )
        )

    async def asyncTearDown(self):
        set_request_context(None)

    async def _run(self, client_mock):
        """mock 留痕 + client,跑一次 execute,返回 WorkflowResult。

        adapter action 首参为 ProcessContext(由 Step.execute 注入),故断言用 ANY 匹配。
        """
        with (
            patch("orchestrator.workflow_engine.create_step", AsyncMock(return_value=1)),
            patch("orchestrator.workflow_engine.finish_step", AsyncMock()),
            patch("orchestrator.workflow_engine.flush_heartbeat", AsyncMock()),
            patch("orchestrator.workflow.meeting_room.room_booking_adapter", client_mock),
        ):
            wf = MeetingRoomBookingWorkflow()
            return await wf.execute(self._args(), process_id=100)

    async def test_all_steps_success(self):
        """全成功:三步顺序执行,bookingId 经 ref 流转,无补偿(cancel 不调)。"""
        client = MagicMock()
        client.submit_booking = AsyncMock(return_value=_resp("submit_booking", data=123))
        client.approve_booking = AsyncMock(return_value=_resp("approve_booking", data=True))
        client.update_use_status = AsyncMock(return_value=_resp("update_use_status", data=True))
        client.cancel_booking = AsyncMock()

        result = await self._run(client)

        self.assertFalse(result.is_error)
        client.submit_booking.assert_awaited_once()
        # bookingId 经 ref 流转:approve/update 收到 123(首参 ProcessContext 用 ANY 匹配)
        client.approve_booking.assert_awaited_once_with(ANY, booking_id=123)
        client.update_use_status.assert_awaited_once_with(ANY, booking_id=123, use_status=1)
        # 全成功不触发补偿
        client.cancel_booking.assert_not_awaited()

    async def test_step2_failure_triggers_compensation(self):
        """Step2(approve)失败:统一补偿 cancel 已成功的 submit 步(bookingId=123)。"""
        client = MagicMock()
        client.submit_booking = AsyncMock(return_value=_resp("submit_booking", data=123))
        client.approve_booking = AsyncMock(return_value=_resp("approve_booking", is_error=True, error="审批失败"))
        client.update_use_status = AsyncMock()
        client.cancel_booking = AsyncMock(return_value=_resp("cancel_booking", data=True))

        result = await self._run(client)

        self.assertTrue(result.is_error)
        client.approve_booking.assert_awaited_once_with(ANY, booking_id=123)
        # 统一补偿:cancel(submit 绑定的 bookingId=123;首参 ProcessContext + 位置 booking_id)
        client.cancel_booking.assert_awaited_once_with(ANY, 123)
        # approve 失败后不应执行 update
        client.update_use_status.assert_not_awaited()

    async def test_compensation_failure_still_returns_error(self):
        """补偿 cancel 自身失败:流程仍返回 is_error(补偿成败不改变流程失败结论)。"""
        client = MagicMock()
        client.submit_booking = AsyncMock(return_value=_resp("submit_booking", data=123))
        client.approve_booking = AsyncMock(return_value=_resp("approve_booking", is_error=True, error="审批失败"))
        client.update_use_status = AsyncMock()
        # cancel 返回失败
        client.cancel_booking = AsyncMock(return_value=_resp("cancel_booking", is_error=True, error="取消失败"))

        result = await self._run(client)

        self.assertTrue(result.is_error)
        client.cancel_booking.assert_awaited_once_with(ANY, 123)


if __name__ == "__main__":
    unittest.main()
