from dataclasses import dataclass

from conf.config import settings


@dataclass
class EnvironmentInfo:
    """当前运行环境."""

    is_git_repo: bool
    git_repo_url: str | None = None
    git_branch: str | None = None
    # intent / reply 阶段各自的远程提示词路径(留空时按阶段默认文件名)
    git_intent_relative_path: str | None = None
    git_reply_relative_path: str | None = None

    @classmethod
    def get_environment(cls) -> "EnvironmentInfo":  # 所有返回类型注解变成延迟求值的字符串,类体内引用自身不再报错
        """从全局配置(.env)构造运行环境信息,供启动时一次性读取。"""
        return cls(
            is_git_repo=settings.is_git_repo,
            git_repo_url=settings.git_repo_url,
            git_branch=settings.git_branch,
            git_intent_relative_path=settings.git_intent_relative_path,
            git_reply_relative_path=settings.git_reply_relative_path,
        )
