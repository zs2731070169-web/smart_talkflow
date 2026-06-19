"""api.deps 认证解析 + RBAC 单元测试。

覆盖:

- :func:`api.deps.resolve_operator`:从 ``X-Operator-*`` 头解析 operator;
  ``roles`` 按逗号拆分;缺 ``X-Operator-Userid`` 返回 ``None``。
- :meth:`orchestrator.base.BaseWorkflow.is_allowed`:空集放行、命中 / 不命中。

SSO(JWT 验签)路径由 ``tests.test_sso`` 覆盖。

运行(项目根)::

    PYTHONPATH=src python -m unittest tests.test_deps
"""
import unittest

from api.deps import resolve_operator
from orchestrator.base import BaseWorkflow
from runtime.context import OperatorContext


class ResolveOperatorTest(unittest.TestCase):
    """resolve_operator:开发态请求头解析。"""

    def test_full_headers_parse(self):
        """全头齐备:user_id / tenant / roles 正确解析,roles 按逗号拆分。"""
        op = resolve_operator({
            "X-Operator-Userid": "9527",
            "X-Operator-Tenant": "1",
            "X-Operator-Roles": "hr_admin, employee",
        })
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


# ---- RBAC:用最小可实例化工作流驱动 is_allowed ----
class _Workflow(BaseWorkflow):
    """最小可实例化工作流(仅用于驱动 is_allowed,不执行真实逻辑)。"""

    description = ""
    input_model = None  # type: ignore[assignment]

    def business_key(self, arguments):
        return None

    async def execute(self, arguments, context):
        ...


class _RoleGatedWorkflow(_Workflow):
    """声明 allowed_roles 的工作流(模拟入职只放行 hr_admin)。"""

    allowed_roles = {"hr_admin"}


class IsAllowedTest(unittest.TestCase):
    """BaseWorkflow.is_allowed:流程级 RBAC。"""

    def _op(self, roles):
        return OperatorContext(user_id="u", roles=list(roles))

    def test_empty_roles_allows_everyone(self):
        """未声明 allowed_roles(空集)= 全员可用。"""
        wf = _Workflow()
        self.assertTrue(wf.is_allowed(self._op(["employee"])))
        self.assertTrue(wf.is_allowed(self._op([])))

    def test_role_match_allows(self):
        """operator 角色命中 allowed_roles -> 放行。"""
        wf = _RoleGatedWorkflow()
        self.assertTrue(wf.is_allowed(self._op(["hr_admin"])))
        self.assertTrue(wf.is_allowed(self._op(["hr_admin", "employee"])))

    def test_role_mismatch_denies(self):
        """operator 角色不命中 -> 拒绝。"""
        wf = _RoleGatedWorkflow()
        self.assertFalse(wf.is_allowed(self._op(["employee"])))
        self.assertFalse(wf.is_allowed(self._op([])))


if __name__ == "__main__":
    unittest.main()
