# agent.md — 多 agent 协作设计

基于 LangGraph 的「主 agent + worker + 验收 agent」三角色协作。主 agent 负责动态规划与全局进度，worker 负责执行，验收 agent 负责判定。

## 1. 角色

| 角色 | 运行环境 | 职责 | 权限 | 模型 |
| --- | --- | --- | --- | --- |
| 主 agent（plan） | LangGraph 内，DeepSeek API | 拆任务方案、每轮读进度动态修订、汇总 | 不碰文件，只调模型 | `main_agent.model`（deepseek-flash） |
| worker | codebuddy CLI 无头模式（独立会话） | 只执行，不判断自己是否完成 | `Read,Write,Edit,Bash` | `worker.model`（留空=CLI 默认） |
| 验收 agent | codebuddy CLI 无头模式（独立会话） | 自己写 test 脚本并执行，判定是否达标 | `Read,Write,Bash`；被测目录只读，脚本写到 `.verify/` | `verifier.model`（留空=CLI 默认） |

## 2. 状态机

```
START → plan ─┬─ dispatch ─► worker → verify ─┐
              │                                │
              └─ done ──────► synthesize ←─────┘
```

- `plan` 是唯一在循环里的思考节点，被调用两次形态：
  1. **第一次**（`current` 为空）：把目标拆成「任务方案」（1~3 条），分发第一条。
  2. **之后每轮**：读当前任务的产出 + 验收结果，决定「打回重做」/「定稿并发布下一个」/「收尾」，可动态修订剩余任务方案。
- 循环终止（是否打回、是否收尾）由代码强制，不交给模型自觉。

## 3. 状态字段（State）

| 字段 | 含义 |
| --- | --- |
| `goal` | 用户目标 |
| `plan` | 任务方案：尚未分发的剩余任务 `[{task, verify}]` |
| `current` | 当前分发给 worker 的任务 |
| `attempts` | 当前任务的已执行轮数 |
| `feedback` | 打回时写给 worker 的修改指令 |
| `last_output` / `last_error` / `last_duration` | worker 最近一次的产出 |
| `verdict_passed` / `verdict_reason` / `verdict_fix` | 验收结论 |
| `plan_raw` / `decision_raw` / `verdict_raw` | 各 agent 原始输出（写日志用） |
| `records` | 已定稿的任务记录（含 pass/attempts/reason） |
| `answer` | 最终答复 |

## 4. 关键机制

### 4.1 任务方案动态规划

- plan 每轮拿到「目标 + 已完成记录 + 当前验收结果 + 剩余方案」，输出 JSON 决策。
- 定稿时可修订剩余方案：补充新任务、改验收标准、删掉不需要的任务、安排补救。
- 打回时输出 `feedback` 给 worker；定稿时输出 `remaining` 修订队列。
- 解析失败 / 无模型（dry-run）→ 回退确定性逻辑。

### 4.2 验收（自己写测试）

- 简单检查：验收 agent 直接跑命令（`python3 x.py`、`grep`）。
- 复杂检查：把 test 脚本写到 `scratch_dir`（`.verify/`）再运行。
- 被测目录只读（靠目录分离 + prompt 约束，非硬隔离）。
- 输出 JSON `{pass, reason, fix}`；解析失败保守判 FAIL。

### 4.3 打回与重试

| 场景 | 重试上限 | 超限后 |
| --- | --- | --- |
| 主 agent 模型请求失败（plan/synthesize） | `main_agent.max_retries` | 终止流程，报失败节点+原因 |
| 拆解输出解析不成 JSON | `main_agent.plan_retries` | 带纠正提示重试；用尽则退化为单任务 |
| worker 执行失败（超时/429/CLI 报错） | `worker.max_retries` | 终止流程，报失败节点+原因 |
| 验收 agent 请求失败 | `verifier.max_retries` | 终止流程，报失败节点+原因 |
| 验收不通过 | `verifier.max_rounds` | 该条定稿为失败，继续下一条 |

「验收不通过」是质量失败（可继续）；「模型请求失败」是可用性失败（终止）。

### 4.4 失败与终止

- 主 agent 的 `llm.invoke` 统一走 `_call_llm`：指数退避重试，耗尽抛 `AgentFailure(node, reason)`。
- worker 执行失败重试耗尽，在 `plan` 节点抛 `AgentFailure("worker(执行)", ...)`。
- `__main__` 捕获 `AgentFailure` → 打印 `[流程终止] 模型请求失败` + 失败节点 + 可能原因，退出码 3。
- `_explain` 把 429 / 超时 / 401 / 连接 / 5xx 映射成中文原因。

