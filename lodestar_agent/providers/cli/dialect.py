"""CLI 方言：同一条子进程调用，不同 CLI 的参数形状和输出格式都不一样。

已知的两种实现：
  - codebuddy  内置（`-p` + `--output-format stream-json`，逐行事件流，最后一行是 result）
  - template   配置驱动：用 `args` 模板拼参数，按 `output` 解析纯文本 / JSON

加一个新 CLI（codex / trae / 其它）时，先看能不能用 template 描述：
能，就只改配置；不能（输出格式特殊），就在这里加一个 Dialect 子类并注册进
`DIALECTS`，别的文件一行都不用动。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ...parsing import clip as _clip_text


@dataclass(frozen=True)
class CliCall:
    """一次 CLI 调用的入参，与具体 CLI 无关。"""

    prompt: str
    model: str = ""
    allowed_tools: str = ""
    add_dirs: Sequence[Path] = ()
    max_turns: int = 0
    resume_session: str = ""


@dataclass
class CliOutput:
    """从 stdout/stderr 里回收出来的信息。"""

    text: str = ""            # 这次调用「说了什么」
    progress: str = ""        # 没跑完时能捞回的进展（超时/强杀用）
    session_id: str | None = None
    is_error: bool = False
    turns: int = 0


class Dialect:
    """一个 CLI 的参数形状 + 输出格式。"""

    name = "template"

    def argv(self, call: CliCall) -> list[str]:
        raise NotImplementedError

    def parse(self, stdout: str, stderr: str) -> CliOutput:
        raise NotImplementedError

    def explain(self, stdout: str, stderr: str) -> str:
        """把原始输出压成一句人话；子类可覆盖。"""
        text = (stdout or stderr or "").strip()
        return clip(text) if text else "CLI 没有任何输出"


# ── codebuddy ───────────────────────────────────────────────


@dataclass
class _StreamEvents:
    session_id: str | None = None
    result: str = ""
    assistant_texts: list[str] = field(default_factory=list)
    num_turns: int | None = None
    is_error: bool = False
    subtype: str = ""
    complete: bool = False
    kind_seen: bool = False

    def last_text(self) -> str:
        if self.result:
            return self.result
        return self.assistant_texts[-1] if self.assistant_texts else ""

    def progress(self, limit: int = 3) -> str:
        """超时/强杀时用：把最后几段进展拼起来，别只留半句「我先看看……」。

        验收 agent 拿到这些才知道 worker 干到哪一步，而不是当它什么都没做。
        """
        if self.result:
            return self.result
        texts = [t.strip() for t in self.assistant_texts if t.strip()]
        return "\n".join(texts[-limit:])


class CodeBuddyDialect(Dialect):
    """codebuddy CLI 的无头模式。

    对应文档：https://www.codebuddy.cn/docs/cli/headless
    关键四点：
      1. `-p` 非交互执行
      2. `-y` 必须带，否则文件读写/命令执行会被权限拦下
      3. `--output-format stream-json`：逐行事件流，最后一行是 result。
         不用 `json` 的原因：它把整段 transcript（含每轮的 memory 注入）序列化成一个巨型
         对象，输出动辄几百 KB；一旦中途断掉整段就不可解析。
      4. 输出走文件而不是管道（见 runner.py）。
    """

    name = "codebuddy"

    def argv(self, call: CliCall) -> list[str]:
        args = ["-p", call.prompt, "--output-format", "stream-json", "-y"]
        if call.resume_session:
            args += ["--resume", call.resume_session]
        if call.allowed_tools:
            args += ["--allowedTools", call.allowed_tools]
        if call.model:
            args += ["--model", call.model]
        if call.max_turns and call.max_turns > 0:
            args += ["--max-turns", str(call.max_turns)]
        for d in call.add_dirs or ():
            args += ["--add-dir", str(d)]
        return args

    def parse(self, stdout: str, stderr: str) -> CliOutput:
        events = _parse_stream(stdout)
        data = None
        if not (events.complete or events.kind_seen or events.assistant_texts):
            data = _parse_json(stdout)   # 老版本 / 假 CLI：退回整段 JSON
        if data is not None:
            value = data.get("result") or data.get("message") or ""
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            return CliOutput(
                text=text.strip(),
                progress=text.strip(),
                session_id=data.get("session_id"),
                is_error=bool(data.get("is_error")),
            )
        return CliOutput(
            text=events.last_text().strip(),
            progress=events.progress().strip(),
            session_id=events.session_id,
            is_error=events.is_error,
            turns=int(events.num_turns or 0),
        )

    def explain(self, stdout: str, stderr: str) -> str:
        return diagnose(stdout, stderr)


def _pick_result(data: object) -> dict | None:
    """--output-format json 在不同版本下返回对象或数组（数组最后一项才是 result）。"""
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for item in reversed(data):
            if isinstance(item, dict) and (item.get("type") == "result" or "result" in item):
                return item
        for item in reversed(data):
            if isinstance(item, dict):
                return item
    return None


def _parse_json(stdout: str) -> dict | None:
    """CLI 正常只输出一段 JSON，但夹杂日志时兜底找最后一个能解析的对象。"""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        picked = _pick_result(json.loads(text))
        if picked is not None:
            return picked
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                picked = _pick_result(json.loads(line))
            except json.JSONDecodeError:
                continue
            if picked is not None:
                return picked
    return None


def _parse_stream(text: str) -> _StreamEvents:
    """逐行解析 stream-json 事件流。

    坏行（被强杀时写在半截）直接跳过，不牵连其它行 —— 这就是不用
    `--output-format json` 的理由：后者截断一处，整段报废。
    """
    info = _StreamEvents()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            info.session_id = sid

        kind = event.get("type")
        if isinstance(kind, str):
            info.kind_seen = True
        if kind == "assistant":
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    info.assistant_texts.append(str(block["text"]))
        elif kind == "result":
            info.is_error = bool(event.get("is_error"))
            info.subtype = str(event.get("subtype") or "")
            info.num_turns = event.get("num_turns")
            value = event.get("result")
            if isinstance(value, str):
                info.result = value
            elif value:
                info.result = json.dumps(value, ensure_ascii=False)
            info.complete = True
    return info


# 错误信息的上限：终端里只留一句人话，绝不倒 transcript。
# 实现与展示层共用 parsing.clip —— 两边口径一致，日志回看才不会错位。
clip = _clip_text


def diagnose(stdout: str, stderr: str = "") -> str:
    """把 CLI 的原始输出压成一句人话。"""
    raw = f"{stdout or ''}\n{stderr or ''}".strip()
    if not raw:
        return "CLI 没有任何输出"

    events = _parse_stream(stdout)
    if events.complete or events.assistant_texts:
        if events.complete:
            detail = events.result or events.subtype or ""
            return clip(detail) if detail else f"CLI 返回 result（subtype={events.subtype or '未知'}）"
        hint = f"CLI 未返回 result（已收到 {len(events.assistant_texts)} 段 assistant 输出）"
        return f"{hint}；最后一条：{clip(events.assistant_texts[-1])}"

    try:
        data: Any = json.loads(stdout)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, list):
        texts, errors = [], []
        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get("is_error"):
                errors.append(str(item.get("error") or item.get("result") or ""))
            msg = item.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts += [
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
        tail = errors[-1] if errors else (texts[-1] if texts else "")
        hint = f"CLI 未返回 result（对话共 {len(data)} 条消息）"
        return f"{hint}；最后一条：{clip(tail)}" if tail else hint

    return clip(raw)


# ── 模板方言（配置驱动）────────────────────────────────────


class TemplateDialect(Dialect):
    """用配置里的 `args` 模板拼参数，按 `output` 解析输出。

    支持两种条目写法（YAML）：
        args:
          - "-p"                        # 字符串：恒定带上
          - ["--model", "{model}"]      # 列表：组内任一占位符为空 → 整组跳过
          - ["{add_dirs}"]              # 展开成多个参数（每个目录一个）

    可用占位符：{prompt} {model} {tools} {max_turns} {add_dirs} {session} {cwd}
    可选的 `resume_args` / `session_regex`：能把会话 id 从输出里揪出来才谈得上续跑。
    """

    name = "template"

    def __init__(
        self,
        args: Sequence[Any] | None = None,
        resume_args: Sequence[Any] | None = None,
        output: str = "text",
        session_regex: str = "",
        prompt_via_stdin: bool = False,
    ):
        self.args = list(args or [["-p", "{prompt}"]])
        self.resume_args = list(resume_args or [])
        self.output = output if output in ("text", "json") else "text"
        self.session_regex = session_regex
        self.prompt_via_stdin = prompt_via_stdin

    def argv(self, call: CliCall) -> list[str]:
        tokens = {
            "prompt": "" if self.prompt_via_stdin else call.prompt,
            "model": call.model,
            "tools": call.allowed_tools,
            "max_turns": str(call.max_turns) if call.max_turns else "",
            "session": call.resume_session,
            "cwd": "",
            "add_dirs": [str(d) for d in call.add_dirs or ()],
        }
        template = self.args
        if call.resume_session and self.resume_args:
            template = self.args + self.resume_args
        out: list[str] = []
        for entry in template:
            group = [entry] if isinstance(entry, str) else list(entry)
            expanded = _expand(group, tokens)
            if expanded is not None:
                out.extend(expanded)
        return out

    def parse(self, stdout: str, stderr: str) -> CliOutput:
        raw = (stdout or "").strip()
        text = raw
        if self.output == "json":
            data = _parse_json(raw)
            if data is not None:
                value = data.get("result") or data.get("text") or data.get("content") or ""
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        session = None
        if self.session_regex:
            match = re.search(self.session_regex, raw)
            if match:
                session = match.group(1) if match.groups() else match.group(0)
        return CliOutput(text=text.strip(), progress=text.strip(), session_id=session)


def _expand(group: list[Any], tokens: dict) -> list[str] | None:
    """展开一组参数；组内有「取值为空」的占位符就返回 None（整组跳过）。"""
    out: list[str] = []
    for item in group:
        item = str(item)
        if item == "{add_dirs}":
            dirs = tokens.get("add_dirs") or []
            if not dirs:
                return None
            out.extend(dirs)
            continue
        value = item
        for key, token in tokens.items():
            if key == "add_dirs":
                continue
            value = value.replace("{" + key + "}", str(token))
        if not value.strip():
            return None
        out.append(value)
    return out


def build_dialect(name: str, *, args=None, resume_args=None, output: str = "text",
                  session_regex: str = "") -> Dialect:
    """按名字取方言；不认识的名字一律当作模板方言，由配置自己描述参数。"""
    key = (name or "").strip().lower()
    if key in ("", "codebuddy", "cbc"):
        return CodeBuddyDialect()
    return TemplateDialect(
        args=args, resume_args=resume_args, output=output,
        session_regex=session_regex,
    )


DIALECTS = {"codebuddy": CodeBuddyDialect, "template": TemplateDialect}
