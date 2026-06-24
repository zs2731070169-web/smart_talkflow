"""step_tracker(process_step 落库)单测。

照 ``test_audit_logging`` 的 ``fake_db`` 模式 mock ``db_session``,验证:
:meth:`create_step` 插入 running 占位、:meth:`finish_step` 更新终态与产出、
:meth:`update_compensation` 标记补偿状态。

运行(项目根)::

    PYTHONPATH=src python -m unittest tests.test_step_recorder
"""
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from repository.models import ProcessStep
from repository.step_tracker import (
    CompensationStatus,
    StepStatus,
    create_step,
    finish_step,
    update_compensation,
)


def _fake_db(*, captured=None, row=None):
    """构造 fake db_session:add 截获到 captured、get 返回 row、flush 为 AsyncMock。"""

    @asynccontextmanager
    async def fake():
        session = MagicMock()
        if captured is not None:
            session.add = captured.append
        session.get = AsyncMock(return_value=row)
        session.flush = AsyncMock()
        yield session

    return fake


class StepRecorderTest(unittest.IsolatedAsyncioTestCase):

    async def test_create_step_inserts_running_placeholder(self):
        """create_step:插入一条 status=running 的 ProcessStep 占位。"""
        captured: list = []
        with patch("repository.step_tracker.db_session", _fake_db(captured=captured)):
            await create_step(
                100, 1, "submit_booking", "提交预订", "oa", "submit_booking", {"roomId": 1}
            )

        self.assertEqual(len(captured), 1)
        step = captured[0]
        self.assertIsInstance(step, ProcessStep)
        self.assertEqual(step.process_id, 100)
        self.assertEqual(step.step_no, 1)
        self.assertEqual(step.step_key, "submit_booking")
        self.assertEqual(step.adapter, "oa")
        self.assertEqual(step.status, StepStatus.RUNNING.value)
        self.assertIsNotNone(step.started_at)

    async def test_finish_step_updates_terminal_status(self):
        """finish_step:把 running 步更新为 completed,写产出 / 耗时 / finished_at。"""
        row = ProcessStep(id=1, status=StepStatus.RUNNING.value)
        with patch("repository.step_tracker.db_session", _fake_db(row=row)):
            await finish_step(
                1, status=StepStatus.COMPLETED,
                output_result={"booking_id": 123}, duration_ms=10,
            )

        self.assertEqual(row.status, StepStatus.COMPLETED.value)
        self.assertEqual(row.output_result, {"booking_id": 123})
        self.assertEqual(row.duration_ms, 10)
        self.assertIsNotNone(row.finished_at)

    async def test_update_compensation_sets_status(self):
        """update_compensation:标记 compensation_status(done / failed)。"""
        row = ProcessStep(id=1)
        with patch("repository.step_tracker.db_session", _fake_db(row=row)):
            await update_compensation(1, CompensationStatus.DONE)
        self.assertEqual(row.compensation_status, CompensationStatus.DONE.value)

    async def test_finish_step_noop_when_row_missing(self):
        """finish_step:行不存在(session.get 返回 None)时安全返回,不报错。"""
        with patch("repository.step_tracker.db_session", _fake_db(row=None)):
            await finish_step(999, status=StepStatus.FAILED)  # 不应抛异常


if __name__ == "__main__":
    unittest.main()