"""worker / verifier agent：把子任务丢给本机 codebuddy CLI 的无头模式执行。

对应文档：https://www.codebuddy.cn/docs/cli/headless
关键三点：
  1. `-p` 非交互执行
  2. `-y` 必须带，否则文件读写/命令执行会被权限拦下
  3. `--output-format json` 便于程序解析（这个版本返回消息数组，result 在最后一项）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

WORKER_PROMPT = """你是 worker agent，负责独立完成下面这一个子任务。

工作目录：{cwd}
子任务：{task}
{feedback}
要求：
1. 直接在磁盘上动手（写文件 / 改文件 / 跑命令），不要只描述做法。
2. 只做这一个子任务，不要顺手重构或扩展别的文件。
3. 完成后用中文汇报 1~3 行：动了哪些文件、怎么验证的、结果如何。
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
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _KEEP_ENV or key.startswith(_KEEP_ENV_PREFIX)
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


@dataclass
class Verdict:
    passed: bool
    reason: str
    fix: str
    raw: str = ""
    duration: float = 0.0
    error: str | None = None  # 非空表示验收 agent 自身的请求失败（不是"验收不通过"）


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


def _terminate_tree(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """给整个子进程组发信号，避免 CLI 自己起的子进程变成孤儿。"""
    if proc.poll() is not None:
        return
    if not hasattr(os, "killpg"):
        proc.kill()
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.kill()
        return
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_cli(
    prompt: str,
    *,
    cwd: Path,
    cli: str = "codebuddy",
    model: str = "",
    allowed_tools: str = "Read,Write,Edit,Bash",
    add_dirs: list[Path] | None = None,
    timeout: float = 300.0,
    max_turns: int = 0,
    extra_env: dict[str, str] | None = None,
) -> CliResult:
    cwd = Path(cwd).expanduser().resolve()
    cwd.mkdir(parents=True, exist_ok=True)

    cli_path = check_cli(cli)
    cmd = [cli_path, "-p", prompt, "--output-format", "json", "-y"]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if model:
        cmd += ["--model", model]
    if max_turns and max_turns > 0:
        cmd += ["--max-turns", str(max_turns)]
    for d in add_dirs or []:
        cmd += ["--add-dir", str(d)]

    env = _child_env(extra_env)

    started = time.time()
    # start_new_session：给 CLI 单独一个进程组，中断/超时时可以整组回收
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_tree(proc)
        stdout, stderr = proc.communicate()
        return CliResult(
            ok=False,
            output="",
            duration=time.time() - started,
            error=f"超时：超过 {timeout:.0f}s（已终止整个子进程组）",
        )
    except KeyboardInterrupt:
        _terminate_tree(proc)
        raise

    duration = time.time() - started
    raw = f"{stdout or ''}\n{stderr or ''}".strip()
    data = _parse_json(stdout)

    # 未登录：CLI 把 "Authentication required." 打到 stderr，且退出码可能仍是 0
    if "Authentication required" in raw:
        return CliResult(ok=False, output="", duration=duration, error=AUTH_HINT)

    if proc.returncode != 0:
        return CliResult(
            ok=False,
            output="",
            duration=duration,
            session_id=(data or {}).get("session_id"),
            error=f"CLI 退出码 {proc.returncode}: {_diagnose(raw)}",
        )

    if data is None:
        return CliResult(ok=False, output="", duration=duration, error=_diagnose(raw))

    output = data.get("result") or data.get("message") or ""
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False)
    is_error = bool(data.get("is_error"))
    return CliResult(
        ok=not is_error,
        output=output.strip(),
        duration=duration,
        session_id=data.get("session_id"),
        error=(output[:800] if is_error else None),
    )


def run_worker(
    task: str,
    *,
    cwd: Path,
    feedback: str = "",
    **kwargs,
) -> CliResult:
    """worker agent：干活。feedback 非空时是上一轮验收打回的原因。"""
    extra = ""
    if feedback.strip():
        extra = (
            "\n上一轮验收没通过，原因是：\n"
            f"{feedback.strip()}\n"
            "请针对这个问题修改，别重做无关部分。\n"
        )
    prompt = WORKER_PROMPT.format(cwd=Path(cwd).expanduser().resolve(), task=task, feedback=extra)
    return run_cli(prompt, cwd=cwd, **kwargs)


def run_verifier(
    task: str,
    verify: str,
    worker_output: str,
    *,
    cwd: Path,
    scratch: Path | None = None,
    max_retries: int = 0,
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
    extra_dirs = [workdir] + list(kwargs.pop("add_dirs", []) or [])

    result: CliResult | None = None
    last_err = "未知错误"
    for attempt in range(max(1, max_retries + 1)):
        result = run_cli(prompt, cwd=scratch, add_dirs=extra_dirs, **kwargs)
        if result.ok:
            break
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
