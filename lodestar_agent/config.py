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
    timeout_seconds: float = 600.0
    allowed_tools: str = "Read,Write,Edit,Bash"
    add_dirs: list[Path] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    max_turns: int = 0   # 0 = 不传 --max-turns，用 CLI 默认（非交互 500）
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


# codebuddy CLI `--model` 支持的取值（来自 `codebuddy --help`，2026-11）。
# 空字符串表示「不传 --model」，由 CLI 自己决定。
CLI_MODELS = [
    "",
    "deepseek-v4.1-flash",
    "deepseek-v4-pro",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "kimi-k3.1-pro",
    "kimi-k3-1",
    "kimi-k2.8-preview",
    "minimax-m3.1-flash",
    "step-5-preview",
    "mimo-v2.6-pro",
    "space-bunny",
    "hy4-preview",
    "hy3",
    "hy3-x",
    "fast-model",
    "balanced-model",
    "deep-model",
    "custom-local:deepseek-v4-flash",
]


@dataclass
class UiConfig:
    """Web 控制台（引导 + 边界退出 + 实时输出）。"""
    web: bool = False          # 等价于命令行 --web
    host: str = "127.0.0.1"
    port: int = 8788           # 被占用时自动回退到随机端口
    # 点退出后最多再等多久：等当前这次模型回复自然结束；超时才强杀。
    # 默认 120s，比常见 worker 单轮（20~60s）宽，正常情况永远是「跑完再退」。
    drain_timeout_seconds: float = 120.0
    # 页面上可选的模型（worker / 验收各一个下拉）；配置里给了就以配置为准
    models: list[str] = field(default_factory=lambda: list(CLI_MODELS))


@dataclass
class Config:
    main_agent: MainAgentConfig
    worker: WorkerConfig
    verifier: VerifierConfig
    ui: UiConfig
    root: Path


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _text(value) -> str:
    return str(value)


def _text_or(default: str):
    """空串 / 缺失都回到默认值（`cli: ""` 这类写法在 yaml 里很常见）。"""
    return lambda value: str(value) or default


def _number(value) -> float:
    return float(value)


def _int_at_least(low: int, high: int | None = None):
    def convert(value) -> int:
        num = int(float(value))
        if high is not None:
            return max(low, min(high, num))
        return max(low, num)
    return convert


def _flag(value) -> bool:
    return bool(value)


def _relative_path(root: Path):
    def convert(value) -> Path:
        path = Path(str(value or "./workspace")).expanduser()
        return path if path.is_absolute() else (root / path).resolve()
    return convert


def _relative_paths(root: Path):
    def convert(value) -> list[Path]:
        items = value if isinstance(value, list) else [value]
        out: list[Path] = []
        for item in items:
            if not item:
                continue
            path = Path(str(item)).expanduser()
            out.append(path if path.is_absolute() else (root / path).resolve())
        return out
    return convert


def _env_map(value) -> dict[str, str]:
    return {str(k): str(v) for k, v in (value or {}).items()}


def _model_list(value) -> list[str]:
    return [str(m) for m in value] if isinstance(value, list) and value else list(CLI_MODELS)


