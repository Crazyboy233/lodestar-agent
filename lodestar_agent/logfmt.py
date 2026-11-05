"""运行日志的格式契约 —— 写入端与解析端共用同一份常量。

以前这是个隐式协议：`session.py` 往日志里写字面量 `任务: ` / `输出:` /
`--- 模型原始输出 ---`，`history.parse_log` 用同样的字面量再认一遍。改一侧
不会报错，只会让「回看历史」的内容分类静默错位。现在两边都引用这里。
"""

from __future__ import annotations

import re

# 终端与日志各用一条分隔线（终端窄、日志宽，保持原来的观感）
RULE = "─" * 62
LOG_RULE = "=" * 70

# `[主 agent] 拆解任务方案` —— 段落标题，tag 决定这一段的 kind
SECTION_RE = re.compile(r"^\[([^\]]{1,20})\]\s*(.*)$")
NUMBERED_RE = re.compile(r"^\d+\.\s+")

# 模型原始输出块的标记行，其后到下一个标题之间的内容按 output 归类
RAW_MARK = "--- 模型原始输出 ---"

# 字段前缀（日志里写成 `前缀: 内容`，解析端据此判断这一块属于哪类角色）
TASK = "任务"
VERIFY = "验收"
OUTPUT = "输出"
ERROR = "错误"
CONCLUSION = "结论"
NOTE = "note"

# 行首前缀 → role（带冒号，避免正文里恰好以「任务」开头的句子误判）
ROLE_START: tuple[tuple[str, str], ...] = (
    (f"{TASK}:", "task"),
    (f"{VERIFY}:", "task"),
    (f"{OUTPUT}:", "output"),
    (f"{CONCLUSION}:", "output"),
    (f"{ERROR}:", "output"),
    (f"{NOTE}:", "log"),
)

# 标题 tag → 页面色条 / 分类
KIND_BY_TAG = {"主 agent": "plan", "汇总": "synth", "worker": "worker", "verifier": "verifier"}
STOP_TAGS = {"流程终止", "已中断", "收尾", "续跑失败", "线程冲突"}

# 只有这些 tag 才算「段落标题」。
# 否则正文里以 `[PASS]`、`[1]` 开头的行会被误判成标题，把整段内容切碎
# （dry-run 的最终答复正好是 `[PASS] 任务（N 轮）`）。
KNOWN_TAGS = frozenset(KIND_BY_TAG) | frozenset(STOP_TAGS)


def is_rule(line: str) -> bool:
    """`======` / `──────` 分隔线。"""
    return len(line) >= 10 and set(line) in ({"="}, {"─"})


def kind_of(tag: str, current: str) -> str:
    return KIND_BY_TAG.get(tag) or ("stop" if tag in STOP_TAGS else current)


def field(prefix: str, text: str) -> str:
    """日志里的一行字段：`任务: 写 a.txt`。"""
    return f"{prefix}: {text}"
