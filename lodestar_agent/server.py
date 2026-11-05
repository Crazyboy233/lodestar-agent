"""HTTP API + SSE + 静态托管（FastAPI / uvicorn）。

前端是 React（`frontend/` 构建到 `static/`），这里只做三件事，不含任何页面内容：

  1. JSON 接口：`/state` / `/config` / `/command` / `/history`
  2. SSE 事件流：`/events`（带 seq，断线重连按 since 补发）
  3. 托管构建产物：`/` 和 `/assets/*`

启动方式：`ConsoleServer` 在后台线程跑 uvicorn，主线程继续等 Ctrl+C。
端口被占用时 `start()` 抛 OSError，由调用方回退到随机端口。
"""

from __future__ import annotations

import asyncio
import json
import queue
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .config import as_editable, collect_overrides, save_overrides
from .control import Control
from .fspick import PickUnavailable, pick_directory
from .history import HistoryStore, parse_log
from .session import EMPTY_SESSION, StartRequest

_STATIC = (Path(__file__).resolve().parent / "static").resolve()

# 前端每次都要拿最新的：否则改完前端重启进程，浏览器还在用旧产物，
# 表现就是「点了没反应 / 少了个按钮」，而终端一切正常，极难查。
_NO_STORE = {"Cache-Control": "no-store, must-revalidate"}


# seed 引导的接收上限：够用又不会被一段超长文本拖死
_MAX_SEED_NOTES = 4
_MAX_SEED_CHARS = 4000


def _parse_start(payload: dict) -> StartRequest:
    """把 POST 的 JSON 转成 StartRequest —— 传输层的类型转换、清洗、截断都在这。

    业务校验（目标不能为空、线程冲突）留给 `StartRequest.normalized()` 和 Runner，
    这样 CLI 与页面走的是同一套规则。
    """
    def opt_str(key: str) -> str | None:
        value = payload.get(key)
        return None if value is None else str(value)

    seeds: list[dict] = []
    raw = payload.get("seed_notes")
    if isinstance(raw, list):
        for item in raw[:_MAX_SEED_NOTES]:
            if not isinstance(item, dict):
                continue
            target = str(item.get("target") or "main").strip()
            text = str(item.get("text") or "").strip()
            if text and target in Control.TARGETS:
                seeds.append({"target": target, "text": text[:_MAX_SEED_CHARS]})

    return StartRequest(
        goal=str(payload.get("goal") or ""),
        thread=opt_str("thread"),
        resume=bool(payload.get("resume")),
        worker_model=opt_str("worker_model"),
        verifier_model=opt_str("verifier_model"),
        workdir=opt_str("workdir"),
        seed_notes=tuple(seeds),
    )


def _snapshot(control: Control, meta: dict, runner) -> dict:
    """GET /state 的快照：Control 的可观测状态 + 这一次运行的整体状态。"""
    session = runner.session if runner is not None else dict(EMPTY_SESSION)
    return {
        "meta": meta,
        "session": session,
        "workdir": str(runner.cfg.worker.workdir) if runner is not None
        else str(meta.get("workdir") or ""),
        "running": bool(runner.busy) if runner is not None else False,
        "stop_requested": control.stop_requested,
        "stop_reason": control.stop_reason,
        # 事件总线的最新 seq：页面据此决定 SSE 从哪儿开始订阅 ——
        # 接的是已结束的那一轮就跳过它的 backlog，别让旧事件把界面重新点亮
        "seq": control.bus.seq,
        **control.snapshot(),
    }


def _require(runner):
    """历史类接口的公共前置：没有运行器就 400，返回 (cfg, store)。"""
    if runner is None:
        raise HTTPException(400, "当前进程没有绑定运行器")
    return runner.cfg, HistoryStore(runner.cfg.root)


