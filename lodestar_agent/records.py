"""任务记录（record）的唯一构造与投影点。

一条子任务从执行到定稿，会在四个地方被"读"：主 agent 的 prompt、页面的任务清单、
运行日志、历史详情。以前每处各自按字段名手拼一遍（`graph` 两处、`session` 一处、
`history` 一处、`frontend/types.ts` 一份），record 加个字段就得改五处。

现在 record 只在这里造，也只在这里摊平成给页面 / 历史的形状。
"""

from __future__ import annotations

# record 的字段语义（没有用 TypedDict 是因为它要经 JSON / checkpoint 往返，保持纯 dict）：
#   index    序号（用于 prompt 里的「子任务 N」）
#   task/verify  子任务与验收标准
#   passed   是否通过
#   verified 是否真的验过（False = 只跑了 worker，不能算失败）
#   attempts 这条已经执行了几轮
#   reason   人读的结论（通过 / 失败原因 / 未验收）
#   duration worker 最后一次执行的耗时（秒）


def make_record(
    *,
    index: int,
    current: dict,
    passed: bool,
    verified: bool,
    attempts: int,
    reason: str,
    duration: float,
) -> dict:
    return {
        "index": index,
        "task": current.get("task", ""),
        "verify": current.get("verify", ""),
        "passed": passed,
        "verified": verified,
        "attempts": attempts,
        "reason": reason,
        "duration": round(duration or 0.0, 1),
    }


def settled(state: dict, current: dict) -> dict:
    """当前任务走完「执行 → 验收」后的定稿记录。"""
    passed = bool(state.get("verdict_passed", False))
    return make_record(
        index=len(state.get("records") or []) + 1,
        current=current,
        passed=passed,
        verified=True,
        attempts=state.get("attempts", 1),
        reason="通过" if passed else (
            state.get("verdict_reason") or state.get("verdict_fix") or "未通过"
        ),
        duration=state.get("last_duration", 0.0),
    )


def drained(state: dict, current: dict | None) -> list[dict]:
    """退出时把当前这条按真实完成度记一笔。

    三态：验过且过 → PASS；验过没过 → FAIL；只跑了 worker 没来得及验收 → 未验收。
    「未验收」不能算失败，否则汇总会冤枉一条其实已经干完的活。
    """
    if not current or not state.get("attempts"):
        return []
    ready = bool(state.get("verdict_ready"))
    passed = bool(state.get("verdict_passed")) if ready else False
    if ready:
        detail = "通过" if passed else (state.get("verdict_reason") or "未通过")
    else:
        detail = "已执行，未验收（用户请求退出）"
    return [make_record(
        index=len(state.get("records") or []) + 1,
        current=current,
        passed=passed,
        verified=ready,
        attempts=state.get("attempts", 1),
        reason=detail,
        duration=state.get("last_duration", 0.0),
    )]


def dedupe(records: list[dict]) -> list[dict]:
    """同一条任务可能「中止/失败 → 重跑」过，只留最后一次，并重排序号。

    只用于最终摘要 / 页面清单；给主 agent 看的进度块仍保留完整历史，
    否则它看不到「这条失败过几次」。
    """
    latest: dict[str, dict] = {}
    for r in records or []:
        latest[str(r.get("task", ""))] = r
    out: list[dict] = []
    for i, r in enumerate(latest.values(), 1):
        item = dict(r)
        item["index"] = i
        out.append(item)
    return out


def task_status(record: dict) -> str:
    """record → 页面上的四态（pending / running 由调用方补，不在这里）。"""
    if not record.get("verified", True):
        return "skip"          # 跑了 worker 没来得及验收
    return "pass" if record.get("passed") else "fail"


def task_item(record: dict) -> dict:
    """record → 页面 / 历史详情里的 TaskItem。"""
    return {
        "task": record.get("task", ""),
        "verify": record.get("verify", ""),
        "status": task_status(record),
        "attempts": record.get("attempts", 1),
        "duration": record.get("duration", 0.0),
        "reason": record.get("reason", ""),
    }


def tasks_from_state(state: dict) -> list[dict]:
    """把 state 摊成页面上的任务清单：已完成 + 当前 + 剩余。

    已完成按任务文本去重，否则清单里会出现好几条同名任务，看着像重复派工。
    """
    tasks = [task_item(r) for r in dedupe(state.get("records") or [])]

    current = state.get("current")
    if current:
        tasks.append({
            "task": current.get("task", ""),
            "verify": current.get("verify", ""),
            "status": "running",
            "attempts": state.get("attempts", 0) or 1,
            "duration": state.get("last_duration", 0.0),
            "reason": "",
        })

    for item in state.get("plan") or []:
        tasks.append({
            "task": item.get("task", ""),
            "verify": item.get("verify", ""),
            "status": "pending",
            "attempts": 0,
            "duration": 0.0,
            "reason": "",
        })
    return tasks
