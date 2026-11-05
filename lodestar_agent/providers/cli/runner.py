"""跑一次 CLI：起子进程、守住超时与退出边界、把输出收回来。

这里只管「怎么安全地跑」，不管「跑的是什么 CLI」（方言在 dialect.py）和
「prompt 怎么写」（在 worker.py）。三层分开，加新 CLI 只动方言。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from ..base import RunResult
from ..subproc import child_env, command_argv, terminate_tree, track, untrack
from .dialect import CliCall, Dialect, clip


def find_cli(command: str) -> str:
    """确认 CLI 存在，返回绝对路径。"""
    found = shutil.which(command)
    if not found:
        raise FileNotFoundError(
            f"找不到 CLI（{command}）。请先安装，或把配置里这个来源的 command 改成绝对路径。"
        )
    return found


def _read_text(path: Path) -> str:
    """读子进程写下的输出。被强杀时末尾可能断在多字节字符中间，errors=replace 兜住。"""
    try:
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


def _looks_like_missing_session(text: str) -> bool:
    low = (text or "").lower()
    if "session" not in low and "conversation" not in low:
        return False
    return any(
        word in low for word in ("not found", "no such", "cannot find", "does not exist", "invalid")
    )


def run(
    command: str,
    dialect: Dialect,
    call: CliCall,
    *,
    cwd: Path,
    timeout: float,
    extra_env: dict[str, str] | None = None,
    cancel=None,
    drain_deadline: float | Callable[[], float | None] | None = None,
    label: str = "CLI",
) -> RunResult:
    """跑一次 CLI。

    cancel / drain_deadline 是「边界退出」的接线：cancel 被置位**不会**打断已经发出去的
    这一轮，它只说明「之后不许再开新的」；只有到了 drain_deadline 还没结束，才真的
    SIGTERM → SIGKILL。这样当前节点的产出是完整的，不会留半成品。

    drain_deadline 可以是常量，也可以是**回调**。必须支持回调：退出请求往往发生在
    CLI 已经跑起来之后，进入这里时它还求不出来；传死值会导致强杀分支永远不触发，
    表现就是「点了退出，进程一直不退」。
    """
    result = _run_once(
        command, dialect, call, cwd=cwd, timeout=timeout, extra_env=extra_env,
        cancel=cancel, drain_deadline=drain_deadline, label=label,
    )
    # 会话可能已经被清理（换了机器 / 清了缓存）：别让整条流程卡死在这里，
    # 退回新会话重跑一次。只兜底一次，避免无限递归。
    if call.resume_session and not result.ok and not result.timed_out and not result.aborted:
        if _looks_like_missing_session(result.error or ""):
            result = _run_once(
                command, dialect,
                CliCall(
                    prompt=call.prompt, model=call.model, allowed_tools=call.allowed_tools,
                    add_dirs=call.add_dirs, max_turns=call.max_turns, resume_session="",
                ),
                cwd=cwd, timeout=timeout, extra_env=extra_env,
                cancel=cancel, drain_deadline=drain_deadline, label=label,
            )
    return result


def _run_once(
    command: str,
    dialect: Dialect,
    call: CliCall,
    *,
    cwd: Path,
    timeout: float,
    extra_env: dict[str, str] | None,
    cancel,
    drain_deadline: float | Callable[[], float | None] | None,
    label: str,
) -> RunResult:
    cwd = Path(cwd).expanduser().resolve()
    cwd.mkdir(parents=True, exist_ok=True)

    try:
        cli_path = find_cli(command)
    except FileNotFoundError as exc:
        return RunResult(ok=False, output="", duration=0.0, error=str(exc))

    cmd = command_argv(cli_path, dialect.argv(call))
    env = child_env(extra_env)

    # 输出落文件，不走管道：CLI 是 Node 程序，stdout 接管道时是异步写，
    # process.exit(0) 会把没 flush 完的部分丢掉（退出码还是 0，最难查）。
    # 文件描述符是同步写；被强杀时也能读到已经落盘的那部分。
    tmpdir = Path(tempfile.mkdtemp(prefix="lodestar-cli-"))
    out_path = tmpdir / "stdout.jsonl"
    err_path = tmpdir / "stderr.log"
    out_f = open(out_path, "wb")
    err_f = open(err_path, "wb")

    started = time.time()
    # POSIX 用 start_new_session 给 CLI 单独一个进程组，中断/超时时整组回收；
    # Windows 没有进程组信号，回收走 taskkill /T（见 subproc._terminate_windows）。
    popen_kwargs: dict = {"cwd": str(cwd), "env": env, "stdout": out_f, "stderr": err_f}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except OSError as exc:
        out_f.close()
        err_f.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
        return RunResult(ok=False, output="", duration=0.0, error=f"启动 {label} 失败：{exc}")

    forced = False
    timed_out = False
    track(proc)

    def _deadline() -> float | None:
        # 每次轮询都重新求值：退出请求可能在这次调用开始之后才发生
        value = drain_deadline
        return value() if callable(value) else value

    try:
        # 输出不再走管道，所以只用 poll 轮询即可，不存在读空管道的死锁。
        while proc.poll() is None:
            now = time.time()
            limit = _deadline()
            if cancel is not None and cancel.is_set() and limit is not None and now >= limit:
                forced = True
                terminate_tree(proc, grace=3.0)
                break
            if now - started > timeout:
                timed_out = True
                terminate_tree(proc)
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        terminate_tree(proc)
        raise
    finally:
        untrack(proc)
        # 强杀分支 break 之后进程可能还没被回收，先等一下再读文件，
        # 免得最后一个事件还在写就被读走。
        try:
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            pass
        out_f.close()
        err_f.close()
        stdout = _read_text(out_path)
        stderr = _read_text(err_path)
        if not os.environ.get("LODESTAR_KEEP_CLI_LOG"):
            shutil.rmtree(tmpdir, ignore_errors=True)

    duration = time.time() - started
    raw = f"{stdout}\n{stderr}".strip()
    parsed = dialect.parse(stdout, stderr)

    # 未登录：CLI 把 "Authentication required." 打到 stderr，且退出码可能仍是 0
    if "Authentication required" in raw:
        return RunResult(
            ok=False, output="", duration=duration,
            error=(
                f"{label} 还没有登录态：先在本机终端跑一次 `{command}` 完成登录，"
                "或在该来源的 env 里配置 token。"
            ),
        )

    if forced:
        # 已经等过 drain_timeout 了，这是兜底路径，不是常规退出
        return RunResult(
            ok=False,
            output=clip(parsed.progress or parsed.text, 2000),
            duration=duration,
            session_id=parsed.session_id,
            error="超过等待上限仍未结束，已强制终止（可用 ui.drain_timeout_seconds 放宽）",
            aborted=True,
        )

    if timed_out:
        # 关键：把中断前已经写出来的产出带回去。扔空串会让验收 agent 判「什么都没做」，
        # 然后触发下一轮从零重跑。
        return RunResult(
            ok=False,
            output=clip(parsed.progress or parsed.text, 2000),
            duration=duration,
            session_id=parsed.session_id,
            error=f"超时：超过 {timeout:.0f}s（已终止整个子进程组；中断前的产出已保留）",
            timed_out=True,
        )

    # 轮次上限：CLI 自己停的，不是崩溃。单独给一条能照做的提示，
    # 否则页面上只有一句 "Max turns (15) exceeded"，看不出该怎么改。
    failed = proc.returncode != 0 or parsed.is_error
    if failed and "max turns" in raw.lower():
        limit_desc = f"--max-turns {call.max_turns}" if call.max_turns else "CLI 默认上限"
        return RunResult(
            ok=False,
            output=clip(parsed.text, 800),
            duration=duration,
            session_id=parsed.session_id,
            error=(
                f"到达执行轮次上限（{limit_desc}），CLI 主动停下："
                "把配置里这个节点的 max_turns 调大（0 = 不传，用 CLI 默认），"
                "或者把这条任务拆小一点。已改动的文件还在产出目录里。"
            ),
        )

    if proc.returncode != 0:
        return RunResult(
            ok=False, output="", duration=duration, session_id=parsed.session_id,
            error=f"CLI 退出码 {proc.returncode}: {dialect.explain(stdout, stderr)}",
        )

    # 退出码 0、但一个字都没说：当成失败，而不是把空串当「干完了」交给上层
    if not parsed.text.strip() and not stdout.strip() and not stderr.strip():
        return RunResult(ok=False, output="", duration=duration, error="CLI 没有任何输出")

    return RunResult(
        ok=not parsed.is_error,
        output=parsed.text,
        duration=duration,
        session_id=parsed.session_id,
        turns=parsed.turns,
        error=(parsed.text[:800] if parsed.is_error and parsed.text
               else (dialect.explain(stdout, stderr) if parsed.is_error else None)),
    )
