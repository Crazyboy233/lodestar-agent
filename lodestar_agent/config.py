"""读配置：config.yaml + 可选的 config.local.yaml 覆盖，api_key 支持环境变量兜底。

配置分两层，刻意分开：

  providers   「模型从哪来」——一条通道（HTTP 接口 / 某个本机 CLI）怎么调用
  nodes       「谁用哪个模型」——main / worker / verifier 三个节点各选一个来源与模型

所以「主 agent 换成 codebuddy 的模型」「验收换成 API」都只是改一个 source 字段，
不需要动节点代码。旧格式（main_agent / worker / verifier 三段）会自动迁移成新形状，
老配置文件照旧能用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 节点与来源的枚举值，供校验与页面使用
NODE_NAMES = ("main", "worker", "verifier")
PROVIDER_KINDS = ("api", "cli")

# cli 来源在没配 ui.models 时的默认候选（取自 `codebuddy --help`，2026-11）。
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


class ConfigError(RuntimeError):
    pass


# ── 来源 ───────────────────────────────────────────────────


@dataclass
class ApiProviderConfig:
    """OpenAI 兼容的 HTTP 接口。"""

    kind: str = "api"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    temperature: float = 0.2      # 当 worker 用时（工具循环）的温度
    timeout_seconds: float = 600.0


@dataclass
class CliProviderConfig:
    """本机 CLI。`dialect` 决定参数形状与输出解析：codebuddy 内置，其余按模板描述。"""

    kind: str = "cli"
    command: str = "codebuddy"
    dialect: str = ""             # 空 = 按 command 推断（含 codebuddy → 内置方言）
    args: list = field(default_factory=list)          # 模板方言的参数（详见 providers/cli/dialect.py）
    resume_args: list = field(default_factory=list)
    output: str = "text"          # text / json
    session_regex: str = ""       # 能从输出里揪出会话 id 才谈得上续跑
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 600.0
    # 当「纯文本模型」用时（比如主 agent 选了这个 CLI）传给它的工具参数。
    # 留空 = 不传，权限由 CLI 自己的默认策略决定。
    chat_tools: str = ""
    chat_cwd: str = ""            # 纯文本调用在哪个目录跑（留空 = 项目根）


ProviderConfig = ApiProviderConfig | CliProviderConfig


# ── 节点 ───────────────────────────────────────────────────


@dataclass
class MainNodeConfig:
    """纯文本角色：拆解方案、动态决策、汇总。"""

    source: str = "api"
    model: str = "deepseek-chat"
    temperature: float = 0.2
    max_retries: int = 3
    # 拆解任务方案时，输出解析不成 JSON 的重试次数（与请求失败重试分开计）
    plan_retries: int = 2


@dataclass
class WorkerNodeConfig:
    """执行角色：在工作目录里动手。"""

    source: str = "codebuddy"
    model: str = ""
    workdir: Path = Path("./workspace")
    allowed_tools: str = "Read,Write,Edit,Bash"
    timeout_seconds: float = 600.0
    max_turns: int = 0        # CLI：0 = 不传；API：工具循环轮数（0 也取默认）
    max_retries: int = 2
    add_dirs: list[Path] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class VerifierNodeConfig:
    """验收角色：独立判定产出是否达标。"""

    source: str = "codebuddy"
    model: str = ""
    timeout_seconds: float = 180.0
    # 验收 agent 要自己写 test 脚本，所以需要 Write；被测目录只读，脚本写到 scratch_dir
    allowed_tools: str = "Read,Write,Bash"
    scratch_dir: str = ".verify"
    max_rounds: int = 3
    max_retries: int = 2
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class UiConfig:
    """Web 控制台（引导 + 边界退出 + 实时输出）。"""

    web: bool = False          # 等价于命令行 --web
    host: str = "127.0.0.1"
    port: int = 8788           # 被占用时自动回退到随机端口
    # 点退出后最多再等多久：等当前这次模型回复自然结束；超时才强杀。
    # 默认 120s，比常见 worker 单轮（20~60s）宽，正常情况永远是「跑完再退」。
    drain_timeout_seconds: float = 120.0
    # 页面下拉的候选模型，按来源分组；没写的来源用自己的默认候选
    models: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Config:
    providers: dict[str, ProviderConfig]
    main: MainNodeConfig
    worker: WorkerNodeConfig
    verifier: VerifierNodeConfig
    ui: UiConfig
    root: Path

    def node(self, name: str) -> MainNodeConfig | WorkerNodeConfig | VerifierNodeConfig:
        return {"main": self.main, "worker": self.worker, "verifier": self.verifier}[name]


# ── 类型转换 ────────────────────────────────────────────────


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


def _str_list(value) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return [str(v) for v in items if str(v)]


def _raw_list(value) -> list:
    """模板方言的 args：允许字符串与列表混排，原样保留结构。"""
    return list(value) if isinstance(value, list) else [value]


def _models_map(value) -> dict[str, list[str]]:
    """ui.models 支持两种写法：按来源分组（新）或一个平铺列表（旧，作用于所有 CLI 来源）。"""
    if isinstance(value, dict):
        return {str(k): [str(m) for m in (v or [])] for k, v in value.items()}
    if isinstance(value, list):
        flat = [str(m) for m in value]
        return {"*": flat}
    return {}


def _pick(raw: dict, rules: dict) -> dict:
    """只取 raw 里**出现过**的字段，其余交给 dataclass 默认值。

    这样默认值只有一处来源（dataclass 字段），不必在 load 里再抄一遍常量。
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


