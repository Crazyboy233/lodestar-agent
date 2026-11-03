"""读配置：config.yaml + 可选的 config.local.yaml 覆盖，api_key 支持环境变量兜底。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(RuntimeError):
    pass


@dataclass
class MainAgentConfig:
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.2
    max_retries: int = 3
    # 拆解任务方案时，输出解析不成 JSON 的重试次数（与请求失败重试分开计）
    plan_retries: int = 2


@dataclass
class WorkerConfig:
    cli: str = "codebuddy"
    model: str = ""
    workdir: Path = Path("./workspace")
    timeout_seconds: float = 300.0
    allowed_tools: str = "Read,Write,Edit,Bash"
    add_dirs: list[Path] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    max_turns: int = 15
    max_retries: int = 2


@dataclass
class VerifierConfig:
    cli: str = "codebuddy"
    model: str = ""
    timeout_seconds: float = 180.0
    # 验收 agent 要自己写 test 脚本，所以需要 Write；被测目录通过 add_dirs 暴露，脚本写到 scratch_dir
    allowed_tools: str = "Read,Write,Bash"
    scratch_dir: str = ".verify"
    max_rounds: int = 3
    max_retries: int = 2
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    main_agent: MainAgentConfig
    worker: WorkerConfig
    verifier: VerifierConfig
    root: Path


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"找不到配置文件: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # 同目录下的 config.local.yaml 用来放真实 key（已在 .gitignore 里）
    local = path.with_name("config.local.yaml")
    if local.exists():
        raw = _deep_merge(raw, yaml.safe_load(local.read_text(encoding="utf-8")) or {})

    main_raw = raw.get("main_agent") or {}
    worker_raw = raw.get("worker") or {}
    root = path.parent

    main = MainAgentConfig(
        base_url=str(main_raw.get("base_url") or "https://api.deepseek.com/v1"),
        api_key=str(main_raw.get("api_key") or os.getenv("DEEPSEEK_API_KEY") or ""),
        model=str(main_raw.get("model") or "deepseek-chat"),
        temperature=float(main_raw.get("temperature", 0.2)),
        max_retries=max(0, int(main_raw.get("max_retries", 3))),
        plan_retries=max(0, int(main_raw.get("plan_retries", 2))),
    )

    workdir = Path(worker_raw.get("workdir") or "./workspace").expanduser()
    if not workdir.is_absolute():
        workdir = (root / workdir).resolve()

    add_dirs = []
    for d in worker_raw.get("add_dirs") or []:
        p = Path(d).expanduser()
        add_dirs.append(p if p.is_absolute() else (root / p).resolve())

    worker = WorkerConfig(
        cli=str(worker_raw.get("cli") or "codebuddy"),
        model=str(worker_raw.get("model") or ""),
        workdir=workdir,
        timeout_seconds=float(worker_raw.get("timeout_seconds", 300)),
        allowed_tools=str(worker_raw.get("allowed_tools") or "Read,Write,Edit,Bash"),
        add_dirs=add_dirs,
        env={str(k): str(v) for k, v in (worker_raw.get("env") or {}).items()},
        max_turns=int(worker_raw.get("max_turns", 15) or 0),
        max_retries=max(0, int(worker_raw.get("max_retries", 2))),
    )

    verifier_raw = raw.get("verifier") or {}
    verifier = VerifierConfig(
        cli=str(verifier_raw.get("cli") or worker.cli),
        model=str(verifier_raw.get("model") or ""),
        timeout_seconds=float(verifier_raw.get("timeout_seconds", 180)),
        # 验收 agent 要自己写 test 脚本，所以默认给 Write；被测目录只读，脚本写到 scratch_dir
        allowed_tools=str(verifier_raw.get("allowed_tools") or "Read,Write,Bash"),
        scratch_dir=str(verifier_raw.get("scratch_dir") or ".verify"),
        max_rounds=max(1, int(verifier_raw.get("max_rounds", 3))),
        max_retries=max(0, int(verifier_raw.get("max_retries", 2))),
        env={str(k): str(v) for k, v in (verifier_raw.get("env") or {}).items()},
    )

    return Config(main_agent=main, worker=worker, verifier=verifier, root=root)
