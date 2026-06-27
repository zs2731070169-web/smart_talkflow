"""看门狗:周期扫描心跳超时的工作流,触发降级处理(补偿 + 标 failed,不重跑)。

进程级单任务,随 agent lifespan 启停;watchdog 与各工作流协程同进程。
本模块只做最薄一层:周期 detect + 触发降级;补偿 / 重建 steps / 标 failed 在 services.downgrade。
"""

from __future__ import annotations

import asyncio

from sqlalchemy.exc import SQLAlchemyError

from conf.config import settings
from infra.logger import setup_logging
from orchestrator.base import WorkflowRegistry
from orchestrator.downgrade import handle_processes

logger = setup_logging(__name__)


async def heartbeat_watchdog(registry: WorkflowRegistry):
    """周期扫描失联工作流,触发降级处理。

    :param registry: 工作流注册器(降级处理重建 steps 需要,由 main 从 Runtime 传入)。
    """
    while True:
        try:
            await asyncio.sleep(settings.process_recovery_interval)
            handled = await handle_processes(registry)
            if handled:
                logger.info("watchdog 本轮降级处理 %d 个失联工作流", handled)
        except asyncio.CancelledError:
            # 停机取消:正常退出循环
            raise
        except SQLAlchemyError:
            logger.exception("watchdog 轮次异常")
