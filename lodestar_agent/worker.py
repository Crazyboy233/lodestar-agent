"""worker / verifier agent：把子任务丢给本机 codebuddy CLI 的无头模式执行。

对应文档：https://www.codebuddy.cn/docs/cli/headless
关键四点：
  1. `-p` 非交互执行
  2. `-y` 必须带，否则文件读写/命令执行会被权限拦下
  3. `--output-format stream-json`：逐行事件流，最后一行是 result。
     不用 `json` 的原因：它把整段 transcript（含每轮的 memory 注入）序列化成一个巨型
     对象，输出动辄几百 KB；一旦中途断掉整段就不可解析。
  4. stdout/stderr 写**文件**而不是管道。CLI 是 Node 程序，stdout 接管道时是异步写，
     `process.exit(0)` 会把没 flush 完的数据直接丢掉（实测 8MB 只收到 65536 字符，退出码
     仍是 0）。文件描述符是同步写，不会丢。
"""

from __future__ import annotations

import json
import atexit
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

WORKER_PROMPT = """你是 worker agent，负责独立完成下面这一个子任务。

工作目录：{cwd}
子任务：{task}
{feedback}{guidance}
要求：
1. 直接在磁盘上动手（写文件 / 改文件 / 跑命令），不要只描述做法。
2. 只做这一个子任务，不要顺手重构或扩展别的文件。
3. 动作要收敛：先读相关文件，再改，再跑一次针对性验证；不要做无目的的全仓搜索，
   也不要把长日志原样打出来（用 -q / --quiet / | tail -20 之类收窄）。
4. 完成后用中文汇报 1~3 行：动了哪些文件、怎么验证的、结果如何。
"""

RESUME_HINT = """
注意：这是接着上一次被中断的执行继续（同一个会话），不是从零开始。
先看磁盘上已有的产出（改了一半的文件、刚写的测试），确认进度后只补没做完的部分，
不要重做已经完成的工作。
"""

# 用户在 worker 跑动过程中投递的引导：不打断当前这一轮，等下一次调用才拼进来。
GUIDANCE_BLOCK = """
用户在你执行过程中插入的引导（优先级高于上面的一般要求，但仍不要越出本子任务范围）：
{notes}
"""

