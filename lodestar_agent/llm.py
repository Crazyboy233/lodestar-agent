"""纯文本角色的调用层：错误归类 + 可中断的流式调用 + 重试。

这层要处理的是「连接」（能不能真的停下生成），和节点里「该怎么规划」是两件事，
混在一起会让节点逻辑更难读。

`LangchainChat` 是 `ChatBackend` 契约的一个实现：任何有 `.stream()` / `.invoke()`
的 langchain 风格对象都能塞进来（真实链路是 ChatOpenAI，测试里是假对象）。
CLI 来源不用这个适配器 —— 它有自己的一套（见 providers/cli）。
"""

from __future__ import annotations

import time

from langchain_core.messages import AIMessage

from .errors import AgentFailure, ModelAborted


def explain(exc: Exception | None) -> str:
    """把底层异常压成一句人读得懂的原因（保留原分类，不吞掉原文）。"""
    if exc is None:
        return "未知错误"
    msg = str(exc)
    low = msg.lower()
    if "429" in low or "rate limit" in low or "too many" in low:
        return "请求被限流（429）：稍后重试，或降低并发/频率"
    if "timeout" in low or "timed out" in low or "timedout" in low:
        return "请求超时：网络慢或模型服务过载"
    if "401" in low or "auth" in low or "api key" in low or "invalid" in low:
        return "认证失败：检查这个来源的 api_key / 登录态"
    if "connection" in low or "connect" in low or "network" in low or "refused" in low:
        return "网络连接问题：检查网络 / base_url 是否可达"
    if "500" in low or "502" in low or "503" in low or "server" in low:
        return "服务端 5xx：模型服务暂时不可用"
    return f"未分类错误：{msg[:200]}"


def invoke_interruptible(llm, prompt: str, interrupted=None):
    """调模型，必要时真的把请求断掉。

    为什么非要走 streaming：非流式请求一旦发出，服务端会把整段生成完再返回，
    本地再怎么放弃、进程再怎么杀，那段 token 已经烧掉了。只有流式 + 主动断开，
    服务端才会在客户端掉线时停止生成 —— 这才是「停掉模型」，而不是「停掉脚本」。
    """
    stream_fn = getattr(llm, "stream", None)
    if interrupted is None or stream_fn is None:
        return llm.invoke(prompt)

    parts: list[str] = []
    aborted = False
    gen = stream_fn(prompt)
    try:
        for chunk in gen:
            content = getattr(chunk, "content", "")
            if isinstance(content, str):
                parts.append(content)
            if interrupted():
                aborted = True
                break
    finally:
        # 关掉生成器 = 关掉底层 HTTP 连接，别让它在后台继续读流
        close = getattr(gen, "close", None)
        if callable(close):
            close()
    if aborted:
        raise ModelAborted("已到等待上限，断开模型连接")
    return AIMessage(content="".join(parts))


class LangchainChat:
    """把 langchain 风格的模型对象适配成 `ChatBackend`。"""

    def __init__(self, llm, label: str = ""):
        self.llm = llm
        self.label = label

    def complete(self, prompt: str, interrupted=None) -> str:
        msg = invoke_interruptible(self.llm, prompt, interrupted)
        return str(getattr(msg, "content", "") or "")


def as_chat(llm, label: str = ""):
    """已经是 ChatBackend 就原样返回，否则套一层适配（测试注入假模型时走这条）。"""
    if hasattr(llm, "complete"):
        return llm
    return LangchainChat(llm, label=label)


def call_chat(backend, node: str, prompt: str, max_retries: int, interrupted=None) -> str:
    """带重试的纯文本调用；重试耗尽则抛 AgentFailure。"""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return backend.complete(prompt, interrupted)
        except ModelAborted:
            raise  # 主动断开不重试，交给调用方收尾
        except Exception as exc:  # noqa: BLE001 - 统一转成 AgentFailure
            last_exc = exc
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 8))
    raise AgentFailure(node, explain(last_exc))
