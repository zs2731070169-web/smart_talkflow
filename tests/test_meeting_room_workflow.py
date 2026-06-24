"""会议室预订工作流编排单测(声明式 Saga)。

mock 下游 client 与 step_recorder(避免真实 yudao / DB),驱动 :meth:`BaseWorkflow.execute`
验证:三步顺序执行、bookingId 经 ``ctx.results`` 流转、approve 失败逆序 ``cancel`` 补偿。

运行(项目根)::

    PYTHONPATH=src python -m unittest tests.test_meeting_room_workflow
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from adapters.base import AdapterResponse, AdapterResult
from orchestrator.base import WorkflowExecutionContext
from orchestrator.step_recorder import CompensationStatus
from orchestrator.workflow.meeting_room import (
    MeetingRoomBookingInput,
    MeetingRoomBookingWorkflow,
)
from runtime.context import OperatorContext, RequestContext, set_request_context


def _resp(action, *, is_error=False, data=None, error=None):
    """构造最小 :class:`AdapterResponse`(``result.data`` 即业务结果)。"""
    return AdapterResponse(
        adapter="oa_adapter_meeting_room", target_system="oa",
        action=action, method="PUT",
        result=AdapterResult(data=data),
        is_error=is_error, error_message=error, duration=1,
    )


class MeetingRoomWorkflowTest(unittest.IsolatedAsyncioTestCase):
    """会议室预订 execute:全成功 / 步骤失败触发逆序补偿。"""

    def _args(self) -> MeetingRoomBookingInput:
        return MeetingRoomBookingInput(
            room_id=1, meeting_title="周会",
            meeting_start_time="2026-06-24 10:00:00", meeting_end_time="2026-06-24 11:00:00",
            moderator_id=10, use_status=1,
        )

    async def asyncSetUp(self):
        set_request_context(RequestContext(
            operator=OperatorContext(user_id="9527", tenant_id="1", name="王五"),
            process_id=100,
        ))

    async def asyncTearDown(self):
        set_request_context(None)

    async def _run(self, client_mock):
        """mock step_recorder + client,跑一次 execute,返回 WorkflowResult。"""
        with patch("orchestrator.base.create_step", AsyncMock(return_value=1)), \
             patch("orchestrator.base.finish_step", AsyncMock()), \
             patch("orchestrator.base.update_compensation", AsyncMock()), \
             patch("orchestrator.workflow.meeting_room.client", client_mock):
            wf = MeetingRoomBookingWorkflow()
            return await wf.execute(self._args(), WorkflowExecutionContext())

    async def test_all_steps_success(self):
        """全成功:三步顺序执行,无补偿(cancel 不应被调用)。"""
        client = MagicMock()
        client.submit_booking = AsyncMock(return_value=_resp("submit_booking", data=123))
        client.approve_booking = AsyncMock(return_value=_resp("approve_booking", data=True))
        client.update_use_status = AsyncMock(return_value=_resp("update_use_status", data=True))
        client.cancel_booking = AsyncMock()

        result = await self._run(client)

        self.assertFalse(result.is_error)
        self.assertEqual(client.submit_booking.await_count, 1)
        self.assertEqual(client.approve_booking.await_count, 1)
        self.assertEqual(client.update_use_status.await_count, 1)
        # 全成功不触发补偿
        client.cancel_booking.assert_not_awaited()

    async def test_step2_failure_triggers_compensation(self):
        """Step2(approve)失败:逆序 cancel 已成功的 submit 步(bookingId=123)。"""
        client = MagicMock()
        client.submit_booking = AsyncMock(return_value=_resp("submit_booking", data=123))
        client.approve_booking = AsyncMock(
            return_value=_resp("approve_booking", is_error=True, error="审批失败"))
        client.update_use_status = AsyncMock()
        client.cancel_booking = AsyncMock(return_value=_resp("cancel_booking", data=True))

        result = await self._run(client)

        self.assertTrue(result.is_error)
        # submit 已成功 -> 补偿 cancel 其 bookingId(123)
        client.cancel_booking.assert_awaited_once_with(123)
        # approve 失败后不应执行 update
        client.update_use_status.assert_not_awaited()

    async def test_compensation_failure_marks_failed(self):
        """补偿 cancel 自身失败:该步标 compensation_status=failed,流程仍返回 is_error。"""
        client = MagicMock()
        client.submit_booking = AsyncMock(return_value=_resp("submit_booking", data=123))
        client.approve_booking = AsyncMock(
            return_value=_resp("approve_booking", is_error=True, error="审批失败"))
        client.update_use_status = AsyncMock()
        # cancel 返回失败
        client.cancel_booking = AsyncMock(
            return_value=_resp("cancel_booking", is_error=True, error="取消失败"))

        # 不走 _run(其内部 patch 会覆盖 update_compensation),此处自己展开 patch
        mock_comp = AsyncMock()
        with patch("orchestrator.base.create_step", AsyncMock(return_value=1)), \
             patch("orchestrator.base.finish_step", AsyncMock()), \
             patch("orchestrator.base.update_compensation", mock_comp), \
             patch("orchestrator.workflow.meeting_room.client", client):
            wf = MeetingRoomBookingWorkflow()
            result = await wf.execute(self._args(), WorkflowExecutionContext())

        self.assertTrue(result.is_error)
        client.cancel_booking.assert_awaited_once_with(123)
        # 补偿被调用,且因 cancel_resp.ok=False 标 FAILED
        mock_comp.assert_awaited_once_with(1, CompensationStatus.FAILED)


if __name__ == "__main__":
    unittest.main()