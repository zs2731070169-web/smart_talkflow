#!/usr/bin/env python3
"""PostToolUse hook:对 Edit/Write/MultiEdit 改动的 ``.py`` 文件自动 ruff 规范。

闭环设计(「生成即合规」):
1. ``ruff format`` —— 直接按 pyproject.toml 的 [tool.ruff] 格式化(双引号/120 列等)。
2. ``ruff check --fix`` —— 自动修可修的 lint 问题(E/W/F/I/B/C4/UP/SIM)。
3. 仍有剩余(无法 auto-fix 的)问题 → exit 2,把 ruff 输出经 stderr 反馈给 Claude,
   驱动 Claude 继续修复,直到干净;全部通过则 exit 0 静默。

约定:用 ``uv run ruff`` 确保与项目锁定版本(uv.lock)一致,而非全局 ruff。
非 .py 文件、非目标工具、stdin 异常一律 exit 0 不阻断。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# 触发本 hook 的工具(只拦截写文件类工具)
_TRIGGER_TOOLS = {"Edit", "Write", "MultiEdit"}


def main() -> int:
    # PostToolUse hook 经 stdin 收到一段 JSON(tool_name / tool_input / tool_response ...)
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # stdin 异常不阻断主流程

    if payload.get("tool_name") not in _TRIGGER_TOOLS:
        return 0

    file_path = (payload.get("tool_input") or {}).get("file_path", "")
    if not file_path or not file_path.endswith(".py"):
        return 0

    path = Path(file_path)
    if not path.is_file():
        return 0  # 文件已被删除等,跳过

    problems: list[str] = []

    # 1. 格式化(直接改盘)
    fmt = subprocess.run(
        ["uv", "run", "ruff", "format", str(path)],
        capture_output=True,
        text=True,
    )
    if fmt.returncode != 0:
        problems.append(f"[ruff format] 退出码 {fmt.returncode}\n{(fmt.stderr or fmt.stdout).strip()}")

    # 2. lint 自动修复
    chk = subprocess.run(
        ["uv", "run", "ruff", "check", "--fix", str(path)],
        capture_output=True,
        text=True,
    )
    if chk.returncode != 0:
        problems.append(f"[ruff check] 仍有未自动修复的问题:\n{(chk.stdout or chk.stderr).strip()}")

    if problems:
        # exit 2:stderr 作为反馈注入给 Claude,驱动其修复剩余问题(自动强制闭环)
        sys.stderr.write(
            "ruff 已自动 format + 修可修项,但仍存在以下问题,请修复后再继续:\n\n"
            + "\n\n".join(problems)
            + "\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())