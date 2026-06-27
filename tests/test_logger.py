"""infra.logger 日志配置测试。

迁移自 ``src/infra/logger.py`` 的 ``__main__`` 段:验证 ``setup_logging``
返回可用 logger,并能正常输出各级别日志。

运行::

    python -m unittest tests.test_logger
"""

import logging
import unittest

from infra.logger import setup_logging
from utils.trace_id_util import new_trace_id


class LoggerTest(unittest.TestCase):
    """日志初始化与各级别输出。"""

    def test_setup_logging_and_levels(self):
        """配置 logger 并依次输出 debug~critical 五个级别日志。"""
        new_trace_id()
        logger = setup_logging(__name__)

        # 与原 __main__ 一致:依次输出各级别日志
        logger.debug("这是一个debug日志")
        logger.info("这是一个info日志")
        logger.warning("这是一个warning日志")
        logger.error("这是一个error日志")
        logger.critical("这是一个critical日志")

        # 返回的应是标准 logging.Logger,且名称与传入一致
        self.assertIsInstance(logger, logging.Logger)
        self.assertEqual(logger.name, __name__)


if __name__ == "__main__":
    unittest.main()
