"""prompts 包:系统提示词加载与运行时拼装。

公共出口:

- :class:`PromptType`:提示词阶段枚举(intent / reply)
- :func:`build_runtime_system_prompt`:运行时主入口(context 层,含本地自定义提示词加载)
- :func:`build_system_prompt` / :func:`get_base_system_prompt`:来源层(system_prompt)
- :class:`EnvironmentInfo`:运行环境信息
"""

from prompts.context import build_runtime_system_prompt
from prompts.environment import EnvironmentInfo
from prompts.system_prompt import (
    PromptType,
    build_system_prompt,
    get_base_system_prompt,
)

__all__ = [
    "PromptType",
    "EnvironmentInfo",
    "build_runtime_system_prompt",
    "build_system_prompt",
    "get_base_system_prompt",
]
