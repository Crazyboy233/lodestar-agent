"""LangGraph 状态机：主 agent（plan，每轮动态规划）+ worker（执行）+ 验收 agent。

    START → plan ─┬─ dispatch ─► worker → verify ─┐
                  │                                │
                  └─ done ──────► synthesize ←─────┘
                                            （plan 循环回来读产出+验收，修订任务方案再发布）

plan 是主 agent 的唯一思考节点，也是循环节点：
  - 第 1 次调用：把目标拆成「任务方案」（1~3 条），分发第一条。
  - 之后每轮：读当前任务的产出 + 验收结果，决定「打回重做」还是「定稿并发布下一个目标」，
    并且可以动态修订剩余的任务方案（增删改任务、安排补救）。
「完成」由验收 agent 判定，不采信 worker 自述；循环上限由代码强制，不靠模型自觉。
"""

from __future__ import annotations

import json
import operator
import re
import time
from typing import Annotated, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from .config import Config
from .worker import run_verifier, run_worker


class AgentFailure(RuntimeError):
    """某个 agent 的模型请求在重试耗尽后仍然失败，用于终止流程并给出清晰提示。"""

    def __init__(self, node: str, reason: str):
        self.node = node
        self.reason = reason
        super().__init__(f"{node}: {reason}")


def _explain(exc: Exception | None) -> str:
    if exc is None:
        return "未知错误"
    msg = str(exc)
    low = msg.lower()
    if "429" in low or "rate limit" in low or "too many" in low:
        return "请求被限流（429）：稍后重试，或降低并发/频率"
    if "timeout" in low or "timed out" in low or "timedout" in low:
        return "请求超时：网络慢或模型服务过载"
    if "401" in low or "auth" in low or "api key" in low or "invalid" in low:
        return "认证失败：检查 api key 或 codebuddy 登录态"
    if "connection" in low or "connect" in low or "network" in low or "refused" in low:
        return "网络连接问题：检查网络 / base_url 是否可达"
    if "500" in low or "502" in low or "503" in low or "server" in low:
        return "服务端 5xx：模型服务暂时不可用"
    return f"未分类错误：{msg[:200]}"


def _call_llm(llm, node: str, prompt: str, max_retries: int):
    """带重试的主 agent 模型调用；重试耗尽则抛 AgentFailure。"""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return llm.invoke(prompt)
        except Exception as exc:  # noqa: BLE001 - 统一转成 AgentFailure
            last_exc = exc
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 8))
    raise AgentFailure(node, _explain(last_exc))

PLAN_INIT_PROMPT = """你是主 agent，负责把用户目标拆成「任务方案」。

用户目标：
{goal}

你的工作目录（worker 就在这个目录里干活）：
{workdir}

拆成 1~3 个子任务，每个子任务：
- task：worker 能直接照着做，写清文件路径和要做什么；文件路径一律写相对于工作目录的路径，或直接用工作目录下的子目录，不要写 /workspace、/tmp 之类与工作目录无关的绝对路径
- verify：验收 agent 能照着跑的检查，必须是可执行的，三种任选其一
  ① 文件存在，且包含指定字符串
  ② 运行命令 <cmd> 退出码为 0，且输出包含 "<期望字符串>"
  ③ 文件通过 <检查工具>（node --check、python -m py_compile、grep …）

禁止无法执行的验收标准：浏览器打开、控制台无报错、响应式/窄屏、视觉/布局、流畅/美观/体验。
遇到这类目标改写成等价的静态检查。一条子任务只对应一个可验证结果。

只输出 JSON 数组，不要解释、不要 Markdown 代码块：
[{{"task": "子任务一", "verify": "验收检查一"}}, {{"task": "子任务二", "verify": "验收检查二"}}]
"""

PLAN_ROUND_PROMPT = """你是主 agent，正在维护「任务方案」。下面是这一轮的实际情况，请据此动态规划。

用户目标：
{goal}

已完成 / 已失败的记录：
{records}

当前任务：
{current}

这一轮的验收结果：
{verdict}

当前任务已尝试 {attempts} 轮（上限 {max_rounds}）。

剩余的任务方案（当前任务之后尚未分发的部分）：
{remaining}

工作目录（worker 就在这个目录里干活）：
{workdir}

情况：{situation}

只输出一个 JSON 对象，不要解释、不要 Markdown 代码块：
{{"feedback": "打回重做时的修改指令（否则填空）",
  "remaining": [{{"task":"...","verify":"..."}}],
  "note": "一句话说明你的决策"}}

- 情况是「打回重做」：feedback 写清楚让 worker 改哪里（可参考验收意见），remaining 保持原样即可。
- 情况是「定稿，规划下一步」：remaining 写修订后的剩余任务方案——可以补充新任务、修改验收标准、删除已不需要的任务、或安排补救；没有剩余了就写 []。文件路径同样要相对于工作目录，不要写 /workspace 之类的绝对路径。
"""

