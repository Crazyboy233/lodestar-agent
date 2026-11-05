"""把图的节点产出翻译成「一行行给人看的内容」，终端 / 页面 / 日志共用一份。

以前这段映射写在 `run_session` 的 `for chunk in app.stream(...)` 里，110 行，
每个节点都是 `con.hr(...) + con.say(...) + _w(logf, ...)` 三连 —— 同一句话写两遍，
其中一遍还是日志解析端的格式契约。

现在节点更新只负责产出 `Line`，去向由两个 sink 决定：

- `Console.show()`（session.py）：终端打印 + 页面事件
- `LogSink.write()`：落盘成可被 `history.parse_log` 切回的文本

`Renderer` 另外持有跨 chunk 的计数（完成 / 失败 / 当前任务），因为节点的 header
要显示「第 N 条」和汇总进度，这些不是单个 update 能算出来的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import logfmt
from .parsing import clip


@dataclass(frozen=True)
class Line:
    """一行待展示内容。中文字段名见各类 sink 的用法。

    - `hr=True`：段落标题，终端画分隔线，日志写成 `[tag] 标题`
    - `raw=True`：模型原始输出块，只进日志（终端从不打印这些）
    - `label`：日志里的字段前缀（任务 / 验收 / 结论 / 输出 / 错误）
    - `log_text`：日志要用另一段文本时填（终端更简、或时长只写进日志）
    - `log_tag`：日志标题的 tag 与终端不同的时候填（如终端 `[主 agent]`、日志 `[汇总]`）
    - `console=False`：只进日志
    """

    text: str = ""
    kind: str = "system"
    role: str = "log"
    label: str = ""
    indent: int = 2
    hr: bool = False
    raw: bool = False
    log_text: str = ""
    log_tag: str = ""
    console: bool = True


class LogSink:
    """把 Line 写进运行日志。格式见 logfmt（解析端按同一份常量读回来）。"""

    def __init__(self, write: Callable[[str], None]):
        self._write = write

    def write(self, line: Line) -> None:
        if line.hr:
            self._write(logfmt.LOG_RULE)
            self._write(self._header(line))
            return
        if line.raw:
            self._write(logfmt.RAW_MARK)
            self._write(line.log_text or line.text)
            return
        body = line.log_text or line.text
        if not body:
            return
        self._write(logfmt.field(line.label, body) if line.label else body)

    @staticmethod
    def _header(line: Line) -> str:
        match = logfmt.SECTION_RE.match(line.text)
        tag = line.log_tag or (match.group(1) if match else "")
        body = line.log_text or (match.group(2) if match else line.text)
        return f"[{tag}] {body}" if tag else body


class Renderer:
    """按节点把 update 翻成 Line 列表；顺带维护「已完成 / 失败 / 当前任务」。"""

    def __init__(self) -> None:
        self.done = 0
        self.failed = 0
        self.current_task = ""

    def render(self, node: str, update: dict) -> list[Line]:
        handler = {
            "plan": self._plan,
            "worker": self._worker,
            "verify": self._verify,
            "synthesize": self._synth,
        }.get(node)
        return handler(update or {}) if handler else []

    # ── 各节点 ────────────────────────────────────────────
    def _plan(self, update: dict) -> list[Line]:
        if update.get("stage") == "abort":
            out = [Line("[主 agent] 边界退出 → 收尾", kind="stop", hr=True)]
            if update.get("note"):
                out.append(Line(update["note"], kind="stop"))
            return out

        if update.get("stage") == "init":
            out = [Line("[主 agent] 拆解任务方案", kind="plan", hr=True)]
            if update.get("note"):
                out.append(Line(update["note"], kind="plan"))
            items = ([update.get("current")] if update.get("current") else []) + (
                update.get("plan") or []
            )
            for i, item in enumerate(items, 1):
                if not item:
                    continue
                out.append(Line(f"{i}. {item['task']}", kind="plan", role="task"))
                verify = item.get("verify") or "(未给出可执行标准，由验收 agent 自行设计)"
                out.append(Line(
                    logfmt.field(logfmt.VERIFY, verify), kind="plan", role="task", indent=5,
                ))
            self.current_task = (update.get("current") or {}).get("task", "") or self.current_task
            if update.get("plan_raw"):
                out.append(Line(kind="plan", raw=True, log_text=update["plan_raw"], console=False))
            return out

        for record in update.get("records") or []:
            self.done += 1
            if not record.get("passed"):
                self.failed += 1
        action = update.get("action", "")
        label = "继续下一个" if action == "dispatch" else "收尾"
        out = [Line(
            f"[主 agent] 动态规划 → {label}（完成 {self.done} 条 / 失败 {self.failed}）",
            kind="plan", hr=True,
        )]
        if update.get("note"):
            out.append(Line(update["note"], kind="plan"))
        if update.get("decision_raw"):
            out.append(Line(kind="plan", raw=True, log_text=update["decision_raw"], console=False))
        if action == "dispatch" and update.get("current"):
            self.current_task = update["current"].get("task", "")
        return out

    def _worker(self, update: dict) -> list[Line]:
        if update.get("skipped"):
            return [Line(
                f"[worker] 已中止 · 这一轮未执行：{self.current_task}", kind="stop", hr=True,
            )]

        nth = self.done + 1
        attempts = update.get("attempts", 1)
        duration = update.get("last_duration", 0)
        suffix = "（被中止）" if update.get("abort") else ""
        out = [Line(
            f"[worker] 执行  第 {nth} 条 · 第 {attempts} 轮{suffix}",
            kind="worker", hr=True,
            log_text=f"执行 第 {nth} 条 第 {attempts} 轮（{duration}s）",
        )]
        if self.current_task:
            out.append(Line(
                kind="worker", label=logfmt.TASK, log_text=self.current_task, console=False,
            ))
        body = update.get("last_output") or update.get("last_error") or ""
        out.append(Line(
            f"{duration}s\n{clip(body) or '(无输出)'}",
            kind="worker", role="output",
            label=logfmt.OUTPUT, log_text=body or "(无输出)",
        ))
        if update.get("last_error"):
            out.append(Line(
                kind="worker", label=logfmt.ERROR,
                log_text=update["last_error"], console=False,
            ))
        return out

    def _verify(self, update: dict) -> list[Line]:
        if update.get("abort"):
            return [Line(
                "[verifier] 超过等待上限，验收被强制终止", kind="stop", hr=True,
            )]
        if update.get("draining") and not update.get("verdict_ready"):
            return [Line(
                "[verifier] 已跳过（收到退出请求，不再开始新的模型调用）",
                kind="stop", hr=True, log_text="已跳过（收到退出请求）",
            )]
        passed = update.get("verdict_passed")
        out = [Line(f"[verifier] 验收 → {'PASS' if passed else 'FAIL'}", kind="verifier", hr=True)]
        reason = update.get("verdict_reason", "")
        out.append(Line(
            clip(reason) or "(无)", kind="verifier", role="output",
            label=logfmt.CONCLUSION, log_text=reason,
        ))
        if update.get("verdict_raw"):
            out.append(Line(
                kind="verifier", raw=True, log_text=update["verdict_raw"], console=False,
            ))
        return out

    def _synth(self, update: dict) -> list[Line]:
        return [
            # 日志里用「汇总」做 tag：它和 plan 都以 `[主 agent]` 开头，
            # 只有分开写，回看历史时才能标成 synth 而不是 plan
            Line("[主 agent] 最终答复", kind="synth", hr=True, log_tag="汇总"),
            Line(update.get("answer") or "(空)", kind="synth", role="output", indent=0),
        ]
