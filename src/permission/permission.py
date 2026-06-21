"""工作流角色准入查询(DB + redis 缓存)。

层 A RBAC 的配置源:``workflow_role_gate`` 表(``workflow_name`` → ``role`` 集合),
运行时可由运维增删。查询走 redis 缓存(TTL = ``settings.workflow_role_cache_ttl``);
改配置后调 :meth:`invalidate` 立即生效,或等 TTL 过期。

**无记录 = 全员可用**(返回空集,由 ``BaseWorkflow.is_allowed`` 据此放行)。
"""
from __future__ import annotations

import json

from sqlalchemy import select

from conf.config import settings
from infra.database import db_session
from infra.models import WorkflowRole
from infra.redis_client import get_redis


class WorkflowRoleChecker:
    """工作流角色准入查询(``workflow_role`` 表 + redis 缓存)。"""

    def _key(self, workflow_name: str) -> str:
        return f"workflow_roles:{workflow_name}"

    async def get_allowed_roles(self, workflow_name: str) -> set[str]:
        """取 workflow 的允许角色集合(无记录 = 空集 = 全员可用)。

        空集也缓存,避免「未配置的 workflow」每次请求都查 DB。
        """
        redis = get_redis()
        workflow_roles = await redis.get(self._key(workflow_name))
        if workflow_roles is not None:
            return set(json.loads(workflow_roles))

        async with db_session() as session:
            rows = await session.execute(
                select(WorkflowRole.role).where(
                    WorkflowRole.workflow_name == workflow_name
                )
            )
            roles = {row[0] for row in rows}

        await redis.set(
            self._key(workflow_name),
            json.dumps(sorted(roles)),
            ex=settings.workflow_role_cache_ttl,
        )
        return roles

    async def invalidate(self, workflow_name: str) -> None:
        """清缓存(运维改 ``workflow_role`` 后调,立即生效)。"""
        await get_redis().delete(self._key(workflow_name))


# 模块级单例
workflow_role_checker = WorkflowRoleChecker()