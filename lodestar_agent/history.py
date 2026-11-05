"""运行历史：左侧导航「项目 → 会话」的数据源。

两层来源，各管一段：

- `history.json`：本次改造后每次运行都会落一条，记 workdir / 模型 / 状态 / 时长，
  这些信息 checkpoint 里没有。
- `checkpoints.sqlite`：只有老记录（json 里没有的 thread）才回填一次，用 checkpoint
  里的 goal / records / answer 以及 checkpoint_id 里的时间戳重建。这样升级后左侧
  不会是一片空白。

一个目录就是一个「项目」，它下面可以有任意多个会话（thread）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_MAX_ITEMS = 300
_FILE_NAME = "history.json"

# UUIDv6 的时间戳从 1582-10-15 起算，单位 100ns
_UUID_EPOCH = 0x01B21DD213814000

_STATE_BY_CODE = {
    0: "done",
    1: "failed",
    2: "error",
    3: "failed",
    130: "aborted",
}


def uuid_time(checkpoint_id: str) -> float:
    """从 UUIDv6 的 checkpoint_id 还原时间；解析不出来就返回 0。

    LangGraph 的 checkpoint_id 是 uuid6，前 60 bit 就是时间。老记录没有 json，
    靠这个才能排出正确的先后顺序，而不是靠文件 mtime 猜。
    """
    h = (checkpoint_id or "").replace("-", "")
    if len(h) < 16:
        return 0.0
    try:
        ts = (int(h[:8], 16) << 28) | (int(h[8:12], 16) << 12) | int(h[13:16], 16)
    except ValueError:
        return 0.0
    unix = (ts - _UUID_EPOCH) / 1e7
    return unix if 1_000_000_000 < unix < 4_000_000_000 else 0.0


def _ro_uri(path: Path) -> str:
    """只读 URI：走 as_uri 让空格 / 中文等字符正确转义，手拼容易在带空格的目录上失败。"""
    return path.expanduser().resolve().as_uri() + "?mode=ro"


def read_checkpoint(db_path: Path, thread: str) -> dict:
    """读一个 thread 的最新 state。失败一律返回空，历史列表不能因为读盘出错而挂掉。"""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(_ro_uri(db_path), uri=True, check_same_thread=False)
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            tup = SqliteSaver(conn).get_tuple({"configurable": {"thread_id": thread}})
            if tup is None:
                return {}
            values = (tup.checkpoint or {}).get("channel_values") or {}
            return {
                "goal": str(values.get("goal") or ""),
                "answer": str(values.get("answer") or ""),
                "records": list(values.get("records") or []),
                "pending": list(values.get("plan") or []),
                "current": values.get("current") or {},
                # 边界退出的线程会正常走到 END（next 为空），只能靠状态标记判断能不能续；
                # 执行失败留下的 last_error / 未完成写入同样说明还有活没干完。
                "abort": bool(values.get("abort")),
                "draining": bool(values.get("draining")),
                "last_error": str(values.get("last_error") or ""),
                "has_pending": bool(tup.pending_writes),
            }
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 读历史失败不该影响页面
        return {}


def drop_checkpoints(db_path: Path, thread: str) -> bool:
    """删掉一个 thread 在 checkpoint 库里的全部行。删到东西返回 True。

    走可写的连接而不是 `_ro_uri`；给 timeout 是怕正好撞上别的进程在写，
    宁可这里失败让调用方报错，也不要无限期卡住页面请求。
    """
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
    except sqlite3.Error:
        return False
    try:
        with conn:  # 同一个事务：writes 与 checkpoints 要么都删，要么都不删
            n = conn.execute("delete from writes where thread_id = ?", (thread,)).rowcount
            n += conn.execute("delete from checkpoints where thread_id = ?", (thread,)).rowcount
        return (n or 0) > 0
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _scan_checkpoints(db_path: Path) -> list[dict]:
    """扫 checkpoints 表，按 thread 聚合出老记录。"""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(_ro_uri(db_path), uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "select thread_id, checkpoint_id from checkpoints order by thread_id, checkpoint_id"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    seen: dict[str, dict] = {}
    for thread, cid in rows:
        ts = uuid_time(cid)
        item = seen.get(thread)
        if item is None:
            seen[thread] = {
                "thread": thread,
                "started": ts,
                "ended": ts,
                "latest": cid,
            }
        else:
            if ts:
                item["started"] = min(item["started"] or ts, ts)
                item["ended"] = max(item["ended"], ts)
            if (cid or "") > (item["latest"] or ""):
                item["latest"] = cid
    return list(seen.values())


class HistoryStore:
    """history.json 的读写。一次只有一个会话在跑，仍加锁以便测试并发调用。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / _FILE_NAME
        self._lock = threading.Lock()

    # ── 底层读写 ──────────────────────────────────────────
    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"version": 1, "items": []}
        if not isinstance(data, dict):
            return {"version": 1, "items": []}
        if not isinstance(data.get("items"), list):
            data["items"] = []
        return data

    def _write(self, data: dict) -> None:
        items = sorted(
            data.get("items") or [], key=lambda x: x.get("started") or 0, reverse=True
        )[:_MAX_ITEMS]
        data["version"] = 1
        data["items"] = items
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ── 写入 ──────────────────────────────────────────────
    def begin(
        self,
        thread: str,
        goal: str,
        workdir: str,
        *,
        worker_model: str = "",
        verifier_model: str = "",
        dry_run: bool = False,
        resume: bool = False,
        log_path: str = "",
    ) -> None:
        now = time.time()
        with self._lock:
            data = self._read()
            items = data.setdefault("items", [])
            item = next((x for x in items if x.get("thread") == thread), None)
            if item is None:
                item = {"thread": thread, "started": now}
                items.append(item)
            item.update({
                "goal": goal,
                "workdir": workdir,
                "state": "running",
                "code": None,
                "ended": 0.0,
                "error": "",
                "workerModel": worker_model,
                "verifierModel": verifier_model,
                "dryRun": bool(dry_run),
                "resume": bool(resume),
                "log": log_path,
            })
            if not item.get("started"):
                item["started"] = now
            self._write(data)

    def finish(
        self,
        thread: str,
        code: int | None,
        *,
        answer: str = "",
        passed: int | None = None,
        failed: int | None = None,
        error: str = "",
    ) -> None:
        now = time.time()
        state = _STATE_BY_CODE.get(code if code is not None else 2, "error")
        with self._lock:
            data = self._read()
            item = next((x for x in data.get("items", []) if x.get("thread") == thread), None)
            if item is None:
                return
            item.update({
                "state": state,
                "code": code,
                "ended": now,
                "duration": round(now - (item.get("started") or now), 1),
                "answer": (answer or "").strip()[:2000],
                "error": (error or "")[:500],
            })
            if passed is not None:
                item["passed"] = passed
            if failed is not None:
                item["failed"] = failed
            self._write(data)

    def touch_goal(self, thread: str, goal: str) -> None:
        """续跑时 goal 以 checkpoint 里的为准，纠正一开始占位的那条。"""
        if not goal:
            return
        with self._lock:
            data = self._read()
            item = next((x for x in data.get("items", []) if x.get("thread") == thread), None)
            if item is None or item.get("goal") == goal:
                return
            item["goal"] = goal
            self._write(data)

    # ── 删除 ──────────────────────────────────────────────
    def delete(self, db_path: Path, thread: str) -> bool:
        """删掉一条历史：json 里的条目 + 这个 thread 的 checkpoint。

        checkpoint 必须跟着删：`_merged()` 会拿库里的老 thread 回填没记录过的会话，
        只删 json 的话刷新一下它就又冒出来了（还标成 bootstrap）。删到东西返回 True。
        """
        removed = False
        with self._lock:
            data = self._read()
            items = data.get("items") or []
            keep = [x for x in items if x.get("thread") != thread]
            if len(keep) != len(items):
                data["items"] = keep
                self._write(data)
                removed = True
        if drop_checkpoints(db_path, thread):
            removed = True
        return removed

    # ── 读取 ──────────────────────────────────────────────
    def items(self) -> list[dict]:
        with self._lock:
            return list(self._read().get("items") or [])

    def _merged(self, db_path: Path) -> list[dict]:
        """json 的记录 + checkpoint 里还没被记录过的老 thread。"""
        items = {x.get("thread"): dict(x) for x in self.items() if x.get("thread")}
        for old in _scan_checkpoints(db_path):
            tid = old["thread"]
            if tid in items:
                continue
            snap = read_checkpoint(db_path, tid)
            if not snap:
                continue
            records = snap.get("records") or []
            passed = sum(1 for r in records if r.get("passed") and r.get("verified", True))
            failed = sum(
                1 for r in records if not r.get("passed") and r.get("verified", True)
            )
            # 老记录没有 json 里的退出码：按 checkpoint 的最终形态反推，别一律标成已完成
            error = ""
            if snap.get("last_error"):
                state, code = "failed", 3
                error = snap["last_error"][:500]
            elif snap.get("abort") or snap.get("draining") or snap.get("has_pending"):
                state, code = "aborted", 130
            elif failed:
                state, code = "failed", 1
            else:
                state, code = "done", 0
            items[tid] = {
                "thread": tid,
                "goal": snap.get("goal", ""),
                "workdir": "",
                "state": state,
                "code": code,
                "started": old["started"],
                "ended": old["ended"],
                "duration": round(old["ended"] - old["started"], 1) if old["started"] else 0,
                "answer": (snap.get("answer") or "")[:2000],
                "error": error,
                "workerModel": "",
                "verifierModel": "",
                "bootstrap": True,
                "passed": passed,
                "failed": failed,
            }
        return list(items.values())

    def groups(self, db_path: Path, default_workdir: str, current: str = "") -> dict:
        """按 workdir 分组，组内按时间倒序。workdir 缺失的老记录归到默认目录。"""
        items = self._merged(db_path)
        buckets: dict[str, list[dict]] = {}
        for item in items:
            wd = str(item.get("workdir") or "").strip() or default_workdir
            item = dict(item)
            item["workdir"] = wd
            # 进程被强杀时 json 里会留着 running；不是当前这次就是中断了
            if item.get("state") == "running" and item.get("thread") != current:
                item["state"] = "aborted"
                item["interrupted"] = True
            buckets.setdefault(wd, []).append(item)

        projects = []
        for wd, group in buckets.items():
            group.sort(key=lambda x: x.get("started") or 0, reverse=True)
            projects.append({
                "workdir": wd,
                "name": Path(wd).name or wd,
                "count": len(group),
                "updated": group[0].get("started") or 0,
                "passed": sum(1 for x in group if x.get("state") == "done"),
                "items": group,
            })
        projects.sort(key=lambda p: p["updated"], reverse=True)
        return {
            "ok": True,
            "total": sum(p["count"] for p in projects),
            "projects": projects,
        }

    def detail(self, db_path: Path, thread: str, default_workdir: str) -> dict | None:
        for item in self._merged(db_path):
            if item.get("thread") != thread:
                continue
            out = dict(item)
            out["workdir"] = out.get("workdir") or default_workdir
            snap = read_checkpoint(db_path, thread)
            out["resumable"] = False
            if snap:
                out["goal"] = out.get("goal") or snap.get("goal", "")
                out["answer"] = out.get("answer") or (snap.get("answer") or "")[:2000]
                out["error"] = out.get("error") or (snap.get("last_error") or "")[:500]
                out["current"] = snap.get("current") or {}
                out["pending"] = snap.get("pending") or []
                out["hasMore"] = bool(snap.get("has_pending"))
                # 页面只按这个字段决定「继续这个会话」能不能点。next 不由 checkpoint
                # 直接存（它是按图即时算的），所以这里看真实状态标记：边界退出、未完成
                # 写入、执行失败留下的 last_error 都可以续；正常跑完的线程续不了。
                out["resumable"] = bool(
                    snap.get("abort") or snap.get("draining")
                    or snap.get("has_pending") or snap.get("last_error")
                )
                records = snap.get("records") or []
                latest: dict[str, dict] = {}
                for r in records:
                    latest[str(r.get("task", ""))] = r
                out["tasks"] = [{
                    "task": r.get("task", ""),
                    "verify": r.get("verify", ""),
                    "status": ("pass" if r.get("passed") else "fail")
                    if r.get("verified", True) else "skip",
                    "attempts": r.get("attempts", 1),
                    "duration": r.get("duration", 0.0),
                    "reason": r.get("reason", ""),
                } for r in latest.values()]
            return out
        return None