SYNTH_PROMPT = """你是主 agent，负责对这次任务做最终交付汇报。

用户目标：
{goal}

每条子任务的执行与验收结果：
{records}

请用中文写 6 行以内的最终答复：整体是否达成、哪些通过、哪些没通过（写清原因）、留在磁盘上的产物。
不要复述过程细节。
"""

# 验收标准里出现这些词，说明它不是可执行检查，一律丢掉让验收 agent 自己设计
_BANNED_IN_VERIFY = (
    "浏览器", "控制台", "响应式", "窄屏", "视觉", "布局美观", "美观", "体验", "流畅",
    "截图", "肉眼", "手动", "用户友好",
)
# 验收标准至少要含一个"可执行"的信号
_EXECUTABLE_MARKERS = (
    "grep", "node --check", "py_compile", "pytest", "curl", "cat ", "ls ", "test ",
    "存在", "包含", "退出码", "输出", "diff", "wc ",
)


class State(TypedDict, total=False):
    goal: str
    plan: list[dict]          # 任务方案：尚未分发的剩余任务
    current: dict             # 当前分发给 worker 的任务
    attempts: int
    feedback: str
    last_output: str
    last_error: str
    last_duration: float
    verdict_passed: bool
    verdict_reason: str
    verdict_fix: str
    action: str               # dispatch / done
    stage: str                # init / round（用于展示）
    note: str
    plan_raw: str             # 拆任务方案时主 agent 的原始输出
    decision_raw: str         # 每轮动态决策时主 agent 的原始输出
    verdict_raw: str          # 验收 agent 的原始输出
    records: Annotated[list[dict], operator.add]
    answer: str


