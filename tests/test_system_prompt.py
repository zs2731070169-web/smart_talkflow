"""engine.prompts.system_prompt 系统提示词构建测试。

迁移自 ``src/engine/prompts/system_prompt.py`` 的 ``__main__`` 段,覆盖
三级降级优先级:远程仓库 > 自定义提示词 > 默认提示词。异步测试使用
``IsolatedAsyncioTestCase``。

运行::

    python -m unittest tests.test_system_prompt
"""
import unittest

from engine.prompts.envirement import EnvironmentInfo
from engine.prompts.system_prompt import build_system_prompt, get_base_system_prompt


class SystemPromptTest(unittest.IsolatedAsyncioTestCase):
    """系统提示词三级降级。"""

    async def test_default_prompt(self):
        """用例 1:未启用远程、未传自定义 → 兜底返回内置默认值。"""
        env = EnvironmentInfo(is_git_repo=False)
        prompt = await build_system_prompt(env)
        self.assertEqual(prompt, get_base_system_prompt())
        print("【默认提示词】 ✓  ->", repr(prompt[:40]) + " ...")

    async def test_custom_prompt(self):
        """用例 2:调用方传入 custom_prompt,优先级高于默认。"""
        local_prompt = "我是本地自定义系统提示词,由调用方传入。"
        env = EnvironmentInfo(is_git_repo=False)
        prompt = await build_system_prompt(env, custom_prompt=local_prompt)
        self.assertEqual(prompt, local_prompt)
        print("【本地提示词】 ✓  ->", repr(prompt))

    async def test_remote_prompt(self):
        """用例 3:读取 .env 配置,优先级最高,失败自动降级。"""
        env = EnvironmentInfo.get_environment()
        prompt = await build_system_prompt(env, custom_prompt="本地降级备用提示词")
        source = "远程仓库" if env.is_git_repo else "未启用远程仓库,已降级"
        print(f"【远程仓库提示词】({source}) ->", repr(prompt[:40]) + " ...")


if __name__ == "__main__":
    unittest.main()