### 4.5 checkpoint 与续跑

- `SqliteSaver` 挂在 `graph.compile(checkpointer=...)` 上，state 落 `checkpoints.sqlite`（gitignore）。
- 一次运行 = 一个 `thread_id`（默认 `run_<时间戳>`，可用 `--thread-id` 指定）。
- 中断 / 失败终止后 `--resume` 从「上次没走完的节点」继续，已定稿任务不重做。
- 执行失败终止的线程，`--resume` 会先把 `attempts` 归零（`update_state(as_node="verify")`），否则 plan 一进去就又撞 `worker.max_retries` 上限。
- 同一 `thread_id` 不加 `--resume` 直接报冲突，防止新 state 脏覆盖旧 checkpoint。

### 4.6 CLI 调用层的硬约束（改 `worker.py` 前必读）

这几条都是被实际踩过才写进去的，不是风格偏好：

| 约束 | 为什么 |
| --- | --- |
| 子进程只继承**白名单**环境变量 | 宿主（WorkBuddy / IDE）注入的 `CODEBUDDY_*`、`SERVER__PORT`、`BAGGAGE` 会让嵌套 CLI 走进另一套认证，直接报 `Authentication required`；`SERVER__PORT` 还会抢端口 `EADDRINUSE` |
| `--output-format json` 按**两种形态**解析 | 实际返回消息数组，`result` 在最后一项（文档示例是对象）。`_parse_json` / `_pick_result` 都兼容 |
| 必须匹配 `Authentication required` 字符串 | 未登录时退出码**仍是 0**，错误只在 stderr，只看 returncode 会误判成功 |
| 失败输出压成一句话（`_diagnose`，上限 400 字） | 否则会把整段 transcript 倒进终端 |
| `-p` 模式要关后台任务、必带 `-y` | 单发模式不支持后台任务（`CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS=1`）；不带 `-y` 文件写入和命令执行会被权限拦下 |
| 子进程独立进程组（`start_new_session=True`） | 中断 / 超时对整组发 SIGTERM→SIGKILL，否则 CLI 自己起的子进程会变成孤儿 |

## 5. 配置（config.yaml）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `main_agent.base_url` / `api_key` / `model` / `temperature` | — | 主 agent 模型 |
| `main_agent.max_retries` | 3 | 模型请求失败重试次数 |
| `main_agent.plan_retries` | 2 | 拆解输出解析失败的重试次数 |
| `worker.cli` / `model` / `workdir` / `allowed_tools` | — | 执行者 |
| `worker.max_turns` | 15 | 单次执行最多轮数（`--max-turns`） |
| `worker.max_retries` | 2 | 执行失败重试次数 |
| `verifier.cli` / `model` / `allowed_tools` | — | 验收者 |
| `verifier.scratch_dir` | `.verify` | 测试脚本目录（相对 workdir） |
| `verifier.max_rounds` | 3 | 「执行→验收」往复轮数 |
| `verifier.max_retries` | 2 | 验收 agent 自身请求失败重试次数 |

key 放 `config.local.yaml`（已 gitignore），`config.yaml` 留空。

## 6. 文件结构

```
agent_demo/
  config.py      配置加载（config.local.yaml 覆盖 + 环境变量兜底）
  graph.py       状态机（plan / worker / verify / synthesize）
  worker.py      CLI 调用层：run_cli / run_worker / run_verifier
  __main__.py    入口：参数、逐节点打印、写日志、捕获 AgentFailure
config.yaml / config.local.yaml
checkpoints.sqlite   每次运行的 state（gitignore），--resume 的数据源
workspace/       worker 的 cwd 和产出（gitignore）
logs/            每次运行的完整日志（gitignore）
tests/offline_check.py  离线自检
```

## 7. 运行

```bash
uv run python -m agent_demo "你的任务"
uv run python -m agent_demo --dry-run "..."   # 不调主模型、跳过验收
```

## 8. 已知边界

- 子任务串行；并行需把 worker 改为 `Send` 分发。
- 验收「被测目录只读」是软约束（目录分离 + prompt），硬隔离需副本模式。
- 后一个子任务只依赖磁盘产出，无显式上下文传递。
- 收尾由「剩余任务为空」决定，不是「全部通过」：模型若忘了往 `remaining` 塞补救任务，失败项会直接进汇总。
