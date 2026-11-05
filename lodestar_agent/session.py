"""一次运行的执行体，以及「页面驱动」需要的会话包装。

- `Console`     终端 + 页面双写（kind 决定页面上的标签与色条）
- `run_session` 同步跑一次完整流程，返回退出码；CLI 和页面线程都走它
- `Runner`      线程包装：页面提交目标 → 后台跑 → 可查询进度 / 可边界退出

为什么单独一层：命令行模式下 `main()` 是同步阻塞的，页面模式下同一个执行体必须能
在后台线程里跑、还要能被页面查询状态。把这些从入口里抽出来，入口才可能「只起页面」。
"""

from __future__ import annotations

import datetime
import os
import secrets
import sqlite3
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from .config import Config, load_config
from .control import Control
from .graph import AgentFailure, build_graph
from .history import HistoryStore
from .worker import terminate_all_cli

_DISPLAY_LIMIT = 1200


def new_thread_id() -> str:
    """默认 thread 名：`run_<时间戳>_<6 位随机>`。

    时间戳只到秒，并发起两个会撞成同一个 thread（checkpoint 主键冲突，
    第二个直接被「线程冲突」挡回来）。补随机位把它变成不撞：3 字节 = 1677 万
    种取值，同一秒内并发撞上的概率可以忽略。
    不用 goal 的 hash：同一目标重跑会算出同一个 id，反而必撞。
    """
    return f"run_{datetime.datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}"


@dataclass(frozen=True)
class StartRequest:
    """一次运行的入参。None 与空串的区别是契约的一部分，别随手写成 `or ""`。

    - `worker_model` / `verifier_model`：None = 沿用配置；"" = 显式用 CLI 默认模型
    - `workdir`：None / 空串 = 回到配置里的默认产出目录
    - `seed_notes`：开跑前写好的引导，在 `reset()` 之后补投（否则会被清空）
    """

    goal: str = ""
    thread: str | None = None
    resume: bool = False
    worker_model: str | None = None
    verifier_model: str | None = None
    workdir: str | None = None
    seed_notes: tuple[dict, ...] = ()

    def normalized(self) -> "StartRequest":
        """把用户输入规整成可执行的值；不合法就抛 ValueError（由调用方转成 400）。"""
        goal = (self.goal or "").strip()
        if not goal and not self.resume:
            raise ValueError("目标不能为空")
        return replace(self, goal=goal, thread=(self.thread or "").strip() or None)


class Console:
    """终端 + 页面双写。

    kind 决定页面上的标签与左边色条：plan / worker / verifier / synth / stop / system。
    role 决定这一条属于哪一类内容，页面据此区分「节点收到的 prompt / 节点的输出 / 旁白日志」：
    prompt（模型调用真正收到的 prompt 原文）/ task（从结果里摘出的任务与验收）/
    output（节点产出）/ log（流程说明）/ guide（用户投递的引导）。

    终端行为与改造前保持一致（分隔线、缩进、截断），页面拿到的内容更全（前端自己做折叠）。
    """

    def __init__(self, control: Control | None = None):
        self.control = control

    def hr(self, title: str = "", kind: str = "system") -> None:
        line = "─" * 62
        print(f"\n{line}")
        if title:
            print(title)
            print(line)
        if self.control and title:
            # 章节标题 / 退出提示都是脚本写的流程旁白，不是模型说的话，统一归「旁白」
            self.control.emit(title, kind=kind, hr=True, role="log")

    def say(self, body: str, kind: str = "system", indent: int = 2,
            role: str = "log") -> None:
        body = "" if body is None else str(body)
        print(textwrap.indent(body, " " * indent) if indent else body)
        if self.control:
            self.control.emit(body, kind=kind, role=role)


