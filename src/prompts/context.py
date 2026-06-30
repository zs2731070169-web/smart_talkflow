"""运行时系统提示词拼装入口"""

from __future__ import annotations

from conf.config import ROOT_PATH
from prompts.environment import EnvironmentInfo
from prompts.system_prompt import PromptType, build_system_prompt

# 本地自定义提示词目录(与远程仓库克隆目录同一处)
_CUSTOM_PROMPT_DIR = ROOT_PATH / ".prompt/custom"


async def build_runtime_system_prompt(
    prompt_type: PromptType,
    *,
    env: EnvironmentInfo,
) -> str:
    """构建运行时系统提示词

    Args:
        prompt_type: 提示词阶段,见 :class:`PromptType`。
        env: 运行环境信息。

    Returns:
        系统提示词文本
    """
    # 加载本地的自定义提示词
    custom_prompt = None
    path = _CUSTOM_PROMPT_DIR / f"{prompt_type}_system_prompt.md"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        if text.strip():
            custom_prompt = text

    return await build_system_prompt(prompt_type, env, custom_prompt=custom_prompt)
