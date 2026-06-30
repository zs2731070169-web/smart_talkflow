"""按调用阶段提供系统提示词。

提供三类来源,按优先级降级拼接::

    远程 git 仓库  >  自定义提示词(入参)  >  内置默认提示词

内置默认提示词按阶段拆成两份:
- intent 阶段:负责意图理解 / workflow 选择 / 参数补位
- reply 阶段:负责基于 tool_result 生成最终回复
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from pathlib import Path

from conf.config import ROOT_PATH
from prompts.environment import EnvironmentInfo

logger = logging.getLogger(__name__)

# 远程提示词仓库的本地缓存目录
_REMOTE_PROMPT_DIR = ROOT_PATH / ".prompt/remote"


class PromptType(StrEnum):
    """提示词类型:``intent`` 意图理解 / ``reply`` 回复生成。"""

    INTENT = "intent"
    REPLY = "reply"


_BASE_INTENT_SYSTEM_PROMPT = """
# 角色

你是 smart_talkflow 的意图解析与工作流选择器。
你的职责不是闲聊,而是把用户自然语言请求转换为对 workflow 的准确调用。

# 目标

1. 识别用户真正要完成的业务任务。
2. 在当前提供的 workflow 中选择最合适的调用目标;确有必要时,才返回多个调用。
3. 从用户消息中提取 workflow 所需入参。
4. 当执行所需信息缺失时,先向用户提出精确、简短的补充问题。

# 约束

- 只能使用当前提供的 workflow,严谨臆造不存在的 workflow。
- 禁止猜测用户未提供的关键参数; 缺失就问用户补齐。
- 不要把面向用户的最终结果说明写得过长; 最终结果反馈由回复生成阶段负责, 不由你负责。
- 如果用户需求超出当前 workflow 能力范围,直接说明当前暂不支持,禁止硬凑成调用。

# 判断规则

- 参数齐全:直接选择 workflow 并给出结构化参数。
- 参数缺失:一次性追问所有关键缺口,降低打扰。
- 语义模糊:先向用户澄清,再调用。
- 非业务闲聊或越界请求:直接拒绝或说明当前仅支持业务流程办理。

# 示例

下列示例刻意不绑定任何具体业务,只演示判断模式。

## 示例 1:信息齐全,直接调用

用户:请帮我办理某项业务,所需参数为 ……(已提供全部必填信息)。
助手:识别为可由当前 workflow 处理的请求,且参数齐全,直接发起对应 workflow 调用。

## 示例 2:信息不全,先补位

用户:帮我办一下那个业务。
助手:请补充以下必要信息,我再为你发起调用:1. …… 2. ……。

## 示例 3:超出当前能力范围

用户:帮我做一件当前没有对应 workflow 的事。
助手:当前可用 workflow 中没有能处理该请求的项,暂不支持;不要臆造不存在的调用。
"""

_BASE_REPLY_SYSTEM_PROMPT = """
# 角色

你是 smart_talkflow 的结果回复生成器。
你的职责是根据已执行 workflow 返回的 tool_result,向用户生成简洁、可信、可执行的最终回复。

# 目标

1. 先说明最终结果:成功、失败 (含补偿回滚)、部分成功。
2. 再补充用户真正关心的必要细节,例如时间、对象、编号、原因或下一步。
3. 保持语言自然、专业、简洁。

# 约束

- 只能依据当前对话中的 tool_result 作答,不得虚构未执行的步骤或结果。
- 不要重新选择 workflow,也不要重新规划调用。
- 不要暴露内部实现细节、异常堆栈、数据库字段或框架术语。
- 若执行失败,明确失败原因,并告诉用户下一步需要补什么或如何重试。
- 若结果显示“历史结果复用”,要明确说明本次未重新执行。
- 若结果显示“已补偿回滚”,要明确说明本次操作未最终生效。

# 表达要求

- 优先说结论,再补必要细节。
- 能一句说清就不要展开成长段。
- 不重复粘贴原始 tool_result,而是把它翻译成用户能理解的话。

# 当前系统现状

- reply 阶段接收的是 workflow 执行后的 tool_result。
- 你的工作是解释结果,不是再次做意图判断或工具选择。

# 示例

下列示例不绑定任何具体业务,只演示如何把 tool_result 翻译成用户能理解的语言。

## 示例 1:执行成功

tool_result: [workflow_x] success ...
助手:已为你办理完成,结果如下:……。如需进一步调整,请告诉我。

## 示例 2:执行失败

tool_result: [workflow_x] failed ...
助手:本次办理失败,原因是 ……。请补充或修正 …… 后,我再为你重试。

## 示例 3:已补偿回滚

