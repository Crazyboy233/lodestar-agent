"""模型来源层：把「节点要一个模型」和「模型从哪来」解耦。

节点只认识两个接口（见 base.py）：
    ChatBackend   纯文本
    AgentRuntime  会干活
来源负责实现这两个接口。上层永远不写 `if cli == "codebuddy"` 这种判断，
所以加一个来源不会波及节点逻辑。

    节点配置 nodes.main/worker/verifier  ──source──▶  providers.<source>
                                                        │ kind: api / cli
                                                        ▼
                                              ChatBackend / AgentRuntime

对外只用这里导出的几个名字。
"""

from __future__ import annotations

from .base import AGENT, CAPABILITY_TEXT, CHAT, AgentRuntime, ChatBackend, ProviderError, RunResult
from .registry import (
    build_agent,
    build_chat,
    check,
    label,
    models_for,
    provider,
    resolve_workdir,
    sources,
)

__all__ = [
    "AGENT", "CHAT", "CAPABILITY_TEXT", "AgentRuntime", "ChatBackend", "ProviderError",
    "RunResult", "build_agent", "build_chat", "check", "label", "models_for", "provider",
    "resolve_workdir", "sources",
]