VERIFIER_PROMPT = """你是独立的验收 agent，负责自己写测试脚本并执行，判定子任务是否达标。

被测产出目录（其中的文件只读，不要改动、删除或覆盖）：
{workdir}

你的测试脚本目录（需要写测试脚本时就写到这里）：
{scratch}

被验收的子任务：{task}
验收标准：{verify}

worker 的自述（不可全信，只当线索）：
{worker_output}

要求：
1. 简单的检查直接执行命令即可；需要多步或断言时，把测试写成脚本（放在 {scratch} 下），再运行它。
2. 逐条执行验收标准里的检查，以真实命令输出为准；能跑就一定要跑。
3. {workdir} 里的文件只读，不要修改、删除、覆盖。
4. 任何一条不满足即判不通过，不准凭猜测放行。
5. 最后只输出一个 JSON 对象，不要 Markdown 代码块，不要多余解释：
{{"pass": true 或 false, "reason": "一句话结论", "fix": "给 worker 的具体修改指令；pass 时填空字符串"}}
"""

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


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
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
    env["CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    for key, value in (extra or {}).items():
        env[str(key)] = str(value)
    return env


@dataclass
class CliResult:
    ok: bool
    output: str
    duration: float
    session_id: str | None = None
    error: str | None = None
    aborted: bool = False  # True 表示因为用户请求退出而被主动终止，不是失败
    timed_out: bool = False  # True 表示撞了 worker.timeout_seconds（产出可能不完整，但已尽量保留）


@dataclass
class StreamEvents:
    """从 `--output-format stream-json` 里回收出来的信息。

    这些事件是逐行的，互不依赖：即使最后一行被截断，前面的事件照样能用。
    这正是它比 `json` 格式抗造的原因。
    """

    session_id: str | None = None
    result: str = ""
    result_event: dict | None = None
    assistant_texts: list[str] = field(default_factory=list)
    num_turns: int | None = None
    is_error: bool = False
    subtype: str = ""
    complete: bool = False   # 收到了 result 事件
    kind_seen: bool = False  # 出现过 type 字段 = 确实是 stream-json 事件流

    def last_text(self) -> str:
        """没有 result 时（超时/截断）尽量捞回最后一段自述。"""
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


@dataclass
class Verdict:
    passed: bool
    reason: str
    fix: str
    raw: str = ""
    duration: float = 0.0
    error: str | None = None  # 非空表示验收 agent 自身的请求失败（不是"验收不通过"）
    aborted: bool = False  # True 表示验收过程被用户请求退出打断


def check_cli(cli: str) -> str:
    """确认 CLI 存在，返回绝对路径。"""
    found = shutil.which(cli)
    if not found:
        raise RuntimeError(
            f"找不到 codebuddy CLI（{cli}）。请先安装，或把配置里的 cli 改成绝对路径。"
        )
    return found


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


def _parse_stream(text: str) -> StreamEvents:
    """逐行解析 stream-json 事件流。

    坏行（被强杀时写在半截）直接跳过，不牵连其它行 —— 这就是不用
    `--output-format json` 的理由：后者截断一处，整段报废。
    """
    info = StreamEvents()
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
            info.result_event = event
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


def _result_text(events: StreamEvents, data: dict | None = None) -> str:
    """统一取「这次 CLI 到底说了什么」，兼容旧的 json 数组格式。"""
    if data is not None:
        value = data.get("result") or data.get("message") or ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return events.last_text()


def _read_text(path: Path) -> str:
    """读子进程写下的输出。被强杀时末尾可能断在多字节字符中间，errors=replace 兜住。"""
    try:
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


def _extract_json(text: str) -> dict | None:
    """从模型自由文本里抠出最后一个 JSON 对象（验收结论）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    for block in re.findall(r"```(?:json)?\s*([\s\S]*?)```", text):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return None


AUTH_HINT = (
    "worker 用到的 codebuddy CLI 还没有登录态：先在本机终端执行 `codebuddy`（或 `cbc`），"
    "输入 /login 完成登录；或在 config.yaml 的 env 里配置 CODEBUDDY_AUTH_TOKEN。"
)

_ERROR_LIMIT = 400


def _clip(text: str, limit: int = _ERROR_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"…(已截断，共 {len(text)} 字)"


def _diagnose(raw: str) -> str:
    """把 CLI 的原始输出压成一句人话，绝不把整段 transcript 往终端里倒。"""
    raw = (raw or "").strip()
    if not raw:
        return "CLI 没有任何输出"

    # stream-json：没收到 result 事件时，用已收到的事件说明它跑到哪儿了
    events = _parse_stream(raw)
    if events.complete or events.assistant_texts:
        if events.complete:
            detail = events.result or events.subtype or ""
            return _clip(detail) if detail else f"CLI 返回 result（subtype={events.subtype or '未知'}）"
        hint = f"CLI 未返回 result（已收到 {len(events.assistant_texts)} 段 assistant 输出）"
        tail = events.assistant_texts[-1]
        return f"{hint}；最后一条：{_clip(tail)}"

    try:
        data = json.loads(raw)
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
                    c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                ]
        tail = (errors[-1] if errors else (texts[-1] if texts else ""))
        hint = f"CLI 未返回 result（对话共 {len(data)} 条消息）"
        return f"{hint}；最后一条：{_clip(tail)}" if tail else hint

    return _clip(raw)


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


def _terminate_tree(proc: subprocess.Popen, grace: float = 3.0, first: int = signal.SIGTERM) -> None:
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


# ── 在途 CLI 进程的兜底回收 ──────────────────────────────
# 光靠 run_cli 的异常路径不够：进程被外部 SIGTERM、或某处抛了没接住的异常时，
# CLI 和它 fork 的 node 子进程会变成孤儿 —— 那它手里的模型请求也就断不掉。
_ACTIVE: "set[subprocess.Popen]" = set()
_ACTIVE_LOCK = threading.Lock()


def _track(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)


def _untrack(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(proc)


def terminate_all_cli() -> int:
    """终止所有还在跑的 CLI 进程组，返回处理个数。

    收尾和进程退出（atexit）都会调一次：杀掉进程组 = 关掉它持有的 HTTP 连接，
    服务端那边才会停止生成，而不是一直算到 token 用完。
    """
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
        _ACTIVE.clear()
    killed = 0
    for proc in procs:
        if proc.poll() is None:
            _terminate_tree(proc, grace=2.0)
            killed += 1
    return killed


def _cli_argv(cli_path: str, args: list[str]) -> list[str]:
    """Windows 的 npm 全局命令常是 .cmd shim，CreateProcess 不能直接执行，
    要交给 cmd.exe；.exe 则原样执行。POSIX 原样执行。"""
    if os.name == "nt" and cli_path.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/c", cli_path, *args]
    return [cli_path, *args]


def run_cli(
    prompt: str,
    *,
    cwd: Path,
    cli: str = "codebuddy",
    model: str = "",
    allowed_tools: str = "Read,Write,Edit,Bash",
    add_dirs: list[Path] | None = None,
    timeout: float = 600.0,
    max_turns: int = 0,
    extra_env: dict[str, str] | None = None,
    cancel: threading.Event | None = None,
    drain_deadline: float | "Callable[[], float | None]" | None = None,
    resume_session: str | None = None,
) -> CliResult:
    """跑一次 CLI。

    cancel / drain_deadline 是「边界退出」的接线：cancel 被置位**不会**打断已经发出去的
    这一轮，它只说明「之后不许再开新的」；只有到了 drain_deadline 还没结束，才真的
    SIGTERM → SIGKILL。这样当前节点的产出是完整的，不会留半成品。

    drain_deadline 可以是常量，也可以是**回调**。必须支持回调：退出请求往往发生在
    CLI 已经跑起来之后，进入这里时它还求不出来；传死值会导致强杀分支永远不触发，
    表现就是「点了退出，进程一直不退」。

    resume_session 非空时走 `--resume`，在同一个 CLI 会话里接着上次跑。超时重试用它：
    否则每轮都从零开始，同一份活重做三遍，永远做不完。
    """
    common = dict(
        cwd=cwd, cli=cli, model=model, allowed_tools=allowed_tools,
        add_dirs=add_dirs, timeout=timeout, max_turns=max_turns,
        extra_env=extra_env, cancel=cancel, drain_deadline=drain_deadline,
    )
    result = _run_cli_once(prompt, resume_session=resume_session, **common)
    # 会话可能已经被清理（比如换了机器/清了缓存）：别让整条流程卡死在这里，
    # 退回新会话重跑一次。只兜底一次，避免无限递归。
    if resume_session and not result.ok and not result.timed_out and not result.aborted:
        if _looks_like_missing_session(result.error or ""):
            result = _run_cli_once(prompt, resume_session=None, **common)
    return result


def _looks_like_missing_session(text: str) -> bool:
    low = (text or "").lower()
    if "session" not in low and "conversation" not in low:
        return False
    return any(word in low for word in ("not found", "no such", "cannot find", "does not exist", "invalid"))


def _run_cli_once(
    prompt: str,
    *,
    cwd: Path,
    cli: str,
    model: str,
    allowed_tools: str,
    add_dirs: list[Path] | None,
    timeout: float,
    max_turns: int,
    extra_env: dict[str, str] | None,
    cancel: threading.Event | None,
    drain_deadline: float | "Callable[[], float | None]" | None,
    resume_session: str | None = None,
) -> CliResult:
    cwd = Path(cwd).expanduser().resolve()
    cwd.mkdir(parents=True, exist_ok=True)

    cli_path = check_cli(cli)
    cli_args = ["-p", prompt, "--output-format", "stream-json", "-y"]
    if resume_session:
        cli_args += ["--resume", resume_session]
    if allowed_tools:
        cli_args += ["--allowedTools", allowed_tools]
    if model:
        cli_args += ["--model", model]
    if max_turns and max_turns > 0:
        cli_args += ["--max-turns", str(max_turns)]
    for d in add_dirs or []:
        cli_args += ["--add-dir", str(d)]
    cmd = _cli_argv(cli_path, cli_args)

    env = _child_env(extra_env)

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
    # Windows 没有进程组信号，回收走 taskkill /T（见 _terminate_windows）。
    popen_kwargs: dict = {"cwd": str(cwd), "env": env, "stdout": out_f, "stderr": err_f}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)
    forced = False
    timed_out = False
    stdout = ""
    stderr = ""
    _track(proc)

    def _deadline() -> float | None:
        # 每次轮询都重新求值：退出请求可能在这次调用开始之后才发生
        return drain_deadline() if callable(drain_deadline) else drain_deadline

    try:
        # 输出不再走管道，所以只用 poll 轮询即可，不存在读空管道的死锁。
        while proc.poll() is None:
            now = time.time()
            limit = _deadline()
            if cancel is not None and cancel.is_set() and limit is not None and now >= limit:
                forced = True
                _terminate_tree(proc, grace=3.0)
                break
            if now - started > timeout:
                timed_out = True
                _terminate_tree(proc)
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        _terminate_tree(proc)
        raise
    finally:
        _untrack(proc)
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

    events = _parse_stream(stdout)
    # 不是 stream-json（例如假 CLI 或老版本）才退回整段 JSON 解析，
    # 否则会把某条 assistant 事件误当成 result。
    is_stream = events.complete or events.kind_seen or bool(events.assistant_texts)
    data = None if is_stream else _parse_json(stdout)
    output = _result_text(events, data).strip()
    session_id = events.session_id or (data or {}).get("session_id")

    # 未登录：CLI 把 "Authentication required." 打到 stderr，且退出码可能仍是 0
    if "Authentication required" in raw:
        return CliResult(ok=False, output="", duration=duration, error=AUTH_HINT)

    if forced:
        # 已经等过 drain_timeout 了，这是兜底路径，不是常规退出
        return CliResult(
            ok=False,
            output=_clip(events.progress() or output, 2000),
            duration=duration,
            session_id=session_id,
            error="超过等待上限仍未结束，已强制终止（可用 ui.drain_timeout_seconds 放宽）",
            aborted=True,
        )

    if timed_out:
        # 关键：把中断前已经写出来的产出带回去。以前这里扔了空串，
        # 验收 agent 只能判「什么都没做」，然后触发下一轮从零重跑。
        return CliResult(
            ok=False,
            output=_clip(events.progress() or output, 2000),
            duration=duration,
            session_id=session_id,
            error=f"超时：超过 {timeout:.0f}s（已终止整个子进程组；中断前的产出已保留）",
            timed_out=True,
        )

    # 轮次上限：CLI 自己停的，不是崩溃。单独给一条能照做的提示，
    # 否则页面上只有一句 "Max turns (15) exceeded"，看不出该怎么改。
    failed = proc.returncode != 0 or events.is_error or bool((data or {}).get("is_error"))
    turns_text = " ".join([raw.lower()] + [t.lower() for t in events.assistant_texts[-2:]])
    if failed and "max turns" in turns_text:
        limit_desc = f"--max-turns {max_turns}" if max_turns and max_turns > 0 else "CLI 默认上限"
        return CliResult(
            ok=False,
            output=_clip(output, 800),
            duration=duration,
            session_id=session_id,
            error=(
                f"worker 到达执行轮次上限（{limit_desc}），CLI 主动停下："
                "把 config.yaml / 设置里的 worker.max_turns 调大（0 = 不传，用 CLI 默认 500），"
                "或者把这条任务拆小一点。已改动的文件还在产出目录里。"
            ),
        )

    if proc.returncode != 0:
        return CliResult(
            ok=False,
            output="",
            duration=duration,
            session_id=session_id,
            error=f"CLI 退出码 {proc.returncode}: {_diagnose(raw)}",
        )

    if not is_stream and data is None:
        return CliResult(ok=False, output="", duration=duration, error=_diagnose(raw))

    is_error = events.is_error or bool((data or {}).get("is_error"))
    return CliResult(
        ok=not is_error,
        output=output,
        duration=duration,
        session_id=session_id,
        error=(output[:800] if is_error and output else (_diagnose(raw) if is_error else None)),
    )



def run_worker(
    task: str,
    *,
    cwd: Path,
    feedback: str = "",
    guidance: str = "",
    resume_session: str = "",
    on_prompt: Callable[[str], None] | None = None,
    **kwargs,
) -> CliResult:
    """worker agent：干活。

    feedback 非空时是上一轮验收打回的原因；guidance 是用户在运行过程中插入的引导
    （排队投递，到这里才拼进 prompt —— 所以不会打断已经在跑的那一轮）。

    resume_session 非空时在同一个 CLI 会话里续跑（超时重试用），省掉重复劳动。
    """
    extra = ""
    if feedback.strip():
        extra = (
            "\n上一轮验收没通过，原因是：\n"
            f"{feedback.strip()}\n"
            "请针对这个问题修改，别重做无关部分。\n"
        )
    if resume_session:
        extra += RESUME_HINT
    guide = ""
    if guidance.strip():
        guide = GUIDANCE_BLOCK.format(notes=guidance.strip())
    prompt = WORKER_PROMPT.format(
        cwd=Path(cwd).expanduser().resolve(), task=task, feedback=extra, guidance=guide
    )
    if on_prompt is not None:
        on_prompt(prompt)   # 页面要把「这次调用真正收到的 prompt」原样打出来
    return run_cli(prompt, cwd=cwd, resume_session=resume_session or None, **kwargs)


def run_verifier(
    task: str,
    verify: str,
    worker_output: str,
    *,
    cwd: Path,
    scratch: Path | None = None,
    max_retries: int = 0,
    on_prompt: Callable[[str], None] | None = None,
    **kwargs,
) -> Verdict:
    """验收 agent：自己写 test 脚本自己跑，判定是否达标。

    被测目录 = cwd（只读，通过 add_dirs 暴露）；测试脚本写到独立的 scratch 目录，
    这样即使验收 agent 用 Write/Bash 写脚本，也不会污染 worker 的产出。

    验收 agent 自身的请求失败（超时/429/CLI 报错）会重试 max_retries 次，
    仍失败则返回 error 非空的 Verdict（由调用方决定是否终止流程）。
    """
    workdir = Path(cwd).expanduser().resolve()
    scratch = Path(scratch).expanduser().resolve() if scratch else workdir / ".verify"
    scratch.mkdir(parents=True, exist_ok=True)

    criteria = verify.strip() or "任务描述里提到的产物与行为（自行设计可执行的静态检查）"
    prompt = VERIFIER_PROMPT.format(
        workdir=workdir,
        scratch=scratch,
        task=task,
        verify=criteria,
        worker_output=_clip(worker_output, 1500) or "(无输出)",
    )
    if on_prompt is not None:
        on_prompt(prompt)   # 重试时 prompt 不变，只打一次
    extra_dirs = [workdir] + list(kwargs.pop("add_dirs", []) or [])

    result: CliResult | None = None
    last_err = "未知错误"
    for attempt in range(max(1, max_retries + 1)):
        result = run_cli(prompt, cwd=scratch, add_dirs=extra_dirs, **kwargs)
        if result.ok:
            break
        if result.aborted:
            # 用户要求退出：不要把它当成"验收请求失败"去重试
            return Verdict(
                passed=False,
                reason="",
                fix="",
                duration=result.duration,
                error=result.error or "已按用户请求中止",
                aborted=True,
            )
        last_err = result.error or "未知错误"
        if attempt < max_retries:
            time.sleep(min(2 ** attempt, 8))

    if result is None or not result.ok:
        return Verdict(
            passed=False,
            reason="",
            fix="",
            duration=result.duration if result else 0.0,
            error=f"验收 agent 请求失败（重试 {max_retries} 次后仍失败）：{last_err}",
        )

    data = _extract_json(result.output)
    if data is None or "pass" not in data:
        # 解析不出结论就保守判失败，并把原文交给 worker 参考
        return Verdict(
            passed=False,
            reason="验收 agent 没有给出结构化结论",
            fix=_clip(result.output, 600),
            raw=result.output,
            duration=result.duration,
        )

    passed = bool(data.get("pass"))
    return Verdict(
        passed=passed,
        reason=str(data.get("reason") or ("通过" if passed else "未通过")),
        fix=str(data.get("fix") or ""),
        raw=result.output,
        duration=result.duration,
    )


# 进程退出（正常结束 / 未捕获异常）时兜底：不留活着的 CLI 子进程。
# 被 SIGKILL 时无法执行，那种情况只能靠进程组本身被一起回收。
atexit.register(terminate_all_cli)
