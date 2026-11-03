"""命令行入口：python -m agent_demo "你的任务"

    python -m agent_demo "在 workspace 里写一个 hello.py，运行后打印 hello langgraph"
    python -m agent_demo --dry-run "只验证 worker 链路，不花主模型 token"

每次运行都会在 logs/ 下写一份完整日志（含各 agent 的原始输出 / 思考过程）。
"""

from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys
import textwrap
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from .config import ConfigError, load_config
from .graph import AgentFailure, build_graph
from .worker import check_cli

_DISPLAY_LIMIT = 800


def _hr(title: str = "") -> None:
    line = "─" * 62
    print(f"\n{line}")
    if title:
        print(title)
        print(line)


def _open_log():
    os.makedirs("logs", exist_ok=True)
    path = os.path.abspath(f"logs/run_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
    return open(path, "a", encoding="utf-8"), path


def _w(logf, text: str) -> None:
    logf.write(text.rstrip("\n") + "\n")
    logf.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_demo",
        description="LangGraph 主 agent（每轮动态规划）+ worker + 验收 agent 示例",
    )
    parser.add_argument("task", nargs="*", help="交给主 agent 完成的目标（--resume 时可省略）")
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--workdir", help="覆盖 worker / 验收 agent 的工作目录")
    parser.add_argument(
        "--thread-id",
        help="本次运行的 checkpoint 线程 id，默认按时间戳生成；续跑时用它指回上次",
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
    args = parser.parse_args(argv)

    if args.resume and not args.thread_id:
        parser.error("--resume 需要配合 --thread-id 指定要续跑的运行")
    if not args.task and not args.resume:
        parser.error("需要给出任务目标")

    goal = " ".join(args.task).strip()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

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

    # ── checkpoint：每次运行一个 thread，state 落 sqlite，中断后可 --resume 续跑 ──
    thread_id = args.thread_id or f"run_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    db_path = cfg.root / "checkpoints.sqlite"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)
    saver.setup()

    app = build_graph(cfg, use_llm=not args.dry_run, checkpointer=saver)
    run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 120}

    snapshot = app.get_state(run_config)
    resume_note = ""
    if args.resume:
        if not snapshot.values:
            conn.close()
            print(f"[续跑失败] 找不到线程 {thread_id} 的 checkpoint（{db_path}）", file=sys.stderr)
            return 2
        if not snapshot.next:
            conn.close()
            print(
                f"[续跑失败] 线程 {thread_id} 已经跑完，没有待执行的节点；要重跑请换一个 --thread-id",
                file=sys.stderr,
            )
            return 2
        goal = str(snapshot.values.get("goal") or goal)
        # 上次是「执行失败终止」的线程：attempts 已经顶到 worker.max_retries，
        # 不重置的话续跑会在 plan 里立刻再撞一次上限。用户显式 --resume 就视为「环境已修好」。
        if snapshot.values.get("last_error"):
            app.update_state(run_config, {"attempts": 0, "last_error": ""}, as_node="verify")
            resume_note = "上次是执行失败终止，已重置当前任务的重试计数"
            print(f"提示    : {resume_note}")
    elif snapshot.values:
        conn.close()
        print(
            f"[线程冲突] thread_id {thread_id} 已有运行记录；"
            "续跑请加 --resume，重跑请换一个 --thread-id",
            file=sys.stderr,
        )
        return 2

    logf, log_path = _open_log()
    _w(logf, f"目标: {goal}")
    _w(logf, f"thread: {thread_id}" + ("（续跑）" if args.resume else ""))
    _w(logf, f"checkpoint: {db_path}")
    if resume_note:
        _w(logf, f"resume: {resume_note}")
    _w(logf, f"主 agent: {cfg.main_agent.model} @ {cfg.main_agent.base_url}")
    _w(logf, f"worker: {cfg.worker.cli} (model={cfg.worker.model or 'CLI 默认'}, "
            f"tools={cfg.worker.allowed_tools}, cwd={cfg.worker.workdir}, max_turns={cfg.worker.max_turns})")
    _w(logf, f"验收: {cfg.verifier.cli} (model={cfg.verifier.model or 'CLI 默认'}, "
            f"tools={cfg.verifier.allowed_tools}, cwd={cfg.worker.workdir}/{cfg.verifier.scratch_dir}, "
            f"max_rounds={cfg.verifier.max_rounds})")
    _w(logf, "")

    print(f"目标    : {goal}")
    print(f"主 agent: {cfg.main_agent.model} @ {cfg.main_agent.base_url}")
    print(f"worker  : {cfg.worker.cli} (model={cfg.worker.model or 'CLI 默认'}, "
          f"tools={cfg.worker.allowed_tools}, cwd={cfg.worker.workdir}, max_turns={cfg.worker.max_turns})")
    print(f"验收    : {cfg.verifier.cli} (model={cfg.verifier.model or 'CLI 默认'}, "
          f"tools={cfg.verifier.allowed_tools}, cwd={cfg.worker.workdir}/{cfg.verifier.scratch_dir}, "
          f"max_rounds={cfg.verifier.max_rounds})")
    if args.dry_run:
        print("模式    : dry-run（确定性决策代替主 agent，跳过验收）")
    print(f"thread  : {thread_id}" + ("（续跑）" if args.resume else ""))

    answer = ""
    done = 0
    failed = 0
    current_task = ""
    # resume 时传 None，让 LangGraph 从 checkpoint 里恢复，而不是用新 state 覆盖
    initial = None if args.resume else {
        "goal": goal, "plan": [], "current": None, "attempts": 0, "feedback": "", "records": [],
    }

    try:
        for chunk in app.stream(initial, run_config, stream_mode="updates"):
            for node, update in chunk.items():
                update = update or {}
                if node == "plan":
                    if update.get("stage") == "init":
                        _hr("[主 agent] 拆解任务方案")
                        if update.get("note"):
                            print(textwrap.indent(update["note"], "  "))
                        items = ([update.get("current")] if update.get("current") else []) + (update.get("plan") or [])
                        for i, item in enumerate(items, 1):
                            if not item:
                                continue
                            print(f"  {i}. {item['task']}")
                            print(f"     验收: {item.get('verify') or '(未给出可执行标准，由验收 agent 自行设计)'}")
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
                        _hr(f"[主 agent] 动态规划 → {label}（完成 {done} 条 / 失败 {failed}）")
                        if update.get("note"):
                            print(textwrap.indent(update["note"], "  "))
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
                    _hr(f"[worker] 执行  第 {done + 1} 条 · 第 {update.get('attempts', 1)} 轮")
                    body = (update.get("last_output") or update.get("last_error") or "").strip()
                    if len(body) > _DISPLAY_LIMIT:
                        body = body[:_DISPLAY_LIMIT] + f"…(已截断，共 {len(body)} 字)"
                    print(textwrap.indent(f"{update.get('last_duration', 0)}s\n{body or '(无输出)'}", "  "))
                    _w(logf, "=" * 70)
                    _w(logf, f"[worker] 执行 第 {done + 1} 条 第 {update.get('attempts', 1)} 轮（{update.get('last_duration', 0)}s）")
                    _w(logf, f"任务: {current_task}")
                    if update.get("last_output"):
                        _w(logf, "输出:\n" + update["last_output"])
                    if update.get("last_error"):
                        _w(logf, "错误: " + update["last_error"])
                elif node == "verify":
                    passed = update.get("verdict_passed")
                    _hr(f"[verifier] 验收 → {'PASS' if passed else 'FAIL'}")
                    body = update.get("verdict_reason", "")
                    if len(body) > _DISPLAY_LIMIT:
                        body = body[:_DISPLAY_LIMIT] + f"…(已截断，共 {len(body)} 字)"
                    print(textwrap.indent(body or "(无)", "  "))
                    _w(logf, "=" * 70)
                    _w(logf, f"[verifier] 验收 → {'PASS' if passed else 'FAIL'}")
                    _w(logf, "结论: " + update.get("verdict_reason", ""))
                    if update.get("verdict_raw"):
                        _w(logf, "--- 模型原始输出 ---")
                        _w(logf, update["verdict_raw"])
                elif node == "synthesize":
                    answer = update.get("answer", "")
    except AgentFailure as exc:
        _hr("[流程终止] 模型请求失败")
        print(f"失败节点: {exc.node}")
        print(f"可能原因: {exc.reason}")
        _w(logf, "=" * 70)
        _w(logf, "[流程终止] 模型请求失败")
        _w(logf, f"失败节点: {exc.node}")
        _w(logf, f"可能原因: {exc.reason}")
        logf.close()
        conn.close()
        print(f"\n日志已写入: {log_path}")
        print(f"可续跑: uv run python -m agent_demo --thread-id {thread_id} --resume")
        return 3
    except KeyboardInterrupt:
        print("\n已中断（worker 子进程组已一并终止）。", file=sys.stderr)
        _w(logf, "\n[已中断]")
        _w(logf, f"续跑: --thread-id {thread_id} --resume")
        logf.close()
        conn.close()
        print(f"可续跑: uv run python -m agent_demo --thread-id {thread_id} --resume", file=sys.stderr)
        return 130

    _hr("[主 agent] 最终答复")
    print(answer or "(空)")
    _w(logf, "=" * 70)
    _w(logf, "[主 agent] 最终答复")
    _w(logf, answer or "(空)")
    logf.close()
    conn.close()

    print(f"\n日志已写入: {log_path}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