async def _json_error(_request: Request, exc: HTTPException) -> JSONResponse:
    """把 HTTPException 统一成页面认的 `{ok: false, error}` 形状。

    这样各接口只需要 `raise HTTPException(...)`，不用每个都手写一份
    JSONResponse(ok=False)，也不会漏掉某处而让前端读到 `detail`。
    """
    return JSONResponse(
        {"ok": False, "error": exc.detail}, status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


def create_app(control: Control, meta: dict, runner, stopping: threading.Event) -> FastAPI:
    app = FastAPI(title="Lodestar Console", docs_url=None, redoc_url=None)
    app.add_exception_handler(HTTPException, _json_error)

    # ── 静态产物 ──────────────────────────────────────────
    @app.get("/")
    def index() -> FileResponse:
        target = _STATIC / "index.html"
        if not target.is_file():
            raise HTTPException(500, "缺少前端产物：先 cd frontend && npm run build")
        return FileResponse(target, headers=_NO_STORE)

    @app.get("/assets/{name:path}")
    def asset(name: str) -> FileResponse:
        target = (_STATIC / "assets" / name).resolve()
        if not target.is_relative_to(_STATIC) or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target, headers=_NO_STORE)

    # ── 状态与配置 ────────────────────────────────────────
    @app.get("/state")
    def get_state() -> JSONResponse:
        return JSONResponse(_snapshot(control, meta, runner))

    @app.get("/config")
    def get_config() -> JSONResponse:
        cfg, _store = _require(runner)
        return JSONResponse({"ok": True, "config": as_editable(cfg)})

    # ── 历史 ──────────────────────────────────────────────
    @app.get("/history")
    def get_history() -> JSONResponse:
        """左侧导航的数据源：按产出目录分组的历史会话。"""
        cfg, store = _require(runner)
        current = runner.session.get("thread", "") if runner.busy else ""
        return JSONResponse(store.groups(
            cfg.root / "checkpoints.sqlite", str(cfg.worker.workdir), current=current,
        ))

    @app.get("/history/{thread}")
    def get_history_detail(thread: str) -> JSONResponse:
        cfg, store = _require(runner)
        item = store.detail(cfg.root / "checkpoints.sqlite", thread, str(cfg.worker.workdir))
        if item is None:
            raise HTTPException(404, f"没有这个会话：{thread}")
        return JSONResponse({"ok": True, "item": item})

    @app.get("/history/{thread}/log")
    def get_history_log(thread: str) -> JSONResponse:
        """一次运行的信息流：把当时那份运行日志切成事件，供「回到操作页面」回看。

        日志路径来自 history.json（本进程自己写的），仍然只认 root 下的文件。
        """
        cfg, store = _require(runner)
        path = store.log_path(thread)
        if path is None:
            if not any(x.get("thread") == thread for x in store.items()):
                raise HTTPException(404, f"没有这个会话：{thread}")
            return JSONResponse({"ok": True, "events": [], "note": "这次运行没有留下日志"})
        root = Path(cfg.root).expanduser().resolve()
        try:
            target = path.resolve()
        except OSError:
            return JSONResponse({"ok": True, "events": [], "note": "日志文件不在了"})
        if not target.is_relative_to(root) or not target.is_file():
            return JSONResponse({"ok": True, "events": [], "note": "日志文件不在了"})
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(500, f"读取日志失败：{exc}") from exc
        return JSONResponse({"ok": True, "events": parse_log(text)})

    @app.delete("/history/{thread}")
    def delete_history(thread: str) -> JSONResponse:
        """删掉一条会话历史（json 条目 + 它的 checkpoint），删掉后不能再续跑。"""
        cfg, store = _require(runner)
        # 正在跑的那条不能删：图还挂在它的 thread 上，删库会让后续写入没地方落
        current = runner.session.get("thread", "") if runner.busy else ""
        if current and current == thread:
            raise HTTPException(409, "这个会话正在运行，等它结束再删")
        if not store.delete(cfg.root / "checkpoints.sqlite", thread):
            raise HTTPException(404, f"没有这个会话：{thread}")
        return JSONResponse({"ok": True})

    # ── SSE ───────────────────────────────────────────────
    @app.get("/events")
    async def get_events(since: int = Query(0)) -> StreamingResponse:
        q = control.bus.subscribe(since)

        async def gen():
            try:
                yield ": connected\n\n"
                while not stopping.is_set():
                    try:
                        # 阻塞读放到线程里，别卡住事件循环；多连接各自占一个线程池 worker
                        ev = await asyncio.to_thread(q.get, True, 1.0)
                    except queue.Empty:
                        yield ": ping\n\n"
                        continue
                    payload = json.dumps(ev, ensure_ascii=False)
                    yield f"id: {ev['seq']}\ndata: {payload}\n\n"
            finally:
                control.bus.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={**_NO_STORE, "X-Accel-Buffering": "no"},
        )

    # ── 命令 ──────────────────────────────────────────────
    @app.post("/command")
    async def do_command(payload: Annotated[dict[str, Any], Body()]) -> JSONResponse:
        kind = str(payload.get("type") or "").strip()

        if kind == "run":
            # 续跑前写好的引导也在里面：跟这次 run 一起投进信箱，
            # 开跑那一刻不会被 reset 冲掉
            _require(runner)
            try:
                info = runner.start(_parse_start(payload))
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return JSONResponse({"ok": True, "session": info})

        if kind == "config":
            _require(runner)
            if runner.busy:
                raise HTTPException(409, "任务正在运行，等它结束再改配置")
            body = payload.get("payload")
            if not isinstance(body, dict):
                raise HTTPException(400, "payload 必须是对象")
            patch, errors = collect_overrides(body, runner.cfg.main_agent.api_key)
            if errors:
                return JSONResponse({"ok": False, "errors": errors}, status_code=400)
            if not patch:
                return JSONResponse({"ok": True, "config": as_editable(runner.cfg),
                                     "note": "没有改动"})
            try:
                save_overrides(runner.cfg.root, patch)
                new_cfg = runner.reload_config()
            except Exception as exc:  # noqa: BLE001 - 保存失败要原样告诉页面
                raise HTTPException(500, str(exc)) from exc
            control.drain_timeout = new_cfg.ui.drain_timeout_seconds
            control.emit("配置已保存到 config.local.yaml（下次开始任务生效）", kind="system")
            return JSONResponse({"ok": True, "config": as_editable(new_cfg),
                                 "note": "下次开始任务生效"})

        if kind == "pickdir":
            # 浏览器拿不到本地绝对路径，只能让后端在本机弹系统选择器。
            # 同步子进程放到线程里，别卡住 uvicorn 的事件循环。
            try:
                picked = await asyncio.to_thread(pick_directory)
            except PickUnavailable as exc:
                raise HTTPException(400, str(exc)) from exc
            if not picked:
                return JSONResponse({"ok": True, "path": "", "canceled": True})
            return JSONResponse({"ok": True, "path": picked})

        if kind == "guide":
            target = str(payload.get("target") or "main").strip()
            if target not in Control.TARGETS:
                raise HTTPException(400, f"target 必须是 {list(Control.TARGETS)}")
            try:
                note = control.push_note(target, str(payload.get("text") or "").strip())
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return JSONResponse({"ok": True, "note": note})

        if kind == "stop":
            accepted = control.request_stop(
                str(payload.get("reason") or "用户在控制台点击了退出")
            )
            return JSONResponse({"ok": True, "accepted": accepted})

        if kind == "clientlog":
            # 页面上的 JS 异常往终端打一份，排查「点了没反应」时不用开 DevTools
            message = str(payload.get("message") or "").strip()[:500]
            if message:
                print(f"[client] {message}", file=sys.stderr, flush=True)
                control.emit(f"[页面异常] {message}", kind="stop")
            return JSONResponse({"ok": True})

        raise HTTPException(
            400, "type 必须是 run / config / guide / stop / pickdir / clientlog",
        )

    return app


