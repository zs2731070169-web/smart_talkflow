from dataclasses import dataclass


@dataclass
class EnvironmentInfo:
    """当前运行环境."""

    os_name: str
    os_version: str
    platform_machine: str
    shell: str
    cwd: str
    date: str
    python_version: str
    python_executable: str
    virtual_env: str | None
    is_git_repo: bool
    git_repo_url: str | None = None
    git_branch: str | None = None
    git_relative_path : str | None = None