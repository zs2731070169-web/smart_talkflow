"""
应用配置

用法::

    from src.conf import settings

    settings.mysql_host            # 读单个配置
    settings.mysql_conf            # 直接拿到拼好的 MySQL 连接串
"""
from __future__ import annotations

from pathlib import Path

from pydantic import model_validator
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

    # ---- 提示词仓库(可选,用于从远程 git 仓库拉取系统提示词)----
    is_git_repo: bool = False

    git_repo_url: str | None = None

    git_branch: str | None = None

    git_relative_path: str | None = None

    # ---- 下游业务系统 ----
    # 接入 OA
    oa_base_url: str

    # api-key
    oa_api_key: str

    # HMAC 签名密钥
    oa_delegation_secret: str

    # ---- Redis----
    redis_url: str = "redis://127.0.0.1:6379/0"

    # ---- 认证模式----
    auth_dev_mode: bool = True

    # ---- SSO(生产态:auth_dev_mode=False 时走 SSO/JWT)----
    sso_issuer: str | None = None       # JWT iss 校验

    sso_jwks_uri: str | None = None     # JWKS 公钥端点

    sso_audience: str | None = None     # JWT aud 校验

    sso_jwks_cache_ttl: int = 3600      # JWKS redis 缓存秒数

    # ---- 非空校验 ----
    @model_validator(mode="after")
    def _required_non_blank(self) -> Settings:
        if not (
                self.llm_model and self.llm_provider and self.llm_base_url and self.llm_api_key and self.llm_timeout and self.llm_temperature):
            raise ValueError("llm 配置不能为空")

        if not (
                self.mysql_host and self.mysql_database and self.mysql_user and self.mysql_password and self.sql_log and self.tz):
            raise ValueError("mysql 配置不能为空")

        # 启用远程仓库拉取时仓库地址必填;分支与相对路径保持可选
        if self.is_git_repo and not (self.git_repo_url and self.git_repo_url.strip()):
            raise ValueError("启用提示词仓库(is_git_repo=True)时必须配置 GIT_REPO_URL")

        if not self.oa_base_url:
            raise ValueError("下游系统配置不能为空:oa_base_url")

        if not (self.oa_api_key and self.oa_delegation_secret):
            raise ValueError("下游系统认证配置不能为空:oa_api_key / oa_delegation_secret")

        # 生产态(auth_dev_mode=False)走 SSO/JWT:issuer / jwks_uri / redis_url 必填
        if not self.auth_dev_mode and not (
            self.sso_issuer and self.sso_jwks_uri and self.redis_url
        ):
            raise ValueError("生产态(auth_dev_mode=False)必须配置:sso_issuer / sso_jwks_uri / redis_url")
        return self

    @property
    def mysql_conf(self) -> str:
        """拼好的 MySQL 连接串(asyncmy 驱动),供 SQLAlchemy 等直接使用。"""
        return (
            f"mysql+asyncmy://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )


# 模块级单例:导入本模块即完成校验,必填项缺失会在此时抛出,做到「启动即失败」。
settings = Settings()
