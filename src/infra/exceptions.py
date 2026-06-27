"""HTTP 业务异常。

为常见 HTTP 状态码各自封装一个异常类,默认描述信息与其语义一致。
业务层直接 ``raise`` 即可,全局异常处理器可统一读取 ``exc.status_code``
与 ``exc.detail`` 进行响应。

示例::

    raise NotFoundException("用户不存在")
    raise UnauthorizedException("token 已过期")
"""

from __future__ import annotations


class ApiException(Exception):
    """所有 HTTP 业务异常的基类。

    :param detail: 异常描述信息,缺省时取 ``default_detail``(与异常语义一致)。
    """

    status_code: int = 500
    default_detail: str = "服务器内部错误"

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail or self.default_detail
        super().__init__(self.detail)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status_code={self.status_code}, detail={self.detail!r})"


class BadRequestException(ApiException):
    """400 请求参数错误。"""

    status_code = 400
    default_detail = "请求参数错误"


class UnauthorizedException(ApiException):
    """401 未认证或认证已过期。"""

    status_code = 401
    default_detail = "未认证或认证已过期"


class ForbiddenException(ApiException):
    """403 无权访问该资源。"""

    status_code = 403
    default_detail = "无权访问该资源"


class NotFoundException(ApiException):
    """404 资源不存在。"""

    status_code = 404
    default_detail = "资源不存在"


class ConflictException(ApiException):
    """409 资源已存在或状态冲突。"""

    status_code = 409
    default_detail = "资源已存在或状态冲突"


class ValidationException(ApiException):
    """422 参数校验失败。"""

    status_code = 422
    default_detail = "参数校验失败"


class RateLimitException(ApiException):
    """429 请求过于频繁。"""

    status_code = 429
    default_detail = "请求过于频繁,请稍后再试"


class ServerErrorException(ApiException):
    """500 服务器内部错误。"""

    status_code = 500
    default_detail = "服务器内部错误"


class BadGatewayException(ApiException):
    """502 上游服务网关错误。"""

    status_code = 502
    default_detail = "上游服务网关错误"


class ServiceUnavailableException(ApiException):
    """503 服务暂不可用。"""

    status_code = 503
    default_detail = "服务暂不可用"


class GatewayTimeoutException(ApiException):
    """504 上游服务响应超时。"""

    status_code = 504
    default_detail = "上游服务响应超时"
