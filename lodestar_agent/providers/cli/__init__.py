"""CLI 来源：把本机的编码 agent（codebuddy / codex / trae …）当模型通道用。

两种能力都走同一个子进程通道：
  agent  带工具权限，在工作目录里干活（worker / 验收）
  chat   不带工具权限，只当纯文本模型（主 agent 想用 codebuddy 的模型时走这条）

差别只在 `allowed_tools` 传什么，以及解析出来的文本交给谁。加新 CLI 不用动这个文件：
参数形状和输出格式都在 dialect.py 里描述。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Sequence

from ...config import CLI_MODELS, CliProviderConfig
from ...errors import ModelAborted
from ..base import AGENT, CHAT, AgentRuntime, ChatBackend, ProviderError, RunResult
from . import runner
from .dialect import CliCall, build_dialect


class CliProvider:
    kind = "cli"
    capabilities = frozenset({CHAT, AGENT})

    def __init__(self, name: str, cfg: CliProviderConfig):
        self.name = name
        self.cfg = cfg
        self.dialect = build_dialect(
            cfg.dialect, args=cfg.args, resume_args=cfg.resume_args,
            output=cfg.output, session_regex=cfg.session_regex,
        )

    # ── 展示 ──────────────────────────────────────────────
    def label(self, model: str) -> str:
        return f"{self.name} · {model or '默认模型'}"

    def describe(self) -> str:
        return f"{self.name}（{self.cfg.command}）"

    def default_models(self) -> list[str]:
        # 空串 = 不传 --model，由 CLI 自己决定；其余取自 `codebuddy --help`
        return list(CLI_MODELS)

    # ── 自检 ──────────────────────────────────────────────
    def check(self) -> None:
        try:
            runner.find_cli(self.cfg.command)
        except FileNotFoundError as exc:
            raise ProviderError(str(exc)) from exc

    # ── 能力 ──────────────────────────────────────────────
    def agent(
        self,
        *,
        model: str,
        allowed_tools: str = "",
        timeout: float = 600.0,
        max_turns: int = 0,
        extra_env: dict[str, str] | None = None,
        cancel=None,
        drain_deadline: float | Callable[[], float | None] | None = None,
    ) -> AgentRuntime:
        return CliAgent(
            provider=self,
            label=self.label(model),
            model=model,
            allowed_tools=allowed_tools,
            timeout=timeout,
            max_turns=max_turns,
            extra_env=extra_env,
            cancel=cancel,
            drain_deadline=drain_deadline,
        )

    def chat(self, model: str, temperature: float = 0.2) -> ChatBackend:
        # temperature 对 CLI 没有意义（参数由各 CLI 自己决定），留着是为了统一签名
        return CliChat(
            provider=self,
            label=self.label(model),
            model=model,
            allowed_tools=self.cfg.chat_tools,
            timeout=self.cfg.timeout_seconds,
            extra_env=self.cfg.env,
            cwd=self.cfg.chat_cwd,
        )


class CliAgent:
    """带工具权限的一次 CLI 调用。"""

    def __init__(
        self,
        *,
        provider: CliProvider,
        label: str,
        model: str,
        allowed_tools: str,
        timeout: float,
        max_turns: int,
        extra_env: dict[str, str] | None,
        cancel,
        drain_deadline,
    ):
        self.provider = provider
        self.label = label
        self.model = model
        self.allowed_tools = allowed_tools
        self.timeout = timeout
        self.max_turns = max_turns
        self.extra_env = dict(extra_env or {})
        self.cancel = cancel
        self.drain_deadline = drain_deadline

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        extra_dirs: Sequence[Path] = (),
        resume_session: str | None = None,
    ) -> RunResult:
        call = CliCall(
            prompt=prompt,
            model=self.model,
            allowed_tools=self.allowed_tools,
            add_dirs=tuple(extra_dirs),
            max_turns=self.max_turns,
            resume_session=resume_session or "",
        )
        return runner.run(
            self.provider.cfg.command,
            self.provider.dialect,
            call,
            cwd=cwd,
            timeout=self.timeout,
            extra_env={**self.provider.cfg.env, **self.extra_env},
            cancel=self.cancel,
            drain_deadline=self.drain_deadline,
            label=self.label,
        )


class CliChat:
    """把 CLI 当纯文本模型用。

    中断语义和 API 那条不同：子进程没法「读到一半停」，只能杀掉；所以到点后
    这一轮的产出会丢掉，抛 ModelAborted 交给节点走收尾分支（与 API 路径一致）。
    """

    def __init__(
        self,
        *,
        provider: CliProvider,
        label: str,
        model: str,
        allowed_tools: str,
        timeout: float,
        extra_env: dict[str, str] | None,
        cwd: Path,
    ):
        self.provider = provider
        self.label = label
        self.model = model
        self.allowed_tools = allowed_tools
        self.timeout = timeout
        self.extra_env = dict(extra_env or {})
        self.cwd = Path(cwd)

    def complete(self, prompt: str, interrupted: Callable[[], bool] | None = None) -> str:
        cancel = threading.Event()
        cancel.set()   # 允许在越过等待上限时被掐断（详见 runner.run 的注释）

        def deadline():
            # 未到点 → None（不许杀）；到点 → 0（立刻杀）
            return 0.0 if (interrupted is not None and interrupted()) else None

        result = runner.run(
            self.provider.cfg.command,
            self.provider.dialect,
            CliCall(prompt=prompt, model=self.model, allowed_tools=self.allowed_tools),
            cwd=self.cwd,
            timeout=self.timeout,
            extra_env={**self.provider.cfg.env, **self.extra_env},
            cancel=cancel,
            drain_deadline=deadline,
            label=self.label,
        )
        if result.aborted:
            raise ModelAborted("已到等待上限，断开模型连接")
        if not result.ok:
            raise ProviderError(result.error or f"{self.label} 调用失败")
        return result.output
