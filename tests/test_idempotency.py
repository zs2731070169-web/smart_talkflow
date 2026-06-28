"""orchestrator.idempotency 流程级幂等校验测试。

测试分两层,对应被测代码的两类逻辑:

1. **纯逻辑层**(``unittest.TestCase``,不碰 DB,快且稳定)
   - :func:`build_idempotency_key`:空值校验 / 超长截断 / 边界长度
   - :class:`IdempotencyChecker` 构造校验
   - :meth:`IdempotencyChecker._check_process`:命中已存在记录后的状态分流
     (completed / running / failed / None / pending)。该方法是
     ``staticmethod``,接收普通 :class:`Process` 对象即可判定,无需落库。

2. **集成层**(``IsolatedAsyncioTestCase``,依赖真实 MySQL)
   - :meth:`IdempotencyChecker.check` 全流程:未命中 -> NEW 落库、命中各状态分流
   - 状态迁移方法 :meth:`completed` / :meth:`failed` 及其幂等保护
   - 并发同 key 触发 ``process.idempotency_key`` 唯一索引 ``IntegrityError``,
     验证「flush 冲突 -> rollback -> 回查」兜底分支

   集成测试与 ``tests.test_database`` 同样依赖真实 MySQL(唯一索引兜底必须真实
   DB,SQLite 写锁串行化测不出并发冲突)。每用例用「测试方法名」作 business_key
   保证唯一,``asyncSetUp`` / ``tearDown`` 清理本用例行,不污染 ``process`` 表。

运行(项目根目录)::

    PYTHONPATH=src python -m unittest tests.test_idempotency
    PYTHONPATH=src python -m unittest tests.test_idempotency.IdempotencyIntegrationTest
"""

import asyncio
import unittest

from sqlalchemy import text

import infra.database as _db
from infra.database import db_session
from orchestrator.idempotency import (
    IdempotencyChecker,
    IdempotencyCheckRequest,
    Status,
    build_idempotency_key,
)
from repository.models import Process

# 与实现一致的最大幂等键长度
_MAX_KEY_LEN = 160


# ============================================================================
# 纯逻辑层:build_idempotency_key
# ============================================================================
class BuildIdempotencyKeyTest(unittest.TestCase):
    """幂等键生成:拼接 / 空值校验 / 超长截断 / 边界。"""

    def test_normal_concat(self):
        """正常入参拼接为 ``{process_key}_{business_key}``。"""
        key = build_idempotency_key("onboarding", "110101199001011234")
        self.assertEqual(key, "onboarding_110101199001011234")

    def test_empty_process_key_raises(self):
        """process_key 为空必须报错。"""
        with self.assertRaises(ValueError):
            build_idempotency_key("", "BK")

    def test_empty_business_key_raises(self):
        """business_key 为空必须报错。"""
        with self.assertRaises(ValueError):
            build_idempotency_key("onboarding", "")

    def test_truncation_when_too_long(self):
        """超过 160 字符截取前 160,且保留 process_key 前缀。"""
        process_key = "onboarding"
        business_key = "B" * 200
        key = build_idempotency_key(process_key, business_key)
        self.assertEqual(len(key), _MAX_KEY_LEN)
        self.assertTrue(key.startswith(f"{process_key}_"))

    def test_boundary_length_not_truncated(self):
        """恰好 160 字符不截断。"""
        # "onboarding_" 共 11 个字符,补齐 business_key 使总长恰为 160
        prefix = "onboarding_"
        business_key = "B" * (_MAX_KEY_LEN - len(prefix))
        key = build_idempotency_key("onboarding", business_key)
        self.assertEqual(len(key), _MAX_KEY_LEN)

    def test_whitespace_business_key_is_rejected(self):
        """纯空白 business_key 经 strip 后视为空,抛 ValueError。"""
        with self.assertRaises(ValueError):
            build_idempotency_key("onboarding", "   ")


# ============================================================================
# 纯逻辑层:IdempotencyChecker 构造
# ============================================================================
class CheckerInitTest(unittest.TestCase):
    """IdempotencyChecker 构造校验。"""

    def test_empty_process_key_raises(self):
        """process_key 为空必须报错。"""
        with self.assertRaises(ValueError):
            IdempotencyChecker("")

    def test_whitespace_process_key_is_rejected(self):
        """纯空白 process_key 经 strip 后视为空,抛 ValueError。"""
        with self.assertRaises(ValueError):
            IdempotencyChecker("   ")

    def test_process_key_property(self):
        """构造后 process_key 可读。"""
        checker = IdempotencyChecker("onboarding")
        self.assertEqual(checker.process_key, "onboarding")


