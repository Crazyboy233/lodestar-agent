"""离线自检：不需要 API key、也不需要 codebuddy 登录态。

    uv run python tests/offline_check.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_demo.config import load_config  # noqa: E402
from agent_demo.graph import AgentFailure, _parse_plan, build_graph  # noqa: E402
from agent_demo.worker import CliResult, Verdict, _diagnose, _parse_json, run_worker  # noqa: E402


class FakeLLM:
    """返回固定文本（或按顺序返回），用来替换主 agent 的真实模型调用。"""

    def __init__(self, content):
        self.replies = list(content) if isinstance(content, (list, tuple)) else [content]
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return SimpleNamespace(content=reply)


def _fake_worker(calls: list[str], ok: bool = True):
    def inner(task, **kwargs):
        calls.append(task)
        return CliResult(ok=ok, output=f"done: {task}", duration=0.0, error="" if ok else "boom")

    return inner


def test_plan_sanitizes_unverifiable_criteria() -> None:
    plan = _parse_plan(
        '好的：\n[{"task":"做导航栏","verify":"在浏览器打开页面控制台无报错"},'
        '{"task":"写 a.txt","verify":"ls a.txt 有输出"},'
        '"纯字符串任务"]'
    )
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

    def fake_verifier(task, verify, out, **kwargs):
        if task == "t1":
            seen["t1"] += 1
            if seen["t1"] == 1:
                return Verdict(False, "缺 a.txt", "创建 a.txt", duration=0.0)
        return Verdict(True, "通过", "", duration=0.0)

    plan = [{"task": "t1", "verify": "ls a.txt"}, {"task": "t2", "verify": "ls b.txt"}]
    with patch("agent_demo.graph.run_worker", side_effect=_fake_worker(worker_calls)), patch(
        "agent_demo.graph.run_verifier", side_effect=fake_verifier
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
    with patch("agent_demo.graph.run_worker", side_effect=_fake_worker(worker_calls)), patch(
        "agent_demo.graph.run_verifier", side_effect=always_fail
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

    def failing_worker(task, **kwargs):
        calls.append(task)
        return CliResult(ok=False, output="", duration=0.0, error="429 too many requests")

    plan = [{"task": "t1", "verify": ""}]
    with patch("agent_demo.graph.run_worker", side_effect=failing_worker):
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

    def always_pass(_task, _verify, _out, **_kw):
        return Verdict(True, "通过", "", duration=0.0)

    plan = [{"task": "t1", "verify": "ls a.txt"}, {"task": "t2", "verify": "ls b.txt"}]
    with patch("agent_demo.graph.run_worker", side_effect=_fake_worker(worker_calls)), patch(
        "agent_demo.graph.run_verifier", side_effect=always_pass
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

    def always_pass(_t, _v, _o, **_kw):
        return Verdict(True, "通过", "", duration=0.0)

    with patch("agent_demo.graph.run_worker", side_effect=_fake_worker(worker_calls)), patch(
        "agent_demo.graph.run_verifier", side_effect=always_pass
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

    def always_pass(_t, _v, _o, **_kw):
        return Verdict(True, "通过", "", duration=0.0)

    with patch("agent_demo.graph.run_worker", side_effect=_fake_worker(worker_calls)), patch(
        "agent_demo.graph.run_verifier", side_effect=always_pass
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

        def interrupted_worker(task, **_kw):
            calls.append(task)
            raise KeyboardInterrupt("模拟 Ctrl+C")

        def always_pass(_t, _v, _o, **_kw):
            return Verdict(True, "通过", "", duration=0.0)

        app = build_graph(cfg, llm=FakeLLM("汇总"), checkpointer=saver)
        with patch("agent_demo.graph.run_worker", side_effect=interrupted_worker), patch(
            "agent_demo.graph.run_verifier", side_effect=always_pass
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

        def ok_worker(task, **_kw):
            resumed.append(task)
            return CliResult(ok=True, output=f"done: {task}", duration=0.0)

        app2 = build_graph(
            cfg,
            llm=FakeLLM(['{"remaining":[],"note":"收工"}', "最终答复"]),
            checkpointer=saver,
        )
        with patch("agent_demo.graph.run_worker", side_effect=ok_worker), patch(
            "agent_demo.graph.run_verifier", side_effect=always_pass
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

        def failing(_task, **_kw):
            return CliResult(ok=False, output="", duration=0.0, error="401 unauthorized")

        with patch("agent_demo.graph.run_worker", side_effect=failing):
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
        with patch("agent_demo.graph.run_worker", side_effect=_fake_worker(calls)):
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


def test_worker_against_fake_cli() -> None:
    """用一个假 CLI 验证子进程调用 + JSON 解析（真实的 codebuddy 行为由登录后实测）。"""
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
        result = run_worker("do something", cwd=Path(tmp) / "wd", cli=str(fake), timeout=30)

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
    test_worker_against_fake_cli()
    print("\n全部通过")
