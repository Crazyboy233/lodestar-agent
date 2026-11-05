"""把「OpenAI 兼容接口」当 worker 用：模型不会自己动文件，就给它一只手。

这是 API 来源与 CLI 来源唯一的实质差别 —— CLI 自己带工具循环，API 只回文本，
所以这里补一个最小的工具循环：模型要调工具就执行、把结果塞回去，直到它不再要工具。

刻意做小：不做计划/反思/多 agent，只保证四件事
  1. 工具只能在沙箱目录里动手（见 local_tools）
  2. 轮数和墙钟都有上限，卡住能停
  3. 退出边界和 CLI 一致：到点才真的掐断
  4. 失败一律变成 RunResult.error，交给上层按既有规则重试/终止
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Sequence

from .base import RunResult
from .local_tools import LocalTools

SYSTEM_PROMPT = """你是一个在本地工作目录里干活的执行型 agent。

规则：
1. 直接动磁盘（读文件、写文件、跑命令），不要只描述做法。
2. 路径一律用相对于工作目录的写法。
3. 命令输出会长，用 -q / --quiet / | tail -20 之类收窄，不要把整段日志打回来。
4. 只做给定的这一件事，不要顺手重构无关文件。
5. 做完后用中文简短汇报：动了哪些文件、怎么验证的、结果如何。
"""


class ApiAgent:
    """一个只会用 OpenAI 兼容 HTTP 接口的 worker。"""

    def __init__(
        self,
        *,
        label: str,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        allowed_tools: str = "",
        max_turns: int = 30,
        timeout: float = 600.0,
        extra_env: dict[str, str] | None = None,
        cancel=None,
        drain_deadline: float | Callable[[], float | None] | None = None,
    ):
        self.label = label
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.allowed_tools = allowed_tools
        self.max_turns = max(1, int(max_turns or 30))
        self.timeout = float(timeout)
        self.extra_env = dict(extra_env or {})
        self.cancel = cancel
        self.drain_deadline = drain_deadline

    # ── 退出边界 ──────────────────────────────────────────
    def _interrupted(self) -> bool:
        """与 CLI 路径同一套语义：cancel 只表示「之后不许再开新的」，到点才掐。"""
        if self.cancel is None or not self.cancel.is_set():
            return False
        value = self.drain_deadline
        deadline = value() if callable(value) else value
        return deadline is not None and time.time() >= deadline

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        extra_dirs: Sequence[Path] = (),
        resume_session: str | None = None,
    ) -> RunResult:
        # resume_session 对无状态 HTTP 接口没有意义：每一轮都是新的对话，
        # 上下文靠 prompt 里的「上一次做到哪」来传递。
        started = time.time()
        cwd = Path(cwd).expanduser().resolve()
        cwd.mkdir(parents=True, exist_ok=True)
        tools = LocalTools(
            root=cwd, extra_dirs=list(extra_dirs), timeout=self.timeout, env=self.extra_env,
        )
        specs = tools.specs(self.allowed_tools)
        has_tools = bool(specs)

        try:
            from openai import OpenAI
        except ImportError:  # pragma: no cover - 依赖由 langchain-openai 带进来
            return RunResult(
                ok=False, output="", duration=0.0,
                error="缺少 openai 依赖：uv sync 再试",
            )
        if not self.api_key:
            return RunResult(
                ok=False, output="", duration=0.0,
                error=f"{self.label} 没有 api_key：在配置里这个来源下填 key，或设对应环境变量",
            )

        client = OpenAI(api_key=self.api_key, base_url=self.base_url or None,
                        timeout=self.timeout, max_retries=0)
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        turns = 0
        while turns < self.max_turns:
            turns += 1
            if self._interrupted():
                return RunResult(
                    ok=False, output="", duration=time.time() - started, turns=turns,
                    error="超过等待上限仍未结束，已终止（可用 ui.drain_timeout_seconds 放宽）",
                    aborted=True,
                )
            if time.time() - started > self.timeout:
                return RunResult(
                    ok=False, output="", duration=time.time() - started, turns=turns,
                    error=f"超时：超过 {self.timeout:.0f}s（工具循环第 {turns} 轮）",
                    timed_out=True,
                )
            try:
                reply = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    tools=specs or None,
                    tool_choice="auto" if has_tools else None,
                )
            except Exception as exc:  # noqa: BLE001 - 统一转成 error 交给上层
                return RunResult(
                    ok=False, output="", duration=time.time() - started, turns=turns,
                    error=f"接口请求失败：{type(exc).__name__}: {exc}",
                )

            message = reply.choices[0].message
            calls = list(getattr(message, "tool_calls", None) or [])
            text = (message.content or "").strip()
            if not calls:
                return RunResult(
                    ok=True, output=text, duration=time.time() - started, turns=turns,
                )

            messages.append({
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments or "{}",
                        },
                    }
                    for call in calls
                ],
            })
            for call in calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                result = tools.call(call.function.name, args)
                messages.append({
                    "role": "tool", "tool_call_id": call.id, "content": result,
                })

        return RunResult(
            ok=False, output="", duration=time.time() - started, turns=turns,
            error=(
                f"到达工具循环上限（{self.max_turns} 轮）仍未收敛："
                "把这个节点的 max_turns 调大，或者把任务拆小一点。"
            ),
        )