def _pick(raw: dict, rules: dict) -> dict:
    """只取 raw 里**出现过**的字段，其余交给 dataclass 默认值。

    这样默认值只有一处来源（dataclass 字段），不必在 load 里再抄一遍常量 ——
    以前 `timeout_seconds = 600.0` 和 `raw.get("timeout_seconds", 600)` 是两条
    独立来源，改一处容易只改一边。
    """
    out: dict = {}
    for name, convert in rules.items():
        if name not in raw:
            continue
        value = raw[name]
        if value is None:      # 显式 null 也按「没配」处理
            continue
        out[name] = convert(value)
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

    root = path.parent
    main_raw = raw.get("main_agent") or {}
    worker_raw = raw.get("worker") or {}
    verifier_raw = raw.get("verifier") or {}
    ui_raw = raw.get("ui") or {}

    main = MainAgentConfig(**_pick(main_raw, {
        "base_url": _text_or("https://api.deepseek.com/v1"),
        "api_key": _text,
        "model": _text_or("deepseek-chat"),
        "temperature": _number,
        "max_retries": _int_at_least(0),
        "plan_retries": _int_at_least(0),
    }))
    # key 也可以只放在环境变量里
    if not main.api_key:
        main.api_key = os.getenv("DEEPSEEK_API_KEY") or ""

    worker = WorkerConfig(**_pick(worker_raw, {
        "cli": _text_or("codebuddy"),
        "model": _text,
        "workdir": _relative_path(root),
        "timeout_seconds": _number,
        "allowed_tools": _text_or("Read,Write,Edit,Bash"),
        "add_dirs": _relative_paths(root),
        "env": _env_map,
        "max_turns": _int_at_least(0),
        "max_retries": _int_at_least(0),
    }))

    verifier = VerifierConfig(**_pick(verifier_raw, {
        # 验收 agent 要自己写 test 脚本，所以默认给 Write；被测目录只读，脚本写到 scratch_dir
        "cli": _text_or(worker.cli),
        "model": _text,
        "timeout_seconds": _number,
        "allowed_tools": _text_or("Read,Write,Bash"),
        "scratch_dir": _text_or(".verify"),
        "max_rounds": _int_at_least(1),
        "max_retries": _int_at_least(0),
        "env": _env_map,
    }))

    ui = UiConfig(**_pick(ui_raw, {
        "web": _flag,
        "host": _text_or("127.0.0.1"),
        "port": lambda v: int(v),
        "drain_timeout_seconds": lambda v: max(0.0, float(v)),
        "models": _model_list,
    }))

    return Config(main_agent=main, worker=worker, verifier=verifier, ui=ui, root=root)


# ── 页面配置面板 ────────────────────────────────────────────
# 只有列在这里的字段允许从页面写回，避免页面把任意内容塞进配置文件。
# 值：(类型, 范围/None)。key 类型特殊：空值或仍是掩码时视为「不修改」。
EDITABLE: dict[str, dict[str, tuple[str, tuple | None]]] = {
    "main_agent": {
        "base_url": ("str", None),
        "api_key": ("key", None),
        "model": ("str", None),
        "temperature": ("float", (0.0, 2.0)),
        "max_retries": ("int", (0, 10)),
        "plan_retries": ("int", (0, 10)),
    },
    "worker": {
        "cli": ("str", None),
        "model": ("str", None),
        "workdir": ("str", None),
        "timeout_seconds": ("float", (10.0, 3600.0)),
        "allowed_tools": ("str", None),
        "max_turns": ("int", (0, 200)),
        "max_retries": ("int", (0, 10)),
        "add_dirs": ("lines", None),
        "env": ("kv", None),
    },
    "verifier": {
        "cli": ("str", None),
        "model": ("str", None),
        "timeout_seconds": ("float", (10.0, 3600.0)),
        "allowed_tools": ("str", None),
        "scratch_dir": ("str", None),
        "max_rounds": ("int", (1, 20)),
        "max_retries": ("int", (0, 10)),
        "env": ("kv", None),
    },
    "ui": {
        "drain_timeout_seconds": ("float", (0.0, 3600.0)),
        "models": ("lines", None),
    },
}


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def as_editable(cfg: Config) -> dict:
    """给页面用的配置视图：api_key 只给掩码 + 是否已设置。"""
    return {
        "main_agent": {
            "base_url": cfg.main_agent.base_url,
            "api_key": mask_key(cfg.main_agent.api_key),
            "api_key_set": bool(cfg.main_agent.api_key.strip()),
            "model": cfg.main_agent.model,
            "temperature": cfg.main_agent.temperature,
            "max_retries": cfg.main_agent.max_retries,
            "plan_retries": cfg.main_agent.plan_retries,
        },
        "worker": {
            "cli": cfg.worker.cli,
            "model": cfg.worker.model,
            "workdir": str(cfg.worker.workdir),
            "timeout_seconds": cfg.worker.timeout_seconds,
            "allowed_tools": cfg.worker.allowed_tools,
            "max_turns": cfg.worker.max_turns,
            "max_retries": cfg.worker.max_retries,
            "add_dirs": "\n".join(str(d) for d in cfg.worker.add_dirs),
            "env": "\n".join(f"{k}={v}" for k, v in cfg.worker.env.items()),
        },
        "verifier": {
            "cli": cfg.verifier.cli,
            "model": cfg.verifier.model,
            "timeout_seconds": cfg.verifier.timeout_seconds,
            "allowed_tools": cfg.verifier.allowed_tools,
            "scratch_dir": cfg.verifier.scratch_dir,
            "max_rounds": cfg.verifier.max_rounds,
            "max_retries": cfg.verifier.max_retries,
            "env": "\n".join(f"{k}={v}" for k, v in cfg.verifier.env.items()),
        },
        "ui": {
            "drain_timeout_seconds": cfg.ui.drain_timeout_seconds,
            # 空字符串 = 「默认（CLI 决定）」，前端自己会补上这个选项，不写进配置
            "models": "\n".join(m for m in cfg.ui.models if m),
        },
    }


