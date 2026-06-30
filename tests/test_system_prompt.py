"""prompts.system_prompt 系统提示词构建测试。

覆盖:
- 按 ``prompt_type``(intent / reply)分流内置默认模板
- 三级降级优先级:远程仓库 > 自定义提示词 > 默认提示词
- 远程仓库内路径按 ``prompt_type`` 分流(``_resolve_remote_path``)

异步测试使用 ``IsolatedAsyncioTestCase``。

运行::

    PYTHONPATH=src python -m unittest tests.test_system_prompt
"""

import unittest
from unittest.mock import AsyncMock, patch

from prompts import EnvironmentInfo, PromptType, build_system_prompt, get_base_system_prompt
from prompts.system_prompt import _resolve_remote_path


class SystemPromptTest(unittest.IsolatedAsyncioTestCase):
    """系统提示词构建:默认模板分流 + 三级降级 + 远程路径分流。"""

    async def test_intent_default_prompt(self):
        """intent 阶段:未启用远程、未传自定义 → 兜底返回 intent 内置默认值。"""
        env = EnvironmentInfo(is_git_repo=False)
        prompt = await build_system_prompt(PromptType.INTENT, env)
        self.assertEqual(prompt, get_base_system_prompt(PromptType.INTENT))
        self.assertIn("意图解析", prompt)
        print("【intent 默认提示词】 ✓  ->", repr(prompt[:30]) + " ...")

    async def test_reply_default_prompt(self):
        """reply 阶段:兜底返回 reply 内置默认值(验证按 prompt_type 分流,不再固定 intent)。"""
        env = EnvironmentInfo(is_git_repo=False)
        prompt = await build_system_prompt(PromptType.REPLY, env)
        self.assertEqual(prompt, get_base_system_prompt(PromptType.REPLY))
        self.assertIn("结果回复生成器", prompt)
        print("【reply 默认提示词】 ✓  ->", repr(prompt[:30]) + " ...")

    async def test_custom_prompt_overrides_default(self):
        """调用方传入 custom_prompt,优先级高于默认。"""
        local_prompt = "我是本地自定义系统提示词,由调用方传入。"
        env = EnvironmentInfo(is_git_repo=False)
        prompt = await build_system_prompt(PromptType.INTENT, env, custom_prompt=local_prompt)
        self.assertEqual(prompt, local_prompt)
        print("【本地提示词覆盖默认】 ✓  ->", repr(prompt))

    async def test_resolve_remote_path_by_kind(self):
        """远程路径按 prompt_type 分流:专用路径优先,留空回退阶段默认文件名。"""
        env_specific = EnvironmentInfo(
            is_git_repo=True,
            git_repo_url="https://example.com/prompts.git",
            git_intent_relative_path="path/intent.md",
            git_reply_relative_path="path/reply.md",
        )
        self.assertEqual(_resolve_remote_path(PromptType.INTENT, env_specific), "path/intent.md")
        self.assertEqual(_resolve_remote_path(PromptType.REPLY, env_specific), "path/reply.md")

        env_blank = EnvironmentInfo(
            is_git_repo=True,
            git_repo_url="https://example.com/prompts.git",
        )
        self.assertEqual(_resolve_remote_path(PromptType.INTENT, env_blank), "intent_system_prompt.md")
        self.assertEqual(_resolve_remote_path(PromptType.REPLY, env_blank), "reply_system_prompt.md")
        print("【远程路径按 prompt_type 分流】 ✓")

    async def test_remote_fetch_uses_resolved_path(self):
        """启用远程时,_fetch_remote_prompt 收到按 prompt_type 解析出的路径。"""
        env = EnvironmentInfo(
            is_git_repo=True,
            git_repo_url="https://example.com/prompts.git",
            git_intent_relative_path="remote/intent.md",
        )
        with patch(
            "prompts.system_prompt._fetch_remote_prompt", new=AsyncMock(return_value="远程 intent 内容")
        ) as mocked:
            prompt = await build_system_prompt(PromptType.INTENT, env)
        mocked.assert_awaited_once_with("https://example.com/prompts.git", "remote/intent.md", None)
        self.assertEqual(prompt, "远程 intent 内容")
        print("【远程拉取按 prompt_type 取路径】 ✓")

    async def test_remote_failure_falls_back_to_custom(self):
        """远程拉取失败(返回 None)→ 自动降级到 custom_prompt。"""
        env = EnvironmentInfo(is_git_repo=True, git_repo_url="https://example.com/prompts.git")
        with patch("prompts.system_prompt._fetch_remote_prompt", new=AsyncMock(return_value=None)):
            prompt = await build_system_prompt(PromptType.INTENT, env, custom_prompt="降级文案")
        self.assertEqual(prompt, "降级文案")
        print("【远程失败降级 custom】 ✓")


if __name__ == "__main__":
    unittest.main()