# ── 旧格式迁移 ──────────────────────────────────────────────


def _provider_name_for_cli(command: str) -> str:
    """`/usr/local/bin/codebuddy` → `codebuddy`：来源名默认取命令名，好写也好读。"""
    return Path(str(command or "cli")).name or "cli"


def _migrate_legacy(raw: dict) -> dict:
    """把旧的三段式配置（main_agent / worker / verifier）折成 providers + nodes。

    只在**新字段缺失时**才折，所以新格式和旧格式可以并存，用户改一半也不会炸。
    """
    out = dict(raw)
    providers = dict(out.get("providers") or {})
    nodes = dict(out.get("nodes") or {})
    ui = dict(out.get("ui") or {})

    legacy_main = out.get("main_agent") or {}
    legacy_worker = out.get("worker") or {}
    legacy_verifier = out.get("verifier") or {}

    # 1) 主 agent 走的就是 HTTP 接口 → 折成 api 来源
    if legacy_main and "api" not in providers:
        providers["api"] = {
            "kind": "api",
            "base_url": legacy_main.get("base_url", ""),
            "api_key": legacy_main.get("api_key", ""),
            "timeout_seconds": legacy_main.get("timeout_seconds", 600),
        }
    if legacy_main and "main" not in nodes:
        nodes["main"] = {
            "source": "api",
            "model": legacy_main.get("model", ""),
            "temperature": legacy_main.get("temperature", 0.2),
            "max_retries": legacy_main.get("max_retries", 3),
            "plan_retries": legacy_main.get("plan_retries", 2),
        }

    # 2) worker / verifier 的 cli 字段 → 折成 cli 来源
    for section, node_name, extra in (
        (legacy_worker, "worker", ("workdir", "allowed_tools", "timeout_seconds",
                                   "max_turns", "max_retries", "add_dirs")),
        (legacy_verifier, "verifier", ("allowed_tools", "timeout_seconds",
                                       "scratch_dir", "max_rounds", "max_retries")),
    ):
        command = str(section.get("cli") or "").strip()
        if not command:
            continue
        name = _provider_name_for_cli(command)
        entry = providers.setdefault(name, {"kind": "cli", "command": command})
        if section.get("env"):
            entry.setdefault("env", {}).update(_env_map(section["env"]))
        if node_name in nodes:
            continue
        node: dict[str, Any] = {"source": name, "model": section.get("model", "")}
        for key in extra:
            if key in section:
                node[key] = section[key]
        nodes[node_name] = node

    # 3) 旧 ui.models 是一个平铺列表，新格式按来源分组：展开到每个 CLI 来源上
    if "models" in ui and isinstance(ui["models"], list):
        flat = [str(m) for m in ui["models"]]
        ui["models"] = {
            name: list(flat) for name, spec in providers.items()
            if (spec or {}).get("kind", "cli") == "cli"
        }

    if providers:
        out["providers"] = providers
    if nodes:
        out["nodes"] = nodes
    if ui:
        out["ui"] = ui
    return out


