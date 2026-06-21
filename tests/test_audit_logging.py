"""adapter_call_logs 审计落库单测。

验证 :meth:`BaseAdapter._call_action` 每次调用落一条 :class:`AdapterCallLog`,
且 operator/tenant/credential/trace/process 关联正确。隔离 http 与 db(mock)。

运行(项目根)::

    PYTHONPATH=src python -m unittest tests.test_audit_logging
"""
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from adapters.base import AdapterRequest, BaseAdapter
from infra.models import AdapterCallLog
from runtime.context import (
    OperatorContext,
    RequestContext,
    set_request_context,
)
from utils.trace_id_util import trace_id_context


class _FakeAdapter(BaseAdapter):
    """最小可执行 adapter:yudao 风格 code==0 成功。"""

    adapter_name = "fake_adapter"
    target_system = "oa"

    def is_success(self, http_status, response_payload):
        if not (200 <= http_status < 300):
            return False, None
        if response_payload.get("code") == 0:
            return True, None
        return False, response_payload.get("msg")

    def extract_result(self, payload):
        data = payload.get("data")
        return data if isinstance(data, dict) else {"value": data}


def _make_resp(code=0, data=None, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"code": code, "data": data}
    return resp


class AdapterCallLogTest(unittest.IsolatedAsyncioTestCase):
    """_call_action 落 AdapterCallLog:operator/tenant/credential/trace/process 关联。"""

    async def asyncSetUp(self):
        self._trace_token = trace_id_context.set("trace-1")
        set_request_context(RequestContext(
            operator=OperatorContext(user_id="9527", tenant_id="1", name="王五"),
            trace_id="trace-1",
            process_id=100,
        ))

    async def asyncTearDown(self):
        set_request_context(None)
        trace_id_context.reset(self._trace_token)

    async def _run_call(self, adapter, resp):
        """mock http + db_session,执行一次 _call_action,返回 captured AdapterCallLog 列表。"""
        captured = []

        @asynccontextmanager
        async def fake_db():
            session = MagicMock()
            session.add = captured.append
            session.flush = AsyncMock()
            yield session

        mock_http = MagicMock()
        mock_http.request = AsyncMock(return_value=resp)

        with patch("adapters.base.http", mock_http), patch("adapters.base.db_session", fake_db):
            await adapter._call_action(AdapterRequest(
                action="submit_booking", method="POST", path="/api/x", payload={"roomId": 1},
            ))
        return captured

    async def test_success_call_logs_full_audit_fields(self):
        """成功调用:落一条 AdapterCallLog,字段齐全且关联正确。"""
        cred_provider = MagicMock()
        cred_provider.resolve = AsyncMock(return_value=MagicMock(headers={"X-API-Key": "k"}))
        adapter = _FakeAdapter(base_url="http://oa", credential_provider=cred_provider)

        captured = await self._run_call(adapter, _make_resp(code=0, data={"value": "B1"}))

        self.assertEqual(len(captured), 1)
        log = captured[0]
        self.assertIsInstance(log, AdapterCallLog)
        # 关联字段
        self.assertEqual(log.process_id, 100)
        self.assertEqual(log.operator_id, "9527")
        self.assertEqual(log.tenant_id, "1")
        self.assertEqual(log.trace_id, "trace-1")
        self.assertEqual(log.credential_source, "service_account_delegated")
        self.assertIsNone(log.step_execution_id)  # 阶段一留空
        # 调用留痕字段
        self.assertEqual(log.adapter, "fake_adapter")
        self.assertEqual(log.target_system, "oa")
        self.assertEqual(log.action, "submit_booking")
        self.assertEqual(log.method, "POST")
        self.assertEqual(log.http_status, 200)

    async def test_no_operator_logs_null_operator_fields(self):
        """无 operator(未认证上下文):operator_id/tenant_id/credential_source 为 None。"""
        set_request_context(None)  # 清掉 operator / process_id

        cred_provider = MagicMock()
        cred_provider.resolve = AsyncMock(return_value=MagicMock(headers={"X-API-Key": "k"}))
        adapter = _FakeAdapter(base_url="http://oa", credential_provider=cred_provider)

        captured = await self._run_call(adapter, _make_resp(code=0, data=True))

        self.assertEqual(len(captured), 1)
        log = captured[0]
        self.assertIsNone(log.operator_id)
        self.assertIsNone(log.tenant_id)
        self.assertIsNone(log.credential_source)

    async def test_failed_call_still_logs(self):
        """业务失败(code!=0):仍落一条留痕(is_error 路径)。"""
        cred_provider = MagicMock()
        cred_provider.resolve = AsyncMock(return_value=MagicMock(headers={"X-API-Key": "k"}))
        adapter = _FakeAdapter(base_url="http://oa", credential_provider=cred_provider)

        captured = await self._run_call(adapter, _make_resp(code=500, data=None, status=200))

        self.assertEqual(len(captured), 1)
        log = captured[0]
        self.assertIsNotNone(log.error_message)  # 失败原因落库
        self.assertEqual(log.process_id, 100)


if __name__ == "__main__":
    unittest.main()