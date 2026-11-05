"""「模型自由文本 → 结构化数据」的唯一实现。

原来这套逻辑有三份：主 agent 的决策对象、任务方案数组、验收结论对象，
各自写了一遍 `json.loads → ```json 代码块 → 正则抠 [] / {}`，而且已经分叉
（其中一份多了逐行回退）。这里统一成一份，解析顺序取三者的并集。

另外 `sanitize_verify` / `normalize_items` 也在这里 —— 它们是「任务方案」
这道闸门，和解析属于同一件事：把模型输出清洗成能执行的结构。
"""

from __future__ import annotations

import json
import re

_DEFAULT_CLIP = 1200

# 验收标准里出现这些词，说明它不是可执行检查，一律丢掉让验收 agent 自己设计
BANNED_IN_VERIFY = (
    "浏览器", "控制台", "响应式", "窄屏", "视觉", "布局美观", "美观", "体验", "流畅",
    "截图", "肉眼", "手动", "用户友好",
)
# 验收标准至少要含一个"可执行"的信号
EXECUTABLE_MARKERS = (
    "grep", "node --check", "py_compile", "pytest", "curl", "cat ", "ls ", "test ",
    "存在", "包含", "退出码", "输出", "diff", "wc ",
)


def clip(text: str, limit: int = _DEFAULT_CLIP) -> str:
    """截断长文本并注明原文长度。终端/日志/提示信息共用一个上限口径。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"…(已截断，共 {len(text)} 字)"


def _loads(text: str) -> object | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _from_fence(text: str) -> object | None:
    for block in re.findall(r"```(?:json)?\s*([\s\S]*?)```", text):
        data = _loads(block.strip())
        if data is not None:
            return data
    return None


def _from_brackets(text: str, open_ch: str, close_ch: str) -> object | None:
    match = re.search(re.escape(open_ch) + r"[\s\S]*" + re.escape(close_ch), text)
    if not match:
        return None
    return _loads(match.group(0))


def _from_lines(text: str, open_ch: str) -> object | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(open_ch):
            data = _loads(line)
            if data is not None:
                return data
    return None


def _extract(text: str, open_ch: str, close_ch: str, want: type) -> object | None:
    text = (text or "").strip()
    if not text:
        return None
    # 按「越可能正确、越便宜」的顺序短路：整段能解析就别再跑正则
    strategies = (
        lambda: _loads(text),
        lambda: _from_fence(text),
        lambda: _from_brackets(text, open_ch, close_ch),
        lambda: _from_lines(text, open_ch),
    )
    for strategy in strategies:
        data = strategy()
        if isinstance(data, want):
            return data
    return None


def extract_object(text: str) -> dict | None:
    """从自由文本里抠出 JSON 对象（模型决策 / 验收结论）。找不到返回 None。"""
    return _extract(text, "{", "}", dict)  # type: ignore[return-value]


def extract_array(text: str) -> list | None:
    """从自由文本里抠出 JSON 数组（任务方案）。"""
    return _extract(text, "[", "]", list)  # type: ignore[return-value]


def sanitize_verify(verify: str) -> str:
    """不可执行 / 不可判定的验收标准返回空串，交给验收 agent 自行设计检查。"""
    text = (verify or "").strip()
    if not text:
        return ""
    if any(word in text for word in BANNED_IN_VERIFY):
        return ""
    if not any(marker in text.lower() for marker in EXECUTABLE_MARKERS):
        return ""
    return text


def normalize_items(items: object) -> list[dict]:
    """把模型给的任务方案归一成 `[{task, verify}]`，顺手过滤空条目与坏验收。"""
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
            out.append({"task": task, "verify": sanitize_verify(verify)})
    return out
