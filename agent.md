# Lodestar — 多 agent 协作设计

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
  1. **第一次**（`current` 为空）：把目标拆成「任务方案」，分发第一条。条数由「可独立验证的结果」决定，不写死；简单任务 1~2 条，复杂任务先拆一条「分析 + 产出一份简短方案」，实施任务留到拿到分析结论后再规划。一次最多 5 条。
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
- 刚定稿的是「分析/方案」类任务时，据其结论补充实施任务（一次仍最多 5 条，其余后续再补）。
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
| 子进程独立进程组（POSIX `start_new_session=True`） | 中断 / 超时对整组发 SIGTERM→SIGKILL；Windows 没有进程组信号，用 `taskkill /PID <pid> /T /F` 收进程树 |

### 4.7 人机协同：引导信箱与边界退出

`lodestar_agent/control.py` 是「人」与「图」之间唯一的通道，控制台页面只跟它打交道。

**引导（不打断）**

- 两个信箱：`main` / `worker`。`push_note` 入队，`take_notes` 消费即清空（投一次生效一次）。
- 消费点固定在节点边界：`worker_node` 在启动 CLI **之前**取（拼进 `WORKER_PROMPT` 的 guidance 段）；`plan_node` 在调模型**之前**取（拼进 PLAN_INIT / PLAN_ROUND 的 guidance 段）。
- 所以 worker 跑到一半时投递，会等它这一轮结束、下一次被调用才生效。这不是妥协 —— `codebuddy -p` 的 stdin 已关闭，本来就没有中途注入通道。
- 引导只改 prompt 文本，不改变循环控制；`_sanitize_verify` 等闸门照旧生效。

**边界退出（不是中断）**

语义：**当前这一次模型回复跑完再说，下一个模型回复开始前收工**。目标是退出时手上不留半成品。

- `request_stop()` 只置 `threading.Event` + 记 `drain_deadline = now + ui.drain_timeout_seconds`，不碰任何进程。
- `run_cli` 每 0.25s 轮询：`cancel` 被置位**不会**打断已经发出去的那一轮（这才叫边界）；只有 `now >= drain_deadline` 才 `_terminate_tree`（POSIX SIGTERM → SIGKILL；Windows `taskkill /T /F`），返回 `aborted=True`，那是兜底路径。
- 节点边界分入口 / 出口两种处理：
  - 入口见 stop → 这次调用根本不启动（`aborted_stage=worker`）；
  - 出口见 stop → 这一轮已跑完，标 `draining=True`（worker 出口记 `aborted_stage=verify`，verify 出口记 `done`）。
- `route_after_worker` 见 `draining`/`abort` 直接回 `plan`，跳过验收 —— 验收也是一次模型调用，属于「下一个」。
- `plan_node` 在调模型**之后**也要再查一次：模型刚回复完就退出，不接着派工。
- `synth_node` 用确定性文案（不调模型）输出三态进度；`aborted_stage` 决定 resume 从哪接。
- 退出码 130。

**强杀必须断到「连接」这一层**

只让 Python 退出、或只 kill 主 CLI 进程，都不叫停掉模型：CLI fork 的 node 子进程还活着 → 它手里的 HTTP 连接还在 → 服务端继续生成、继续计费。四条硬经验：

1. `_terminate_tree` 的 pgid 用 `proc.pid`（`start_new_session=True` 保证它是 leader），不用 `os.getpgid` —— 后者在进程刚退出时抛 `ProcessLookupError`，那条分支只 kill 主进程，会漏掉组里还活着的孙进程。
2. SIGTERM 之后**无论主进程退没退**，都再对整组补一次 SIGKILL：主进程死了不代表孙进程死了。
3. `_drain_pipes` 给 `communicate()` 加超时：孙进程攥着 stdout pipe 时，无超时的 communicate 会永久阻塞，表现为「点了退出但进程一直不退」。
4. 主 agent 走 `llm.stream()`（`_invoke_interruptible`），到点 `gen.close()` 断连；非流式做不到真正停止生成。`_call_llm` 遇到 `ModelAborted` 不重试。

