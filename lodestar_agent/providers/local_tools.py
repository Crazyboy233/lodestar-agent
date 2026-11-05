"""API 来源的 agent 用到的本地工具：读写文件、列目录、跑命令。

只有一件事必须严肃对待 —— **沙箱**：模型给的路径一律先解析成绝对路径，再确认它落在
工作目录（或显式授权的额外目录）里，否则直接拒绝。这条规则不靠提示词，靠代码。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .subproc import child_env

# 配置里的工具名（沿用了 CLI 的叫法）→ 这里实际暴露的能力
_ALIASES = {
    "read": "read_file", "read_file": "read_file", "view": "read_file",
    "write": "write_file", "write_file": "write_file", "edit": "write_file",
    "create": "write_file",
    "bash": "run_command", "shell": "run_command", "run_command": "run_command",
    "list": "list_dir", "list_dir": "list_dir", "ls": "list_dir",
}


@dataclass
class LocalTools:
    """在 root 目录内干活的最小工具集。"""

    root: Path
    extra_dirs: list[Path] = field(default_factory=list)
    timeout: float = 120.0
    env: dict[str, str] = field(default_factory=dict)
    max_output: int = 6000

    # ── 能力表 ────────────────────────────────────────────
    def enabled(self, allowed: str) -> set[str]:
        wanted = {
            _ALIASES.get(part.strip().lower())
            for part in (allowed or "").split(",")
            if part.strip()
        }
        wanted.discard(None)
        return {w for w in wanted if w} or {"read_file", "write_file", "list_dir", "run_command"}

    def specs(self, allowed: str) -> list[dict]:
        """OpenAI function-calling 的工具描述。没授权的工具不出现，模型也就没法调。"""
        enabled = self.enabled(allowed)
        specs: list[dict] = []
        if "read_file" in enabled:
            specs.append(_spec(
                "read_file", "读取一个文本文件的内容（可指定起始行与行数）",
                {"path": ("string", "相对于工作目录的文件路径"),
                 "offset": ("integer", "从第几行开始（从 1 计，可省略）"),
                 "limit": ("integer", "最多读多少行（默认 400）")},
                required=["path"],
            ))
        if "write_file" in enabled:
            specs.append(_spec(
                "write_file", "把内容写入文件（整文件覆盖，父目录会自动创建）",
                {"path": ("string", "相对于工作目录的文件路径"),
                 "content": ("string", "要写入的完整文本")},
                required=["path", "content"],
            ))
        if "list_dir" in enabled:
            specs.append(_spec(
                "list_dir", "列出一个目录下的条目",
                {"path": ("string", "相对于工作目录的目录路径，省略即工作目录本身")},
            ))
        if "run_command" in enabled:
            specs.append(_spec(
                "run_command", "在工作目录里执行一条 shell 命令，返回退出码与输出",
                {"command": ("string", "要执行的命令"),
                 "timeout": ("integer", "超时秒数，默认 120")},
                required=["command"],
            ))
        return specs

    def call(self, name: str, args: dict) -> str:
        """执行一次工具调用。任何异常都转成字符串交还模型 —— 让它自己纠错。"""
        try:
            if name == "read_file":
                return self._read(args)
            if name == "write_file":
                return self._write(args)
            if name == "list_dir":
                return self._list(args)
            if name == "run_command":
                return self._run(args)
            return f"未知工具：{name}"
        except _Denied as exc:
            return f"拒绝：{exc}"
        except Exception as exc:  # noqa: BLE001 - 工具失败要让模型看到，不能炸掉整轮
            return f"工具执行失败（{type(exc).__name__}）：{exc}"

    # ── 具体实现 ──────────────────────────────────────────
    def _resolve(self, raw: str) -> Path:
        if not str(raw or "").strip():
            raise _Denied("路径为空")
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        allowed = [self.root.resolve(), *[d.resolve() for d in self.extra_dirs]]
        if not any(path == base or path.is_relative_to(base) for base in allowed):
            raise _Denied(f"路径超出工作目录：{path}")
        return path

    def _read(self, args: dict) -> str:
        path = self._resolve(str(args.get("path") or ""))
        if not path.is_file():
            return f"文件不存在：{path}"
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, min(2000, int(args.get("limit") or 400)))
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        chunk = lines[offset - 1: offset - 1 + limit]
        head = f"{path}（第 {offset}~{offset + len(chunk) - 1} 行，共 {len(lines)} 行）\n"
        return _clip(head + "\n".join(chunk), self.max_output)

    def _write(self, args: dict) -> str:
        path = self._resolve(str(args.get("path") or ""))
        content = args.get("content")
        if not isinstance(content, str):
            return "content 必须是字符串"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"已写入 {path}（{len(content)} 字符）"

    def _list(self, args: dict) -> str:
        path = self._resolve(str(args.get("path") or "."))
        if not path.is_dir():
            return f"不是目录：{path}"
        entries = []
        for item in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
            entries.append(f"{item.name}{'/' if item.is_dir() else ''}")
        return _clip("\n".join(entries) or "(空目录)", self.max_output)

    def _run(self, args: dict) -> str:
        command = str(args.get("command") or "").strip()
        if not command:
            return "command 为空"
        limit = float(args.get("timeout") or self.timeout)
        try:
            done = subprocess.run(
                command, shell=True, cwd=str(self.root.resolve()),
                env=child_env(self.env), capture_output=True, text=True,
                timeout=max(1.0, min(limit, 1800.0)),
            )
        except subprocess.TimeoutExpired:
            return f"命令超过 {limit:.0f}s 被终止：{command}"
        out = (done.stdout or "").strip()
        err = (done.stderr or "").strip()
        body = f"退出码 {done.returncode}"
        if out:
            body += f"\n[stdout]\n{out}"
        if err:
            body += f"\n[stderr]\n{err}"
        return _clip(body, self.max_output)


class _Denied(Exception):
    pass


def _spec(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {
                    key: {"type": kind, "description": text}
                    for key, (kind, text) in props.items()
                },
                "required": list(required or []),
            },
        },
    }


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原始长度 {len(text)} 字符）"
