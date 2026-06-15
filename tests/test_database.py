"""infra.database 数据库会话测试。

迁移自 ``src/infra/database.py`` 的 ``__main__`` 段:通过 ``db_session``
执行一次 ``select 1``,验证连接池与会话上下文(自动提交 / 回滚)可用。
异步测试使用 ``IsolatedAsyncioTestCase``。

注意:依赖真实 MySQL(原 ``__main__`` 同样如此),数据库不可达时会失败。

运行::

    python -m unittest tests.test_database
"""
import unittest

from sqlalchemy import text

from infra.database import db_session, dispose_engine


class DatabaseTest(unittest.IsolatedAsyncioTestCase):
    """数据库会话冒烟测试。"""

    async def test_select_one(self):
        async with db_session() as session:
            sql = "select 1"
            result = await session.execute(text(sql))
            value = result.scalar()  # 获取标量值 1
            print(value)
            self.assertEqual(value, 1)

    async def asyncTearDown(self):
        # 释放进程级连接池
        await dispose_engine()


if __name__ == "__main__":
    unittest.main()