def open_log():
    os.makedirs("logs", exist_ok=True)
    path = os.path.abspath(f"logs/run_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
    return open(path, "a", encoding="utf-8"), path


def _w(logf, text: str) -> None:
    logf.write(text.rstrip("\n") + "\n")
    logf.flush()


def _clip(text: str, limit: int = _DISPLAY_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"…(已截断，共 {len(text)} 字)"


def _task_status(record: dict) -> str:
    if not record.get("verified", True):
        return "skip"          # 跑了 worker 没来得及验收
    return "pass" if record.get("passed") else "fail"


def build_tasks(state: dict) -> list[dict]:
    """把 state 摊成页面上的任务清单：已完成 + 当前 + 剩余。

    已完成按任务文本去重（同一条被打回或重跑过只留最后一次），
    否则清单里会出现好几条同名任务，看着像重复派工。
    """
    records = state.get("records") or []
    latest: dict[str, dict] = {}
    for r in records:
        latest[str(r.get("task", ""))] = r

    tasks: list[dict] = []
    for r in latest.values():
        tasks.append({
            "task": r.get("task", ""),
            "verify": r.get("verify", ""),
            "status": _task_status(r),
            "attempts": r.get("attempts", 1),
            "duration": r.get("duration", 0.0),
            "reason": r.get("reason", ""),
        })

    current = state.get("current")
    if current:
        tasks.append({
            "task": current.get("task", ""),
            "verify": current.get("verify", ""),
            "status": "running",
            "attempts": state.get("attempts", 0) or 1,
            "duration": state.get("last_duration", 0.0),
            "reason": "",
        })

    for item in state.get("plan") or []:
        tasks.append({
            "task": item.get("task", ""),
            "verify": item.get("verify", ""),
            "status": "pending",
            "attempts": 0,
            "duration": 0.0,
            "reason": "",
        })
    return tasks


class SessionError(RuntimeError):
    """这次运行起不来（线程冲突 / checkpoint 不存在 / 已跑完），不是流程本身失败。"""


def _prepare_resume(app, run_config, cfg, thread_id: str, goal: str) -> tuple[str, str]:
    """处理续跑：把 checkpoint 收拾成「能继续跑」的样子。返回 (goal, 给用户看的提示)。

    三种接法对应 aborted_stage：worker 重跑、verify 直接补验收（不重跑 worker）、done 跳过。
    """
    snapshot = app.get_state(run_config)
    if not snapshot.values:
        raise SessionError(f"找不到线程 {thread_id} 的 checkpoint")
    goal = str(snapshot.values.get("goal") or goal)
    if snapshot.next:
        if snapshot.values.get("last_error"):
            # 上次是「执行失败终止」：attempts 已顶到上限，不重置会立刻再撞一次
            app.update_state(run_config, {"attempts": 0, "last_error": ""}, as_node="verify")
            return goal, "上次是执行失败终止，已重置当前任务的重试计数"
        return goal, ""

    # 边界退出的线程是正常走到 END 的，next 为空；要续跑得先把它「重开」
    values = snapshot.values
    if not (values.get("abort") or values.get("draining")):
        raise SessionError(f"线程 {thread_id} 已经跑完，没有待执行的节点；要重跑请换一个 thread")
    plan = list(values.get("plan") or [])
    task = values.get("aborted_task") or {}
    stage = values.get("aborted_stage") or ("worker" if task else "done")
    attempts = int(values.get("attempts") or 0) or 1
    clear = {
        "abort": False, "draining": False, "abort_reason": "",
        "aborted_task": {}, "aborted_stage": "", "feedback": "", "last_error": "",
    }
    if task and stage == "verify":
        # worker 已经跑完、只差验收：假装 worker 刚结束，next 直接落到 verify
        app.update_state(
            run_config,
            {**clear, "current": task, "plan": plan, "attempts": attempts},
            as_node="worker",
        )
        return goal, "上次是边界退出（worker 已跑完），这次直接补验收"
    if task:
        app.update_state(
            run_config,
            {**clear, "current": None, "plan": [task] + plan, "attempts": 0},
            as_node="verify",
        )
        return goal, "上次是边界退出，被中断的任务已排回队首"
    app.update_state(
        run_config, {**clear, "current": None, "plan": plan, "attempts": 0}, as_node="verify"
    )
    return goal, "上次是边界退出，已完成的任务不会重做"


def run_session(
    cfg: Config,
    goal: str,
    thread_id: str,
    *,
    control: Control,
    con: Console,
    resume: bool = False,
    dry_run: bool = False,
) -> int:
    """同步跑一次完整流程，返回退出码。

    调用方负责控制台服务的生命周期；这里只跑图、写日志、清理 CLI 子进程。
    """
    db_path = cfg.root / "checkpoints.sqlite"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)
    saver.setup()

    app = build_graph(cfg, use_llm=not dry_run, checkpointer=saver, control=control)
    run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 120}

    logf = None
    code = 0
    store = HistoryStore(cfg.root)
    hist_thread: str | None = None
    answer = ""
    done = 0
    failed = 0
    hist_error = ""
    try:
        resume_note = ""
        if resume:
            try:
                goal, resume_note = _prepare_resume(app, run_config, cfg, thread_id, goal)
            except SessionError as exc:
                con.say(f"[续跑失败] {exc}", kind="stop")
                print(f"[续跑失败] {exc}", file=sys.stderr)
                return 2
            if resume_note:
                con.say(f"提示    : {resume_note}", kind="system")
        else:
            snapshot = app.get_state(run_config)
            if snapshot.values:
                msg = (
                    f"thread_id {thread_id} 已有运行记录；续跑请勾选「续跑」，"
                    "重跑请换一个 thread"
                )
                con.say(f"[线程冲突] {msg}", kind="stop")
                print(f"[线程冲突] {msg}", file=sys.stderr)
                return 2

        logf, log_path = open_log()
        # 历史记录在这里落盘：此时 goal 已按续跑规则修正过，workdir 也已定
        store.begin(
            thread_id, goal, str(cfg.worker.workdir),
            worker_model=cfg.worker.model, verifier_model=cfg.verifier.model,
            dry_run=dry_run, resume=resume, log_path=str(log_path),
        )
        hist_thread = thread_id
        _w(logf, f"目标: {goal}")
        _w(logf, f"thread: {thread_id}" + ("（续跑）" if resume else ""))
        _w(logf, f"checkpoint: {db_path}")
        if resume_note:
            _w(logf, f"resume: {resume_note}")
        _w(logf, f"主 agent: {cfg.main_agent.model} @ {cfg.main_agent.base_url}")
        _w(logf, f"worker: {cfg.worker.cli} (model={cfg.worker.model or 'CLI 默认'}, "
                f"tools={cfg.worker.allowed_tools}, cwd={cfg.worker.workdir}, "
                f"max_turns={cfg.worker.max_turns or 'CLI 默认'})")
        _w(logf, f"验收: {cfg.verifier.cli} (model={cfg.verifier.model or 'CLI 默认'}, "
                f"tools={cfg.verifier.allowed_tools}, "
                f"cwd={cfg.worker.workdir}/{cfg.verifier.scratch_dir}, "
                f"max_rounds={cfg.verifier.max_rounds})")
        _w(logf, "")

        con.say(f"目标    : {goal}", kind="system", indent=0)
        con.say(
            f"主 agent: {cfg.main_agent.model} @ {cfg.main_agent.base_url}",
            kind="system", indent=0,
        )
        con.say(
            f"worker  : {cfg.worker.cli} (model={cfg.worker.model or 'CLI 默认'}, "
            f"cwd={cfg.worker.workdir})",
            kind="system", indent=0,
        )
        con.say(
            f"验收    : {cfg.verifier.cli} (model={cfg.verifier.model or 'CLI 默认'}, "
            f"max_rounds={cfg.verifier.max_rounds})",
            kind="system", indent=0,
        )
        if dry_run:
            con.say("模式    : dry-run（确定性决策代替主 agent，跳过验收）", kind="system", indent=0)
        con.say(f"thread  : {thread_id}" + ("（续跑）" if resume else ""), kind="system", indent=0)

        control.set_stage("开始执行")
        # 先推一次：续跑时页面上要能立刻看到已有的任务记录
        control.tasks_event(build_tasks(app.get_state(run_config).values or {}))
        done = 0
        current_task = ""
        initial = None if resume else {
            "goal": goal, "plan": [], "current": None,
            "attempts": 0, "feedback": "", "records": [],
        }

        for chunk in app.stream(initial, run_config, stream_mode="updates"):
            for node, update in chunk.items():
                update = update or {}
                if node == "plan":
                    if update.get("stage") == "abort":
                        con.hr("[主 agent] 边界退出 → 收尾", kind="stop")
                        if update.get("note"):
                            con.say(update["note"], kind="stop")
                        _w(logf, "=" * 70)
                        _w(logf, "[主 agent] 边界退出")
                        if update.get("note"):
                            _w(logf, update["note"])
                    elif update.get("stage") == "init":
                        con.hr("[主 agent] 拆解任务方案", kind="plan")
                        if update.get("note"):
                            con.say(update["note"], kind="plan")
                        items = ([update.get("current")] if update.get("current") else []) + (
                            update.get("plan") or []
                        )
                        for i, item in enumerate(items, 1):
                            if not item:
                                continue
                            con.say(f"{i}. {item['task']}", kind="plan", role="task")
                            con.say(
                                f"   验收: {item.get('verify') or '(未给出可执行标准，由验收 agent 自行设计)'}",
                                kind="plan", role="task",
                            )
                        current_task = (update.get("current") or {}).get("task", "")
                        _w(logf, "=" * 70)
                        _w(logf, "[主 agent] 拆解任务方案")
                        if update.get("note"):
                            _w(logf, "note: " + update["note"])
                        for i, item in enumerate(items, 1):
                            if item:
                                _w(logf, f"{i}. {item['task']}")
                                _w(logf, f"   验收: {item.get('verify') or '(未给出)'}")
                        if update.get("plan_raw"):
                            _w(logf, "--- 模型原始输出 ---")
                            _w(logf, update["plan_raw"])
                    else:
                        for r in update.get("records", []) or []:
                            done += 1
                            if not r.get("passed"):
                                failed += 1
                        action = update.get("action", "")
                        label = "继续下一个" if action == "dispatch" else "收尾"
                        con.hr(
                            f"[主 agent] 动态规划 → {label}（完成 {done} 条 / 失败 {failed}）",
                            kind="plan",
                        )
                        if update.get("note"):
                            con.say(update["note"], kind="plan")
                        _w(logf, "=" * 70)
                        _w(logf, f"[主 agent] 动态规划 → {label}（完成 {done} 条 / 失败 {failed}）")
                        if update.get("note"):
                            _w(logf, "note: " + update["note"])
                        if update.get("decision_raw"):
                            _w(logf, "--- 模型原始输出 ---")
                            _w(logf, update["decision_raw"])
                        if action == "dispatch" and update.get("current"):
                            current_task = update["current"].get("task", "")
                elif node == "worker":
                    if update.get("skipped"):
                        con.hr(f"[worker] 已中止 · 这一轮未执行：{current_task}", kind="stop")
                        _w(logf, "=" * 70)
                        _w(logf, f"[worker] 已中止 · 这一轮未执行：{current_task}")
                    else:
                        con.hr(
                            f"[worker] 执行  第 {done + 1} 条 · 第 {update.get('attempts', 1)} 轮"
                            + ("（被中止）" if update.get("abort") else ""),
                            kind="worker",
                        )
                        body = _clip(update.get("last_output") or update.get("last_error") or "")
                        con.say(f"{update.get('last_duration', 0)}s\n{body or '(无输出)'}",
                                kind="worker", role="output")
                        _w(logf, "=" * 70)
                        _w(logf, f"[worker] 执行 第 {done + 1} 条 第 {update.get('attempts', 1)} 轮"
                                 f"（{update.get('last_duration', 0)}s）")
                        _w(logf, f"任务: {current_task}")
                        if update.get("last_output"):
                            _w(logf, "输出:\n" + update["last_output"])
                        if update.get("last_error"):
                            _w(logf, "错误: " + update["last_error"])
                elif node == "verify":
                    if update.get("abort"):
                        con.hr("[verifier] 超过等待上限，验收被强制终止", kind="stop")
                        _w(logf, "=" * 70)
                        _w(logf, "[verifier] 超过等待上限，验收被强制终止")
                        continue
                    if update.get("draining") and not update.get("verdict_ready"):
                        con.hr("[verifier] 已跳过（收到退出请求，不再开始新的模型调用）",
                               kind="stop")
                        _w(logf, "=" * 70)
                        _w(logf, "[verifier] 已跳过（收到退出请求）")
                        continue
                    passed = update.get("verdict_passed")
                    con.hr(f"[verifier] 验收 → {'PASS' if passed else 'FAIL'}", kind="verifier")
                    con.say(_clip(update.get("verdict_reason", "")) or "(无)", kind="verifier",
                            role="output")
                    _w(logf, "=" * 70)
                    _w(logf, f"[verifier] 验收 → {'PASS' if passed else 'FAIL'}")
                    _w(logf, "结论: " + update.get("verdict_reason", ""))
                    if update.get("verdict_raw"):
                        _w(logf, "--- 模型原始输出 ---")
                        _w(logf, update["verdict_raw"])
                elif node == "synthesize":
                    answer = update.get("answer", "")
            # 每批节点更新后刷新任务清单：读 checkpoint 比从 update 里拼更准
            # （records 是累加的、current/plan 会被后续节点改写）
            control.tasks_event(build_tasks(app.get_state(run_config).values or {}))

        final = app.get_state(run_config).values or {}
        drained = bool(final.get("abort") or final.get("draining"))

        con.hr("[主 agent] 最终答复", kind="synth")
        con.say(answer or "(空)", kind="synth", indent=0, role="output")
        _w(logf, "=" * 70)
        _w(logf, "[主 agent] 最终答复")
        _w(logf, answer or "(空)")

        if drained:
            control.status("aborted", control.stop_reason)
            control.emit("已按边界退出：当前这一轮已完整结束，checkpoint 已保存", kind="stop")
            code = 130
        else:
            control.status("failed" if failed else "done")
            code = 1 if failed else 0

    except AgentFailure as exc:
        con.hr("[流程终止] 模型请求失败", kind="stop")
        con.say(f"失败节点: {exc.node}", kind="stop")
        con.say(f"可能原因: {exc.reason}", kind="stop")
        if logf:
            _w(logf, "=" * 70)
            _w(logf, "[流程终止] 模型请求失败")
            _w(logf, f"失败节点: {exc.node}")
            _w(logf, f"可能原因: {exc.reason}")
        control.status("failed", f"{exc.node}: {exc.reason}")
        control.emit(f"流程终止于 {exc.node}：{exc.reason}", kind="stop")
        hist_error = f"{exc.node}: {exc.reason}"
        code = 3
    except KeyboardInterrupt:
        if logf:
            _w(logf, "\n[已中断]")
        control.status("aborted", "终端中断（Ctrl+C）")
        control.emit("已中断，checkpoint 保留", kind="stop")
        hist_error = "终端中断（Ctrl+C）"
        code = 130
    finally:
        if hist_thread:
            store.finish(
                hist_thread, code,
                answer=answer,
                passed=max(0, done - failed), failed=failed,
                error=hist_error,
            )
        # 收尾必须落到「连接断开」这一层：只让 Python 退出，CLI 里的模型请求
        # 还会在服务端继续生成，token 照样烧。杀进程组 = 断连接。
        leftover = terminate_all_cli()
        if leftover:
            con.say(f"[收尾] 已终止 {leftover} 个仍在运行的 CLI 子进程组（模型连接随之断开）",
                    kind="stop")
            if logf:
                _w(logf, f"[收尾] 已终止 {leftover} 个仍在运行的 CLI 子进程组")
        if logf:
            logf.close()
        conn.close()

    if code == 130:
        con.say(f"可续跑: 在页面上填 thread「{thread_id}」并勾选续跑", kind="stop")

    return code


