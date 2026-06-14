"""异步 HTTP 客户端。

基于 ``httpx.AsyncClient`` 封装 GET / POST / PUT / PATCH / DELETE 等常用
请求方法,底层 client 为进程级单例,复用连接池(keep-alive)。每个请求
自动把当前上下文的 ``trace_id`` 注入请求头,便于与日志串联整条调用链。

client 采用懒加载:模块导入时不会创建,首次请求时才实例化,避免在导入
阶段触发环境代理探测等副作用。

示例::

    from infra.http import http_get, http_post

    resp = await http_get("https://api.example.com/users")
    data = await http_post(url, json={"name": "tom"})
    # 调用方自行决定如何消费响应
    print(resp.status_code, resp.json())
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from infra.logger import setup_logging
from utils.trace_id_util import get_trace_id

logger = setup_logging(__name__)

# trace_id 注入到请求头的字段名
_TRACE_ID_HEADER = "X-Trace-Id"

# 默认请求超时(秒):总时长 30s,其中连接建立最多 5s
_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

# 首次请求时懒创建
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """获取(必要时创建)进程级单例 client,全进程复用其连接池。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
    return _client


async def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """发送任意方法的异步 HTTP 请求。"""
    headers = kwargs.pop("headers", None) or {}
    trace_id = get_trace_id()
    if trace_id:
        headers.setdefault(_TRACE_ID_HEADER, trace_id)

    logger.info("%s %s", method, url)
    resp = await _get_client().request(method, url, headers=headers, **kwargs)
    logger.info("%s %s -> %s", method, url, resp.status_code)
    return resp


async def http_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET 请求。"""
    return await request("GET", url, **kwargs)


async def http_post(url: str, **kwargs: Any) -> httpx.Response:
    """POST 请求。"""
    return await request("POST", url, **kwargs)


async def http_put(url: str, **kwargs: Any) -> httpx.Response:
    """PUT 请求。"""
    return await request("PUT", url, **kwargs)


async def http_patch(url: str, **kwargs: Any) -> httpx.Response:
    """PATCH 请求。"""
    return await request("PATCH", url, **kwargs)


async def http_delete(url: str, **kwargs: Any) -> httpx.Response:
    """DELETE 请求。"""
    return await request("DELETE", url, **kwargs)


async def close() -> None:
    """关闭 client、释放连接池(应用停机时调用一次)。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


if __name__ == "__main__":
    async def main() -> None:
        resp = await http_get("https://www.baidu.com")
        print(resp.status_code, resp.text[:100])

    asyncio.run(main())
