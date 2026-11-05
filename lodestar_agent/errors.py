"""流程级异常。

两类的区别是「要不要终止整次运行」：

- `AgentFailure`：可用性失败（重试耗尽 / CLI 报错），调用方应终止流程并报失败节点。
- `ModelAborted`：主动断开连接（到等待上限），不是失败，调用方该收尾而不是重试。
"""

from __future__ import annotations


class AgentFailure(RuntimeError):
    """某个 agent 的请求在重试耗尽后仍然失败，用于终止流程并给出清晰提示。"""

    def __init__(self, node: str, reason: str):
        self.node = node
        self.reason = reason
        super().__init__(f"{node}: {reason}")


class ModelAborted(RuntimeError):
    """到点还没生成完，主动断开连接。

    不是失败，所以不走 AgentFailure：调用方拿到它就该收尾，
    而不是当成 429/超时去重试。
    """