def _load_providers(raw: dict, root: Path) -> dict[str, ProviderConfig]:
    out: dict[str, ProviderConfig] = {}
    for name, spec in (raw.get("providers") or {}).items():
        spec = dict(spec or {})
        kind = str(spec.get("kind") or "").strip().lower()
        if not kind:
            # 没写 kind 就按字段猜：有 base_url / api_key → api，否则 cli
            kind = "api" if ("base_url" in spec or "api_key" in spec) else "cli"
        if kind not in PROVIDER_KINDS:
            raise ConfigError(
                f"providers.{name}.kind={kind} 不支持（可选：{'、'.join(PROVIDER_KINDS)}）"
            )
        if kind == "api":
            out[name] = ApiProviderConfig(**_pick(spec, {
                "base_url": _text_or("https://api.deepseek.com/v1"),
                "api_key": _text,
                "temperature": _number,
                "timeout_seconds": _number,
            }))
        else:
            out[name] = CliProviderConfig(**_pick(spec, {
                "command": _text_or("codebuddy"),
                "dialect": _text,
                "args": _raw_list,
                "resume_args": _raw_list,
                "output": _text_or("text"),
                "session_regex": _text,
                "env": _env_map,
                "timeout_seconds": _number,
                "chat_tools": _text,
                "chat_cwd": _text,
            }))
    return out


def _default_source(providers: dict[str, ProviderConfig], *, prefer: str) -> str:
    if prefer in providers:
        return prefer
    for kind in (prefer, "cli", "api"):
        for name, spec in providers.items():
            if spec.kind == kind:
                return name
    return ""

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
    raw = _migrate_legacy(raw)

    providers = _load_providers(raw, root)
    nodes_raw = raw.get("nodes") or {}
    ui_raw = raw.get("ui") or {}

    cli_default = _default_source(providers, prefer="codebuddy")
    api_default = _default_source(providers, prefer="api")

    main_raw = nodes_raw.get("main") or {}
    worker_raw = nodes_raw.get("worker") or {}
    verifier_raw = nodes_raw.get("verifier") or {}

    main = MainNodeConfig(**_pick({**main_raw, "source": str(main_raw.get("source") or api_default)}, {
        "source": _text_or(api_default),
        "model": _text_or("deepseek-chat"),
        "temperature": _number,
        "max_retries": _int_at_least(0),
        "plan_retries": _int_at_least(0),
    }))

    worker = WorkerNodeConfig(**_pick({**worker_raw, "source": str(worker_raw.get("source") or cli_default)}, {
        "source": _text_or(cli_default),
        "model": _text,
        "workdir": _relative_path(root),
        "allowed_tools": _text_or("Read,Write,Edit,Bash"),
        "timeout_seconds": _number,
        "max_turns": _int_at_least(0),
        "max_retries": _int_at_least(0),
        "add_dirs": _relative_paths(root),
        "env": _env_map,
    }))

    verifier = VerifierNodeConfig(**_pick(
        {**verifier_raw, "source": str(verifier_raw.get("source") or worker.source)}, {
            # 验收 agent 要自己写 test 脚本，所以默认给 Write；被测目录只读，脚本写到 scratch_dir
            "source": _text_or(worker.source),
            "model": _text,
            "timeout_seconds": _number,
            "allowed_tools": _text_or("Read,Write,Bash"),
            "scratch_dir": _text_or(".verify"),
            "max_rounds": _int_at_least(1),
            "max_retries": _int_at_least(0),
            "env": _env_map,
        }))

    # 产出目录没配时用默认值，那也是个相对路径：一并按配置文件所在目录解析，
    # 免得「在哪个目录启动就落到哪个目录」。
    if not worker.workdir.is_absolute():
        worker.workdir = (root / worker.workdir).resolve()

    ui = UiConfig(**_pick(ui_raw, {
        "web": _flag,
        "host": _text_or("127.0.0.1"),
        "port": lambda v: int(v),
        "drain_timeout_seconds": lambda v: max(0.0, float(v)),
        "models": _models_map,
    }))

    # key 也可以只放在环境变量里：来源名 → 环境变量名
    for name, spec in providers.items():
        if isinstance(spec, ApiProviderConfig) and not spec.api_key:
            env_key = "DEEPSEEK_API_KEY" if "deepseek" in (spec.base_url or "") else ""
            spec.api_key = (os.getenv(env_key) if env_key else "") or ""

    # 旧版 ui.models 兼容：`*` 表示「所有 CLI 来源都用这份列表」
    star = ui.models.pop("*", None)
    if star:
        for name, spec in providers.items():
            if spec.kind == "cli" and name not in ui.models:
                ui.models[name] = list(star)

    # cli 来源的纯文本调用目录默认落在项目根
    for spec in providers.values():
        if isinstance(spec, CliProviderConfig) and not spec.chat_cwd.strip():
            spec.chat_cwd = str(root)

    return Config(
        providers=providers, main=main, worker=worker, verifier=verifier, ui=ui, root=root,
    )