class Runner:
    """页面驱动的会话管理：一次只跑一个，正在跑时拒绝新的。

    结束后可以再 start 一次（新 thread），Control 会在 start 时被 reset，
    页面收到 reset 事件后清屏重来。
    """

    def __init__(self, cfg: Config, control: Control, dry_run: bool = False,
                 config_path: str | Path | None = None):
        self.cfg = cfg
        self.control = control
        self.dry_run = dry_run
        self.config_path = Path(config_path) if config_path else None
        self.con = Console(control)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # 配置里的默认产出目录；页面每次不指定时就回到这里，避免上一次的选择长期粘住
        self._base_workdir = Path(cfg.worker.workdir)
        self._session: dict = {
            "goal": "", "thread": "", "state": "idle",
            "code": None, "started": 0.0, "ended": 0.0, "error": "",
            "workerModel": "", "verifierModel": "", "workdir": str(cfg.worker.workdir),
        }

    # ── 查询 ──────────────────────────────────────────────
    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    @property
    def session(self) -> dict:
        with self._lock:
            return dict(self._session)

    # ── 控制 ──────────────────────────────────────────────
    def start(self, req: StartRequest) -> dict:
        """开始一次运行。一次只跑一个，所以直接改 cfg 上那几个字段是安全的。"""
        req = req.normalized()
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("已有任务在运行，先等它结束或先点边界退出")
            if req.worker_model is not None:
                self.cfg.worker.model = req.worker_model.strip()
            if req.verifier_model is not None:
                self.cfg.verifier.model = req.verifier_model.strip()
            self.cfg.worker.workdir = self._resolve_workdir(req.workdir)
            tid = req.thread or new_thread_id()
            self.control.reset()
            # 开跑前的引导必须在这里补投：reset 刚把信箱清空，
            # 而线程一旦启动就可能立刻进入第一个节点去取。
            self.control.push_notes(req.seed_notes)
            self._session = {
                "goal": req.goal, "thread": tid, "state": "running",
                "code": None, "started": time.time(), "ended": 0.0, "error": "",
                "workerModel": self.cfg.worker.model,
                "verifierModel": self.cfg.verifier.model,
                "workdir": str(self.cfg.worker.workdir),
            }
            info = dict(self._session)
            self._thread = threading.Thread(
                target=self._body, args=(req.goal, tid, req.resume),
                name="lodestar-session", daemon=True,
            )
            self._thread.start()
        self.control.session_event("running", goal=req.goal, thread=tid,
                                   workdir=str(self.cfg.worker.workdir))
        return info

    def _resolve_workdir(self, workdir: str | None) -> Path:
        """把页面传来的产出目录变成绝对路径；不存在就建，建不了就说清楚原因。"""
        raw = (workdir or "").strip()
        if not raw:
            return self._base_workdir
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.cfg.root / path
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"产出目录无法创建：{path}（{exc.strerror or exc}）") from exc
        if not path.is_dir():
            raise ValueError(f"产出目录不是文件夹：{path}")
        return path.resolve()

    def _body(self, goal: str, thread_id: str, resume: bool) -> None:
        code = 2
        err = ""
        try:
            code = run_session(
                self.cfg, goal, thread_id,
                control=self.control, con=self.con, resume=resume, dry_run=self.dry_run,
            )
        except BaseException as exc:  # noqa: BLE001 - 线程里不能把异常丢了
            err = f"{type(exc).__name__}: {exc}"
            code = 2
            self.control.emit(f"运行异常：{err}", kind="stop")
        finally:
            state = {
                0: "done", 1: "failed", 2: "error", 3: "failed", 130: "aborted",
            }.get(code, "failed")
            with self._lock:
                self._session.update(
                    state=state, code=code, ended=time.time(), error=err
                )
                info = dict(self._session)
            self.control.session_event(state, **{
                "goal": info["goal"], "thread": info["thread"],
                "code": code, "error": err,
                "workdir": info.get("workdir", ""),
            })

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread:
            thread.join(timeout)

    def reload_config(self) -> Config:
        """保存配置后重新加载。

        运行中直接拒绝：跑到一半换模型、换 workdir、换 CLI，会让同一个任务前后半段
        跑在不同环境里，出问题极难复现。等它结束再改，代价只是点一下。
        """
        if not self.config_path:
            raise RuntimeError("没有配置文件路径，无法重载配置")
        if self.busy:
            raise RuntimeError("任务正在运行，等它结束再改配置")
        self.cfg = load_config(self.config_path)
        self._base_workdir = Path(self.cfg.worker.workdir)
        return self.cfg
