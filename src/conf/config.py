"""
应用配置

用法::

    from src.conf import settings

    settings.mysql_host            # 读单个配置
    settings.mysql_conf            # 直接拿到拼好的 MySQL 连接串
"""
from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录
ROOT_PATH = Path(__file__).parents[2]


class Settings(BaseSettings):
    """全局配置。

    字段名采用小写下划线,默认大小写不敏感地匹配 `MYSQL_HOST` 这类全大写环境变量。
    """

    model_config = SettingsConfigDict(
        env_file=f"{ROOT_PATH}/.env",
        env_file_encoding="utf-8",
        case_sensitive=False,  # MYSQL_HOST 与 mysql_host 等价
        extra="ignore",  # 容忍未声明的变量
    )

    # ---- MySQL----
    mysql_host: str = "127.0.0.1"

    mysql_port: int = 3306

    mysql_database: str

    mysql_user: str

    mysql_password: str

    pool_size: int = 10

    max_size: int = 20

    keep_alive: int = 3600

    # 生产关闭 SQL 日志; 本地调试可置 True
    sql_log: bool = False

    # ---- 通用 ----
    tz: str = "Asia/Shanghai"

    # ---- LLM ----
    llm_provider: str | None = None

    llm_api_key: str | None = None

    llm_model: str | None = None

    llm_base_url: str | None = None

    llm_timeout: int = 60

    llm_temperature: float = 0.3

    # ---- 非空校验 ----
    @field_validator("mysql_host", "mysql_database", "mysql_user", "mysql_password", "tz",
                     "llm_provider", "llm_api_key", "llm_model", "llm_base_url")
    @classmethod
    def _required_non_blank(cls, value: str | None, info) -> str | None:
        if value is None or not value.strip():
            # 根据字段名前缀判断配置类型
            category = "数据库" if info.field_name.startswith("mysql_") else "大模型"
            raise ValueError(f"{category}配置不能为空")
        return value.strip()

    @property
    def mysql_conf(self) -> str:
        """拼好的 MySQL 连接串(asyncmy 驱动),供 SQLAlchemy 等直接使用。"""
        return (
            f"mysql+asyncmy://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )


# 模块级单例:导入本模块即完成校验,必填项缺失会在此时抛出,做到「启动即失败」。
settings = Settings()

if __name__ == '__main__':
    for k, v in settings.__dict__.items():
        print(k, v)
