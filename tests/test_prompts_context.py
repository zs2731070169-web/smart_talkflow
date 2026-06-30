"""prompts.context 运行时系统提示词入口测试。

覆盖 :func:`build_runtime_system_prompt`:

- custom 完全由本地 ``.prompt/custom/`` 加载
- intent / reply 分别读取各自的阶段专属文件
- 本地文件缺失或仅空白 → 自动走内置默认模板(workflow 清单不在此追加,已由 tools 承担)

运行::

    PYTHONPATH=src python -m unittest tests.test_prompts_context
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prompts import (
    EnvironmentInfo,
    PromptType,
    build_runtime_system_prompt,
    get_base_system_prompt,
)


class ContextBuildTest(unittest.IsolatedAsyncioTestCase):
    """build_runtime_system_prompt 行为。"""

    async def test_loads_intent_and_reply_respectively(self):
        """intent / reply 分别读取各自的阶段专属文件。"""
        env = EnvironmentInfo(is_git_repo=False)
        with tempfile.TemporaryDirectory() as tmp:
            custom_dir = Path(tmp)
            (custom_dir / "intent_system_prompt.md").write_text("INTENT 文件", encoding="utf-8")
            (custom_dir / "reply_system_prompt.md").write_text("REPLY 文件", encoding="utf-8")
            with patch("prompts.context._CUSTOM_PROMPT_DIR", custom_dir):
                intent = await build_runtime_system_prompt(PromptType.INTENT, env=env)
                reply = await build_runtime_system_prompt(PromptType.REPLY, env=env)
        self.assertEqual(intent, "INTENT 文件")
        self.assertEqual(reply, "REPLY 文件")
        print("【intent/reply 各读各的阶段文件】 ✓")

    async def test_local_file_loaded_when_exists(self):
        """本地存在阶段专属文件(非空)→ 加载为 custom_prompt。"""
        env = EnvironmentInfo(is_git_repo=False)
        with tempfile.TemporaryDirectory() as tmp:
            custom_dir = Path(tmp)
            (custom_dir / "intent_system_prompt.md").write_text("模拟本地 intent 提示词", encoding="utf-8")
            with patch("prompts.context._CUSTOM_PROMPT_DIR", custom_dir):
                prompt = await build_runtime_system_prompt(PromptType.INTENT, env=env)
        self.assertEqual(prompt, "模拟本地 intent 提示词")
        print("【本地文件加载为 custom】 ✓")

    async def test_blank_local_file_falls_back_to_default(self):
        """本地文件仅含空白 → 视同缺失 → 走内置默认模板。"""
        env = EnvironmentInfo(is_git_repo=False)
        with tempfile.TemporaryDirectory() as tmp:
            custom_dir = Path(tmp)
            (custom_dir / "reply_system_prompt.md").write_text("   \n  ", encoding="utf-8")
            with patch("prompts.context._CUSTOM_PROMPT_DIR", custom_dir):
                prompt = await build_runtime_system_prompt(PromptType.REPLY, env=env)
        self.assertEqual(prompt, get_base_system_prompt(PromptType.REPLY))
        print("【空白本地文件降级默认】 ✓")

    async def test_missing_local_file_falls_back_to_default(self):
        """本地目录无对应文件 → 走内置默认模板。"""
        env = EnvironmentInfo(is_git_repo=False)
        with tempfile.TemporaryDirectory() as tmp, patch("prompts.context._CUSTOM_PROMPT_DIR", Path(tmp)):
            prompt = await build_runtime_system_prompt(PromptType.REPLY, env=env)
        self.assertEqual(prompt, get_base_system_prompt(PromptType.REPLY))
        print("【本地文件缺失降级默认】 ✓")


if __name__ == "__main__":
    unittest.main()
