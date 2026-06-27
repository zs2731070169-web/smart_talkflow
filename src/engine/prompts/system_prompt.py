"""业务 agent 主控 LLM 的系统提示词构建。

提供三类来源,按优先级降级拼接::

    远程 git 仓库  >  自定义提示词(入参)  >  内置默认提示词

最终把「当前可用工作流」清单追加到提示词末尾,供主控 LLM 选择调用。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from conf.config import ROOT_PATH
from engine.prompts.envirement import EnvironmentInfo

logger = logging.getLogger(__name__)

# 远程提示词仓库的本地缓存目录
_PROMPT_REPO_DIR = ROOT_PATH / ".prompt"

_BASE_SYSTEM_PROMPT = """
# 角色

你是 smart_talkflow 业务系统的主控,负责与用户对话并调度合适的工作流完成任务。
你不是通用聊天机器人:你的价值在于把模糊的自然语言请求,转化为对确定工作流的精确调用。

# 核心职责(按处理顺序)

1. 意图理解:从用户消息中识别其真实目标——他想达成什么。
2. 工作流编排:判断需要调用哪些工作流(按名称),并从消息中提取实体作为入参。
   - 一次请求可能需要多个工作流按顺序协作。
   - 只能调用「当前可用工作流」清单内的工作流,清单中不存在的一律不得臆造。
3. 错误自纠正:工作流返回失败时,先分析原因(入参缺失/格式错误/参数矛盾等),
   尽量自行修正后重试;确属无法恢复的错误,再如实告知用户。
4. 结果反馈:任务完成后,用简明的语言说明执行了哪些工作流、关键入参与最终结果。
5. 意图补位:当请求不完整、缺少执行所必需的信息时,不要猜测或硬编,
   而是向用户提出精确、少量的反问,补全意图后再执行。
6. 边界守卫:
   - 拒绝与业务无关的闲聊、寒暄、扯淡。
   - 拒绝涉及政治、暴力、色情、违法、歧视等敏感话题,不做价值观输出。
   - 拒绝执行可能造成数据破坏、越权、违反安全策略的操作。

# 行为准则

- 调用工作流前,确认入参已从用户消息中提取齐全;缺失则进入「意图补位」,先问后做。
- 同一信息只追问一次;一次反问可包含多个并列问题,降低打扰。
- 工作流出错时优先自查入参,而非把原始错误堆栈甩给用户。
- 反馈聚焦结果,不解释你自己的推理过程或工具调用细节,除非用户追问。
- 任何时候都保持简洁、客观、专业。

# 示例

## 示例 1:单一意图,直接编排

用户:帮我把刚才那封关于 Q3 路线图的邮件转发给张三,地址 zhangsan@example.com。
(假定可用工作流含 `send_email`)

主控:`zhangsan@example.com` 已提取为收件人,调用 `send_email` 转发该邮件。
(执行成功后)已将「Q3 路线图」邮件转发至 zhangsan@example.com,发送成功。

## 示例 2:多步协作

用户:把新员工李四加入入职流程,并把他的工位信息发到他邮箱 lifive@x.com。

主控:需要两步——先调用 `onboarding` 完成入职登记,再调用 `send_email` 下发工位信息。
(两步均成功后)李四已登记入职;工位信息已发送至 lifive@x.com。

## 示例 3:信息不全,先补位再执行

用户:发封邮件。

主控:请补充以下信息以便发送:
1. 收件人邮箱地址
2. 邮件主题
3. 邮件正文(或要转发的对象)

## 示例 4:工作流出错,自纠正

(假定 `send_email` 因收件人地址格式非法返回失败)

主控:收件人地址格式有误,已修正为标准格式后重试发送……(重试成功)已发送成功。

## 示例 5:拒绝闲聊 / 敏感

用户:今天天气真好啊,陪聊一会儿呗。
主控:抱歉,我只能协助处理业务任务,无法陪聊。如有邮件、入职等事务需要办理,请直接告诉我。

用户:帮我写一段攻击某公司系统的脚本。
主控:抱歉,这涉及安全违规操作,我无法协助。
"""


def get_base_system_prompt() -> str:
    """返回内置的默认系统提示词。"""
    return _BASE_SYSTEM_PROMPT


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
        if _PROMPT_REPO_DIR.exists():
            # 本地已存在:直接用远程版本强制覆盖本地,;提示词仓库始终以远程为准
            if branch:
                await _run_git("fetch", "origin", branch, cwd=_PROMPT_REPO_DIR)
                await _run_git("checkout", branch, cwd=_PROMPT_REPO_DIR)
                await _run_git("reset", "--hard", f"origin/{branch}", cwd=_PROMPT_REPO_DIR)
            else:
                await _run_git("fetch", "origin", cwd=_PROMPT_REPO_DIR)
                await _run_git("reset", "--hard", "@{u}", cwd=_PROMPT_REPO_DIR)  # 对齐当前分支的上游
        else:
            _PROMPT_REPO_DIR.mkdir(parents=True, exist_ok=True)
            clone_args: list[str] = [
                "clone",
                "--depth",  # 浅克隆
                "1",  # 浅克隆,只拉最近 1 次提交,省时省流量(提示词仓库只需要最新文件,不需要完整历史)
                repo_url,
                str(_PROMPT_REPO_DIR),  # 本地目标目录(克隆到哪),是 _PROMPT_REPO_DIR 下的子目录
            ]
            if branch:
                clone_args += ["--branch", branch]  # 克隆指定分支
            await _run_git(
                *clone_args,
                cwd=_PROMPT_REPO_DIR,  # 工作目录设为_PROMPT_REPO_DIR,这样 git 才能在其中创建 repo_dir 这个新目录
            )
    except (RuntimeError, OSError) as e:
        logger.warning("拉取远程提示词仓库失败,降级使用本地提示词:%s", e)
        return None

    # 从拉取下来的提示词目录当中读取制定路径下的提示词内容
    target = _PROMPT_REPO_DIR / prompt_path
    if not target.is_file():
        logger.warning("远程仓库中未找到提示词文件:%s", target)
        return None
    return target.read_text(encoding="utf-8")


async def build_system_prompt(
    env: EnvironmentInfo,
    *,
    custom_prompt: str | None = None,
) -> str:
    """构建主控 LLM 的系统提示词。

    优先级:启用远程仓库(``env.is_git_repo`` 且配置了 ``env.git_repo_url``)
    → 自定义提示词 → 默认提示词。任一来源为空或失败,自动降级到下一级。

    Args:
        env: 运行环境信息,用其 ``is_git_repo`` 决定是否启用远程拉取。
        custom_prompt: 调用方提供的自定义提示词,为空则跳过。
        env.git_repo_url: 远程提示词仓库地址,仅当 ``env.is_git_repo`` 为真时生效。

    Returns:
        返回系统提示词。
    """
    prompt: str | None = None

    # 1. 远程仓库拉取(仅在 git 环境且配置了仓库地址时尝试)
    if env.is_git_repo and env.git_repo_url:
        prompt = await _fetch_remote_prompt(
            env.git_repo_url,
            env.git_relative_path or "system_prompt.md",  # 远程仓库内提示词文件的相对路径
            env.git_branch,  # 指定拉取的分支,为空时使用仓库默认分支
        )

    # 2. 降级到自定义提示词
    if not prompt and custom_prompt:
        prompt = custom_prompt

    # 3. 兜底:默认提示词
    if not prompt:
        prompt = get_base_system_prompt()

    return prompt