另外 `drain_deadline` 必须传**回调**而不是常量：退出请求通常发生在 CLI 已经跑起来之后，进入 `run_cli` 时还求不出来；传死值会让强杀分支永远不触发。

`atexit` 注册 `terminate_all_cli()` 兜底，进程退出时回收所有存活 CLI 进程（Windows 为进程树）。

**记录三态**：`verified` 字段区分「验过」「没验过」。只跑了 worker、没来得及验收的记 `[SKIP] 已执行，未验收`，**不算失败** —— 否则汇总会冤枉一条其实已经干完的活。

**resume 接续**

| `aborted_stage` | 含义 | `--resume` 怎么做 |
| --- | --- | --- |
| `worker` | 这条还没开始跑 | 排回队首重跑 |
| `verify` | worker 跑完了、只差验收 | `update_state(as_node="worker")` → next 直接落 `verify`，不重跑 worker |
| `done` | 已验证完 | 直接派下一个，这条不重做 |

**事件流**

- 所有节点打印同时走 `Console.hr/say`：终端照旧，页面通过 SSE 收。事件带单调 `seq`，断线重连按 `since` 补发、页面按 `seq` 去重。

### 4.8 页面驱动：入口只起服务

`--web` 且不带任务时，`main()` 只做三件事：建 `Control`、建 `Runner`、起 `ConsoleServer`，然后阻塞等待。任务从页面来：`POST /command {"type":"run","goal":...}` → `Runner.start()` 起后台线程跑 `run_session`。

- **一次一个**：`Runner` 用线程 + 锁保证；运行中再 `start` 抛 `RuntimeError`，HTTP 层转 409。
- **状态分两个维度**：`status` 描述「图跑到哪一步」（busy / draining / done），`session` 描述「这一次运行整体」（idle / running / done / failed / aborted / error）。页面靠 `session.state == idle` 决定显示任务输入还是控制台。
- **reset**：`Control.reset()` 清停止标志 / 信箱 / 忙碌 / 计时，清空事件 backlog，再发一条 `reset` 事件；页面收到后清屏并把 `lastSeq` 推过去，避免上一轮排队的事件被重放。
- **执行逻辑只有一份**：CLI 模式直接同步调 `run_session`，页面模式把它丢进线程，入口不重复实现。
- **模型选择**：页面把 worker / 验收的模型随 `run` 一起提交，`Runner.start` 在起线程前改 `cfg.worker.model` / `cfg.verifier.model`。`None` = 沿用配置，`""` = 显式让 CLI 决定。一次只跑一个，改共享 cfg 是安全的。候选列表来自 `codebuddy --help` 的 `--model` 说明，可用 `ui.models` 覆盖。
- **产出目录**：`run` 可带 `workdir`（绝对路径或相对项目根），`Runner._resolve_workdir()` 展开、按需 `mkdir`，再改 `cfg.worker.workdir`。`/state` 的 `workdir` 是当前生效值；不传或传空串时回到配置里的默认目录，避免上一次的选择长期粘住。
- **目录选择**：`POST /command {"type":"pickdir"}` 由 `fspick.py` 在后端所在机器弹系统选择器（macOS `osascript`、Windows PowerShell `FolderBrowserDialog`、Linux `zenity`/`kdialog`），返回绝对路径或 `canceled`。浏览器拿不到本地绝对路径（File System Access API 只有 handle），所以必须由本机进程代劳；同步子进程放 `asyncio.to_thread`，并用全局锁防止叠出多个弹窗。
- **历史与项目分组**：`HistoryStore`（`history.py`）在每次 run 开始时写 `history.json`（thread / goal / workdir / 模型 / 状态），结束时补退出码、耗时、最终答复。`GET /history` 用 `groups()` 按 workdir 分组：一个目录就是一个项目，组内按时间倒序。json 里没有的老 thread 从 `checkpoints.sqlite` 回填一次（用 uuid6 checkpoint_id 还原时间）。`GET /history/{thread}` 返回详情：任务清单由 checkpoint 的 `records` 映射，`resumable` 由 `abort / draining / last_error / pending_writes` 判断 —— 边界退出、执行失败、写了一半的线程可续，正常跑完的不可续（`next` 不在 checkpoint 里，是按图即时算的，不能直接读）。
- **任务清单**：`build_tasks(state)` 把 `records` + `current` + `plan` 摊成一张列表，每个 chunk 后推一条 `tasks` 事件。`records` 按 task 文本去重（同一条被打回或重跑只留最后一次），状态映射 `pass / fail / skip / running / pending`。用 `app.get_state()` 读而不是从 `update` 拼 —— `records` 是 `operator.add` 累积的，`current` 会被后续节点改写。
- **配置面板**：`GET /config` 返回 `as_editable(cfg)`（api_key 只给掩码 + `api_key_set`）；`POST /command {"type":"config"}` 经 `collect_overrides()` 走 `EDITABLE` 白名单 + 类型/范围校验，写进 `config.local.yaml`，再 `runner.reload_config()` 重新 load。运行中拒绝（409）。掩码不参与回写：前端 api_key 输入框留空，后端判断「空或等于掩码」就跳过这个字段，不会把掩码写进配置。

