"""来源注册表：名字 → 实现。

节点只写 `source: api` / `source: codebuddy`，由这里换成真正的对象。加一类新来源
（比如某个 SDK 直连、某个私有网关）只要在 `_KINDS` 里登记一行；加一个新 CLI 连
这行都不用加 —— 改配置用 template 方言就行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

from ..config import Config
from .api import ApiProvider
from .base import AGENT, CHAT, AgentRuntime, ChatBackend, ProviderError
from .cli import CliProvider

_KINDS = {
    "api": ApiProvider,
    "cli": CliProvider,
}


def provider(cfg: Config, name: str):
    """按名字取来源实例（每次新建，无状态）。"""
    spec = (cfg.providers or {}).get(name)
    if spec is None:
        known = "、".join(cfg.providers.keys()) or "（无）"
        raise ProviderError(
            f"没有这个来源：{name}（已配置的有 {known}）。"
            "在 config.yaml 的 providers 下新增一条，或把节点的 source 改成已有来源。"
        )
    kind = _KINDS.get(spec.kind)
    if kind is None:
        raise ProviderError(
            f"来源 {name} 的 kind={spec.kind} 不认识（支持：{'、'.join(_KINDS)}）"
        )
    return kind(name, spec)


def label(cfg: Config, source: str, model: str) -> str:
    """展示用的「来源 · 模型」。取不到来源时退回原始值，不要因为展示失败炸掉流程。"""
    try:
        return provider(cfg, source).label(model)
    except ProviderError:
        return f"{source} · {model or '默认模型'}"


def build_chat(cfg: Config, *, source: str, model: str, temperature: float = 0.2) -> ChatBackend:
    """纯文本角色（主 agent）的模型通道。"""
    impl = provider(cfg, source)
    if CHAT not in impl.capabilities:
        raise ProviderError(f"来源 {source} 不支持纯文本调用（只有：{'、'.join(impl.capabilities)}）")
    return impl.chat(model=model, temperature=temperature)


def build_agent(
    cfg: Config,
    *,
    source: str,
    model: str,
    allowed_tools: str = "",
    timeout: float = 600.0,
    max_turns: int = 0,
    extra_env: dict[str, str] | None = None,
    cancel=None,
    drain_deadline: float | Callable[[], float | None] | None = None,
) -> AgentRuntime:
    """会干活角色（worker / 验收）的运行时。"""
    impl = provider(cfg, source)
    if AGENT not in impl.capabilities:
        raise ProviderError(f"来源 {source} 不支持执行型调用（只有：{'、'.join(impl.capabilities)}）")
    return impl.agent(
        model=model,
        allowed_tools=allowed_tools,
        timeout=timeout,
        max_turns=max_turns,
        extra_env=extra_env,
        cancel=cancel,
        drain_deadline=drain_deadline,
    )


def models_for(cfg: Config, source: str) -> list[str]:
    """下拉候选：配置里给了就用配置的，没给就用来源自己声明的默认值。"""
    configured = (cfg.ui.models or {}).get(source)
    if configured:
        return list(configured)
    try:
        return list(provider(cfg, source).default_models())
    except ProviderError:
        return []


def sources(cfg: Config) -> list[dict]:
    """给页面用：有哪些来源、各是什么类型、分别能选哪些模型。"""
    out = []
    for name, spec in (cfg.providers or {}).items():
        kind = spec.kind
        try:
            impl = provider(cfg, name)
            caps = sorted(impl.capabilities)
        except ProviderError:
            caps = []
        out.append({
            "name": name,
            "kind": kind,
            "capabilities": caps,
            "models": models_for(cfg, name),
            "display": (spec.base_url if kind == "api" else spec.command),
        })
    return out


def check(cfg: Config, only: Sequence[str] | None = None) -> list[str]:
    """启动自检：来源存在、能力匹配、该登录的登录、该有 key 的有 key。

    `only` 用来跳过这次运行用不到的节点（比如 --dry-run 不调主 agent）。
    返回人类可读的问题列表（空 = 全通过）。这里**不抛异常**：调用方要一次把
    所有问题都报出来，而不是让用户改一个、重启、再撞下一个。
    """
    problems: list[str] = []
    nodes = (
        ("main", cfg.main.source, CHAT),
        ("worker", cfg.worker.source, AGENT),
        ("verifier", cfg.verifier.source, AGENT),
    )
    seen: set[str] = set()
    for node, source, need in nodes:
        if only is not None and node not in only:
            continue
        try:
            impl = provider(cfg, source)
        except ProviderError as exc:
            problems.append(f"{node}.source：{exc}")
            continue
        if need not in impl.capabilities:
            problems.append(
                f"{node}.source={source} 不支持「{need}」能力（该来源支持："
                f"{'、'.join(sorted(impl.capabilities)) or '无'}）"
            )
            continue
        if source in seen:
            continue
        seen.add(source)
        try:
            impl.check()
        except ProviderError as exc:
            problems.append(str(exc))
    return problems


def resolve_workdir(cfg: Config, value: str | None) -> Path:
    """节点配置里的相对路径 → 绝对路径（相对配置文件所在目录）。"""
    raw = (value or "").strip()
    path = Path(raw).expanduser() if raw else Path("./workspace")
    return path if path.is_absolute() else (cfg.root / path).resolve()


def agent_dirs(cfg: Config, dirs: Sequence[Path]) -> list[Path]:
    return [Path(d).expanduser() for d in dirs or ()]
