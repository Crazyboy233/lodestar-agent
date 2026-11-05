"""Lodestar 入口。

    python -m lodestar_agent --web               # 只起控制台，任务在页面里填
    python -m lodestar_agent --web --port 9000   # 换端口
    python -m lodestar_agent --web "任务"         # 起控制台并立即开始
    python -m lodestar_agent "任务"               # 纯命令行，不开页面
    python -m lodestar_agent --dry-run "任务"     # 不调主模型、跳过验收，只验证 worker 链路
    python -m lodestar_agent --thread-id x --resume

页面上能做三件事，都不需要回终端：填目标开始跑、投递引导（排队注入，不打断当前这一轮）、
边界退出（等当前这次模型回复跑完再收工，超过 drain_timeout 才强制断开）。
"""

from __future__ import annotations

import argparse
import datetime
import signal
import sys
import threading
from pathlib import Path

from .config import ConfigError, load_config
from .control import Control
from .session import Runner, StartRequest, run_session
from .server import ConsoleServer
from .worker import check_cli


def _start_console(cfg, control: Control, runner: Runner, host: str | None,
                   port: int | None) -> ConsoleServer:
    """起控制台；端口被占用时回退到随机端口，不让控制台本身成为跑不起来的原因。"""
    bind_host = host or cfg.ui.host
    bind_port = cfg.ui.port if port is None else port
    meta = {
        "model": cfg.main_agent.model,
        "base_url": cfg.main_agent.base_url,
        "workdir": str(cfg.worker.workdir),
        "worker": f"{cfg.worker.cli} · {cfg.worker.model or '默认模型'}",
        "verifier": f"{cfg.verifier.cli} · {cfg.verifier.model or '默认模型'}",
        "max_rounds": cfg.verifier.max_rounds,
        "drain_timeout": cfg.ui.drain_timeout_seconds,
        # 页面下拉用：可选模型 + 当前配置值（空串 = CLI 默认）
        "models": cfg.ui.models,
        "workerModel": cfg.worker.model,
        "verifierModel": cfg.verifier.model,
    }
    try:
        # bind 发生在 start() 里：端口被占用会在这里抛 OSError
        return ConsoleServer(
            control, meta=meta, runner=runner, host=bind_host, port=bind_port
        ).start()
    except OSError as exc:
        print(f"[控制台] 端口 {bind_port} 不可用（{exc}），改用随机端口", file=sys.stderr)
        return ConsoleServer(control, meta=meta, runner=runner, host=bind_host, port=0).start()


def _on_sigterm(signum, frame):  # noqa: ARG001
    """把 SIGTERM 变成 KeyboardInterrupt。

    默认的 SIGTERM 会直接杀进程，跳过 finally 里的收尾（work_dir 里的子进程、
    边界退出都来不及）。`kill <pid>` 发的正是 TERM，日常运维天天会遇到。
    """
    raise KeyboardInterrupt


def _install_signal_handlers() -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    # SIGHUP 在 Windows 上不存在，getattr 跳过；SIGBREAK 只给 Windows 用
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_sigterm)
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lodestar_agent",
        description="Lodestar：主 agent（动态规划）+ worker + 独立验收，网页控制台可引导、可边界退出",
    )
    parser.add_argument("task", nargs="*", help="目标；--web 时可省略，改为在页面里填")
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--workdir", help="覆盖 worker / 验收 agent 的工作目录")
    parser.add_argument(
        "--thread-id",
        help="checkpoint 线程 id，默认按时间戳生成；续跑时用它指回上次",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="从 --thread-id 指定的 checkpoint 继续跑（用于中断后恢复）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不调用主模型（规划/决策用确定性逻辑）、跳过验收，只验证 worker 链路",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="启动本地 Web 控制台：填任务、发引导（不打断）、边界退出、实时输出",
    )
    parser.add_argument("--host", help="控制台监听地址，默认取配置 ui.host（127.0.0.1）")
    parser.add_argument("--port", type=int, help="控制台端口，默认取配置 ui.port（8788）")
    args = parser.parse_args(argv)
    _install_signal_handlers()

    if args.resume and not args.thread_id:
        parser.error("--resume 需要配合 --thread-id 指定要续跑的运行")

    goal = " ".join(args.task).strip()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    use_web = args.web or cfg.ui.web
    if not goal and not args.resume and not use_web:
        parser.error("需要给出任务目标，或用 --web 在页面里填写")

    if args.workdir:
        cfg.worker.workdir = Path(args.workdir).expanduser().resolve()

    if not args.dry_run and not cfg.main_agent.api_key:
        print(
            "[配置错误] 没有 api_key：请在 config.local.yaml 的 main_agent.api_key 填写，"
            "或设置环境变量 DEEPSEEK_API_KEY。",
            file=sys.stderr,
        )
        return 2

    for cli in {cfg.worker.cli, cfg.verifier.cli}:
        try:
            check_cli(cli)
        except RuntimeError as exc:
            print(f"[环境错误] {exc}", file=sys.stderr)
            return 2

    control = Control(drain_timeout=cfg.ui.drain_timeout_seconds)
    config_path = Path(args.config).expanduser().resolve()
    runner = Runner(cfg, control, dry_run=args.dry_run, config_path=config_path)

    # ── 纯命令行：跑完就退，不开页面 ──
    if not use_web:
        thread_id = args.thread_id or f"run_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        code = run_session(
            cfg, goal, thread_id,
            control=control, con=runner.con, resume=args.resume, dry_run=args.dry_run,
        )
        if code == 130:
            print(f"可续跑: uv run python -m lodestar_agent --thread-id {thread_id} --resume")
        return code

    # ── 页面模式：先起服务，任务在页面上填 ──
    server = _start_console(cfg, control, runner, args.host, args.port)
    print(f"控制台  : {server.url}")
    print("        打开后填目标即可开始；Ctrl+C 退出（有任务在跑时会先边界退出）")

    if goal or args.resume:
        try:
            info = runner.start(
                StartRequest(goal=goal, thread=args.thread_id, resume=args.resume)
            )
            print(f"已自动开始: {info['thread']}  {goal or '(续跑)'}")
        except (RuntimeError, ValueError) as exc:
            print(f"[启动失败] {exc}", file=sys.stderr)

    try:
        server.wait()
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        if runner.busy:
            control.request_stop("终端中断")
            print("正在等待当前这一轮收尾…", file=sys.stderr)
            runner.join(timeout=cfg.ui.drain_timeout_seconds + 10)
    finally:
        server.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