# ============================================================================
# 纯逻辑层:_check_process 状态分流(不落库,构造 Process 对象即可)
# ============================================================================
class CheckProcessStateTest(unittest.TestCase):
    """_check_process 按已存在记录 status 生成决策。"""

    @staticmethod
    def _make_process(status: str, **extra) -> Process:
        """构造一个不落库的 Process 对象,仅用于 _check_process 判定。"""
        return Process(
            id=extra.get("id", 42),
            process_key="onboarding",
            business_key="BK",
            idempotency_key="onboarding_BK",
            status=status,
            result=extra.get("result"),
            error_message=extra.get("error_message"),
            context=extra.get("context"),
        )

    def test_none_returns_reject(self):
        """并发 IntegrityError 回查仍未命中 -> 拒绝执行。"""
        decision = IdempotencyChecker._check_process(None)
        self.assertFalse(decision.is_new)
        self.assertIsNone(decision.process)

    def test_completed_returns_completed_with_result(self):
        """命中 completed -> 返回历史结果,跳过重复执行。"""
        process = self._make_process("completed", result={"emp_id": "9527"})
        decision = IdempotencyChecker._check_process(process)
        self.assertEqual(decision.process.status, Status.COMPLETED)
        self.assertEqual(decision.complete_result, {"emp_id": "9527"})

    def test_running_returns_reject(self):
        """命中 running(流程进行中)-> 拒绝并发重入。"""
        decision = IdempotencyChecker._check_process(self._make_process("running"))
        self.assertEqual(decision.process.status, Status.RUNNING)

    def test_failed_returns_failed(self):
        """命中 failed -> 交上层决策是否重跑(不直接拒绝)。"""
        process = self._make_process("failed", error_message="boom")
        decision = IdempotencyChecker._check_process(process)
        self.assertEqual(decision.process.status, Status.FAILED)
        self.assertEqual(decision.error, "boom")

    def test_pending_returns_reject(self):
        """pending 等其它中间态 -> 直接拒绝执行。"""
        decision = IdempotencyChecker._check_process(self._make_process("pending"))
        self.assertEqual(decision.process.status, "pending")

    def test_unknown_status_returns_reject(self):
        """未知状态 -> 兜底拒绝。"""
        decision = IdempotencyChecker._check_process(self._make_process("whatever"))
        self.assertEqual(decision.process.status, "whatever")


# ============================================================================
# 集成层:真实 MySQL(唯一索引兜底必须真实 DB)
# ============================================================================
async def _cleanup_business_keys(keys: set[str]) -> None:
    """按 business_key 精确删除 process 行,避免测试间相互污染。"""
    if not keys:
        return
    async with db_session() as session:
        for key in keys:
            await session.execute(text("DELETE FROM process WHERE business_key = :k"), {"k": key})


async def _reset_engine_for_loop() -> None:
    """重建 ``infra.database`` 的全局引擎与会话工厂。

    ``IsolatedAsyncioTestCase`` 为每个测试方法启用独立事件循环,而
    ``infra.database`` 的 ``async_engine`` 是模块级单例,连接池里的连接绑定
    首个循环;后续方法复用旧连接会触发 ``RuntimeError: Event loop is closed``。
    每方法开始前 dispose 旧引擎并重建,使连接池绑定当前循环。

    重建后 ``db_session`` 内部引用的模块级 ``AsyncSessionLocal`` 同步更新,
    被测代码 ``from infra.database import db_session`` 无需改动即可生效。
    """
    try:
        await _db.async_engine.dispose()
    except Exception:
        pass
    _db.init_engine()  # 重建引擎与会话工厂(替代手动 create + sessionmaker)


class IdempotencyIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """check() 全流程 + 状态迁移 + 并发兜底(真实 MySQL)。"""

    def setUp(self):
        # 收集本用例产生的 business_key,tearDown 统一清理
        self._keys: set[str] = set()

    def _req(self, **kwargs) -> IdempotencyCheckRequest:
        """生成请求:business_key 默认取测试方法名,保证用例间唯一。"""
        kwargs.setdefault("business_key", f"BK_{self._testMethodName}")
        self._keys.add(kwargs["business_key"])
        return IdempotencyCheckRequest(**kwargs)

    async def asyncSetUp(self):
        # 每方法重建引擎,使连接池绑定当前事件循环(见 _reset_engine_for_loop)
        await _reset_engine_for_loop()
        # 防御:清理可能残留的同名行
        await _cleanup_business_keys({f"BK_{self._testMethodName}"})

    async def asyncTearDown(self):
        await _cleanup_business_keys(self._keys)
        self._keys.clear()
        # 在当前循环内释放引擎,避免循环关闭后连接池后台清理报错
        try:
            await _db.async_engine.dispose()
        except Exception:
            pass

    # ---- check():未命中 -> NEW ----
    async def test_first_check_returns_new_and_persists(self):
        """首次 check 未命中 -> NEW,落库一条 running 记录。"""
        checker = IdempotencyChecker("onboarding")
        decision = await checker.check(self._req(input_params={"name": "张三"}, trace_id="trace-1"))

        self.assertTrue(decision.is_new)
        self.assertIsNotNone(decision.process)
        self.assertEqual(decision.process.status, "running")

        # 落库校验:另开会会话查得到,字段写对
        async with db_session() as session:
            row = await session.get(Process, decision.process.id)
            self.assertIsNotNone(row)
            self.assertEqual(row.status, "running")
            self.assertEqual(row.input_params, {"name": "张三"})
            self.assertEqual(row.trace_id, "trace-1")
            self.assertEqual(row.idempotency_key, "onboarding_BK_test_first_check_returns_new_and_persists")

    async def test_first_check_preserves_user_context(self):
        """调用方传入 context 时原样保留,不覆盖已有键。"""
        checker = IdempotencyChecker("onboarding")
        decision = await checker.check(self._req(context={"foo": "bar"}))
        self.assertEqual(decision.process.context, {"foo": "bar"})

    # ---- check():命中 running -> REJECT ----
    async def test_second_check_while_running_returns_reject(self):
        """同一 key 第二次 check(前一次仍在 running)-> 拒绝并发重入。"""
        checker = IdempotencyChecker("onboarding")
        first = await checker.check(self._req())
        self.assertTrue(first.is_new)

        second = await checker.check(self._req())
        self.assertFalse(second.is_new)
        # 命中同一条记录
        self.assertEqual(second.process.id, first.process.id)

    # ---- check():命中 completed -> COMPLETED ----
    async def test_check_after_completed_returns_completed(self):
        """completed() 后再 check -> COMPLETED,回带历史结果。"""
        checker = IdempotencyChecker("onboarding")
        new_proc = (await checker.check(self._req())).process
        await IdempotencyChecker.completed(new_proc, {"emp_id": "9527"})

        decision = await checker.check(self._req())
        self.assertEqual(decision.process.status, Status.COMPLETED)
        self.assertEqual(decision.complete_result, {"emp_id": "9527"})

    # ---- check():命中 failed -> FAILED ----
    async def test_check_after_failed_returns_failed(self):
        """failed() 后再 check -> FAILED(允许上层决策重跑)。"""
        checker = IdempotencyChecker("onboarding")
        new_proc = (await checker.check(self._req())).process
        await IdempotencyChecker.failed(new_proc, "下游 OA 503")

        decision = await checker.check(self._req())
        self.assertEqual(decision.process.status, Status.FAILED)
        self.assertEqual(decision.error, "下游 OA 503")

    # ---- check():空 business_key ----
    async def test_check_empty_business_key_raises(self):
        """business_key 为空 / None 必须 ValueError,不落库。"""
        checker = IdempotencyChecker("onboarding")
        with self.assertRaises(ValueError):
            await checker.check(IdempotencyCheckRequest(business_key=""))
        with self.assertRaises(ValueError):
            await checker.check(IdempotencyCheckRequest(business_key=None))

    # ---- 状态迁移:completed 幂等 ----
    async def test_completed_is_idempotent(self):
        """重复 completed 不覆盖既有结果。"""
        checker = IdempotencyChecker("onboarding")
        new_proc = (await checker.check(self._req())).process
        await IdempotencyChecker.completed(new_proc, {"r": 1})
        # 第二次 completed 传入不同结果,因 status 已是 completed 应被跳过
        await IdempotencyChecker.completed(new_proc, {"r": 2})

        async with db_session() as session:
            row = await session.get(Process, new_proc.id)
            self.assertEqual(row.status, "completed")
            self.assertEqual(row.result, {"r": 1})

    # ---- 状态迁移:failed 不覆盖 completed ----
    async def test_failed_not_overwrite_completed(self):
        """对已 completed 的记录调 failed() 应被跳过,状态/结果不变。"""
        checker = IdempotencyChecker("onboarding")
        new_proc = (await checker.check(self._req())).process
        await IdempotencyChecker.completed(new_proc, {"r": 1})
        await IdempotencyChecker.failed(new_proc, "late error")

        async with db_session() as session:
            row = await session.get(Process, new_proc.id)
            self.assertEqual(row.status, "completed")
            self.assertEqual(row.result, {"r": 1})

    # ---- 状态迁移:前驱状态约束(completed 仅可由 running 推进)----
    async def test_completed_does_not_overwrite_failed(self):
        """前驱状态约束:对 failed 记录调 completed() 应被跳过,
        不得把「未经重跑的失败」直接标记为成功。"""
        checker = IdempotencyChecker("onboarding")
        new_proc = (await checker.check(self._req())).process
        await IdempotencyChecker.failed(new_proc, "boom")
        # failed 状态不应被 completed 覆盖
        await IdempotencyChecker.completed(new_proc, {"r": 1})

        async with db_session() as session:
            row = await session.get(Process, new_proc.id)
            self.assertEqual(row.status, "failed")
            self.assertIsNone(row.result)

    # ---- 状态迁移:finished_at 终态收尾时间戳 ----
    async def test_terminal_transition_sets_finished_at(self):
        """推进终态后 finished_at 由 None 变非空,且不早于 started_at;
        NEW 阶段 started_at 已置、finished_at 仍为空。"""
        checker = IdempotencyChecker("onboarding")
        new_proc = (await checker.check(self._req())).process

        # NEW 阶段:已开始、未结束
        async with db_session() as session:
            row = await session.get(Process, new_proc.id)
            self.assertIsNotNone(row.started_at)
            self.assertIsNone(row.finished_at)
            started_at = row.started_at

        await IdempotencyChecker.completed(new_proc, {"r": 1})

        async with db_session() as session:
            row = await session.get(Process, new_proc.id)
            self.assertIsNotNone(row.finished_at)
            self.assertGreaterEqual(row.finished_at, started_at)

    # ---- 并发兜底:同 key 并发 check ----
    async def test_concurrent_same_key_only_one_new(self):
        """两个协程并发对同一 key check:唯一索引保证恰好一个 NEW,
        另一个因 flush IntegrityError(或回查命中 running)返回 REJECT,
        二者均不抛异常。覆盖 check() 的并发兜底分支。"""
        checker = IdempotencyChecker("onboarding")
        req = self._req()

        first, second = await asyncio.gather(
            checker.check(req),
            checker.check(req),
        )

        # 恰好一个首次(is_new=True, NEW),另一个非首次(拒绝)
        self.assertEqual(sum(1 for c in (first, second) if c.is_new), 1)

    # ---- 已知缺陷:FAILED 重跑链路缺失 ----
    @unittest.expectedFailure
    async def test_failed_retry_path_missing(self):
        """已知设计缺陷:failed 后没有「重置为 running」的重跑入口。

        现状:check 命中 failed 永远返回 FAILED,旧记录不会被推进,
        上层即使决策「重跑」也无法通过 check 闸门重新执行 ——
        重跑链路在阶段一尚未闭合。此用例断言「能重跑」,故为 expectedFailure。
        """
        checker = IdempotencyChecker("onboarding")
        new_proc = (await checker.check(self._req())).process
        await IdempotencyChecker.failed(new_proc, "boom")

        # 第一次重入:FAILED(交上层决策)
        reentry = await checker.check(self._req())
        self.assertEqual(reentry.process.status, Status.FAILED)

        # 期望:上层决策重跑后,应能再次进入执行(NEW);实际仍为 FAILED
        retried = await checker.check(self._req())
        self.assertTrue(retried.is_new)


if __name__ == "__main__":
    unittest.main()
