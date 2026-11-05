"""图的四个节点：plan（动态规划）/ worker（执行）/ verify（验收）/ synth（汇总）。

从 `graph.py` 里搬出来的独立类，依赖显式传进来（cfg / control / chat），不再靠
闭包捕获。这样节点可以单独构造和测试，`graph.py` 只剩「把节点连成图」。

节点不关心模型从哪来：主 agent 只说「我要一段文本」，worker / 验收只说「我要一个
会在目录里动手的 agent」；换成 API、codebuddy 还是别的 CLI，都在 `providers/` 里。
"""

from __future__ import annotations

import json
import time

from . import prompts
from .config import Config
from .control import Control
from .errors import AgentFailure, ModelAborted
from .llm import call_chat
from .parsing import extract_array, extract_object, normalize_items
from .providers import registry
from .records import dedupe, drained, settled
from .worker import run_verifier, run_worker

_PLAN_LABEL = "plan(拆解任务方案)"
_DECISION_LABEL = "plan(动态决策)"


class Nodes:
    """把一个 Config + 一个纯文本模型 + 一个控制面绑成四个节点函数。"""

    def __init__(self, cfg: Config, control: Control, chat=None):
        self.cfg = cfg
        self.control = control
        self.chat = chat

    # ── 主 agent 的模型调用 ────────────────────────────────
    def _interrupt(self) -> bool:
        """到点还没生成完才断。边界退出期间是「等它跑完」，过了上限才「断开」。"""
        deadline = self.control.drain_deadline
        return deadline is not None and time.time() >= deadline

    def _emit_prompt(self, kind: str, label: str, prompt: str) -> None:
        """把这次模型调用「真正收到的 prompt」原样推进事件流。

        页面上归为「派发」，默认折叠，展开就是全文 —— 想知道节点到底被喂了什么，
        不用再翻日志文件。
        """
        self.control.emit(f"prompt → {label}\n{prompt}", kind=kind, role="prompt")

    def _ask(self, node: str, label: str, prompt: str) -> str:
        """主 agent 的模型调用统一走这里：前后打点 + 可中断，页面才知道谁在干活。"""
        self.control.set_busy(node, model=self._main_label())
        self._emit_prompt(node, label, prompt)
        try:
            return call_chat(
                self.chat, label, prompt, self.cfg.main.max_retries, self._interrupt,
            )
        except ModelAborted:
            # 连接已断，别等结果：节点里的 stop 检查会把流程导向收尾
            self.control.emit(f"已断开 {label} 的模型连接（超过等待上限）", kind="stop")
            return ""
        finally:
            self.control.set_busy(None)

    def _split_plan(self, goal: str) -> tuple[list[dict], str]:
        """拆「任务方案」。JSON 解析失败时带上纠正提示重试，避免一次跑偏就降级。

        注意这里是「解析重试」，和 call_chat 里的「请求重试」是两层：
        请求能正常返回、但内容不是合法 JSON 数组，算解析失败。
        """
        guidance = prompts.format_guidance(self.control.take_notes("main"), "拆解任务方案")
        prompt = prompts.PLAN_INIT_PROMPT.format(
            goal=goal, workdir=self.cfg.worker.workdir, guidance=guidance,
        )
        raw = ""
        for attempt in range(self.cfg.main.plan_retries + 1):
            if self.control.stop_requested:
                # 已经要收工了，别再为了拆解去开新的模型调用
                return [], raw
            raw = self._ask("plan", _PLAN_LABEL, prompt).strip()
            plan = normalize_items(extract_array(raw) or [])
            if plan:
                return plan, raw
            if attempt < self.cfg.main.plan_retries:
                snippet = raw[:300] or "(空输出)"
                prompt = prompts.PLAN_INIT_PROMPT.format(
                    goal=goal, workdir=self.cfg.worker.workdir, guidance=guidance,
                ) + (
                    "\n\n上一次的输出无法解析成 JSON 数组。这次只输出 JSON 数组本身，"
                    "不要任何解释文字、不要 Markdown 代码块。上一次的输出片段：\n" + snippet
                )
        return [], raw

    def _decide(self, state: dict, situation: str, verdict_text: str) -> tuple[dict | None, str]:
        """每轮的动态决策：打回和定稿走同一个 prompt，差别只在 situation 与后续处理。

        以前这两段是各写一遍的（同一份 format 参数表出现两次），改提示词要改两处。
        """
        if self.chat is None:
            return None, ""
        current = state.get("current") or {}
        guidance = prompts.format_guidance(self.control.take_notes("main"), "决策")
        prompt = prompts.PLAN_ROUND_PROMPT.format(
            goal=state.get("goal", ""),
            records=prompts.format_records_block(state.get("records") or []),
            current=json.dumps(current, ensure_ascii=False),
            verdict=verdict_text,
            attempts=state.get("attempts", 1),
            max_rounds=self.cfg.verifier.max_rounds,
            remaining=json.dumps(state.get("plan") or [], ensure_ascii=False) or "[]",
            workdir=self.cfg.worker.workdir,
            situation=situation,
            guidance=guidance,
        )
        msg = self._ask("plan", _DECISION_LABEL, prompt)
        raw = msg or ""
        return extract_object(raw), raw

    # ── 分支结果 ──────────────────────────────────────────
    def _settle(self, state: dict, current: dict, remaining: list[dict], note: str) -> dict:
        """定稿当前任务，发布下一个（或结束）。"""
        record = settled(state, current)
        base = {
            "stage": "round", "attempts": 0, "feedback": "", "records": [record],
            "note": note,
            # 定稿即换会话：别让它续跑到上一条任务的上下文里
            "worker_session": "", "last_error": "",
        }
        if remaining:
            return {**base, "action": "dispatch", "current": remaining[0],
                    "plan": remaining[1:]}
        return {**base, "action": "done", "current": None, "plan": []}

    def _drain(self, state: dict, current: dict | None, plan: list[dict]) -> dict:
        """边界退出的统一出口：当前这一轮已经跑完，不再开新的节点。

        aborted_stage 记录「接下来本该做什么」，决定 --resume 从哪儿接：
          worker = 还没跑，重跑这条
          verify = 跑了没验，直接补验收（不重跑 worker）
          done   = 验证过，这条已完成，下次直接派下一个
        """
        reason = (
            state.get("abort_reason")
            or self.control.stop_reason
            or "用户请求退出"
        )
        forced = bool(state.get("abort"))   # 兜底强杀过：这一轮的产出不完整，要重跑
        attempts = state.get("attempts", 0)
        ready = bool(state.get("verdict_ready"))
        if forced or not attempts:
            stage = "worker"
        elif ready:
            stage = "done"
        else:
            stage = "verify"
        return {
            "action": "done", "stage": "abort", "current": None, "plan": plan,
            "attempts": attempts, "feedback": "",
            "records": drained(state, current),
            "abort": forced, "draining": True, "abort_reason": reason,
            "aborted_task": {} if stage == "done" else (current or {}),
            "aborted_stage": stage,
            "note": f"{reason}：当前这一轮已结束，不再开始新的节点，进入收尾",
        }

    # ── plan ─────────────────────────────────────────────
    def plan(self, state: dict) -> dict:
        current = state.get("current")
        plan = state.get("plan") or []

        # ── 已请求退出：不再开新的模型调用，直接收尾 ──
        if state.get("abort") or self.control.stop_requested:
            return self._drain(state, current, plan)

        # ── 第 1 次调用：拆「任务方案」，分发第一条 ──
        if current is None:
            plan_raw = ""
            note = ""
            if not plan:
                if self.chat is None:
                    plan = [{"task": state.get("goal", ""), "verify": ""}]
                else:
                    plan, plan_raw = self._split_plan(state.get("goal", ""))
                    if not plan:
                        # 解析重试用尽仍拆不出来：退化为单任务，并把降级原因带到日志里
                        plan = [{"task": state.get("goal", ""), "verify": ""}]
                        note = (
                            f"拆解重试 {self.cfg.main.plan_retries} 次后仍无法解析成 JSON，"
                            "已降级为单任务（goal 本身）"
                        )
            if self.control.stop_requested:
                # 拆解模型刚回复完就被要求退出：任务方案留着，但一条都不派
                return self._drain(state, None, plan)
            if not plan:
                return {"action": "done", "stage": "init", "plan": [], "current": None,
                        "note": "没有可执行的任务", "plan_raw": plan_raw}
            return {"action": "dispatch", "stage": "init", "current": plan[0],
                    "plan": plan[1:], "attempts": 0, "feedback": "", "note": note,
                    "plan_raw": plan_raw, "worker_session": "", "last_error": ""}

        # ── 之后每轮：读产出 + 验收结果，动态规划 ──
        passed = state.get("verdict_passed", False)
        attempts = state.get("attempts", 1)

        # 执行失败（worker 的模型/CLI 请求失败）→ 按 worker.max_retries 重试，超限终止整个流程
        if state.get("last_error"):
            if attempts <= self.cfg.worker.max_retries:
                return {"action": "dispatch", "stage": "round", "current": current,
                        "plan": plan, "attempts": attempts,
                        "feedback": state.get("last_error") or "请重试", "records": [],
                        "note": "执行失败，重试"}
            raise AgentFailure("worker(执行)", state.get("last_error") or "未知执行失败")

        # 验收不通过且未到轮次上限 → 打回重做
        if not passed and attempts < self.cfg.verifier.max_rounds:
            data, decision_raw = self._decide(
                state, "打回重做", state.get("verdict_reason", "未通过"),
            )
            feedback = state.get("verdict_fix") or state.get("verdict_reason") or "请修改后重试"
            if data and data.get("feedback"):
                feedback = str(data["feedback"]).strip()
            if self.control.stop_requested:
                # 决策模型刚回复完：不要接着派工，直接收尾
                return self._drain(state, current, plan)
            return {"action": "dispatch", "stage": "round", "current": current,
                    "plan": plan, "attempts": attempts, "feedback": feedback, "records": [],
                    "note": "验收不通过，打回重做", "decision_raw": decision_raw,
                    # 打回是「换个方向改」，新会话更干净；续跑只用于执行失败/超时
                    "worker_session": "", "last_error": ""}

        # 通过 / 验收轮次耗尽 → 定稿，动态修订剩余方案
        revised = plan
        note = "通过，继续下一个任务" if passed else "轮次耗尽，判失败"
        data, decision_raw = self._decide(
            state, "定稿，规划下一步",
            state.get("verdict_reason", "通过" if passed else "未通过"),
        )
        if data:
            if data.get("remaining") is not None:
                revised = normalize_items(data.get("remaining"))
            if data.get("note"):
                note = str(data["note"]).strip()

        if self.control.stop_requested:
            # 定稿决策刚回来：把当前这条按真实结果记下，然后收尾，不派下一个
            return self._drain(state, current, plan)

        out = self._settle(state, current, revised, note)
        out["decision_raw"] = decision_raw
        return out

    # ── worker ───────────────────────────────────────────
    def worker(self, state: dict) -> dict:
        item = state["current"]
        control = self.control

        # 进入节点时已经收到退出请求：这一轮连跑都不跑（还没开始的调用不该开始）
        if control.stop_requested:
            return {
                "draining": True,
                "abort_reason": control.stop_reason or "用户请求退出",
                "aborted_stage": "worker",
                "aborted_task": item,
                "skipped": True,
            }

        # 取走排队中的引导（消费即清空）。此刻 worker 还没启动，
        # 所以拼进去不会打断任何东西 —— 这就是「引导但不打断」的落点。
        guidance = "\n".join(f"- {n['text']}" for n in control.take_notes("worker"))

        # 上一次是执行失败 / 超时，且已经拿到了 CLI 会话 id → 在同一个会话里续跑。
        # 否则每轮都是全新会话，同一份活重做三遍，永远做不完（超时→重试→再超时的根源）。
        resume_session = ""
        if state.get("last_error") and state.get("worker_session"):
            resume_session = str(state["worker_session"])

        control.set_busy(
            "worker",
            model=self._worker_label(),
            task=item["task"],
            attempt=state.get("attempts", 0) + 1,
            max_attempts=self.cfg.verifier.max_rounds,
        )
        try:
            result = run_worker(
                item["task"],
                self._worker_agent(),
                cwd=self.cfg.worker.workdir,
                feedback=state.get("feedback", ""),
                guidance=guidance,
                resume_session=resume_session,
                on_prompt=lambda text: self._emit_prompt("worker", "worker(执行)", text),
            )
        finally:
            control.set_busy(None)

        out = {
            "attempts": state.get("attempts", 0) + 1,
            "last_output": result.output,
            "last_error": result.error or "",
            "last_duration": round(result.duration, 1),
            "verdict_ready": False,
            # 记下会话 id：下一轮如果是超时/失败重试，就在这个会话里接着跑
            "worker_session": result.session_id or str(state.get("worker_session") or ""),
        }
        if result.aborted:
            # 等超了上限才被强杀：这一轮产出不可信，resume 时重跑
            out["abort"] = True
            out["abort_reason"] = control.stop_reason or "用户请求退出"
            out["aborted_stage"] = "worker"
            out["aborted_task"] = item
            out["last_error"] = ""
        elif control.stop_requested:
            # 边界退出：这一轮完整跑完了，只是不再进入验收
            out["draining"] = True
            out["aborted_stage"] = "verify"
            out["aborted_task"] = item
            out["last_error"] = ""
        return out

    # ── verify ───────────────────────────────────────────
    def verify(self, state: dict) -> dict:
        item = state["current"]
        control = self.control
        verdict_raw = ""

        # 还没开始验收就收到退出：不开始这次模型调用（它属于「下一个」）
        if control.stop_requested:
            return {"draining": True, "verdict_ready": False,
                    "aborted_stage": "verify", "aborted_task": item}

        if self.chat is None:  # dry-run：跳过验收，但执行失败照样算失败
            passed = not state.get("last_error")
            reason = "dry-run：跳过验收" if passed else f"worker 执行失败：{state['last_error']}"
            fix = ""
        elif state.get("last_error"):
            passed, reason = False, f"worker 执行失败：{state['last_error']}"
            fix = "上一步执行就失败了，请先让命令跑通再交付。"
        else:
            control.set_busy(
                "verifier",
                model=self._verifier_label(),
                task=item["task"],
                attempt=state.get("attempts", 0),
                max_attempts=self.cfg.verifier.max_rounds,
            )
            try:
                verdict = run_verifier(
                    item["task"],
                    item.get("verify", ""),
                    state.get("last_output", ""),
                    self._verifier_agent(),
                    cwd=self.cfg.worker.workdir,
                    scratch=self.cfg.worker.workdir / self.cfg.verifier.scratch_dir,
                    max_retries=self.cfg.verifier.max_retries,
                    on_prompt=lambda text: self._emit_prompt("verifier", "verifier(验收)", text),
                )
            finally:
                control.set_busy(None)
            if verdict.aborted:
                return {
                    "verdict_passed": False,
                    "verdict_reason": "验收超时被强制终止",
                    "verdict_fix": "",
                    "verdict_raw": "",
                    "verdict_ready": False,
                    "abort": True,
                    "abort_reason": control.stop_reason or "用户请求退出",
                    "aborted_stage": "verify",
                    "aborted_task": item,
                }
            if verdict.error:
                raise AgentFailure("verify(验收)", verdict.error)
            passed, reason, fix = verdict.passed, verdict.reason, verdict.fix
            verdict_raw = verdict.raw

        out = {
            "verdict_passed": passed,
            "verdict_reason": "通过" if passed else reason,
            "verdict_fix": fix,
            "verdict_raw": verdict_raw,
            "verdict_ready": True,
        }
        if control.stop_requested:
            # 验收刚跑完，结果有效；只是不再进入下一轮规划
            out["draining"] = True
            out["aborted_stage"] = "done"
        return out

    # ── synthesize ───────────────────────────────────────
    def synth(self, state: dict) -> dict:
        records = state.get("records") or []

        # 边界退出：用确定性文案收尾，不再调模型（退出要快、要可预期）
        if state.get("abort") or state.get("draining"):
            return {"answer": self._drain_report(state)}

        if self.chat is None:
            lines = [
                f"[{'PASS' if r['passed'] else 'FAIL'}] {r['task']}（{r['attempts']} 轮）\n{r['reason']}"
                for r in dedupe(records)
            ]
            return {"answer": "\n\n".join(lines)}

        answer = self._ask(
            "synth", "synthesize(汇总)",
            prompts.SYNTH_PROMPT.format(
                goal=state.get("goal", ""),
                records=prompts.format_records_block(records),
            ),
        )
        return {"answer": answer.strip()}

    def _drain_report(self, state: dict) -> str:
        remaining = state.get("plan") or []
        shown = dedupe(state.get("records") or [])
        ok = sum(1 for r in shown if r["passed"] and r.get("verified", True))
        bad = sum(1 for r in shown if not r["passed"] and r.get("verified", True))
        skip = len(shown) - ok - bad
        lines = [
            f"边界退出：{state.get('abort_reason') or '用户请求退出'}"
            "（当前这一轮已跑完，不再开始新的节点）",
            f"进度：已记录 {len(shown)} 条（通过 {ok} / 未通过 {bad} / 未验收 {skip}），"
            f"剩余 {len(remaining)} 条未执行",
        ]
        for r in shown:
            verified = r.get("verified", True)
            mark = "PASS" if (r["passed"] and verified) else ("SKIP" if not verified else "FAIL")
            lines.append(f"  [{mark}] {r['task']}（{r['attempts']} 轮 / {r['reason']}）")
        if remaining:
            lines.append(f"未执行 {len(remaining)} 条：")
            for i, item in enumerate(remaining, 1):
                lines.append(f"  {i}. {item.get('task', '')}")
        stage = state.get("aborted_stage")
        if stage == "worker" and state.get("aborted_task"):
            lines.append("resume 会重跑被中断的这一条。")
        elif stage == "verify" and state.get("aborted_task"):
            lines.append("resume 会直接补验收，不重跑 worker 的产出。")
        lines.append("产物留在 workdir；checkpoint 已保存，可用 --resume 接着做。")
        return "\n".join(lines)

    # ── 来源与模型 ───────────────────────────────────────
    def _main_label(self) -> str:
        return registry.label(self.cfg, self.cfg.main.source, self.cfg.main.model)

    def _worker_label(self) -> str:
        return registry.label(self.cfg, self.cfg.worker.source, self.cfg.worker.model)

    def _verifier_label(self) -> str:
        return registry.label(self.cfg, self.cfg.verifier.source, self.cfg.verifier.model)

    def _cancel(self):
        return self.control.stop_event

    def _deadline(self):
        return self.control.drain_deadline

    def _worker_agent(self):
        """按配置现造一个执行 agent：来源可以是 API，也可以是任意 CLI。"""
        w = self.cfg.worker
        return registry.build_agent(
            self.cfg,
            source=w.source, model=w.model, allowed_tools=w.allowed_tools,
            timeout=w.timeout_seconds, max_turns=w.max_turns, extra_env=dict(w.env),
            cancel=self._cancel(), drain_deadline=self._deadline,
        )

    def _verifier_agent(self):
        v = self.cfg.verifier
        return registry.build_agent(
            self.cfg,
            source=v.source, model=v.model, allowed_tools=v.allowed_tools,
            timeout=v.timeout_seconds, max_turns=0, extra_env=dict(v.env),
            cancel=self._cancel(), drain_deadline=self._deadline,
        )

    # ── 路由 ─────────────────────────────────────────────
    @staticmethod
    def route_after_plan(state: dict) -> str:
        return "worker" if state.get("action") == "dispatch" else "synthesize"

    @staticmethod
    def route_after_worker(state: dict) -> str:
        # 退出时跳过验收直接回 plan 收尾：验收也是一次模型调用，不该在退出后再起
        if state.get("abort") or state.get("draining"):
            return "plan"
        return "verify"
