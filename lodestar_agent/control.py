"""进程内控制总线：引导信箱 + 边界退出标志 + 事件广播。

这一层是「人」与「图」之间唯一的通道，页面上的一切操作都落在这里：

- **引导信箱（note）**：分 `main` / `worker` 两个收件人。
  投递时不打断任何正在跑的东西，只入队；由具体节点在**自己的边界**取走（take_notes）。
  worker 正在执行时投递，会等它这一轮跑完、下一次被调用时才拼进 prompt —— 这正是
  「引导但不打断」的语义。消息是「消费即清空」，投一次只生效一次。

- **停止标志（stop_requested）**：只置位、不杀进程。语义是「边界退出」——
  正在跑的那一次模型回复**跑完再说**，下一个模型回复开始前收工。
  真正的收尾由三处配合完成：节点入口（不再开始新的调用）、节点出口（跑完了就转收尾）、
  `synth_node`（产出中止摘要）。只有等太久（`drain_timeout`）才会强杀当前子进程。

- **事件总线（EventBus）**：节点内的每一次打印都 publish 一条带 seq 的事件，
  server 用 SSE 推给页面；seq 单调递增，断线重连可按 since 重放，页面侧再按 seq 去重。
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import deque


class EventBus:
    """单进程内的多订阅者广播，带环形 backlog 供断线重连重放。"""

    def __init__(self, backlog: int = 3000, queue_size: int = 4000):
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._backlog: deque[dict] = deque(maxlen=backlog)
        self._queue_size = queue_size
        self._seq = 0

    def publish(self, event: dict) -> dict:
        with self._lock:
            self._seq += 1
            event = {**event, "seq": self._seq, "ts": time.time()}
            self._backlog.append(event)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # 订阅者（浏览器连接）读得太慢就丢给它，不拖累主流程
                pass
        return event

    def subscribe(self, since: int = 0) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            replay = [e for e in self._backlog if e["seq"] > since]
            self._subs.append(q)
        for e in replay:
            try:
                q.put_nowait(e)
            except queue.Full:
                break
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def clear(self) -> None:
        """丢掉历史事件（新一轮开始时用）。已连接的订阅者不受影响，只是拿不到旧事件。"""
        with self._lock:
            self._backlog.clear()


class Control:
    """页面与图共享的控制面。所有方法都可跨线程调用。"""

    TARGETS = ("main", "worker")

    def __init__(self, drain_timeout: float = 120.0, bus: EventBus | None = None):
        self.bus = bus or EventBus()
        # 点退出后最多再等多久（等当前这次模型回复跑完）；超时才强杀
        self.drain_timeout = max(0.0, float(drain_timeout))
        self._lock = threading.Lock()
        self._notes: dict[str, list[dict]] = {t: [] for t in self.TARGETS}
        self._stop = threading.Event()
        self._stop_reason = ""
        self._stop_ts = 0.0
        self._drain_deadline: float | None = None
        self._busy: str | None = None
        self._busy_detail = ""
        self._busy_model = ""
        self._busy_task = ""
        self._busy_attempt = 0
        self._busy_max_attempts = 0
        self._busy_since = 0.0
        self._stage = "启动"
        self._started = time.time()

    # ── 事件 ──────────────────────────────────────────────
    def emit(self, text: str, kind: str = "system", role: str = "log", **meta) -> dict:
        """kind: system / plan / worker / verifier / synth / stop
        role: prompt（prompt 原文）/ task（任务与验收摘要）/ output（节点产出）/ log（旁白）/ guide（引导）
        """
        return self.bus.publish({
            "type": "log", "kind": kind, "text": text, "meta": {**meta, "role": role},
        })

    def status(self, state: str, detail: str = "") -> dict:
        """state: starting / running / busy / draining / done / aborted / failed"""
        stopping = self._stop.is_set()
        return self.bus.publish({
            "type": "status",
            "state": state,
            "stage": self._stage,
            "detail": detail,
            "elapsed": round(time.time() - self._started, 1),
            # 当前这一次模型调用是谁在跑、用哪个模型、跑哪条任务 —— 页面顶部的工作栏靠这个渲染
            "busy": self._busy,
            "busyDetail": self._busy_detail,
            "model": self._busy_model,
            "task": self._busy_task,
            "attempt": self._busy_attempt,
            "maxAttempts": self._busy_max_attempts,
            "busySince": self._busy_since,
            "draining": stopping,
            "waited": round(time.time() - self._stop_ts, 1) if stopping else 0,
            "drainTimeout": self.drain_timeout,
            "pending": {t: len(v) for t, v in self._notes.items()},
        })

    def set_stage(self, stage: str) -> None:
        with self._lock:
            self._stage = stage
        self.status("running" if not self._stop.is_set() else "draining", stage)

    def set_busy(
        self,
        who: str | None,
        detail: str = "",
        *,
        model: str = "",
        task: str = "",
        attempt: int = 0,
        max_attempts: int = 0,
    ) -> None:
        """标记「当前正在跑哪一次模型调用」。

        who 是节点名（plan / worker / verify / synth），model 是这个节点实际用的模型。
        页面靠这几个字段渲染工作栏；引导面板也用它提示「什么时候生效」。
        """
        with self._lock:
            self._busy = who
            self._busy_detail = detail or model
            self._busy_model = model
            self._busy_task = task
            self._busy_attempt = attempt
            self._busy_max_attempts = max_attempts
            self._busy_since = time.time() if who else 0.0
        if who:
            self.emit(
                f"调用模型 {model or '(默认)'}"
                + (f"｜第 {attempt} 条" if attempt else "")
                + (f"｜{task[:60]}" if task else ""),
                kind=who,
                node=who,
            )
        if self._stop.is_set():
            self.status("draining", f"等待 {who or self._stage} 完成")
        else:
            self.status("busy" if who else "running", f"{who} {model}".strip() or self._stage)

    # ── 会话级事件 ────────────────────────────────────────
    def session_event(self, state: str, **fields) -> dict:
        """告诉页面「当前这次运行」的状态：running / done / failed / aborted / error。

        和 status 的分工：status 描述「图跑到哪一步」，session 描述「这一次运行整体」。
        页面靠 session.state == idle 判断该不该显示任务输入框。
        """
        return self.bus.publish({"type": "session", "state": state, **fields})

    def tasks_event(self, tasks: list[dict]) -> dict:
        """推送任务清单（拆解出的每一条 + 状态），页面据此渲染任务列表。

        status: pending（还没派）/ running（正在跑）/ pass / fail / skip（跑了没验收）
        """
        passed = sum(1 for t in tasks if t.get("status") == "pass")
        failed = sum(1 for t in tasks if t.get("status") == "fail")
        done = sum(1 for t in tasks if t.get("status") in ("pass", "fail", "skip"))
        return self.bus.publish({
            "type": "tasks",
            "tasks": tasks,
            "summary": {
                "total": len(tasks), "done": done, "passed": passed, "failed": failed,
            },
        })

    def reset(self) -> None:
        """开始新一次运行：清掉上一轮的停止标志、信箱、忙碌状态和计时。"""
        with self._lock:
            self._stop.clear()
            self._stop_reason = ""
            self._stop_ts = 0.0
            self._drain_deadline = None
            self._notes = {t: [] for t in self.TARGETS}
            self._busy = None
            self._busy_detail = ""
            self._busy_model = ""
            self._busy_task = ""
            self._busy_attempt = 0
            self._busy_max_attempts = 0
            self._busy_since = 0.0
            self._stage = "启动"
            self._started = time.time()
        self.bus.clear()
        self.bus.publish({"type": "reset"})
        self.status("starting", "新任务")

    # ── 引导信箱 ──────────────────────────────────────────
    def push_note(self, target: str, text: str) -> dict:
        target = target if target in self.TARGETS else "main"
        text = (text or "").strip()
        if not text:
            raise ValueError("引导内容为空")
        note = {
            "id": uuid.uuid4().hex[:8],
            "target": target,
            "text": text,
            "ts": time.time(),
            "consumed": False,
        }
        with self._lock:
            self._notes[target].append(note)
        self.bus.publish({"type": "note", "note": note})
        self.emit(
            f"收到给{'主 agent' if target == 'main' else 'worker'}的引导：{text}",
            kind="note",
            role="guide",
            note_id=note["id"],
        )
        self.status("busy" if self._busy else "running", self._stage)
        return note

    def push_notes(self, notes) -> list[dict]:
        """批量投递（开跑前的 seed 引导用）：跳过空内容和不认识的收件人。"""
        out: list[dict] = []
        for note in notes or ():
            target = str(note.get("target") or "main").strip()
            text = str(note.get("text") or "").strip()
            if text and target in self.TARGETS:
                out.append(self.push_note(target, text))
        return out

    def take_notes(self, target: str) -> list[dict]:
        """取走该收件人的全部待投递引导（消费即清空）。"""
        with self._lock:
            notes = [n for n in self._notes.get(target, []) if not n["consumed"]]
            self._notes[target] = [n for n in self._notes.get(target, []) if n["consumed"]]
        for n in notes:
            n["consumed"] = True
            n["consumed_ts"] = time.time()
        if notes:
            self.bus.publish({"type": "note_consumed", "ids": [n["id"] for n in notes]})
        return notes

    def pending_notes(self, target: str) -> list[dict]:
        with self._lock:
            return [n for n in self._notes.get(target, []) if not n["consumed"]]

    # ── 边界退出 ──────────────────────────────────────────
    def request_stop(self, reason: str = "用户请求退出") -> bool:
        """请求退出。只置标志 + 算一个「最晚等到什么时候」，不碰正在跑的进程。"""
        if self._stop.is_set():
            return False
        self._stop_reason = reason
        self._stop_ts = time.time()
        self._drain_deadline = self._stop_ts + self.drain_timeout
        self._stop.set()
        self.bus.publish({
            "type": "stop",
            "reason": reason,
            "busy": self._busy,
            "busyDetail": self._busy_detail,
            "drainTimeout": self.drain_timeout,
        })
        waiting = (
            f"等当前 {self._busy} 回复结束（{self._busy_detail or '模型调用中'}）"
            if self._busy else "当前没有在执行的调用"
        )
        self.emit(
            f"已收到退出请求（{reason}）：不再开始新的模型调用，{waiting}",
            kind="stop",
        )
        self.status("draining", f"等待 {self._busy or self._stage} 完成")
        return True

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @property
    def stop_event(self) -> threading.Event:
        """给 run_cli 用：只读，用来判断是否已过等待上限。"""
        return self._stop

    @property
    def stop_reason(self) -> str:
        return self._stop_reason

    @property
    def stop_ts(self) -> float:
        return self._stop_ts

    @property
    def drain_deadline(self) -> float | None:
        """点退出后允许当前调用跑到哪个时刻。None = 还没请求退出。"""
        return self._drain_deadline

    @property
    def busy(self) -> str | None:
        return self._busy

    @property
    def busy_detail(self) -> str:
        return self._busy_detail

    @property
    def busy_model(self) -> str:
        return self._busy_model

    @property
    def busy_task(self) -> str:
        return self._busy_task

    @property
    def busy_attempt(self) -> int:
        return self._busy_attempt

    @property
    def busy_max_attempts(self) -> int:
        return self._busy_max_attempts

    @property
    def busy_since(self) -> float:
        return self._busy_since
