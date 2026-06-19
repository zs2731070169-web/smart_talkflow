"""适配层:封装对传统业务系统的 HTTP / RPA 调用。

- :class:`BaseAdapter`:适配器统一抽象(错误码归一 + 调用留痕)。
- :class:`AdapterResponse`:结构化调用留痕(字段对齐 ``AdapterCallLog`` 表)。
"""

from adapters.base import AdapterResponse, BaseAdapter
from services.credential import Credential, CredentialProvider

__all__ = ["AdapterResponse", "BaseAdapter", "Credential", "CredentialProvider"]
