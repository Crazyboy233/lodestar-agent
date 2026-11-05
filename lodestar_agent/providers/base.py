"""节点 ⇄ 模型来源 之间的契约层。

整条链路里其实只有两类「用模型」的方式：

  chat   纯文本：给一段 prompt，要一段文本。plan / 动态决策 / 汇总都用它。
  agent  会干活：给一段 prompt，它自己在工作目录里读写文件、跑命令。worker / 验收用它。

一个「来源（provider）」= 一条能提供模型的通道，可以是 HTTP 接口（OpenAI 兼容），
也可以是本机 CLI（codebuddy / codex / trae …）。来源声明自己支持哪几类能力，
节点只声明自己要哪一类 —— 两边互不认识对方的具体实现，加一个新来源不用改节点。

这个文件只放契约（协议 + 数据结构），具体来源在 `api.py` / `cli/` 里实现，
注册表在 `registry.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

# ── 能力 ────────────────────────────────────────────────────
# 一个来源可以只支持其中一类：比如纯 HTTP 模型天然支持 chat，
# 而某些 CLI 只能接 prompt 吐出文本（没有工具权限），那就只有 chat。
CHAT = "chat"
AGENT = "agent"

CAPABILITY_TEXT = {
    CHAT: "纯文本（拆解 / 决策 / 汇总）",
    AGENT: "会干活（读写文件 / 跑命令）",
}


class ProviderError(RuntimeError):
    """来源不可用：未安装、未登录、缺 key、能力不匹配……都归到这里。"""


@dataclass
class RunResult:
    """一次 agent 调用的结果。CLI 与 API 两条路径共用，上层不必分情况处理。"""

    ok: bool
    output: str
    duration: float
    session_id: str | None = None
    error: str | None = None
    aborted: bool = False    # 用户请求退出导致被主动终止，不是失败
    timed_out: bool = False  # 撞了单次调用墙钟上限（产出可能不完整）
    turns: int = 0           # 这次调用内部走过了几轮（CLI 的 num_turns / API 的工具循环轮数）


class ChatBackend(Protocol):
    """纯文本角色：一次 prompt → 一段文本。

    `interrupted()` 为真时要尽快停掉，并抛 `errors.ModelAborted` —— 语义是
    「停掉模型」而不是「停掉脚本」。
    """

    label: str

    def complete(self, prompt: str, interrupted: Callable[[], bool] | None = None) -> str:
        ...


class AgentRuntime(Protocol):
    """会干活的角色：给任务，它自己动手。cwd 是它的落地目录。"""

    label: str

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        extra_dirs: Sequence[Path] = (),
        resume_session: str | None = None,
    ) -> RunResult:
        ...