def _sanitize_verify(verify: str) -> str:
    text = (verify or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if any(word in text for word in _BANNED_IN_VERIFY):
        return ""
    if not any(marker in lowered for marker in _EXECUTABLE_MARKERS):
        return ""
    return text


def _normalize_items(items: object) -> list[dict]:
    out: list[dict] = []
    for item in items or []:
        if isinstance(item, str):
            task, verify = item.strip(), ""
        elif isinstance(item, dict):
            task = str(item.get("task") or item.get("描述") or "").strip()
            verify = str(item.get("verify") or item.get("验收") or "").strip()
        else:
            continue
        if task:
            out.append({"task": task, "verify": _sanitize_verify(verify)})
    return out


def _parse_plan(text: str) -> list[dict]:
    raw = (text or "").strip()
    match = re.search(r"\[[\s\S]*\]", raw)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return _normalize_items(data) if isinstance(data, list) else []


def _extract_decision(text: str) -> dict | None:
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
    return None


def _format_records(records: list[dict]) -> str:
    if not records:
        return "(暂无)"
    return "\n".join(
        f"- 子任务 {r['index']}: {r['task']} → {'通过' if r['passed'] else '未通过'}（{r['attempts']} 轮，{r['reason']}）"
        for r in records
    )


def build_graph(cfg: Config, use_llm: bool = True, llm=None, checkpointer=None):
    if llm is None and use_llm:
        llm = ChatOpenAI(
            model=cfg.main_agent.model,
            api_key=cfg.main_agent.api_key,
            base_url=cfg.main_agent.base_url,
            temperature=cfg.main_agent.temperature,
        )

    def _split_plan(goal: str) -> tuple[list[dict], str]:
        """拆「任务方案」。JSON 解析失败时带上纠正提示重试，避免一次跑偏就降级。

        注意这里是「解析重试」，和 _call_llm 里的「请求重试」是两层：
        请求能正常返回、但内容不是合法 JSON 数组，算解析失败。
        """
        prompt = PLAN_INIT_PROMPT.format(goal=goal, workdir=cfg.worker.workdir)
        raw = ""
        for attempt in range(cfg.main_agent.plan_retries + 1):
            msg = _call_llm(llm, "plan(拆解任务方案)", prompt, cfg.main_agent.max_retries)
            raw = (getattr(msg, "content", "") or "").strip()
            plan = _parse_plan(raw)
            if plan:
                return plan, raw
            if attempt < cfg.main_agent.plan_retries:
                snippet = raw[:300] or "(空输出)"
                prompt = PLAN_INIT_PROMPT.format(goal=goal, workdir=cfg.worker.workdir) + (
                    "\n\n上一次的输出无法解析成 JSON 数组。这次只输出 JSON 数组本身，"
                    "不要任何解释文字、不要 Markdown 代码块。上一次的输出片段：\n" + snippet
                )
        return [], raw

    def _make_record(state: State, current: dict) -> dict:
        passed = state.get("verdict_passed", False)
        return {
            "index": len(state.get("records") or []) + 1,
            "task": current.get("task", ""),
            "verify": current.get("verify", ""),
            "passed": passed,
            "attempts": state.get("attempts", 1),
            "reason": "通过" if passed else (state.get("verdict_reason") or state.get("verdict_fix") or "未通过"),
            "duration": round(state.get("last_duration", 0.0), 1),
        }

    def _settle(state: State, current: dict, remaining: list[dict], note: str) -> dict:
        """定稿当前任务，发布下一个（或结束）。"""
        record = _make_record(state, current)
        if remaining:
            nxt, rest = remaining[0], remaining[1:]
            return {
                "action": "dispatch", "stage": "round", "current": nxt, "plan": rest,
                "attempts": 0, "feedback": "", "records": [record], "note": note,
            }
        return {
            "action": "done", "stage": "round", "current": None, "plan": [],
            "attempts": 0, "feedback": "", "records": [record], "note": note,
        }

    def plan_node(state: State) -> dict:
        current = state.get("current")
        plan = state.get("plan") or []

        # ── 第 1 次调用：拆「任务方案」，分发第一条 ──
        if current is None:
            plan_raw = ""
            note = ""
            if not plan:
                if llm is None:
                    plan = [{"task": state.get("goal", ""), "verify": ""}]
                else:
                    plan, plan_raw = _split_plan(state.get("goal", ""))
                    if not plan:
                        # 解析重试用尽仍拆不出来：退化为单任务，并把降级原因带到日志里
                        plan = [{"task": state.get("goal", ""), "verify": ""}]
                        note = (
                            f"拆解重试 {cfg.main_agent.plan_retries} 次后仍无法解析成 JSON，"
                            "已降级为单任务（goal 本身）"
                        )
            if not plan:
                return {"action": "done", "stage": "init", "plan": [], "current": None,
                        "note": "没有可执行的任务", "plan_raw": plan_raw}
            nxt, rest = plan[0], plan[1:]
            return {"action": "dispatch", "stage": "init", "current": nxt, "plan": rest,
                    "attempts": 0, "feedback": "", "note": note, "plan_raw": plan_raw}

        # ── 之后每轮：读产出 + 验收结果，动态规划 ──
        records = state.get("records") or []
        passed = state.get("verdict_passed", False)
        attempts = state.get("attempts", 1)

        # 执行失败（worker 的模型/CLI 请求失败）→ 按 worker.max_retries 重试，超限终止整个流程
        if state.get("last_error"):
            if attempts <= cfg.worker.max_retries:
                feedback = state.get("last_error") or "请重试"
                return {"action": "dispatch", "stage": "round", "current": current, "plan": plan,
                        "attempts": attempts, "feedback": feedback, "records": [],
                        "note": "执行失败，重试"}
            raise AgentFailure("worker(执行)", state.get("last_error") or "未知执行失败")

        # 验收不通过且未到轮次上限 → 打回重做
        if not passed and attempts < cfg.verifier.max_rounds:
            feedback = state.get("verdict_fix") or state.get("verdict_reason") or "请修改后重试"
            decision_raw = ""
            if llm is not None:
                decision_raw = getattr(_call_llm(
                    llm, "plan(动态决策)", PLAN_ROUND_PROMPT.format(
                        goal=state.get("goal", ""),
                        records=_format_records(records),
                        current=json.dumps(current, ensure_ascii=False),
                        verdict=state.get("verdict_reason", "未通过"),
                        attempts=attempts,
                        max_rounds=cfg.verifier.max_rounds,
                        remaining=json.dumps(plan, ensure_ascii=False) or "[]",
                        workdir=cfg.worker.workdir,
                        situation="打回重做",
                    ), cfg.main_agent.max_retries,
                ), "content", "") or ""
                data = _extract_decision(decision_raw)
                if data and data.get("feedback"):
                    feedback = str(data["feedback"]).strip()
            return {"action": "dispatch", "stage": "round", "current": current, "plan": plan,
                    "attempts": attempts, "feedback": feedback, "records": [],
                    "note": "验收不通过，打回重做", "decision_raw": decision_raw}

        # 通过 / 验收轮次耗尽 → 定稿，动态修订剩余方案
        revised = plan
        note = "通过，继续下一个任务" if passed else "轮次耗尽，判失败"
        decision_raw = ""
        if llm is not None:
            decision_raw = getattr(_call_llm(
                llm, "plan(动态决策)", PLAN_ROUND_PROMPT.format(
                    goal=state.get("goal", ""),
                    records=_format_records(records),
                    current=json.dumps(current, ensure_ascii=False),
                    verdict=state.get("verdict_reason", "通过" if passed else "未通过"),
                    attempts=attempts,
                    max_rounds=cfg.verifier.max_rounds,
                    remaining=json.dumps(plan, ensure_ascii=False) or "[]",
                    workdir=cfg.worker.workdir,
                    situation="定稿，规划下一步",
                ), cfg.main_agent.max_retries,
            ), "content", "") or ""
            data = _extract_decision(decision_raw)
            if data:
                if data.get("remaining") is not None:
                    revised = _normalize_items(data.get("remaining"))
                if data.get("note"):
                    note = str(data["note"]).strip()

        out = _settle(state, current, revised, note)
        out["decision_raw"] = decision_raw
        return out

    def worker_node(state: State) -> dict:
        item = state["current"]
        result = run_worker(
            item["task"],
            cwd=cfg.worker.workdir,
            feedback=state.get("feedback", ""),
            cli=cfg.worker.cli,
            model=cfg.worker.model,
            allowed_tools=cfg.worker.allowed_tools,
            add_dirs=cfg.worker.add_dirs,
            timeout=cfg.worker.timeout_seconds,
            max_turns=cfg.worker.max_turns,
            extra_env=cfg.worker.env,
        )
        return {
            "attempts": state.get("attempts", 0) + 1,
            "last_output": result.output,
            "last_error": result.error or "",
            "last_duration": round(result.duration, 1),
        }

    def verify_node(state: State) -> dict:
        item = state["current"]
        verdict_raw = ""
        if llm is None:  # dry-run：跳过验收，但执行失败照样算失败
            passed = not state.get("last_error")
            reason = "dry-run：跳过验收" if passed else f"worker 执行失败：{state['last_error']}"
            fix = ""
        elif state.get("last_error"):
            passed, reason = False, f"worker 执行失败：{state['last_error']}"
            fix = "上一步执行就失败了，请先让命令跑通再交付。"
        else:
            verdict = run_verifier(
                item["task"],
                item.get("verify", ""),
                state.get("last_output", ""),
                cwd=cfg.worker.workdir,
                scratch=cfg.worker.workdir / cfg.verifier.scratch_dir,
                max_retries=cfg.verifier.max_retries,
                cli=cfg.verifier.cli,
                model=cfg.verifier.model,
                allowed_tools=cfg.verifier.allowed_tools,
                timeout=cfg.verifier.timeout_seconds,
                extra_env=cfg.verifier.env,
            )
            if verdict.error:
                raise AgentFailure("verify(验收)", verdict.error)
            passed, reason, fix = verdict.passed, verdict.reason, verdict.fix
            verdict_raw = verdict.raw

        return {
            "verdict_passed": passed,
            "verdict_reason": "通过" if passed else reason,
            "verdict_fix": fix,
            "verdict_raw": verdict_raw,
        }

    def synth_node(state: State) -> dict:
        records = state.get("records") or []
        if llm is None:
            lines = [
                f"[{'PASS' if r['passed'] else 'FAIL'}] {r['task']}（{r['attempts']} 轮）\n{r['reason']}"
                for r in records
            ]
            return {"answer": "\n\n".join(lines)}
        msg = _call_llm(
            llm, "synthesize(汇总)",
            SYNTH_PROMPT.format(goal=state.get("goal", ""), records=_format_records(records)),
            cfg.main_agent.max_retries,
        )
        return {"answer": (getattr(msg, "content", "") or "").strip()}

    def route_after_plan(state: State) -> str:
        return "worker" if state.get("action") == "dispatch" else "synthesize"

    graph = StateGraph(State)
    graph.add_node("plan", plan_node)
    graph.add_node("worker", worker_node)
    graph.add_node("verify", verify_node)
    graph.add_node("synthesize", synth_node)

    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", route_after_plan, {"worker": "worker", "synthesize": "synthesize"})
    graph.add_edge("worker", "verify")
    graph.add_edge("verify", "plan")
    graph.add_edge("synthesize", END)

    return graph.compile(checkpointer=checkpointer)