# ── 页面配置面板 ────────────────────────────────────────────
# 只有列在这里的字段允许从页面写回，避免页面把任意内容塞进配置文件。
# 值：(类型, 范围/None)。key 类型特殊：空值或仍是掩码时视为「不修改」。

PROVIDER_FIELDS: dict[str, dict[str, tuple[str, tuple | None]]] = {
    "api": {
        "base_url": ("str", None),
        "api_key": ("key", None),
        "temperature": ("float", (0.0, 2.0)),
        "timeout_seconds": ("float", (10.0, 3600.0)),
    },
    "cli": {
        "command": ("str", None),
        "dialect": ("str", None),
        "env": ("kv", None),
        "timeout_seconds": ("float", (10.0, 3600.0)),
        "chat_tools": ("str", None),
        "chat_cwd": ("str", None),
        "output": ("str", None),
    },
}

NODE_FIELDS: dict[str, dict[str, tuple[str, tuple | None]]] = {
    "main": {
        "source": ("str", None),
        "model": ("str", None),
        "temperature": ("float", (0.0, 2.0)),
        "max_retries": ("int", (0, 10)),
        "plan_retries": ("int", (0, 10)),
    },
    "worker": {
        "source": ("str", None),
        "model": ("str", None),
        "workdir": ("str", None),
        "allowed_tools": ("str", None),
        "timeout_seconds": ("float", (10.0, 3600.0)),
        "max_turns": ("int", (0, 200)),
        "max_retries": ("int", (0, 10)),
        "add_dirs": ("lines", None),
        "env": ("kv", None),
    },
    "verifier": {
        "source": ("str", None),
        "model": ("str", None),
        "timeout_seconds": ("float", (10.0, 3600.0)),
        "allowed_tools": ("str", None),
        "scratch_dir": ("str", None),
        "max_rounds": ("int", (1, 20)),
        "max_retries": ("int", (0, 10)),
        "env": ("kv", None),
    },
}

UI_FIELDS: dict[str, tuple[str, tuple | None]] = {
    "drain_timeout_seconds": ("float", (0.0, 3600.0)),
}


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def _node_view(cfg: Config, name: str) -> dict:
    node = cfg.node(name)
    view = {key: getattr(node, key) for key in NODE_FIELDS[name]}
    if name == "worker":
        view["workdir"] = str(node.workdir)
        view["add_dirs"] = "\n".join(str(d) for d in node.add_dirs)
        view["env"] = "\n".join(f"{k}={v}" for k, v in node.env.items())
    elif name == "verifier":
        view["env"] = "\n".join(f"{k}={v}" for k, v in node.env.items())
    return view


def as_editable(cfg: Config) -> dict:
    """给页面用的配置视图：api_key 只给掩码 + 是否已设置。"""
    providers = []
    for name, spec in cfg.providers.items():
        if isinstance(spec, ApiProviderConfig):
            fields = {
                "base_url": spec.base_url,
                "api_key": mask_key(spec.api_key),
                "api_key_set": bool(spec.api_key.strip()),
                "temperature": spec.temperature,
                "timeout_seconds": spec.timeout_seconds,
            }
        else:
            fields = {
                "command": spec.command,
                "dialect": spec.dialect,
                "env": "\n".join(f"{k}={v}" for k, v in spec.env.items()),
                "timeout_seconds": spec.timeout_seconds,
                "chat_tools": spec.chat_tools,
                "chat_cwd": spec.chat_cwd,
                "output": spec.output,
            }
        providers.append({"name": name, "kind": spec.kind, "fields": fields})

    return {
        "nodes": {name: _node_view(cfg, name) for name in NODE_NAMES},
        "providers": providers,
        "ui": {
            "drain_timeout_seconds": cfg.ui.drain_timeout_seconds,
            # 每行一个；空串 = 「默认模型」，前端自己补这个选项，不写进配置
            "models": {
                name: "\n".join(str(m) for m in models if str(m))
                for name, models in cfg.ui.models.items()
            },
        },
    }