**前端**

React 19 + TypeScript + Vite，源码在 `frontend/`，构建产物写进 `lodestar_agent/static/`。后端是 FastAPI + uvicorn（`server.py`），只做 JSON 接口 / SSE / 静态托管，不拼任何页面内容。

- **起服务**：`ConsoleServer.start()` 里先 `config.bind_socket()`（端口占用在这里抛 OSError，调用方据此回退随机端口），再把 socket 交给 `uvicorn.Server.run(sockets=[sock])`。uvicorn 0.54 的 `capture_signals()` 已经判断了主线程，所以在后台线程跑不会碰信号。
- **SSE**：`StreamingResponse` + async 生成器，`queue.get` 用 `asyncio.to_thread` 阻塞读取，1s 无事件就发 `: ping` 心跳；客户端断开时生成器被取消，`finally` 里退订。
- **布局**：左侧 `Sidebar`（新任务 / 设置 / 按目录分组的历史），右侧 `main-col`。`body[data-view]` 控制 `launch / run / history` 三种视图，一个页面里不同时挤三套内容。`launch` / `run` 都是「上方过程区 + 底部独立圆角输入卡」的现代 agent 布局：空闲时上方是欢迎语，运行中上方是事件流，底部卡在运行中切成「给主 agent 的引导」，结束后恢复为新任务输入框；`history` 则隐藏输入卡，只显示历史详情。
- **设置窗口**：`ConfigDialog` 是居中弹窗（调试时 `?cfg=1` 可直接打开），按 `SCHEMA` 分组渲染；字段标签全中文，原始 key 作为副标题；错误信息把 `worker.xxx` 翻成「执行端 · xxx」。
- **历史详情**：`HistoryView` 展示任务清单 / 最终答复，提供「继续这个会话」（按后端 `resumable` 决定能否点）与「在此目录新建任务」（回填 goal + workdir）。
- **运行中刷新**：新会话由后台线程写 `history.json`，开始那一刻可能还没落盘；`session.state == running` 时页面延迟 1.2s 刷新一次、之后每 6s 刷新，左侧才能及时出现「运行中」的会话。
- 开发 `npm run dev`：5174，`/state /config /command /events /history` 代理到 8788。
- 生产 `npm run build`：产物覆盖 `static/`，刷新浏览器即可，不用重启 Python。
- SSE 在 `useStream` 里统一处理 seq 去重；`reset` 事件清界面状态（旧事件已由 seq 过滤，不需要额外跳 seq）。

