"""端到端链路测试:dispatcher.execute → engine drive → adapter(mock OA HTTP) → MySQL。

mock 下游 OA(http.request)+ mock 权限(is_allowed),真实 MySQL。
单 test method 跑 3 场景(全成功/补偿/幂等),共用同一 event loop(避免 asyncmy QueuePool 跨 loop)。

运行(项目根目录)::

    PYTHONPATH=src python -m unittest tests.test_dispatcher_e2e
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from infra.database import init_engine
from orchestrator.dispatcher import WorkflowDispatcher
from orchestrator.workflow.meeting_room import MeetingRoomBookingInput, MeetingRoomBookingWorkflow
from runtime.context import OperatorContext, RequestContext, set_request_context


def _oa_ok(data):
    """yudao 成功响应(HTTP 200, code=0, data=...)。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"code": 0, "data": data}
    return resp


def _oa_fail(code=500, msg="业务失败"):
    """yudao 失败响应(HTTP 200, code!=0)。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"code": code, "msg": msg}
    return resp


def _inputs(room_id):
    """构造会议室预订入参(不同 room_id 保证不同 business_key → 不撞幂等)。"""
    return MeetingRoomBookingInput(
        room_id=room_id,
        meeting_title=f"E2E测试_{room_id}",
        meeting_start_time="2026-07-01 10:00:00",
        meeting_end_time="2026-07-01 11:00:00",
        moderator_id=10,
    )


class DispatcherE2ETest(unittest.IsolatedAsyncioTestCase):
    """dispatcher.execute 端到端:全链路打通(mock OA + 真实 MySQL)。"""

    async def asyncSetUp(self):
        init_engine()  # 装配 DB 引擎(真实 MySQL)
        self._dispatcher = WorkflowDispatcher()
        operator = OperatorContext(user_id="e2e_user", tenant_id="1", name="测试人")
        set_request_context(
            RequestContext(
                operator=operator,
                trace_id="e2e-trace",
            )
        )

    async def asyncTearDown(self):
        set_request_context(None)

    async def test_e2e_full_chain(self):
        """端到端 3 场景:全成功 / approve 失败补偿 / 幂等短路(同一 loop)。"""
        base = int(time.time()) % 100000
        """端到端 3 场景:全成功 / approve 失败补偿 / 幂等短路(同一 loop)。"""
        workflow = MeetingRoomBookingWorkflow()
        results = {}

        # ── 场景 1:全成功(submit→approve→update→completed)──
        call_log_1 = []

        async def mock_http_1(method, **kwargs):
            path = kwargs.get("url", "")
            call_log_1.append(path.split("/")[-1])
            return _oa_ok(123) if "submit" in path else _oa_ok(True)

        with (
            patch("infra.http.request", side_effect=mock_http_1),
            patch("permission.permission.workflow_role_checker.is_allowed", return_value=True),
        ):
            results["success"] = await self._dispatcher.execute(workflow, _inputs(base))

        r = results["success"]
        self.assertFalse(r.is_error, f"[全成功] 应成功: {r.output}")
        self.assertIn("123", r.output, f"[全成功] output 应含 bookingId: {r.output}")
        self.assertEqual(call_log_1, ["submit", "approve", "update-use-status"], f"[全成功] 3 步: {call_log_1}")
        print(f"  ✅ 场景1 全成功: {r.output}")

        # ── 场景 2:approve 失败 → cancel 补偿 → failed ──
        call_log_2 = []

        async def mock_http_2(method, **kwargs):
            path = kwargs.get("url", "")
            call_log_2.append(path.split("/")[-1])
            if "submit" in path:
                return _oa_ok(456)
            if "approve" in path:
                return _oa_fail(code=500, msg="审批被拒绝")
            return _oa_ok(True)  # cancel

        with (
            patch("infra.http.request", side_effect=mock_http_2),
            patch("permission.permission.workflow_role_checker.is_allowed", return_value=True),
        ):
            results["compensate"] = await self._dispatcher.execute(workflow, _inputs(base + 1))

        r = results["compensate"]
        self.assertTrue(r.is_error, "[补偿] approve 失败应 is_error=True")
        self.assertEqual(call_log_2, ["submit", "approve", "cancel"], f"[补偿] submit/approve/cancel: {call_log_2}")
        print(f"  ✅ 场景2 补偿: is_error=True, steps={call_log_2}")

        # ── 场景 3:幂等(第一次成功→第二次命中 completed 短路)──
        http_count = [0]

        async def mock_http_3(method, **kwargs):
            http_count[0] += 1
            path = kwargs.get("url", "")
            return _oa_ok(789) if "submit" in path else _oa_ok(True)

        with (
            patch("infra.http.request", side_effect=mock_http_3),
            patch("permission.permission.workflow_role_checker.is_allowed", return_value=True),
        ):
            results["first"] = await self._dispatcher.execute(workflow, _inputs(base + 2))
            results["second"] = await self._dispatcher.execute(
                workflow, _inputs(base + 2)
            )  # 同 business_key → 命中 completed

        self.assertFalse(results["first"].is_error, "[幂等] 第一次应成功")
        self.assertFalse(results["second"].is_error, "[幂等] 第二次应短路 completed")
        self.assertIn("idempotent_hit", results["second"].metadata, "[幂等] 第二次应标 idempotent_hit")
        self.assertEqual(http_count[0], 3, f"[幂等] 第二次 0 HTTP(短路),总 3 次: {http_count[0]}")
        print(f"  ✅ 场景3 幂等: 第二次短路(idempotent_hit), HTTP={http_count[0]}")


if __name__ == "__main__":
    unittest.main()
