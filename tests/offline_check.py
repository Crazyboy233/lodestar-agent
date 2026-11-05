"""离线自检：不需要 API key、也不需要 codebuddy 登录态。

    uv run python tests/offline_check.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lodestar_agent.config import load_config  # noqa: E402
from lodestar_agent.control import Control  # noqa: E402
from lodestar_agent.errors import AgentFailure, ModelAborted  # noqa: E402
from lodestar_agent.graph import build_graph  # noqa: E402
from lodestar_agent.llm import invoke_interruptible  # noqa: E402
from lodestar_agent.parsing import extract_array, normalize_items  # noqa: E402
from lodestar_agent.records import tasks_from_state  # noqa: E402
from lodestar_agent.session import Runner, StartRequest  # noqa: E402
from lodestar_agent.worker import (  # noqa: E402
    CliOptions,
    CliResult,
    Verdict,
    _diagnose,
    _parse_json,
    run_worker,
)


class FakeLLM:
    """返回固定文本（或按顺序返回），用来替换主 agent 的真实模型调用。"""

    def __init__(self, content):
        self.replies = list(content) if isinstance(content, (list, tuple)) else [content]
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return SimpleNamespace(content=reply)

    def stream(self, messages):
        # 真实链路走 stream（这样才能中途断开），桩也照着来，保证计数一致
        yield self.invoke(messages)


def _fake_worker(calls: list[str], ok: bool = True):
    def inner(task, *_a, **kwargs):
        calls.append(task)
        return CliResult(ok=ok, output=f"done: {task}", duration=0.0, error="" if ok else "boom")

    return inner


def test_plan_sanitizes_unverifiable_criteria() -> None:
    plan = normalize_items(extract_array(
        '好的：\n[{"task":"做导航栏","verify":"在浏览器打开页面控制台无报错"},'
        '{"task":"写 a.txt","verify":"ls a.txt 有输出"},'
        '"纯字符串任务"]'
    ) or [])
    assert [p["task"] for p in plan] == ["做导航栏", "写 a.txt", "纯字符串任务"]
    assert plan[0]["verify"] == "", "不可执行的验收标准必须被丢掉"
    assert plan[1]["verify"] == "ls a.txt 有输出"
    print("ok  plan_sanitizes_unverifiable_criteria")


def test_verify_fail_then_pass_retries_worker() -> None:
    cfg = load_config("config.yaml")
    cfg.verifier.max_rounds = 3
    app = build_graph(cfg, llm=FakeLLM("汇总完成"))

    worker_calls: list[str] = []
    seen = {"t1": 0}

    def fake_verifier(task, verify, out, *_a, **kwargs):
        if task == "t1":
            seen["t1"] += 1
            if seen["t1"] == 1:
                return Verdict(False, "缺 a.txt", "创建 a.txt", duration=0.0)
        return Verdict(True, "通过", "", duration=0.0)

    plan = [{"task": "t1", "verify": "ls a.txt"}, {"task": "t2", "verify": "ls b.txt"}]
    with patch("lodestar_agent.nodes.run_worker", side_effect=_fake_worker(worker_calls)), patch(
        "lodestar_agent.nodes.run_verifier", side_effect=fake_verifier
    ):
        out = app.invoke(
            {"goal": "g", "plan": plan, "current": None, "attempts": 0, "feedback": "", "records": []},
            {"recursion_limit": 60},
        )

    assert worker_calls == ["t1", "t1", "t2"], worker_calls
    assert [r["passed"] for r in out["records"]] == [True, True]
    assert [r["attempts"] for r in out["records"]] == [2, 1]
    print("ok  verify_fail_then_pass_retries_worker", worker_calls)


def test_retry_cap_stops_dead_loop() -> None:
    cfg = load_config("config.yaml")
    cfg.verifier.max_rounds = 2
    app = build_graph(cfg, llm=FakeLLM("汇总"))

    worker_calls: list[str] = []

    def always_fail(*_args, **_kwargs):
        return Verdict(False, "永远不达标", "再改", duration=0.0)

    plan = [{"task": "t1", "verify": "ls a.txt"}, {"task": "t2", "verify": "ls b.txt"}]
    with patch("lodestar_agent.nodes.run_worker", side_effect=_fake_worker(worker_calls)), patch(
        "lodestar_agent.nodes.run_verifier", side_effect=always_fail
    ):
        out = app.invoke(
            {"goal": "g", "plan": plan, "current": None, "attempts": 0, "feedback": "", "records": []},
            {"recursion_limit": 60},
        )

    # 每条子任务最多 2 轮，绝不无限循环
    assert worker_calls == ["t1", "t1", "t2", "t2"], worker_calls
    assert [r["passed"] for r in out["records"]] == [False, False]
    print("ok  retry_cap_stops_dead_loop", worker_calls)


def test_exec_failure_terminates_after_retries() -> None:
    """执行失败（模型/CLI 请求失败）重试 worker.max_retries 次后终止流程。"""
    cfg = load_config("config.yaml")
    cfg.worker.max_retries = 2
    app = build_graph(cfg, use_llm=False)
    calls: list[str] = []

    def failing_worker(task, *_a, **kwargs):
        calls.append(task)
        return CliResult(ok=False, output="", duration=0.0, error="429 too many requests")

    plan = [{"task": "t1", "verify": ""}]
    with patch("lodestar_agent.nodes.run_worker", side_effect=failing_worker):
        try:
            app.invoke(
                {"goal": "g", "plan": plan, "current": None, "attempts": 0, "feedback": "", "records": []},
                {"recursion_limit": 60},
            )
            raise AssertionError("应该抛 AgentFailure 终止流程")
        except AgentFailure as exc:
            assert exc.node == "worker(执行)", exc.node
            assert "429" in exc.reason

    assert len(calls) == cfg.worker.max_retries + 1, f"应执行 {cfg.worker.max_retries + 1} 次，实际 {len(calls)}"
    print("ok  exec_failure_terminates_after_retries", len(calls))


def test_plan_dynamic_replan() -> None:
    """plan 每轮读进度后能动态改写剩余任务方案。"""
    cfg = load_config("config.yaml")
    replies = [
        '{"remaining":[{"task":"t2-b","verify":"ls b.txt"}],"note":"改道"}',
        '{"remaining":[],"note":"收工"}',
        "最终答复",
    ]
    app = build_graph(cfg, llm=FakeLLM(replies))

    worker_calls: list[str] = []

    def always_pass(_task, _verify, _out, *_a, **_kw):
        return Verdict(True, "通过", "", duration=0.0)

    plan = [{"task": "t1", "verify": "ls a.txt"}, {"task": "t2", "verify": "ls b.txt"}]
    with patch("lodestar_agent.nodes.run_worker", side_effect=_fake_worker(worker_calls)), patch(
        "lodestar_agent.nodes.run_verifier", side_effect=always_pass
    ):
        out = app.invoke(
            {"goal": "g", "plan": plan, "current": None, "attempts": 0, "feedback": "", "records": []},
            {"recursion_limit": 60},
        )

    assert worker_calls == ["t1", "t2-b"], worker_calls
    assert [r["passed"] for r in out["records"]] == [True, True]
    assert out["records"][1]["task"] == "t2-b"
    print("ok  plan_dynamic_replan", worker_calls)


def test_plan_parse_retry() -> None:
    """拆解输出不是合法 JSON 时，带纠正提示重试，而不是一次跑偏就降级。"""
    cfg = load_config("config.yaml")
    cfg.main_agent.plan_retries = 2
    cfg.verifier.max_rounds = 3

    llm = FakeLLM([
        "我先想想……大概分两步走。",                              # 第 1 次：解析失败
        '好的：\n```json\n[{"task":"t1","verify":"ls a.txt"}]\n```',  # 第 2 次：成功
        '{"remaining":[],"note":"收工"}',
        "最终答复",
    ])
    app = build_graph(cfg, llm=llm)
    worker_calls: list[str] = []

    def always_pass(_t, _v, _o, *_a, **_kw):
        return Verdict(True, "通过", "", duration=0.0)

    with patch("lodestar_agent.nodes.run_worker", side_effect=_fake_worker(worker_calls)), patch(
        "lodestar_agent.nodes.run_verifier", side_effect=always_pass
    ):
        out = app.invoke(
            {"goal": "g", "plan": [], "current": None, "attempts": 0, "feedback": "", "records": []},
            {"recursion_limit": 60},
        )

    assert worker_calls == ["t1"], worker_calls
    assert out["records"][0]["task"] == "t1"
    assert llm.calls == 4, f"拆解重试后应共 4 次模型调用，实际 {llm.calls}"
    print("ok  plan_parse_retry", worker_calls)


def test_plan_parse_retry_exhausted_falls_back() -> None:
    """重试用尽仍解析不出来时，退化为单任务（goal 本身），不影响后续流程。"""
    cfg = load_config("config.yaml")
    cfg.main_agent.plan_retries = 2
    cfg.verifier.max_rounds = 3

    llm = FakeLLM([
        "不是 JSON",
        "还是不 JSON",
        "依然不是 JSON",
        '{"remaining":[],"note":"收工"}',
        "最终答复",
    ])
    app = build_graph(cfg, llm=llm)
    worker_calls: list[str] = []

    def always_pass(_t, _v, _o, *_a, **_kw):
        return Verdict(True, "通过", "", duration=0.0)

    with patch("lodestar_agent.nodes.run_worker", side_effect=_fake_worker(worker_calls)), patch(
        "lodestar_agent.nodes.run_verifier", side_effect=always_pass
    ):
        out = app.invoke(
            {"goal": "把 a 改成 b", "plan": [], "current": None, "attempts": 0, "feedback": "", "records": []},
            {"recursion_limit": 60},
        )

    assert worker_calls == ["把 a 改成 b"], worker_calls
    assert llm.calls == 1 + cfg.main_agent.plan_retries + 2, llm.calls
    assert out["records"][0]["task"] == "把 a 改成 b"
    print("ok  plan_parse_retry_exhausted_falls_back", llm.calls)


def test_checkpoint_persists_and_resumes() -> None:
    """中途被打断后，state 落在 sqlite 里，换一个进程能 --resume 接着跑。"""
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    cfg = load_config("config.yaml")
    cfg.verifier.max_rounds = 3

    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(str(Path(tmp) / "ckpt.sqlite"), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()

        run_config = {"configurable": {"thread_id": "t-resume"}, "recursion_limit": 60}
        plan = [{"task": "t1", "verify": "ls a.txt"}]
        initial = {"goal": "g", "plan": plan, "current": None, "attempts": 0, "feedback": "", "records": []}
        calls: list[str] = []

        def interrupted_worker(task, *_a, **_kw):
            calls.append(task)
            raise KeyboardInterrupt("模拟 Ctrl+C")

        def always_pass(_t, _v, _o, *_a, **_kw):
            return Verdict(True, "通过", "", duration=0.0)

        app = build_graph(cfg, llm=FakeLLM("汇总"), checkpointer=saver)
        with patch("lodestar_agent.nodes.run_worker", side_effect=interrupted_worker), patch(
            "lodestar_agent.nodes.run_verifier", side_effect=always_pass
        ):
            try:
                app.invoke(initial, run_config)
                raise AssertionError("应该被 KeyboardInterrupt 打断")
            except KeyboardInterrupt:
                pass

        snap = app.get_state(run_config)
        assert snap.values.get("current", {}).get("task") == "t1", snap.values.get("current")
        assert snap.next == ("worker",), snap.next

        # 换一个图实例（模拟另起进程），从 checkpoint 续跑
        resumed: list[str] = []

        def ok_worker(task, *_a, **_kw):
            resumed.append(task)
            return CliResult(ok=True, output=f"done: {task}", duration=0.0)

        app2 = build_graph(
            cfg,
            llm=FakeLLM(['{"remaining":[],"note":"收工"}', "最终答复"]),
            checkpointer=saver,
        )
        with patch("lodestar_agent.nodes.run_worker", side_effect=ok_worker), patch(
            "lodestar_agent.nodes.run_verifier", side_effect=always_pass
        ):
            out = app2.invoke(None, run_config)

        assert resumed == ["t1"], resumed
        assert out["answer"] == "最终答复", out.get("answer")
        assert [r["passed"] for r in out["records"]] == [True]
        conn.close()

    print("ok  checkpoint_persists_and_resumes", resumed)


def test_resume_after_exec_failure_resets_attempts() -> None:
    """执行失败终止后 state 仍在；重置 attempts 后能续跑，不会一进去就又撞上限。"""
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    cfg = load_config("config.yaml")
    cfg.worker.max_retries = 1

    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(str(Path(tmp) / "c.sqlite"), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        run_config = {"configurable": {"thread_id": "t-fail"}, "recursion_limit": 60}
        app = build_graph(cfg, use_llm=False, checkpointer=saver)

        def failing(_task, *_a, **_kw):
            return CliResult(ok=False, output="", duration=0.0, error="401 unauthorized")

        with patch("lodestar_agent.nodes.run_worker", side_effect=failing):
            try:
                app.invoke(
                    {"goal": "g", "plan": [{"task": "t1", "verify": ""}], "current": None,
                     "attempts": 0, "feedback": "", "records": []},
                    run_config,
                )
                raise AssertionError("应该抛 AgentFailure")
            except AgentFailure:
                pass

        snap = app.get_state(run_config)
        assert snap.values["attempts"] == cfg.worker.max_retries + 1, snap.values["attempts"]
        assert snap.next == ("plan",), snap.next

        # 模拟 __main__ 的 --resume：重置计数后应能重新执行同一条任务
        app.update_state(run_config, {"attempts": 0, "last_error": ""}, as_node="verify")
        calls: list[str] = []
        with patch("lodestar_agent.nodes.run_worker", side_effect=_fake_worker(calls)):
            out = app.invoke(None, run_config)

        assert calls == ["t1"], calls
        assert out["records"][0]["passed"] is True
        conn.close()

    print("ok  resume_after_exec_failure_resets_attempts", calls)


def test_json_parse_with_noise() -> None:
    noisy = 'some log\n{"result":"x","session_id":"s1"}\n'
    assert _parse_json(noisy) == {"result": "x", "session_id": "s1"}
    assert _parse_json("plain text") is None
    arr = '[{"type":"message","role":"user"},{"type":"result","result":"done","session_id":"s2","is_error":false}]'
    assert _parse_json(arr) == {"type": "result", "result": "done", "session_id": "s2", "is_error": False}
    print("ok  json_parse_with_noise")


def test_diagnose_never_dumps_transcript() -> None:
    big = (
        '[{"type":"message","role":"user","message":{"role":"user","content":[{"type":"text","text":"'
        + "x" * 20000
        + '"}]}},{"type":"message","role":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"我在尝试修改 script.js"}]}}]'
    )
    msg = _diagnose(big)
    assert len(msg) < 600, f"错误信息过长: {len(msg)}"
    assert "未返回 result" in msg and "2 条消息" in msg
    assert "我在尝试修改 script.js" in msg
    assert _diagnose("plain text " * 200).endswith("字)")
    print("ok  diagnose_never_dumps_transcript")


def test_guidance_reaches_worker_once() -> None:
    """投递给 worker 的引导只在下一次调用生效，且消费后不再重复注入。"""
    cfg = load_config("config.yaml")
    cfg.verifier.max_rounds = 3
    control = Control()
    # 决策里不带 remaining 字段 → 沿用原队列，两条任务都会跑
    app = build_graph(cfg, llm=FakeLLM('{"note":"继续"}'), control=control)

    seen: list[str] = []

    def fake_worker(task, *_a, **kwargs):
        seen.append(kwargs.get("guidance", ""))
        if task == "t1":
            # 第一条正在跑时投第二条：不打断它，等下一次调用再注入
            control.push_note("worker", "第二条：记得写中文注释")
        return CliResult(ok=True, output=f"done: {task}", duration=0.0)

    def always_pass(_t, _v, _o, *_a, **_kw):
        return Verdict(True, "通过", "", duration=0.0)

    control.push_note("worker", "第一条：别动 index.html")
    plan = [{"task": "t1", "verify": "ls a"}, {"task": "t2", "verify": "ls b"}]
    with patch("lodestar_agent.nodes.run_worker", side_effect=fake_worker), patch(
        "lodestar_agent.nodes.run_verifier", side_effect=always_pass
    ):
        app.invoke(
            {"goal": "g", "plan": plan, "current": None, "attempts": 0,
             "feedback": "", "records": []},
            {"recursion_limit": 60},
        )

    assert "第一条" in seen[0] and "第二条" not in seen[0], seen
    assert "第二条" in seen[1] and "第一条" not in seen[1], seen
    assert control.pending_notes("worker") == [], control.pending_notes("worker")
    print("ok  guidance_reaches_worker_once")


def test_boundary_stop_waits_for_current_node() -> None:
    """边界退出：正在跑的 worker 要跑完，验收不再开始，记录为「未验收」。"""
    cfg = load_config("config.yaml")
    control = Control()
    llm = FakeLLM("不该被调用")
    app = build_graph(cfg, llm=llm, control=control)

    calls: list[str] = []
    verdicts: list[str] = []

    def finishing_worker(task, *_a, **_kwargs):
        calls.append(task)
        # 跑到一半时用户点退出 —— 关键：这次调用仍然正常返回，不被打断
        control.request_stop("测试退出")
        return CliResult(ok=True, output=f"done: {task}", duration=3.0)

    def fake_verifier(task, *_a, **_kw):
        verdicts.append(task)
        return Verdict(True, "通过", "", duration=0.0)

    plan = [{"task": "t1", "verify": "ls a"}, {"task": "t2", "verify": "ls b"}]
    with patch("lodestar_agent.nodes.run_worker", side_effect=finishing_worker), patch(
        "lodestar_agent.nodes.run_verifier", side_effect=fake_verifier
    ):
        out = app.invoke(
            {"goal": "g", "plan": plan, "current": None, "attempts": 0,
             "feedback": "", "records": []},
            {"recursion_limit": 60},
        )

    assert calls == ["t1"], f"退出后不应再派新任务：{calls}"
    assert verdicts == [], f"退出后不应开始验收（下一个模型调用）：{verdicts}"
    assert out.get("draining") is True and not out.get("abort")
    assert out["aborted_stage"] == "verify", out["aborted_stage"]
    assert out["aborted_task"]["task"] == "t1"
    rec = out["records"][0]
    assert rec["verified"] is False and "未验收" in rec["reason"], rec
    assert len(out.get("plan") or []) == 1, "剩余未执行任务应保留"
    assert "边界退出" in out["answer"] and "未验收 1" in out["answer"], out["answer"]
    assert llm.calls == 0, "退出路径不应再调模型"
    print("ok  boundary_stop_waits_for_current_node")


def test_forced_kill_marks_task_for_retry() -> None:
    """超过等待上限被强杀：算 abort（不是 draining），resume 会重跑这条。"""
    cfg = load_config("config.yaml")
    control = Control(drain_timeout=0.01)
    app = build_graph(cfg, llm=FakeLLM("不该被调用"), control=control)

    def forcing_worker(task, *_a, **_kwargs):
        control.request_stop("测试超时")
        return CliResult(ok=False, output="", duration=60.0,
                         error="超过等待上限仍未结束，已强制终止", aborted=True)

    plan = [{"task": "t1", "verify": "ls a"}, {"task": "t2", "verify": "ls b"}]
    with patch("lodestar_agent.nodes.run_worker", side_effect=forcing_worker), patch(
        "lodestar_agent.nodes.run_verifier", side_effect=lambda *_a, **_k: Verdict(True, "通过", "")
    ):
        out = app.invoke(
            {"goal": "g", "plan": plan, "current": None, "attempts": 0,
             "feedback": "", "records": []},
            {"recursion_limit": 60},
        )

    assert out.get("abort") is True and out.get("draining") is True
    assert out["aborted_stage"] == "worker", out["aborted_stage"]
    assert out["aborted_task"]["task"] == "t1"
    assert out["records"][0]["verified"] is False
    print("ok  forced_kill_marks_task_for_retry")


def test_cancel_event_does_not_kill_running_cli() -> None:
    """cancel 只表示「之后不许开新的」；已经跑起来的那一轮要让它自然结束。"""
    if os.name == "nt":
        print("skip  cancel_event_does_not_kill_running_cli（Windows 无 /bin/sh，进程树由 windows_tree_kill 覆盖）")
        return
    import threading
    import time

    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "slow-cli"
        fake.write_text(
            "#!/bin/sh\nsleep 1\n"
            'echo \'{"result":"finished","session_id":"s","is_error":false}\'\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)

        cancel = threading.Event()
        cancel.set()  # 一开始就是「已请求退出」，模拟退出请求早于这次调用

        started = time.time()
        ok_result = run_worker(
            "do", CliOptions(cwd=Path(tmp) / "wd", cli=str(fake), timeout=30,
                             cancel=cancel, drain_deadline=time.time() + 30),
        )
        elapsed = time.time() - started
        assert ok_result.ok, ok_result.error
        assert ok_result.output == "finished"
        assert elapsed >= 0.9, f"不该提前掐断：{elapsed:.2f}s"

        # 等待上限已经过了 → 才强杀
        killed = run_worker(
            "do", CliOptions(cwd=Path(tmp) / "wd2", cli=str(fake), timeout=30,
                             cancel=cancel, drain_deadline=time.time() + 0.05),
        )
        assert killed.aborted is True, killed
        assert not killed.ok
    print("ok  cancel_event_does_not_kill_running_cli")


def test_model_call_is_really_cut_off() -> None:
    """到点要断的是模型连接本身：断开后不再继续生成，也不重试。

    这是「停掉模型」和「停掉脚本」的区别：非流式请求发出后服务端会把整段生成完，
    本地放弃也没用；只有流式 + 主动断开，服务端才会停。
    """
    import time as _t

    class SlowLLM:
        def __init__(self):
            self.finished = False

        def stream(self, _p):
            yield SimpleNamespace(content="{")
            _t.sleep(5)          # 模拟服务端还在继续生成
            yield SimpleNamespace(content="}")
            self.finished = True

        def invoke(self, _p):
            raise AssertionError("不该退回非流式：那样断不掉服务端生成")

    llm = SlowLLM()
    started = _t.time()
    try:
        invoke_interruptible(llm, "p", lambda: True)
        raise AssertionError("应该在第一个 chunk 之后断开")
    except ModelAborted:
        pass
    elapsed = _t.time() - started
    assert elapsed < 2, f"没断开，等了 {elapsed:.1f}s"
    assert llm.finished is False, "断开后不该再读到第二个 chunk"

    # 不到点就不该断：完整收完
    llm2 = SlowLLM()
    llm2.stream = lambda _p: iter([SimpleNamespace(content="完整答案")])
    msg = invoke_interruptible(llm2, "p", lambda: False)
    assert msg.content == "完整答案"
    print("ok  model_call_is_really_cut_off")


def test_drain_deadline_is_read_lazily() -> None:
    """退出请求发生在 CLI 启动之后：deadline 必须每次轮询重新求值。

    传死值（进入 run_cli 时求一次）时，那时还没点退出 → 永远是 None → 强杀分支
    一次都不触发，表现就是「点了退出但进程不退、模型一直在跑」。
    """
    if os.name == "nt":
        print("skip  drain_deadline_is_read_lazily（Windows 无 /bin/sh，进程树由 windows_tree_kill 覆盖）")
        return
    import threading
    import time

    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "slow-cli"
        fake.write_text("#!/bin/sh\nsleep 6\n", encoding="utf-8")
        fake.chmod(0o755)

        box = {"deadline": None}
        cancel = threading.Event()
        cancel.set()  # 已经在退出态，但「到点」是稍后才发生的
        timer = threading.Timer(0.8, lambda: box.update(deadline=time.time() - 1))
        timer.start()
        try:
            started = time.time()
            result = run_worker(
                "do", CliOptions(cwd=Path(tmp) / "wd", cli=str(fake), timeout=30,
                                 cancel=cancel, drain_deadline=lambda: box["deadline"]),
            )
            elapsed = time.time() - started
        finally:
            timer.cancel()

        assert result.aborted is True, result
        assert elapsed < 4, f"到点没强杀，等了 {elapsed:.1f}s（CLI 自己要睡 6s）"
    print("ok  drain_deadline_is_read_lazily")


def test_runner_lifecycle() -> None:
    """页面驱动：start 占位 → 并发被拒 → 结束后状态回落 → reset 清掉上一轮的停止标志。"""
    import threading

    cfg = load_config("config.yaml")
    control = Control()
    runner = Runner(cfg, control)

    control.request_stop("上一轮的退出请求")
    assert control.stop_requested

    gate = threading.Event()

    def fake_session(*_a, **_kw):
        gate.wait(5)
        return 0

    with patch("lodestar_agent.session.run_session", side_effect=fake_session):
        info = runner.start(StartRequest(goal="第一个目标"))
        assert info["state"] == "running", info
        assert not control.stop_requested, "start 应该把上一轮的停止标志清掉"
        assert control.pending_notes("worker") == []
        assert runner.busy

        try:
            runner.start(StartRequest(goal="并发目标"))
            raise AssertionError("运行中应拒绝并发提交")
        except RuntimeError:
            pass

        gate.set()
        runner.join(5)

    assert not runner.busy
    s = runner.session
    assert s["state"] == "done" and s["code"] == 0, s
    assert s["goal"] == "第一个目标" and s["thread"]
    print("ok  runner_lifecycle")


def test_runner_model_override() -> None:
    """页面选模型：传值就覆盖配置，传空串 = 显式用 CLI 默认，传 None = 沿用配置。"""
    cfg = load_config("config.yaml")
    cfg.worker.model = "glm-5.3"
    cfg.verifier.model = "kimi-k3.1-pro"
    control = Control()
    runner = Runner(cfg, control)
    seen: dict = {}

    def fake_session(cfg_arg, goal, thread, *, control, con, resume, dry_run):
        seen["worker"] = cfg_arg.worker.model
        seen["verifier"] = cfg_arg.verifier.model
        return 0

    with patch("lodestar_agent.session.run_session", side_effect=fake_session):
        runner.start(StartRequest(goal="t1", worker_model="deepseek-v4.1-flash", verifier_model=""))
        runner.join(5)
        assert seen == {"worker": "deepseek-v4.1-flash", "verifier": ""}, seen

        runner.start(StartRequest(goal="t2", worker_model=None, verifier_model=None))
        runner.join(5)
        # None = 不覆盖，沿用上次设置后的值
        assert seen == {"worker": "deepseek-v4.1-flash", "verifier": ""}, seen

        runner.start(StartRequest(goal="t3", worker_model="deepseek-v4-pro", verifier_model="glm-5.2"))
        runner.join(5)
        assert seen == {"worker": "deepseek-v4-pro", "verifier": "glm-5.2"}, seen
    print("ok  runner_model_override")


def test_build_tasks_states() -> None:
    """任务清单：三态映射对、同一条重跑只留最后一次、当前和剩余都在。"""
    state = {
        "records": [
            {"task": "A", "verify": "v", "passed": True, "verified": True,
             "attempts": 1, "duration": 3.0, "reason": "通过"},
            {"task": "B", "verify": "v", "passed": False, "verified": False,
             "attempts": 1, "duration": 2.0, "reason": "已执行，未验收"},
            # A 后来重跑过且失败：清单里应该只剩最后这条
            {"task": "A", "verify": "v", "passed": False, "verified": True,
             "attempts": 2, "duration": 4.0, "reason": "缺参数"},
        ],
        "current": {"task": "C", "verify": "v"},
        "attempts": 2,
        "last_duration": 1.5,
        "plan": [{"task": "D", "verify": "v"}],
    }
    tasks = tasks_from_state(state)

    assert [t["task"] for t in tasks] == ["A", "B", "C", "D"], tasks
    assert [t["status"] for t in tasks] == ["fail", "skip", "running", "pending"], tasks
    assert tasks[0]["attempts"] == 2, "同一条应保留最后一次"
    assert tasks[2]["attempts"] == 2 and tasks[2]["duration"] == 1.5
    print("ok  build_tasks_states")


def test_log_roundtrip_keeps_roles() -> None:
    """写入端（Renderer → LogSink）与解析端（parse_log）必须对得上。

    这层以前是字符串字面量的隐式协议：改一侧不报错，只会让「回看历史」的分类错位。
    """
    from lodestar_agent.history import parse_log
    from lodestar_agent.view import LogSink, Renderer

    lines: list[str] = []
    sink = LogSink(lines.append)
    renderer = Renderer()
    for node, update in (
        ("plan", {"stage": "init", "current": {"task": "t1", "verify": "ls a"},
                  "plan": [], "plan_raw": "[...]"}),
        ("worker", {"attempts": 1, "last_output": "done", "last_duration": 2.0}),
        ("verify", {"verdict_passed": True, "verdict_reason": "通过", "verdict_ready": True}),
        ("plan", {"action": "done", "records": [{"task": "t1", "passed": True}],
                  "note": "收工", "decision_raw": "{}"}),
        ("synthesize", {"answer": "[PASS] t1（1 轮）"}),
    ):
        for line in renderer.render(node, update):
            sink.write(line)

    events = parse_log("\n".join(lines))
    assert [e["kind"] for e in events if e.get("hr")] == [
        "plan", "worker", "verifier", "plan", "synth",
    ], events
    roles = {e.get("role") for e in events}
    assert {"task", "output"} <= roles, roles
    # 正文里以 [PASS] 开头的行不能被当成段落标题（dry-run 的最终答复正是这个形状）
    assert events[-1]["kind"] == "synth" and "[PASS]" in events[-1]["text"], events[-1]
    print("ok  log_roundtrip_keeps_roles")


def test_config_overrides() -> None:
    """页面配置：白名单、范围校验、掩码不回写、lines/kv 解析、写回 local 覆盖层。"""
    from lodestar_agent.config import (
        as_editable, collect_overrides, mask_key, save_overrides,
    )

    real_key = "sk-abcdefgh12345678"
    masked = mask_key(real_key)
    assert masked != real_key and masked.startswith("sk-a"), masked

    # 掩码 / 空值不回写
    patch, errors = collect_overrides(
        {"main_agent": {"api_key": masked, "model": "m1"}}, real_key
    )
    assert errors == [] and patch == {"main_agent": {"model": "m1"}}, patch
    patch, _ = collect_overrides({"main_agent": {"api_key": ""}}, real_key)
    assert patch == {}, "空 key 不该覆盖已有值"

    # 填了新 key 才写
    patch, _ = collect_overrides({"main_agent": {"api_key": "sk-new"}}, real_key)
    assert patch["main_agent"]["api_key"] == "sk-new"

    # 白名单：未知字段忽略
    patch, _ = collect_overrides({"main_agent": {"evil": "x"}, "nope": {"a": 1}})
    assert patch == {}, patch

    # 范围校验
    _, errors = collect_overrides({"main_agent": {"temperature": 9}})
    assert errors and "temperature" in errors[0], errors

    # lines / kv
    patch, _ = collect_overrides({
        "worker": {"add_dirs": "a\nb", "env": "A=1\nB=2", "max_turns": "7"},
    })
    assert patch["worker"]["add_dirs"] == ["a", "b"]
    assert patch["worker"]["env"] == {"A": "1", "B": "2"}
    assert patch["worker"]["max_turns"] == 7

    # 模型候选：空串是「默认（CLI 决定）」，不写进配置（否则保存一次就被过滤掉）
    view = as_editable(load_config("config.yaml"))
    assert view["ui"]["models"] and not view["ui"]["models"].startswith("\n"), view["ui"]
    patch, _ = collect_overrides({"ui": {"models": view["ui"]["models"]}})
    assert "" not in patch["ui"]["models"] and patch["ui"]["models"], patch["ui"]

    # 写回 config.local.yaml，并且多次保存不会互相覆盖
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg_file = root / "config.yaml"
        cfg_file.write_text(
            "main_agent:\n  base_url: 'https://x'\n  model: 'base'\n"
            "worker:\n  model: ''\nverifier:\n  model: ''\n",
            encoding="utf-8",
        )
        save_overrides(root, {"worker": {"model": "glm-5.3"}})
        assert load_config(cfg_file).worker.model == "glm-5.3"

        save_overrides(root, {"verifier": {"model": "kimi-k3.1-pro"}})
        cfg = load_config(cfg_file)
        assert cfg.worker.model == "glm-5.3", "第二次保存不该冲掉第一次的字段"
        assert cfg.verifier.model == "kimi-k3.1-pro"
        assert (root / "config.local.yaml").exists()
    print("ok  config_overrides")


def test_windows_tree_kill() -> None:
    """Windows 没有 killpg：停止任务时应该走 taskkill /T /F，把 CLI 的 node 子进程一起收掉。"""
    from types import SimpleNamespace

    from lodestar_agent import worker

    seen: dict = {}

    class FakeProc:
        pid = 4321

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            seen["killed"] = True

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    with patch("lodestar_agent.worker.os.name", "nt"), patch(
        "lodestar_agent.worker.subprocess.run", side_effect=fake_run
    ):
        worker._terminate_tree(FakeProc())

    cmd = seen["cmd"]
    assert cmd[:4] == ["taskkill", "/PID", "4321", "/T"], cmd
    assert "/F" in cmd, cmd
    assert "creationflags" in seen["kwargs"], "Windows 下不应该闪黑框"

    # Windows 环境变量大小写不敏感：SystemRoot / Path 必须能传给 node
    fake_env = {
        "SystemRoot": "C:\\Windows", "Path": "C:\\Windows\\System32",
        "USERPROFILE": "C:\\Users\\x", "CODEBUDDY_SERVER_PORT": "7777",
        "UNRELATED_HOST_VAR": "drop-me",
    }
    with patch("lodestar_agent.worker.os.name", "nt"), patch.dict(
        "lodestar_agent.worker.os.environ", fake_env, clear=True
    ):
        env = worker._child_env({"EXTRA": "1"})
    assert env["SYSTEMROOT"] == "C:\\Windows", env
    assert env["PATH"] == "C:\\Windows\\System32", env
    assert "CODEBUDDY_SERVER_PORT" not in env, "宿主注入的变量不能带进去"
    assert "UNRELATED_HOST_VAR" not in env, env
    assert env["CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS"] == "1", env

    # npm 全局命令在 Windows 上是 .cmd shim，要走 cmd.exe /c；.exe 直接执行
    with patch("lodestar_agent.worker.os.name", "nt"), patch.dict(
        "lodestar_agent.worker.os.environ", {"COMSPEC": "C:\\Windows\\System32\\cmd.exe"},
        clear=True,
    ):
        argv = worker._cli_argv("C:\\npm\\codebuddy.cmd", ["-p", "hi"])
    assert argv[:4] == [
        "C:\\Windows\\System32\\cmd.exe", "/c", "C:\\npm\\codebuddy.cmd", "-p",
    ], argv
    assert worker._cli_argv("/usr/local/bin/codebuddy", ["-p"])[0].endswith("codebuddy")
    print("ok  windows_tree_kill")


def test_folder_picker() -> None:
    """系统文件夹选择器：mac / Windows 命令构造、取消、报错、路径清洗。"""
    import base64
    import os

    from lodestar_agent import fspick
    from lodestar_agent.fspick import PickUnavailable

    with tempfile.TemporaryDirectory() as tmp:
        picked = os.path.normpath(tmp)
        done = SimpleNamespace(returncode=0, stdout=tmp + "\n", stderr="")

        # macOS：osascript 返回 POSIX 路径，末尾换行要被清掉
        with patch("lodestar_agent.fspick.sys.platform", "darwin"), patch(
            "lodestar_agent.fspick._run", return_value=done
        ):
            assert fspick.pick_directory() == picked, "选中的目录没返回"

        # 用户取消：空输出 → None，不当作错误
        cancel = SimpleNamespace(returncode=0, stdout="\n", stderr="")
        with patch("lodestar_agent.fspick.sys.platform", "darwin"), patch(
            "lodestar_agent.fspick._run", return_value=cancel
        ):
            assert fspick.pick_directory() is None

        # osascript 报错 / 选中的目录已经不存在 → 说清楚原因
        bad = SimpleNamespace(returncode=1, stdout="", stderr="not allowed\n")
        with patch("lodestar_agent.fspick.sys.platform", "darwin"), patch(
            "lodestar_agent.fspick._run", return_value=bad
        ):
            try:
                fspick.pick_directory()
                raise AssertionError("非零退出码应该报错")
            except PickUnavailable as exc:
                assert "not allowed" in str(exc), exc

        gone = SimpleNamespace(returncode=0, stdout=str(Path(tmp) / "nope"), stderr="")
        with patch("lodestar_agent.fspick.sys.platform", "darwin"), patch(
            "lodestar_agent.fspick._run", return_value=gone
        ):
            try:
                fspick.pick_directory()
                raise AssertionError("不存在的目录应该报错")
            except PickUnavailable as exc:
                assert "目录不存在" in str(exc), exc

        # Windows：脚本走 EncodedCommand，中文提示不能丢
        seen: dict = {}

        def fake_run(cmd, timeout):
            seen["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout=tmp, stderr="")

        def fake_which(name):
            return "/usr/bin/powershell" if name in ("powershell.exe", "powershell") else None

        with patch("lodestar_agent.fspick.shutil.which", side_effect=fake_which), patch(
            "lodestar_agent.fspick._run", side_effect=fake_run
        ):
            assert fspick._pick_windows("选择目录", 1) == tmp
        cmd = seen["cmd"]
        assert "-EncodedCommand" in cmd, cmd
        script = base64.b64decode(cmd[-1]).decode("utf-16-le")
        assert "FolderBrowserDialog" in script and "选择目录" in script, script
        assert "[Console]::OutputEncoding" in script, "中文路径需要强制 UTF-8 输出"

        # Linux：zenity 的退出码 1 = 用户取消
        with patch(
            "lodestar_agent.fspick.shutil.which",
            side_effect=lambda n: "/usr/bin/zenity" if n == "zenity" else None,
        ), patch(
            "lodestar_agent.fspick._run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
        ):
            assert fspick._pick_linux("选择目录", 1) == ""

        # 一个都没有 → 明确提示装 zenity / kdialog
        with patch("lodestar_agent.fspick.shutil.which", return_value=None):
            try:
                fspick._pick_linux("选择目录", 1)
                raise AssertionError("没有选择器命令时应该报错")
            except PickUnavailable as exc:
                assert "zenity" in str(exc), exc

    print("ok  folder_picker")


def test_history_store_and_api() -> None:
    """左侧导航：按 workdir 分组、同目录多会话、续跑判断、/history 接口。"""
    import json as _json
    import urllib.error
    import urllib.request

    from lodestar_agent.history import HistoryStore, uuid_time
    from lodestar_agent.server import ConsoleServer
    from lodestar_agent.session import Runner

    # 老 checkpoint 没有 history.json，排序靠 uuid6 里的时间戳
    assert uuid_time("1f0e7c9a-1c00-6000-8000-000000000000") > 1_700_000_000
    assert uuid_time("not-a-uuid") == 0
    assert uuid_time("") == 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dir_a = tmp / "proj-a"
        dir_b = tmp / "proj-b"
        dir_a.mkdir()
        dir_b.mkdir()
        default_dir = tmp / "workspace"
        db = tmp / "checkpoints.sqlite"

        store = HistoryStore(tmp)
        store.begin("t-a", "目标 A", str(dir_a), worker_model="m1")
        store.finish("t-a", 0, answer="A 完成", passed=1, failed=0)
        store.begin("t-b", "目标 B", str(dir_b))
        store.finish("t-b", 130, error="边界退出")
        store.begin("t-c", "目标 C", str(dir_a))  # 故意不结束：模拟进程被强杀
        store.begin("t-d", "目标 D", str(dir_a))
        store.finish("t-d", 1, passed=0, failed=1)

        # 一个目录 = 一个项目，目录下可以有多条会话
        groups = store.groups(db, str(default_dir), current="t-c")
        projects = {p["name"]: p for p in groups["projects"]}
        assert set(projects) == {"proj-a", "proj-b"}, projects
        assert projects["proj-a"]["count"] == 3, projects["proj-a"]
        assert projects["proj-b"]["count"] == 1, projects["proj-b"]
        by_thread = {i["thread"]: i for p in groups["projects"] for i in p["items"]}
        assert by_thread["t-c"]["state"] == "running", by_thread["t-c"]

        # 不是当前这次还挂着 running，就是上次被强杀了
        stale = store.groups(db, str(default_dir))
        stale_items = {i["thread"]: i for p in stale["projects"] for i in p["items"]}
        assert stale_items["t-c"]["state"] == "aborted", stale_items["t-c"]
        assert stale_items["t-c"]["interrupted"], stale_items["t-c"]

        # 老记录没有 workdir：归到默认产出目录，而不是凭空消失
        data = store._read()
        data["items"][0]["workdir"] = ""
        store._write(data)
        groups2 = store.groups(db, str(default_dir))
        assert any(p["workdir"] == str(default_dir) for p in groups2["projects"]), groups2

        detail = store.detail(db, "t-a", str(default_dir))
        assert detail and detail["state"] == "done", detail
        assert detail["answer"] == "A 完成" and detail["workdir"] == str(dir_a), detail

        # 任务清单 + 能不能续：边界退出的线程 next 为空但 abort 为真，应该可续
        snap = {
            "records": [
                {"task": "t1", "verify": "v", "passed": True, "attempts": 1, "duration": 1.0},
                {"task": "t2", "verify": "v", "passed": False, "verified": False, "attempts": 1},
            ],
            "current": {}, "pending": [],
            "abort": True, "draining": False, "last_error": "", "has_pending": False,
        }
        with patch("lodestar_agent.history.read_checkpoint", return_value=snap):
            item = store.detail(db, "t-a", str(default_dir))
        assert item["resumable"] is True, item
        assert [t["status"] for t in item["tasks"]] == ["pass", "skip"], item["tasks"]

        # 正常跑完（无 aborted / 无未完成写入）不可续；执行失败 / 有未完成写入可续
        for snap_state, flag in (
            ({"abort": False, "draining": False, "last_error": "", "has_pending": False}, False),
            ({"abort": False, "draining": False, "last_error": "boom", "has_pending": False}, True),
            ({"abort": False, "draining": False, "last_error": "", "has_pending": True}, True),
            ({"abort": False, "draining": True, "last_error": "", "has_pending": False}, True),
        ):
            with patch("lodestar_agent.history.read_checkpoint", return_value=snap_state):
                got = store.detail(db, "t-a", str(default_dir))
            assert got["resumable"] is flag, (snap_state, got["resumable"])
        assert store.detail(db, "nope", str(default_dir)) is None

        # HTTP 接口：左侧导航与详情页的数据源
        cfg = load_config("config.yaml")
        cfg.root = tmp
        control = Control()
        runner = Runner(cfg, control)
        server = ConsoleServer(control, meta={}, runner=runner, host="127.0.0.1", port=0).start()
        base = server.url.rstrip("/")
        try:
            with urllib.request.urlopen(base + "/history", timeout=5) as resp:
                body = _json.loads(resp.read())
            assert body["ok"] and body["total"] == 4, body
            assert len(body["projects"]) == 3, body["projects"]

            with urllib.request.urlopen(base + "/history/t-a", timeout=5) as resp:
                one = _json.loads(resp.read())
            assert one["ok"] and one["item"]["thread"] == "t-a", one

            try:
                urllib.request.urlopen(base + "/history/nope", timeout=5)
                raise AssertionError("不存在的会话应返回 404")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404, exc.code
        finally:
            server.stop()

    print("ok  history_store_and_api")


def test_web_console_guide_and_stop() -> None:
    """控制台：页面可访问、引导能投递、事件流可重放、退出请求能置位。"""
    import json as _json
    import urllib.error
    import urllib.request

    from lodestar_agent.server import ConsoleServer

    control = Control()
    server = ConsoleServer(control, meta={"thread": "t-web"}, host="127.0.0.1", port=0).start()
    base = server.url.rstrip("/")

    def post(payload: dict) -> dict:
        req = urllib.request.Request(
            base + "/command",
            data=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        return _json.loads(urllib.request.urlopen(req, timeout=5).read())

    try:
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
        assert 'id="root"' in html, "页面没加载出来"
        assert "/assets/" in html, "构建产物没被引用（先 cd frontend && npm run build）"

        assert post({"type": "guide", "target": "worker", "text": "别动 a.txt"})["ok"]
        assert control.pending_notes("worker")[0]["text"] == "别动 a.txt"

        # SSE：先来一条注释帧，再补发 backlog 里的事件
        with urllib.request.urlopen(base + "/events?since=0", timeout=5) as resp:
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            assert resp.readline().decode().startswith(":")

        assert post({"type": "stop"})["ok"]
        assert control.stop_requested

        try:
            post({"type": "guide", "target": "main", "text": "   "})
            raise AssertionError("空引导应被拒绝")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400, exc.code

        # 目录选择：后端返回绝对路径 / 取消，不要把异常抛成 500
        with tempfile.TemporaryDirectory() as picked:
            with patch("lodestar_agent.server.pick_directory", return_value=picked):
                res = post({"type": "pickdir"})
            assert res["ok"] and res["path"] == picked, res

        with patch("lodestar_agent.server.pick_directory", return_value=None):
            res = post({"type": "pickdir"})
        assert res["ok"] and res["canceled"] is True, res

        from lodestar_agent.fspick import PickUnavailable

        with patch("lodestar_agent.server.pick_directory",
                   side_effect=PickUnavailable("当前环境没有图形界面")):
            try:
                post({"type": "pickdir"})
                raise AssertionError("打不开选择器应该返回 400")
            except urllib.error.HTTPError as exc:
                assert exc.code == 400, exc.code
    finally:
        server.stop()

    print("ok  web_console_guide_and_stop")


def test_worker_against_fake_cli() -> None:
    """用一个假 CLI 验证子进程调用 + JSON 解析（真实的 codebuddy 行为由登录后实测）。"""
    if os.name == "nt":
        print("skip  worker_against_fake_cli（Windows 无 /bin/sh；JSON 解析由 json_parse_with_noise 覆盖）")
        return
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "fake-codebuddy"
        fake.write_text(
            "#!/bin/sh\n"
            'echo "noise line"\n'
            'echo \'[{"type":"message","role":"user"},'
            '{"type":"result","result":"fake worker ok","session_id":"abc","is_error":false}]\'\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)
        result = run_worker("do something", CliOptions(cwd=Path(tmp) / "wd", cli=str(fake), timeout=30))

    assert result.ok, result.error
    assert result.output == "fake worker ok", result.output
    assert result.session_id == "abc"
    print("ok  worker_against_fake_cli", result.output)


if __name__ == "__main__":
    test_plan_sanitizes_unverifiable_criteria()
    test_verify_fail_then_pass_retries_worker()
    test_retry_cap_stops_dead_loop()
    test_exec_failure_terminates_after_retries()
    test_plan_dynamic_replan()
    test_plan_parse_retry()
    test_plan_parse_retry_exhausted_falls_back()
    test_checkpoint_persists_and_resumes()
    test_resume_after_exec_failure_resets_attempts()
    test_json_parse_with_noise()
    test_diagnose_never_dumps_transcript()
    test_guidance_reaches_worker_once()
    test_boundary_stop_waits_for_current_node()
    test_forced_kill_marks_task_for_retry()
    test_cancel_event_does_not_kill_running_cli()
    test_model_call_is_really_cut_off()
    test_drain_deadline_is_read_lazily()
    test_runner_lifecycle()
    test_runner_model_override()
    test_build_tasks_states()
    test_log_roundtrip_keeps_roles()
    test_config_overrides()
    test_windows_tree_kill()
    test_folder_picker()
    test_history_store_and_api()
    test_web_console_guide_and_stop()
    test_worker_against_fake_cli()
    print("\n全部通过")