def collect_overrides(payload: dict, current_api_key: str = "") -> tuple[dict, list[str]]:
    """把页面表单转成能写进 config.local.yaml 的 patch。

    返回 (patch, errors)。只处理 EDITABLE 里的字段；填错的值收进 errors，
    调用方据此拒绝整次保存（不做「一半生效」）。
    """
    patch: dict = {}
    errors: list[str] = []
    masked = mask_key(current_api_key)

    for section, fields in EDITABLE.items():
        raw_section = payload.get(section)
        if not isinstance(raw_section, dict):
            continue
        out: dict = {}
        for key, (kind, rule) in fields.items():
            if key not in raw_section:
                continue
            value = raw_section[key]
            label = f"{section}.{key}"
            try:
                if kind == "str":
                    out[key] = str(value if value is not None else "").strip()
                elif kind == "key":
                    new = str(value or "").strip()
                    # 空 / 还是掩码 → 用户没动这个字段，别把掩码写进配置
                    if new and new != masked:
                        out[key] = new
                elif kind == "float":
                    num = float(value)
                    lo, hi = rule  # type: ignore[misc]
                    if not (lo <= num <= hi):
                        raise ValueError(f"{label} 需要在 {lo}~{hi} 之间")
                    out[key] = num
                elif kind == "int":
                    num = int(float(value))
                    lo, hi = rule  # type: ignore[misc]
                    if not (lo <= num <= hi):
                        raise ValueError(f"{label} 需要在 {lo}~{hi} 之间")
                    out[key] = num
                elif kind == "lines":
                    text = str(value or "").replace(",", "\n")
                    out[key] = [s.strip() for s in text.splitlines() if s.strip()]
                elif kind == "kv":
                    pairs: dict[str, str] = {}
                    for line in str(value or "").splitlines():
                        line = line.strip()
                        if not line or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        if k.strip():
                            pairs[k.strip()] = v.strip()
                    out[key] = pairs
            except (TypeError, ValueError) as exc:
                errors.append(str(exc) or f"{label} 格式不对")
        if out:
            patch[section] = out
    return patch, errors


def save_overrides(root: Path, patch: dict, path: Path | None = None) -> Path:
    """把 patch 合并进 config.local.yaml（不动 config.yaml）。"""
    target = Path(path) if path else (Path(root) / "config.local.yaml")
    current: dict = {}
    if target.exists():
        try:
            current = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{target.name} 解析失败，先手动修一下：{exc}")
    merged = _deep_merge(current, patch)
    target.write_text(
        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target
