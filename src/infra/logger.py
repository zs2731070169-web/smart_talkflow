"""日志配置。

统一日志输出格式,并通过自定义 :class:`TraceIdFilter` 把全局
``trace_id`` 注入到每条日志记录,便于按请求/任务串联整条调用链。

初始化方式:首次导入本模块即完成 logger 的配置。因此只需在
程序入口(如 ``main.py``)``from src.infra import logger`` 一次,
全进程的业务代码即可直接使用::

    import logger
    logger = logger.setup_logging(__name__)
    logger.info("用户请求开始")
"""

from __future__ import annotations

import logging
from logging import Logger
from logging.handlers import TimedRotatingFileHandler

from conf.config import ROOT_PATH
from utils.trace_id_util import get_trace_id

# trace_id 缺省时日志里显示的占位符
_TRACE_ID_PLACEHOLDER = ""

# 定义格式
LOG_FORMAT = "%(asctime)s - %(trace_id)s - %(name)s - %(levelname)s:%(lineno)d - %(message)s"

# 日志存放路径
LOG_PATH = ROOT_PATH / "logs"


class TraceIdFilter(logging.Filter):
    """当前上下文的 trace_id 配置"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or _TRACE_ID_PLACEHOLDER
        return True


class LoggerFormatter(logging.Formatter):
    """日志样式配置"""

    grey = "\x1b[90;20m"  # 灰色(亮黑)
    green = "\x1b[32;20m"  # 绿色
    yellow = "\x1b[33;20m"  # 黄色
    red = "\x1b[31;20m"  # 红色
    bold_red = "\x1b[31;1m"  # 加粗红色
    reset = "\x1b[0m"  # 重置颜色（否则后面所有字都会变色）

    FORMAT = {
        logging.DEBUG: grey + LOG_FORMAT + reset,
        logging.INFO: green + LOG_FORMAT + reset,
        logging.WARNING: yellow + LOG_FORMAT + reset,
        logging.ERROR: red + LOG_FORMAT + reset,
        logging.CRITICAL: bold_red + LOG_FORMAT + reset,
    }

    def format(self, record: logging.LogRecord) -> str:
        format = LoggerFormatter.FORMAT[record.levelno]
        return logging.Formatter(format, datefmt="%Y-%m-%d %H:%M:%S").format(record)


def setup_logging(name: str = "") -> logging.Logger:
    """配置 logger:控制台输出 + 日志文件输出 + 带 trace_id 的统一格式。"""

    logger = logging.getLogger(name)

    # 清掉既有 handler,避免叠加重复输出
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    # 日志级别 (默认为debug最低)
    logger.setLevel(logging.DEBUG)

    # 控制台日志输出
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.addFilter(TraceIdFilter())
    handler.setFormatter(LoggerFormatter())
    logger.addHandler(handler)

    # 文件日志保存
    _file_logging(logger, name, logging.getLevelName(logging.INFO), 7)
    _file_logging(logger, name, logging.getLevelName(logging.ERROR), 30)
    _file_logging(logger, name, logging.getLevelName(logging.DEBUG), 7)

    return logger


def _file_logging(logger: Logger, name: str, level: int | str, backup: int) -> None:
    level_name = str(level).lower()
    log_dir = LOG_PATH / level_name
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        filename=str(log_dir / f"{name}_{level_name}.log"),  # 当前活跃日志:logs/{level}/{name}_{level}_{date}.log
        when="midnight",  # 每天午夜滚动
        interval=1,  # 滚动间隔(天)
        encoding="utf-8",  # 日志字符集,防止中文乱码
        backupCount=backup,  # 保留的归档天数
    )
    file_handler.setLevel(level)
    file_handler.addFilter(TraceIdFilter())
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(file_handler)