tool_result: [workflow_x] failed ... (已补偿回滚)
助手:本次办理未最终生效,任务已完成回退。请确认相关信息后重新提交。
"""


def get_base_system_prompt(prompt_type: PromptType = PromptType.INTENT) -> str:
    """返回对应阶段的内置默认系统提示词。

    Args:
        prompt_type: 提示词阶段,见 :class:`PromptType`。
    """
    if prompt_type == PromptType.REPLY:
        return _BASE_REPLY_SYSTEM_PROMPT
    if prompt_type == PromptType.INTENT:
        return _BASE_INTENT_SYSTEM_PROMPT
    raise ValueError(f"不支持的提示词类型:{prompt_type!r}")


async def _run_git(*args: str, cwd: Path) -> None:
    """执行一次 git 命令,失败抛 RuntimeError。"""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败:{stderr.decode(errors='ignore').strip()}")


async def _fetch_remote_prompt(repo_url: str, prompt_path: str, branch: str | None = None) -> str | None:
    """从远程 git 仓库拉取系统提示词文件。

    采用 clone-or-reset:本地缓存不存在则浅克隆,已存在则用远程版本强制覆盖本地(直接覆盖,不合并)。
    ``branch`` 指定要拉取的分支,为空时使用仓库默认分支。
    任何 git 或读取失败都返回 None,交由上层降级到自定义/默认提示词。
    """
    try:
        if _REMOTE_PROMPT_DIR.exists():
            # 本地已存在:直接用远程版本强制覆盖本地,;提示词仓库始终以远程为准
            if branch:
                await _run_git("fetch", "origin", branch, cwd=_REMOTE_PROMPT_DIR)
                await _run_git("checkout", branch, cwd=_REMOTE_PROMPT_DIR)
                await _run_git("reset", "--hard", f"origin/{branch}", cwd=_REMOTE_PROMPT_DIR)
            else:
                await _run_git("fetch", "origin", cwd=_REMOTE_PROMPT_DIR)
                await _run_git("reset", "--hard", "@{u}", cwd=_REMOTE_PROMPT_DIR)  # 对齐当前分支的上游
        else:
            _REMOTE_PROMPT_DIR.mkdir(parents=True, exist_ok=True)
            clone_args: list[str] = [
                "clone",
                "--depth",  # 浅克隆
                "1",  # 浅克隆,只拉最近 1 次提交,省时省流量(提示词仓库只需要最新文件,不需要完整历史)
                repo_url,
                str(_REMOTE_PROMPT_DIR),  # 本地目标目录(克隆到哪),是 _PROMPT_REPO_DIR 下的子目录
            ]
            if branch:
                clone_args += ["--branch", branch]  # 克隆指定分支
            await _run_git(
                *clone_args,
                cwd=_REMOTE_PROMPT_DIR,  # 工作目录设为_PROMPT_REPO_DIR,这样 git 才能在其中创建 repo_dir 这个新目录
            )
    except (RuntimeError, OSError) as e:
        logger.warning("拉取远程提示词仓库失败,降级使用本地提示词:%s", e)
        return None

    # 从拉取下来的提示词目录当中读取制定路径下的提示词内容
    target = _REMOTE_PROMPT_DIR / prompt_path
    if not target.is_file():
        logger.warning("远程仓库中未找到提示词文件:%s", target)
        return None
    return target.read_text(encoding="utf-8")


def _resolve_remote_path(prompt_type: PromptType, env: EnvironmentInfo) -> str:
    """按阶段解析远程仓库内的提示词文件相对路径。

    阶段专用路径(``git_intent_relative_path`` / ``git_reply_relative_path``)
    为空时回退到阶段默认文件名。

    Raises:
        ValueError: ``prompt_type`` 非 :class:`PromptType` 合法成员。
    """
    if prompt_type == PromptType.INTENT:
        return env.git_intent_relative_path or "intent_system_prompt.md"
    if prompt_type == PromptType.REPLY:
        return env.git_reply_relative_path or "reply_system_prompt.md"
    raise ValueError(f"不支持的提示词类型:{prompt_type!r}")


async def build_system_prompt(
    prompt_type: PromptType,
    env: EnvironmentInfo,
    *,
    custom_prompt: str | None = None,
) -> str:
    """构建基础系统提示词(仅负责来源选择,不做运行时拼装)。

    优先级:启用远程仓库(``env.is_git_repo`` 且配置了 ``env.git_repo_url``)
    → 自定义提示词 → 默认提示词。任一来源为空或失败,自动降级到下一级。

    Args:
        prompt_type: 提示词阶段,见 :class:`PromptType`,决定默认模板与远程路径分流。
        env: 运行环境信息,用其 ``is_git_repo`` 决定是否启用远程拉取。
        custom_prompt: 调用方提供的自定义提示词,为空则跳过。

    Returns:
        基础系统提示词文本。运行时拼装请见
        :func:`prompts.context.build_runtime_system_prompt`。
    """
    prompt: str | None = None

    # 1. 远程仓库拉取(仅在 git 环境且配置了仓库地址时尝试)
    if env.is_git_repo and env.git_repo_url:
        prompt = await _fetch_remote_prompt(
            env.git_repo_url,
            _resolve_remote_path(prompt_type, env),  # 按 prompt_type 分流远程仓库内路径
            env.git_branch,  # 指定拉取的分支,为空时使用仓库默认分支
        )

    # 2. 降级到自定义提示词
    if not prompt and custom_prompt:
        prompt = custom_prompt

    # 3. 兜底:默认提示词(按 prompt_type)
    if not prompt:
        prompt = get_base_system_prompt(prompt_type)

    return prompt
