"""API 来源：OpenAI 兼容的 HTTP 接口。

同一个来源能提供两种能力，因为它们用的是同一把 key、同一个 base_url：
  chat   直接把 prompt 丢给模型（走 langchain，保留流式 + 可中断）
  agent  补一个工具循环当 worker 用（见 api_agent.py）
"""

from __future__ import annotations

from typing import Callable

from ..config import ApiProviderConfig
from ..llm import LangchainChat
from .api_agent import ApiAgent
from .base import AGENT, CHAT, AgentRuntime, ChatBackend, ProviderError


class ApiProvider:
    kind = "api"
    capabilities = frozenset({CHAT, AGENT})

    def __init__(self, name: str, cfg: ApiProviderConfig):
        self.name = name
        self.cfg = cfg

    # ── 展示 ──────────────────────────────────────────────
    def label(self, model: str) -> str:
        return f"{self.name} · {model or '未指定模型'}"

    def describe(self) -> str:
        return f"{self.name}（{self.cfg.base_url}）"

    def default_models(self) -> list[str]:
        # HTTP 接口的型号没法枚举：给个空列表，页面就会让用户自己填
        return []

    # ── 自检 ──────────────────────────────────────────────
    def check(self) -> None:
        if not (self.cfg.api_key or "").strip():
            raise ProviderError(
                f"来源 {self.name} 没有 api_key：在 config.local.yaml 的 providers.{self.name}.api_key "
                "填写，或设置对应环境变量"
            )

    # ── 能力 ──────────────────────────────────────────────
    def chat(self, model: str, temperature: float = 0.2) -> ChatBackend:
        self.check()
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            model=model,
            api_key=self.cfg.api_key,
            base_url=self.cfg.base_url or None,
            temperature=temperature,
            timeout=self.cfg.timeout_seconds,
            max_retries=0,   # 重试由 nodes 层统一控制，别叠两层
        )
        return LangchainChat(llm, label=self.label(model))

    def agent(
        self,
        *,
        model: str,
        allowed_tools: str = "",
        timeout: float = 600.0,
        max_turns: int = 30,
        extra_env: dict[str, str] | None = None,
        cancel=None,
        drain_deadline: float | Callable[[], float | None] | None = None,
    ) -> AgentRuntime:
        self.check()
        return ApiAgent(
            label=self.label(model),
            base_url=self.cfg.base_url,
            api_key=self.cfg.api_key,
            model=model,
            temperature=self.cfg.temperature,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            timeout=timeout,
            extra_env=extra_env,
            cancel=cancel,
            drain_deadline=drain_deadline,
        )
