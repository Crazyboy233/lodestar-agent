"""图的定义与装配。

    START → plan ─┬─ dispatch ─► worker → verify ─┐
                  │                                │
                  └─ done ──────► synthesize ←─────┘
                                          （plan 循环回来读产出+验收，修订方案再发布）

节点逻辑在 `nodes.py`，prompt 在 `prompts.py`，记录结构在 `records.py`。
这里只做三件事：声明 State、建模型、把节点连起来。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from .config import Config
from .control import Control
from .errors import AgentFailure, ModelAborted
from .nodes import Nodes

__all__ = ["AgentFailure", "ModelAborted", "State", "build_graph"]


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
    stage: str                # init / round / abort（用于展示）
    note: str
    plan_raw: str             # 拆任务方案时主 agent 的原始输出
    decision_raw: str         # 每轮动态决策时主 agent 的原始输出
    verdict_raw: str          # 验收 agent 的原始输出
    records: Annotated[list[dict], operator.add]
    abort: bool               # 被兜底强杀（超时上限）或跳过：当前任务不完整
    abort_reason: str
    worker_session: str       # 上一次 worker CLI 的 session_id（超时重试时用它续跑）
    draining: bool            # 边界退出：当前这一轮已经跑完，只是不再开新的
    aborted_task: dict        # 被中断的那条任务，供 --resume 时接着做
    aborted_stage: str        # 中断时「接下来本该做什么」：worker / verify / done
    verdict_ready: bool       # 本轮是否真的走过验收（决定记录里是 PASS/FAIL 还是「未验收」）
    skipped: bool             # 还没开始跑就被中止（用于区分「跑了一半被掐」）
    answer: str


def build_graph(cfg: Config, use_llm: bool = True, llm=None, checkpointer=None,
                control: Control | None = None):
    """`control` 缺省时新建一个「没人订阅」的控制面 —— 节点里不必再到处判空。"""
    control = control if control is not None else Control()
    if llm is None and use_llm:
        llm = ChatOpenAI(
            model=cfg.main_agent.model,
            api_key=cfg.main_agent.api_key,
            base_url=cfg.main_agent.base_url,
            temperature=cfg.main_agent.temperature,
        )

    nodes = Nodes(cfg, control, llm)

    graph = StateGraph(State)
    graph.add_node("plan", nodes.plan)
    graph.add_node("worker", nodes.worker)
    graph.add_node("verify", nodes.verify)
    graph.add_node("synthesize", nodes.synth)

    graph.add_edge(START, "plan")
    graph.add_conditional_edges(
        "plan", Nodes.route_after_plan, {"worker": "worker", "synthesize": "synthesize"},
    )
    graph.add_conditional_edges(
        "worker", Nodes.route_after_worker, {"verify": "verify", "plan": "plan"},
    )
    graph.add_edge("verify", "plan")
    graph.add_edge("synthesize", END)

    return graph.compile(checkpointer=checkpointer)