class ConsoleServer:
    """把 uvicorn 挂到后台线程上，主线程继续跑图或等 Ctrl+C。"""

    def __init__(
        self,
        control: Control,
        meta: dict | None = None,
        runner=None,
        host: str = "127.0.0.1",
        port: int = 8788,
    ):
        self.control = control
        self.meta = meta or {}
        self.runner = runner
        self.host = host
        self.port = int(port)
        self._stopping = threading.Event()
        self._app = create_app(control, self.meta, runner, self._stopping)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}/"

    def start(self) -> "ConsoleServer":
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
            # SSE 是长连接，默认优雅关闭会一直等它；给 1s 上限，Ctrl+C 才能干脆退出
            timeout_graceful_shutdown=1,
        )
        # 先 bind：端口被占用会在这里抛 OSError，调用方据此回退到随机端口；
        # port=0 时由内核分配，回读实际端口
        self._sock = config.bind_socket()
        self.port = self._sock.getsockname()[1]
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [self._sock]},
            name="lodestar-console",
            daemon=True,
        )
        self._thread.start()
        self.control.emit(f"控制台已就绪：{self.url}", kind="system", url=self.url)
        return self

    def wait(self) -> None:
        """阻塞到 stop() 或服务线程退出。Ctrl+C 会从这里穿出去。"""
        while not self._stopping.is_set():
            if self._thread is not None and not self._thread.is_alive():
                return
            time.sleep(0.3)

    def stop(self) -> None:
        self._stopping.set()
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=3)
