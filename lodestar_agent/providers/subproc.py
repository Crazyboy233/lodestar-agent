"""两条来源都要用到的子进程公共件：环境白名单、进程树回收、在途进程登记。

单独成文件的原因：CLI 来源要起子进程，API 来源的本地工具（跑命令）也要起子进程，
而「起完怎么收干净」这件事只有一个正确答案，抄两遍必然有一处漏。

关于进程树：杀掉整个进程组 = 关掉它持有的 HTTP 连接，服务端那边才会停止生成。
只让 Python 退出不够 —— 子进程还活着，token 就还在烧。
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import threading

# 子进程只继承下面这些变量。
# 宿主进程（WorkBuddy / IDE / 其他 agent）会往环境里塞几十个 CODEBUDDY_*、SERVER__PORT、
# BAGGAGE……实测会让嵌套 CLI 走进另一套认证路径，直接报 "Authentication required"。
# 因此用白名单，而不是黑名单。
_KEEP_ENV = {
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "TZ", "LANG",
    "SSH_AUTH_SOCK", "XPC_SERVICE_NAME",
    # Windows 下 node 可能需要
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "COMSPEC", "PATHEXT", "SYSTEMROOT",
    # 认证 / 模型相关，可按需注入
    "CODEBUDDY_AUTH_TOKEN",
    "CODEBUDDY_API_KEY",
    "CODEBUDDY_BASE_URL",
    "CODEBUDDY_MODEL",
}
_KEEP_ENV_PREFIX = ("LC_",)


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    # Windows 环境变量大小写不敏感，SystemRoot / Path 这类常常不是全大写；
    # 统一按大写比较并归一化 key，避免 node 子进程丢掉 SYSTEMROOT / PATHEXT。
    win = os.name == "nt"

    def keep(key: str) -> bool:
        probe = key.upper() if win else key
        return probe in _KEEP_ENV or probe.startswith(_KEEP_ENV_PREFIX)

    env = {
        (key.upper() if win else key): value
        for key, value in os.environ.items()
        if keep(key)
    }
    # 单发 -p 模式不支持后台任务，关掉避免 worker 起后台命令时报错
    env.setdefault("CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS", "1")
    for key, value in (extra or {}).items():
        env[str(key)] = str(value)
    return env


def command_argv(path: str, args: list[str]) -> list[str]:
    """Windows 的 npm 全局命令常是 .cmd shim，CreateProcess 不能直接执行，
    要交给 cmd.exe；.exe 则原样执行。POSIX 原样执行。"""
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/c", path, *args]
    return [path, *args]


def _terminate_windows(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """Windows 没有进程组信号，用 taskkill /T /F 把 CLI 和它拉起的 node 一起收掉。"""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            timeout=grace,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def terminate_tree(proc: subprocess.Popen, grace: float = 3.0,
                   first: int = signal.SIGTERM) -> None:
    """给整个子进程组发信号，确保 CLI 和它 fork 的 node 子进程一起被回收。

    三条都是从实际卡死里换来的：

    1. pgid 直接用 `proc.pid`。`start_new_session=True` 保证它是新会话的 leader，
       比自己 `getpgid` 可靠 —— 进程刚退出时 getpgid 会抛 ProcessLookupError，
       那条 except 分支只 kill 主进程，组里还活着的孙进程就被漏掉了。
    2. SIGTERM 之后**无论主进程退没退**，都再对整组补一次 SIGKILL。主进程死了
       不代表孙进程死了，而孙进程会攥着 stdout pipe 不放。
    3. 组已经空了（ProcessLookupError）才是真的收干净了，直接返回。
    """
    if os.name == "nt":
        _terminate_windows(proc, grace)
        return

    pgid = proc.pid
    if not hasattr(os, "killpg"):
        if proc.poll() is None:
            proc.kill()
        return
    try:
        os.killpg(pgid, first)
    except ProcessLookupError:
        return  # 组里一个进程都没有了
    except PermissionError:
        if proc.poll() is None:
            proc.kill()
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ── 在途子进程的兜底回收 ──────────────────────────────────
# 光靠调用处的异常路径不够：进程被外部 SIGTERM、或某处抛了没接住的异常时，
# 子进程会变成孤儿 —— 那它手里的模型请求也就断不掉。
_ACTIVE: "set[subprocess.Popen]" = set()
_ACTIVE_LOCK = threading.Lock()


def track(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)


def untrack(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(proc)


def terminate_all() -> int:
    """终止所有还在跑的子进程组，返回处理个数。

    收尾和进程退出（atexit）都会调一次：杀掉进程组 = 关掉它持有的 HTTP 连接，
    服务端那边才会停止生成，而不是一直算到 token 用完。
    """
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
        _ACTIVE.clear()
    killed = 0
    for proc in procs:
        if proc.poll() is None:
            terminate_tree(proc, grace=2.0)
            killed += 1
    return killed


# 进程退出（正常结束 / 未捕获异常）时兜底：不留活着的子进程。
# 被 SIGKILL 时无法执行，那种情况只能靠进程组本身被一起回收。
atexit.register(terminate_all)