def _convert(kind: str, value, rule, label: str, masked: str):
    if kind == "str":
        return str(value if value is not None else "").strip()
    if kind == "key":
        new = str(value or "").strip()
        # 空 / 还是掩码 → 用户没动这个字段，别把掩码写进配置
        return new if new and new != masked else None
    if kind == "float":
        num = float(value)
        lo, hi = rule  # type: ignore[misc]
        if not (lo <= num <= hi):
            raise ValueError(f"{label} 需要在 {lo}~{hi} 之间")
        return num
    if kind == "int":
        num = int(float(value))
        lo, hi = rule  # type: ignore[misc]
        if not (lo <= num <= hi):
            raise ValueError(f"{label} 需要在 {lo}~{hi} 之间")
        return num
    if kind == "lines":
        text = str(value or "").replace(",", "\n")
        return [s.strip() for s in text.splitlines() if s.strip()]
    if kind == "kv":
        pairs: dict[str, str] = {}
        for line in str(value or "").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip():
                pairs[k.strip()] = v.strip()
        return pairs
    return value


def _collect_section(payload: dict, rules: dict, label_prefix: str, masked: str,
                     errors: list[str]) -> dict:
    out: dict = {}
    for key, (kind, rule) in rules.items():
        if key not in payload:
            continue
        label = f"{label_prefix}.{key}"
        try:
            value = _convert(kind, payload[key], rule, label, masked)
        except (TypeError, ValueError) as exc:
            errors.append(str(exc) or f"{label} 格式不对")
            continue
        if value is not None:
            out[key] = value
    return out


def collect_overrides(payload: dict, current_api_key: dict[str, str] | str = "",
                      provider_kinds: dict[str, str] | None = None) -> tuple[dict, list[str]]:
    """把页面表单转成能写进 config.local.yaml 的 patch。

    形状：{"nodes": {main|worker|verifier: {...}}, "providers": {名字: {...}}, "ui": {...}}
    返回 (patch, errors)。填错的值收进 errors，调用方据此拒绝整次保存
    （不做「一半生效」）。
    """
    patch: dict = {}
    errors: list[str] = []
    kinds = provider_kinds or {}

    def masked_for(name: str) -> str:
        if isinstance(current_api_key, str):
            return mask_key(current_api_key) if name == "api" else ""
        return mask_key(current_api_key.get(name, ""))

    nodes = payload.get("nodes")
    if isinstance(nodes, dict):
        for name in NODE_NAMES:
            section = nodes.get(name)
            if not isinstance(section, dict):
                continue
            fields = _collect_section(section, NODE_FIELDS[name], f"nodes.{name}", "", errors)
            if fields:
                patch.setdefault("nodes", {})[name] = fields

    providers = payload.get("providers")
    if isinstance(providers, dict):
        for name, section in providers.items():
            if not isinstance(section, dict):
                continue
            kind = kinds.get(name)
            if kind is None:
                errors.append(f"providers.{name}：没有这个来源")
                continue
            rules = PROVIDER_FIELDS.get(kind, {})
            fields = _collect_section(section, rules, f"providers.{name}", masked_for(name), errors)
            if fields:
                patch.setdefault("providers", {})[name] = fields

    ui = payload.get("ui")
    if isinstance(ui, dict):
        fields = _collect_section(ui, UI_FIELDS, "ui", "", errors)
        models = ui.get("models")
        if isinstance(models, dict):
            out: dict[str, list[str]] = {}
            for name, text in models.items():
                items = [s.strip() for s in str(text or "").replace(",", "\n").splitlines()]
                out[str(name)] = [s for s in items if s]
            if out:
                fields["models"] = out
        elif isinstance(models, str):
            items = [s.strip() for s in models.replace(",", "\n").splitlines()]
            fields["models"] = {"*": [s for s in items if s]}
        if fields:
            patch["ui"] = fields

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
    # ui.models 允许按来源逐条覆盖，字典要深合并；旧值里的 "*" 平铺列表顺手清掉，
    # 否则它会一直盖住页面新存的按来源分组的值。
    merged = _deep_merge(current, patch)
    ui = merged.get("ui")
    if isinstance(ui, dict) and isinstance(ui.get("models"), dict) and "*" in ui["models"]:
        legacy = ui["models"].pop("*")
        for name, value in (patch.get("ui") or {}).get("models", {}).items():
            ui["models"][name] = value
        if not ui["models"]:
            ui["models"] = legacy
    target.write_text(
        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target
