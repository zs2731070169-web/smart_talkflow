"""OA(yudao)系统级适配器基类。

承载与具体业务域无关的 yudao 系统级协议约定,供各 OA 业务域 adapter
(会议室 / 员工 / 邮箱 …)继承:

- ``target_system = "oa"``:服务账号凭证按此自动加载(BaseAdapter.__init__),
  编排层 ``Step.adapter`` 也据此自动推断。
- ``is_success`` / ``extract_result``:yudao 统一响应协议——HTTP 2xx 且
  body ``code == 0`` 视为业务成功,业务数据取 body ``data`` 字段。

业务域 adapter(如会议室预订)只声明自己的 action,无需重复实现协议解析。
"""

from __future__ import annotations

from adapters.base import AdapterResult, BaseAdapter


class OAAdapter(BaseAdapter):
    """OA(yudao)系统适配器基类,统一 yudao 响应协议解析。"""

    target_system = "oa"

    def is_success(self, http_status: int, response_payload: dict) -> tuple[bool, str | None]:
        """yudao 判定:HTTP 2xx 且 body ``code == 0`` 视为业务成功。"""
        if not (200 <= http_status < 300):
            return False, None
        code = response_payload.get("code")
        if code == 0:
            return True, None
        return False, response_payload.get("msg") or f"业务失败 code={code}"

    def extract_result(self, payload: dict) -> AdapterResult:
        """提取 yudao 响应的 ``data``。"""
        return AdapterResult(data=payload.get("data"))
