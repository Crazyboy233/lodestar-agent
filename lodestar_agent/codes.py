"""退出码与「会话状态」的唯一映射。

以前这张表在 `session.py` 和 `history.py` 各写一份，改一处忘另一处就会让
「运行结果」和「历史记录」对同一次运行给出不同状态。
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0        # 全部通过
    FAILED = 1    # 有子任务未通过
    SETUP = 2     # 这次运行没起来（配置错 / 线程冲突 / checkpoint 不存在）
    FATAL = 3     # 模型请求失败，流程终止
    ABORTED = 130  # 边界退出 / Ctrl+C（可续跑）


_SESSION_STATE: dict[int, str] = {
    ExitCode.OK: "done",
    ExitCode.FAILED: "failed",
    ExitCode.SETUP: "error",
    ExitCode.FATAL: "failed",
    ExitCode.ABORTED: "aborted",
}


def session_state(code: int | None) -> str:
    """退出码 → 页面上的会话状态（idle / running / done / failed / aborted / error）。"""
    return _SESSION_STATE.get(code if code is not None else int(ExitCode.SETUP), "error")
