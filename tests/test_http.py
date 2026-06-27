"""infra.http 异步 HTTP 客户端测试。

迁移自 ``src/infra/http.py`` 的 ``__main__`` 段:发起一次真实 GET 请求,
验证客户端可用并能正常返回。异步测试使用 ``IsolatedAsyncioTestCase``。

注意:依赖外网可达(原 ``__main__`` 同样如此),断网时会失败。

运行::

    python -m unittest tests.test_http
"""

import unittest

from infra.http import close, http_get


class HttpTest(unittest.IsolatedAsyncioTestCase):
    """异步 HTTP 客户端冒烟测试。"""

    async def test_http_get(self):
        resp = await http_get("https://www.baidu.com")
        print(resp.status_code, resp.text[:100])
        self.assertEqual(resp.status_code, 200)

    async def asyncTearDown(self):
        # 释放进程级单例 client 的连接池,避免资源告警
        await close()


if __name__ == "__main__":
    unittest.main()