**运行时可见性**

`Control.set_busy(node, model=..., task=..., attempt=..., max_attempts=...)` 是唯一打点入口，四类节点都接：

| 节点 | 打点位置 | model 取值 |
| --- | --- | --- |
| `plan` / `synth` | `build_graph._ask()` 包住主 agent 的 `_call_llm` | `main_agent.model` |
| `worker` | `worker_node` 启动 CLI 前 | `worker.cli` basename + `worker.model` |
| `verifier` | `verify_node` 启动验收前 | `verifier.cli` basename + `verifier.model` |

`status` 事件带 `busy / model / task / attempt / maxAttempts / busySince`，页面工作栏和 `/state` 快照用的是同一份数据，所以刷新页面也能看到「正在等谁」。`set_busy` 还会往事件流里补一条 `调用模型 X｜第 N 条｜任务`，事后回看日志能对上是哪个模型在干哪条。

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
| `ui.web` | false | true = 不写 `--web` 也默认开控制台 |
| `ui.host` / `ui.port` | 127.0.0.1 / 8788 | 控制台监听地址；端口占用回退随机 |
| `ui.drain_timeout_seconds` | 120 | 边界退出时最多再等多久（等当前模型回复跑完），超时才强杀 |
| `ui.models` | 内置列表 | 页面 worker / 验收下拉的候选（取自 `codebuddy --help`） |

key 放 `config.local.yaml`（已 gitignore），`config.yaml` 留空。

## 6. 文件结构

```
lodestar_agent/
  config.py      配置加载（config.local.yaml 覆盖 + 环境变量兜底）
  graph.py       状态机（plan / worker / verify / synthesize）
  worker.py      CLI 调用层：run_cli / run_worker / run_verifier（可取消、可注入引导）
  control.py     控制面：引导信箱 + 边界退出标志 + 事件总线
  session.py     执行体：Console / run_session / Runner（页面驱动）
  history.py     运行历史：history.json 读写 / 按产出目录分组 / 旧 checkpoint 回填
  fspick.py      系统文件夹选择器（macOS osascript / Windows PowerShell / Linux zenity）
  server.py      FastAPI + uvicorn：JSON 接口 / SSE / 静态托管（不拼页面内容）
  static/        前端构建产物（Vite 输出，勿手改）
frontend/        React + TypeScript 源码（Vite），构建到 lodestar_agent/static/
  src/components/  Sidebar / Header / Workbar / LaunchPanel / Stream / TaskPanel /
                   GuidePanel / QuitPanel / ConfigDialog / HistoryView
  __main__.py    入口：参数、模式选择（CLI 同步 / 页面驱动）
config.yaml / config.local.yaml
checkpoints.sqlite   每次运行的 state（gitignore），--resume 的数据源
history.json    左侧导航的运行历史（gitignore），按产出目录分组
workspace/       默认产出目录（gitignore）；每次任务可在页面另选
logs/            每次运行的完整日志（gitignore）
tests/offline_check.py  离线自检
```

## 7. 运行

```bash
uv run python -m lodestar_agent --web           # 只起页面，任务在页面里填
uv run python -m lodestar_agent "你的任务"       # 纯命令行
uv run python -m lodestar_agent --dry-run "..."  # 不调主模型、跳过验收
uv run python -m lodestar_agent --web "..."      # 起页面并立即开始
```

## 8. 已知边界

- 子任务串行；并行需把 worker 改为 `Send` 分发。
- 验收「被测目录只读」是软约束（目录分离 + prompt），硬隔离需副本模式。
- 后一个子任务只依赖磁盘产出，无显式上下文传递。
- 收尾由「剩余任务为空」决定，不是「全部通过」：模型若忘了往 `remaining` 塞补救任务，失败项会直接进汇总。
