"""配置包:对外暴露唯一的 ``settings`` 单例。"""
from src.conf.config import Settings, settings

__all__ = ["Settings", "settings"]
