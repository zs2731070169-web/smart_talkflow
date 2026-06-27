"""api.deps 认证解析 + RBAC 单元测试。

覆盖:

- :func:`api.deps.resolve_operator`:从 ``X-Operator-*`` 头解析 operator;
  ``roles`` 按逗号拆分;缺 ``X-Operator-Userid`` 返回 ``None``。
- :meth:`orchestrator.base.BaseWorkflow.is_allowed`:无配置放行、命中 / 不命中
  (``is_allowed`` 异步查 ``workflow_role`` 配置 + redis 缓存,这里 mock checker)。

SSO(JWT 验签)路径由 ``tests.test_sso`` 覆盖。

运行(项目根)::

    PYTHONPATH=src python -m unittest tests.test_deps
"""

import unittest
from unittest.mock import AsyncMock, patch

from api.deps import resolve_operator
from permission.permission import WorkflowRoleChecker
from runtime.context import OperatorContext


class ResolveOperatorTest(unittest.TestCase):
    """resolve_operator:开发态请求头解析。"""

    def test_full_headers_parse(self):
        """全头齐备:user_id / tenant / roles 正确解析,roles 按逗号拆分。"""
        op = resolve_operator(
            {
                "X-Operator-Userid": "9527",
                "X-Operator-Tenant": "1",
                "X-Operator-Roles": "hr_admin, employee",
            }
        )
        self.assertIsNotNone(op)
        self.assertEqual(op.user_id, "9527")
        self.assertEqual(op.tenant_id, "1")
        self.assertEqual(op.roles, ["hr_admin", "employee"])

    def test_only_userid_sufficient(self):
        """仅 user_id(其余缺省):roles 为空、 tenant 为空串。"""
        op = resolve_operator({"X-Operator-Userid": "9528"})
        self.assertIsNotNone(op)
        self.assertEqual(op.user_id, "9528")
        self.assertEqual(op.roles, [])
        self.assertEqual(op.tenant_id, "")

    def test_missing_userid_returns_none(self):
        """缺 X-Operator-Userid -> None(未认证)。"""
        self.assertIsNone(resolve_operator({}))

    def test_blank_userid_treated_as_missing(self):
        """user_id 纯空白 -> strip 后为空 -> None。"""
        self.assertIsNone(resolve_operator({"X-Operator-Userid": "   "}))


# ---- RBAC:WorkflowRoleChecker.is_allowed(mock get_allowed_roles)----
class IsAllowedTest(unittest.IsolatedAsyncioTestCase):
    """WorkflowRoleChecker.is_allowed:层 A 角色准入(空集放行 / 命中放行 / 不命中拒绝)。"""

    def _op(self, roles):
        return OperatorContext(user_id="u", roles=list(roles))

    async def test_no_config_allows_everyone(self):
        """无配置(空集)= 全员可用。"""
        checker = WorkflowRoleChecker()
        with patch.object(checker, "get_allowed_roles", AsyncMock(return_value=set())):
            self.assertTrue(await checker.is_allowed("test_workflow", self._op(["employee"])))
            self.assertTrue(await checker.is_allowed("test_workflow", self._op([])))

    async def test_role_match_allows(self):
        """operator 角色命中配置的允许集合 -> 放行。"""
        checker = WorkflowRoleChecker()
        with patch.object(checker, "get_allowed_roles", AsyncMock(return_value={"hr_admin"})):
            self.assertTrue(await checker.is_allowed("test_workflow", self._op(["hr_admin"])))
            self.assertTrue(await checker.is_allowed("test_workflow", self._op(["hr_admin", "employee"])))

    async def test_role_mismatch_denies(self):
        """operator 角色不在允许集合 -> 拒绝。"""
        checker = WorkflowRoleChecker()
        with patch.object(checker, "get_allowed_roles", AsyncMock(return_value={"hr_admin"})):
            self.assertFalse(await checker.is_allowed("test_workflow", self._op(["employee"])))
            self.assertFalse(await checker.is_allowed("test_workflow", self._op([])))


if __name__ == "__main__":
    unittest.main()
