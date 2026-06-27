"""适配层统一抽象。所有面向外部业务系统的适配器均继承"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx
from sqlalchemy.exc import SQLAlchemyError

from infra import http
from infra.database import db_session
from infra.exceptions import (
    ApiException,
    BadGatewayException,
    BadRequestException,
    ConflictException,
    ForbiddenException,
    GatewayTimeoutException,
    NotFoundException,
    RateLimitException,
    ServerErrorException,
    ServiceUnavailableException,
    UnauthorizedException,
    ValidationException,
)
from infra.logger import setup_logging
from orchestrator.workflow_engine import StepResult
from repository.models import AdapterCallLog
from runtime.context import get_operator, get_process_id, get_step_id
from services.credential import CredentialProvider, default_credential_provider
from utils.trace_id_util import get_trace_id

logger = setup_logging(__name__)

# HTTP 状态码 -> 业务异常(错误码归一),未列出的 4xx/5xx 统一归一为 500
_STATUS_EXCEPTIONS: dict[int, type[ApiException]] = {
    400: BadRequestException,
    401: UnauthorizedException,
    403: ForbiddenException,
    404: NotFoundException,
    409: ConflictException,
    422: ValidationException,
    429: RateLimitException,
    500: ServerErrorException,
    502: BadGatewayException,
    503: ServiceUnavailableException,
    504: GatewayTimeoutException,
}


@dataclass(frozen=True)
class AdapterRequest:
    """适配层请求参数。

    - ``payload``:JSON 请求体(curl 的 ``-d``);无 body 的请求留空,底层不会发送请求体。
    - ``params``:query 参数(url 上的 ``?k=v``,如 ``approve?id=123``)。
    - ``path_params``:路径参数;渲染 ``path`` 中的 ``{key}`` 占位符
      (如 ``path="/api/orders/{id}"`` + ``path_params={"id": 123}`` -> ``/api/orders/123``)。
    """

    action: str
    method: str
    path: str
    payload: dict[str, Any] | None = None  # JSON body参数
    params: dict | None = None  # query参数


@dataclass(frozen=True)
class AdapterResult:
    """adapter 从下游响应提取的结构化业务结果。

    :param data: 业务数据
    """

    data: Any = None


@dataclass(frozen=True)
class AdapterResponse:
    """单次外部调用的结构化留痕。

    字段与 ``repository.models.AdapterCallLog`` 对齐,编排层可直接据此落库审计。
    """

    adapter: str
    target_system: str
    action: str
    method: str
    request_payload: dict = field(default_factory=dict)
    response_payload: dict = field(default_factory=dict)
    result: AdapterResult = field(default_factory=AdapterResult)
    http_status: int = 200
    duration: int = 0
    is_error: bool = False
    error_message: str | None = None
    operator_id: str | None = None  # 真实操作人
    tenant_id: str | None = None  # 所属租户
    credential_source: str | None = None  # 凭证来源

    @property
    def ok(self) -> bool:
        """调用是否成功(非错误)。"""
        return not self.is_error


class BaseAdapter(ABC):
    """外部业务系统适配器基类。"""

    # 适配器名称
    adapter_name: ClassVar[str] = ""
    # 下游业务系统
    target_system = ""

    def __init__(
            self,
            base_url: str = "",
            credential_provider: CredentialProvider | None = None,
    ):
        self.base_url = base_url
        # 凭证默认按 target_system 自动加载服务账号; 也可显式注入覆盖。
        self.credential_provider = credential_provider or default_credential_provider(self.target_system)

    async def _call_action(self, request: AdapterRequest) -> AdapterResponse:
        """统一调用入口。

        :param request 适配器
        """
        operator = get_operator()
        request_payload = request.payload or {}
        request_params = request.params or {}

        started = time.perf_counter()

        http_status = 0
        response_payload: dict = {}
        result = AdapterResult()
        error_message: str | None = None
        is_error: bool = False

        try:
            # 代签:凭证由 credential_provider 供给(服务账号 api-key + operator 代签头)
            if self.credential_provider is None:
                headers: dict[str, str] = {}
            else:
                credential = await self.credential_provider.resolve(operator)
                headers = credential.headers
            resp = await http.request(
                request.method,
                url=f"{self.base_url}{request.path}",
                json=request_payload or None,  # 空 dict -> None:无 body 的请求不发请求体
                params=request_params or None,  # query 参数(?k=v)
                headers=headers,
            )
            http_status = resp.status_code

            # 从真实 HTTP 响应解析出 data,失败时回退为文本
            try:
                data = resp.json()
                response_payload = data if isinstance(data, dict) else {"data": data}
            except ValueError:
                response_payload = {"text": resp.text}

            # 判断业务执行是否成功
            success, reason = self.is_success(http_status, response_payload)
            if not success:
                exp = _STATUS_EXCEPTIONS.get(http_status, ServerErrorException)
                raise exp(reason or f"下游返回状态码 {http_status}")

            # 从原始 body 提取业务结果
            result = self.extract_result(response_payload)
        except ApiException as exc:
            # 非 2xx:响应体已解析,归一为业务异常后记下错误信息,构造失败留痕返回
            error_message = str(exc)
            is_error = True
        except httpx.HTTPError as exc:
            # 网络层异常(连接 / 超时等):无响应体,统一记为 503 并留痕返回
            http_status = ServiceUnavailableException.status_code
            error_message = f"{self.target_system} 调用失败: {exc}"
            is_error = True

        duration = int((time.perf_counter() - started) * 1000)
        if error_message is None:
            logger.info(
                "[%s] %s %s -> %s (%dms)", self.target_system, request.action, request.method, http_status, duration
            )
        else:
            logger.warning(
                "[%s] %s %s -> %s (%dms) 失败: %s",
                self.target_system,
                request.action,
                request.method,
                http_status,
                duration,
                error_message,
            )

        # 请求参数快照(供审计留痕;落库由 step_call 统一做)
        traced_payload: dict[str, Any] = {}
        if request_payload:
            traced_payload["body"] = request_payload
        if request_params:
            traced_payload["params"] = request_params

        return AdapterResponse(
            adapter=self.adapter_name,
            target_system=self.target_system,
            action=request.action,
            method=request.method,
            request_payload=traced_payload,
            response_payload=response_payload,
            result=result,
            http_status=http_status,
            duration=duration,
            is_error=is_error,
            error_message=error_message,
            operator_id=operator.user_id if operator else None,
            tenant_id=operator.tenant_id if operator else None,
            credential_source="service_account_delegated" if operator else None,
        )

    async def step_call(self, request: AdapterRequest) -> StepResult:
        """_call_action + 转 StepResult(供 workflow engine 的 Step.func 用)。

        adapter 层负责 AdapterResponse → StepResult 转换(引擎不认 AdapterResponse)。
        """
        resp = await self._call_action(request)

        # 落库审计(每次下游 HTTP 调用一条留痕;落库失败只记日志,不影响主流程)
        try:
            async with db_session() as session:
                session.add(
                    AdapterCallLog(
                        process_id=get_process_id(),
                        step_execution_id=get_step_id(),
                        adapter=resp.adapter,
                        target_system=resp.target_system,
                        action=resp.action,
                        method=resp.method,
                        http_status=resp.http_status,
                        request_payload=resp.request_payload,
                        response_payload=resp.response_payload,
                        error_message=resp.error_message,
                        duration_ms=resp.duration,
                        trace_id=get_trace_id(),
                        operator_id=resp.operator_id,
                        tenant_id=resp.tenant_id,
                        credential_source=resp.credential_source,
                    )
                )
        except SQLAlchemyError:
            logger.exception("落库 AdapterCallLog 失败(action=%s)", resp.action)

        return StepResult(
            ok=not resp.is_error,
            data=resp.result.data if resp.result else None,
            error=resp.error_message,
        )

    @abstractmethod
    def is_success(self, http_status: int, response_payload: dict) -> tuple[bool, str | None]:
        """判定本次调用是否业务成功,并给出失败原因"""

    @abstractmethod
    def extract_result(self, payload: dict) -> AdapterResult:
        """从响应体提取结构化业务结果(data,作为步产出持久化)"""
