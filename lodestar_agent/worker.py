"""worker / 验收的任务编排：拼 prompt、跑一次 agent、把结果整理成结构化结论。

这里刻意只管「说什么」，不管「怎么跑」：

  怎么跑（子进程 / HTTP、超时、进程树回收）→ providers/
  prompt 长什么样                              → prompts.py
  跑完算不算通过                               → 本文件（`Verdict`）

以前这几层是混在一起的（`CliOptions` 既有 prompt 参数又有进程参数），加一个模型来源
就得同时改三处；现在换来源只改配置。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .parsing import clip as _clip_text, extract_object
from .prompts import (
    GUIDANCE_BLOCK,
    RESUME_HINT,
    VERIFIER_PROMPT,
    WORKER_PROMPT,
)
from .providers.base import AgentRuntime, RunResult
from .providers.subproc import terminate_all

# 老名字保留：进程收尾的语义没变，调用方不必跟着改
terminate_all_cli = terminate_all


@dataclass
class Verdict:
    passed: bool
    reason: str
    fix: str
    raw: str = ""
    duration: float = 0.0
    error: str | None = None  # 非空表示验收 agent 自身的请求失败（不是"验收不通过"）
    aborted: bool = False     # True 表示验收过程被用户请求退出打断


def _clip(text: str, limit: int = 400) -> str:
    """错误信息专用裁剪；实现与展示层共用 parsing.clip。"""
    return _clip_text(text, limit)


def build_worker_prompt(task: str, *, cwd: Path, feedback: str = "",
                        guidance: str = "", resume_session: str = "") -> str:
    """worker 的 prompt：任务 + 上一轮打回原因 + 用户引导 + 续跑提示。

    feedback 非空时是上一轮验收打回的原因；guidance 是用户在运行过程中插入的引导
    （排队投递，到这里才拼进 prompt —— 所以不会打断已经在跑的那一轮）。
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
    return WORKER_PROMPT.format(
        cwd=Path(cwd).expanduser().resolve(), task=task, feedback=extra, guidance=guide,
    )


def run_worker(
    task: str,
    agent: AgentRuntime,
    *,
    cwd: Path,
    feedback: str = "",
    guidance: str = "",
    resume_session: str = "",
    on_prompt: Callable[[str], None] | None = None,
) -> RunResult:
    """worker agent：干活。

    agent 只要求实现 `run(prompt, cwd=…)`：CLI 来源是起子进程，API 来源是工具循环，
    这里不关心。
    """
    prompt = build_worker_prompt(
        task, cwd=cwd, feedback=feedback, guidance=guidance, resume_session=resume_session,
    )
    if on_prompt is not None:
        on_prompt(prompt)   # 页面要把「这次调用真正收到的 prompt」原样打出来
    return agent.run(prompt, cwd=Path(cwd).expanduser().resolve(),
                     resume_session=resume_session or None)


def build_verifier_prompt(task: str, verify: str, worker_output: str, *,
                          cwd: Path, scratch: Path) -> str:
    workdir = Path(cwd).expanduser().resolve()
    criteria = verify.strip() or "任务描述里提到的产物与行为（自行设计可执行的静态检查）"
    return VERIFIER_PROMPT.format(
        workdir=workdir,
        scratch=Path(scratch).expanduser().resolve(),
        task=task,
        verify=criteria,
        worker_output=_clip(worker_output, 1500) or "(无输出)",
    )


def run_verifier(
    task: str,
    verify: str,
    worker_output: str,
    agent: AgentRuntime,
    *,
    cwd: Path,
    scratch: Path | None = None,
    max_retries: int = 0,
    on_prompt: Callable[[str], None] | None = None,
) -> Verdict:
    """验收 agent：自己写 test 脚本自己跑，判定是否达标。

    被测目录 = cwd（只读，通过 extra_dirs 暴露）；测试脚本写到独立的 scratch 目录，
    这样即使验收 agent 用 Write/Bash 写脚本，也不会污染 worker 的产出。

    验收 agent 自身的请求失败（超时/429/接口报错）会重试 max_retries 次，
    仍失败则返回 error 非空的 Verdict（由调用方决定是否终止流程）。
    """
    workdir = Path(cwd).expanduser().resolve()
    scratch = Path(scratch).expanduser().resolve() if scratch else workdir / ".verify"
    scratch.mkdir(parents=True, exist_ok=True)

    prompt = build_verifier_prompt(task, verify, worker_output, cwd=workdir, scratch=scratch)
    if on_prompt is not None:
        on_prompt(prompt)   # 重试时 prompt 不变，只打一次

    result: RunResult | None = None
    last_err = "未知错误"
    for attempt in range(max(1, max_retries + 1)):
        result = agent.run(prompt, cwd=scratch, extra_dirs=[workdir])
        if result.ok:
            break
        if result.aborted:
            # 用户要求退出：不要把它当成"验收请求失败"去重试
            return Verdict(
                passed=False, reason="", fix="", duration=result.duration,
                error=result.error or "已按用户请求中止", aborted=True,
            )
        last_err = result.error or "未知错误"
        if attempt < max_retries:
            time.sleep(min(2 ** attempt, 8))

    if result is None or not result.ok:
        return Verdict(
            passed=False, reason="", fix="",
            duration=result.duration if result else 0.0,
            error=f"验收 agent 请求失败（重试 {max_retries} 次后仍失败）：{last_err}",
        )

    data = extract_object(result.output)
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
