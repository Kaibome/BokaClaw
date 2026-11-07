# BokaClaw 工具调用机制与边界

> 状态：现状梳理 + 改造分析（对照当前仓库版本）
>
> 适用范围：`agent-loop/`（`bokaclaw_agent`）与 `server/app/agent/`（BokaClaw 集成层）
>
> 目标读者：SDK 维护者、server 集成开发者、需要排查工具行为的一线同学
>
> 相关：[`runtime-behavior.md`](runtime-behavior.md)（循环终止、取消、压缩、历史重放）

本文回答工具链路上的一组问题：工具怎么注册和下发、调用前有没有校验和权限、失败怎么处理、结果多大时会被截断、并发能不能做、压缩会不会影响工具历史、失败重试/熔断/幂等现状如何、工具变多后怎么检索和按需加载。

除特别说明外，行号对应当前仓库版本。

---

## 0. 一图看懂

```text
build_runtime
  └─ _memory_tools + build_dev_tools + SubagentTool
       └─ ToolRegistry（全量注册，run 内不变）

每一轮迭代
  ├─ context.build()                全量消息 + 工具结果截断
  ├─ tools.specs()                  全部工具 schema 下发
  ├─ compress(...)                  视图压缩（工具 schema token 计入预算）
  ├─ 模型返回 tool_calls
  ├─ 逐个（串行）invoke
  │    ├─ 参数校验（Pydantic）
  │    ├─ 执行 → 异常归一化为 ToolResult
  │    ├─ 事件 TOOL_CALL / TOOL_RESULT（内容截断）
  │    └─ append tool message（按声明顺序）
  └─ 下一轮

run 结束
  └─ ToolTrace 快照 → assistant.metadata.tool_trace → 下一轮 build_history 重建
```

---

## 1. 工具从哪里来

### 1.1 注册是 build-time 的

server 在每个 run 开始时构造一次工具列表（`server/app/agent/runtime.py:904-912`）：

```text
_memory_tools(user_id)        2 个（有 user_id 时）
build_dev_tools(dev_config)   8 个基础 + 可选 office + 可选 shell
SubagentTool                  explore / research_agent
```

`build_dev_tools`（`server/app/agent/dev_tools.py:801-1197`）返回的工具集：

| 工具 | 条件 |
|---|---|
| `Read` / `Write` / `Edit` / `NotebookEdit` / `Glob` / `Grep` / `WebFetch` / `WebSearch` | 工作区已配置 |
| office 工具 | `AGENT_OFFICE_TOOLS` |
| `Bash` / `PowerShell` / `Monitor` | `AGENT_TOOLS_ALLOW_SHELL`（默认 false） |

子 Agent 由 `AgentRuntime` 在构造时注册成普通 `Tool`（`agent-loop/.../agent/runtime.py:153-185`）。

### 1.2 下发是全量的

每一轮模型请求都用 `self.tools.specs()` 把**全部**已注册工具的 schema 发给模型（`runtime.py:393`）。没有过滤、没有延迟注册、没有元工具。

工具 schema 会被 JSON 序列化并估算 token，再从压缩预算里扣除（`runtime.py:394-399`）。工具越多，留给对话的预算越少。

### 1.3 现有的三层静态裁剪

| 层 | 机制 | 效果 |
|---|---|---|
| 配置开关 | `allow_shell`、`office_tools`、workspace 未配置 | 不满足条件的工具根本不注册 |
| 身份 | 无 `user_id` 不注册记忆工具 | 工具集随用户上下文变化 |
| 子 Agent 策略 | `read_only` / `research` / `code` / `custom` 白名单（`agent/subagent.py:637-652`） | 子 Agent 只看得到允许的工具 |

都是 run 开始前决定的；run 中途不会变。

---

## 2. 参数校验与权限边界

### 2.1 参数校验有三层

1. **JSON 解析**：`parse_tool_arguments` 把模型给的字符串解析成对象；不是 JSON 对象时返回 `arguments_error`，不执行工具（`core/utils.py:11-31`）。
2. **Schema 生成**：`Tool.from_callable` 用函数签名动态创建 Pydantic 模型（`tools/registry.py:97-133`），并据此生成 OpenAI function schema。
3. **调用时校验**：`Tool.invoke` 用 `args_model(**arguments)` 校验并转换；失败抛 `ToolArgumentError`，由 registry 转成 `is_error` 的 ToolResult（`registry.py:221-263`、`293-322`）。

未知工具同样在 registry 层变成错误结果，或在 `error_mode="raise"` 时抛 `ToolNotFoundError`。

### 2.2 没有“权限校验”

SDK 里没有 per-user / per-role 的工具授权。权限只体现在**构建期**：

- shell 工具默认不注册；
- 文件工具通过 `_resolve()` 做工作区路径限制；
- `WebFetch` 通过 `_validate_public_url()` 拒绝内网地址，并在每次重定向后重新校验；
- 子 Agent 通过工具白名单隔离；
- server 的 scope 校验拒绝工作区外的绝对路径和非 http/https scheme（`server/app/agent/subagents.py:144-189`）。

也就是说：**能不能用某个工具在构建期决定；调用期只校验参数是否合法，不校验调用者是否有权。**

---

## 3. 调用与错误归一化

`ToolRegistry.invoke`（`registry.py:293-322`）的顺序：

1. `arguments_error` → 直接返回错误结果；
2. 未知工具 → `ToolNotFoundError` 或错误结果；
3. `Tool.invoke` → 参数校验 → 调用函数 → 序列化返回值；
4. 工具抛异常 → `ToolExecutionError` → 按 `tool_error_mode` 返回或抛出。

server 使用 `tool_error_mode="return"`，因此**工具失败不会中断 run**，模型会收到一条 `{"error": ...}` 的 tool 结果。

同步/异步工具都支持：`Tool.is_async` 记录函数是否协程，`resolve_awaitable` 负责把 awaitable 跑完（`core/utils.py:39-53`）。运行时本身是同步生成器，`arun()` 只是把它丢到线程里。

---

## 4. 消息协议顺序

工具调用块必须满足 OpenAI tool-calling 的消息契约：

1. 带 `tool_calls` 的 assistant 消息后面，必须紧跟对应的 tool 消息；
2. 每个 `tool_call_id` 有且仅有一条对应的 tool 消息，不多不少；
3. tool 消息的顺序应与 `tool_calls` 的 index 一致（严格兼容上游按位置校验）；
4. tool 消息的 `content` 必须是字符串。

SDK 中的对应实现与兜底：

| 位置 | 作用 |
|---|---|
| `context.py:119-137` `assistant_message` | 按 `response.tool_calls` 原序生成 `id/type/function` |
| `context.py:140-152` `tool_message` | 只带 `role` / `tool_call_id` / `content` |
| `runtime.py:468-508` | 串行执行时“调用一个、追加一条”，顺序天然正确 |
| `runtime.py:1127-1162` `_abort_pending_tool_calls` | 取消时给已声明未执行的调用补 `{"error": "run aborted..."}`，防止写出的历史残缺 |
| `context.py:71-94` `safe_start_index` | 截断历史时把起点推到合法边界，避免视图以孤儿 tool 消息开头 |
| `server/app/agent/runtime.py:build_messages` | 回放历史时丢弃没有 assistant 声明的孤儿 tool 消息 |

`_abort_pending_tool_calls` 能精确知道“哪些没执行”，靠的是 `pending_calls` 的切片：进入工具循环前是完整列表，每执行完一个就推进一位，整轮结束后置空（`runtime.py:450/509/513`）。

---

## 5. 并发改造分析

> 详细的分阶段改造方案见 [`tool-concurrency-plan.md`](tool-concurrency-plan.md)。P0 已实现：`Tool.effect` / `concurrency_safe`、`ToolRegistry.plan_batches` / `invoke_batch`、`AgentConfig.tool_concurrency`，server 通过 `AGENT_TOOL_CONCURRENCY` 开启（默认 1）。

### 5.1 现状（改造前）

一次模型响应里的多个 `tool_call` 是严格串行执行的（`runtime.py:449-512`）。没有线程池，也没有并发执行子 Agent 的路径。`run_in_background` 被明确拒绝（`subagent.py:397-401`）。

### 5.2 直接给循环套线程池会破坏四件事

| 类别 | 问题 |
|---|---|
| **协议顺序** | 完成顺序 ≠ 声明顺序；按完成顺序 append 会让 tool 消息与 `tool_calls` 错位，严格上游直接 400 |
| **run 级状态** | `_event_seq` 自增、`ToolLedger.record`、`ProgressGuard.record`、usage 合并、`pending_calls` 都是单线程假设 |
| **工具副作用** | `Write`/`Edit`/`Bash` 共享 workspace，没有锁；并发写同一文件会互相覆盖 |
| **取消语义** | 已提交的任务无法中断，取消延迟从“当前工具剩余时间”变成“最慢工具剩余时间” |

### 5.3 可行的最小改造

1. 给 `Tool` 增加 `concurrency_safe: bool = False`，默认保守；
2. 增加 `AgentConfig.tool_concurrency = 1`，只在 >1 时启用；
3. 新增 `ToolRegistry.invoke_many(calls, max_workers, cancel_event)`：先发全部 `TOOL_CALL`，再提交线程池，用 `concurrent.futures.wait(timeout=0.2)` 轮询取消，结果写回 `results[index]`；
4. runtime 只把执行并行化，**状态回放仍按 index 顺序**：`tool_results.append`、`ledger.record`、`guard.record`、`_merge_usage`、`emit(TOOL_RESULT)`、`append(tool_message)` 全部在收敛后按原序执行；
5. `_emit` 加锁保证 `_event_seq` 单调。

与现有语义的兼容性：`guard_stop` 本来就等本轮所有调用执行完才生效（`runtime.py:514-519`），所以“全部跑完再按序 record”与原行为等价。

### 5.4 子 Agent 并发

子 Agent 是最适合并发的对象，但共享四处状态：`_DelegationState`（有锁）、`DelegationLedger`（无锁）、父 provider（client 线程安全，但受上游限流）、父取消 watcher（每个子 Agent 一个 50ms 轮询线程）。需要额外增加 `max_concurrent`、给 ledger 加锁、把 validator 移出 `state.lock`，否则并发下会出现台账竞争和预算误判。

### 5.5 不建议并发的工具

写文件、shell、`Monitor`、任何改共享状态的自定义工具。默认保持串行，用 `concurrency_safe` 白名单开启读写分离。

---

## 6. 结果截断与跨轮持久化

### 6.1 一次工具结果会经过五层裁剪

| 层 | 上限 | 位置 |
|---|---|---|
| 工具返回值 | 不截断，完整进 `RunResult.tool_results` | `Tool.invoke` |
| 模型视图 | `tool_result_max_chars`（server 默认 16,000），保留头尾各 50% + 截断标记 | `context.py:198-207` |
| 事件 / SSE | 同一上限的视图，带 `truncated` / `original_chars` | `runtime.py:486-505` |
| 持久化 `tool_trace` | 单条 16 KB、参数 4 KB、整份 64 KB | `server/app/agent/trace.py:16-19`、`199-218` |
| 下一轮历史 | 单条结果 2,000 字符、参数 1,000 字符、总 24,000 字符、最多 40 行 | `server/app/agent/store.py:22-33`、`287-380` |

完整原文只存在于本次 run 的内存里；server 不给 SDK 传 store，所以它不会落库。

### 6.2 下一轮历史的原料

不是 SDK 的 `RunResult.tool_results`，而是服务端在运行期间从事件流抄下来的 `ToolTrace`：

```text
tool.call   → ToolTrace.start(id, name, args)
tool.result → ToolTrace.finish(id, content, is_error)
run 结束     → attach_trace(final_metadata) → conversation_messages.metadata.tool_trace
下一轮       → build_history → _render_history_message → assistant(tool_calls) + tool 消息
```

`ToolTrace` 按 `tool_call_id` 收敛，一个调用和它的结果合并为一条；`status="running"` 的条目在合成历史时被跳过；失败/取消的 run 保留工具块，但不追加最终回答文本。

`agent_run_events` 里的 `tool.call` / `tool.result` 是审计与 SSE 用的，**不是历史合成的原料**。

### 6.3 一个需要注意的预算方向

`ToolTrace.snapshot()` 按时间正序消耗 64 KB 总预算：一旦某条超出剩余预算，就把该条内容清空并把 remaining 置零，于是**更新的条目反而全部只剩骨架**。单 run 工具输出超过 64 KB 时，下一轮看到的是“早期完整、最近为空”，与通常期望的“保新弃旧”相反。详见问题记录 P-24。

---

## 7. 与上下文压缩的关系

压缩是**视图级**的，不改变工具历史：

- `ContextManager._current` 始终保留完整消息，`compress()` 返回新列表，不写回；
- `tool_results` 完整保留；
- 事件在工具执行时已经发出，压缩不会撤回；
- 摘要状态（`_summary` / `_folded_until`）是 run 作用域，run 结束即消失。

因此：

| 问题 | 答案 |
|---|---|
| 压缩后本 run 的内存工具历史会丢吗？ | 不会，丢的是模型视图里的可见性 |
| 下一轮会少看到工具记录吗？ | 不会，下一轮从 `tool_trace` 重建，与压缩无关 |
| 压缩能减少下一轮的 token 吗？ | 不能，下一轮重新装入原始记录并再压一次 |
| 压缩失败会怎样？ | 退化为纯截断，`_folded_until` 仍然推进，那段内容只剩省略提示 |

工具 schema 的 token 在压缩前就从预算里扣除（`runtime.py:394-399`），所以“工具多”和“历史长”会争抢同一个 `context_max_tokens`。

---

## 8. 失败处理：重试 / 熔断 / 幂等

### 8.1 重试

| 调用 | 现状 |
|---|---|
| 工具调用 | **无框架级重试**。失败变成 `is_error` 结果，由模型自行决定是否再调 |
| 模型调用 | OpenAI 客户端 `max_retries=2`（`ModelConfig` 默认），只覆盖请求创建；流式中途失败不续传 |
| `stream_options` 不兼容 | 去掉参数后重试一次（特例） |
| 摘要调用 | 不重试，失败直接 fallback 截断 |
| 子 Agent | 不重试，返回失败信封 |
| executor `/exec` | 不重试 |

没有错误分类、没有退避、没有重试预算。模型的重试节奏完全不可控。

### 8.2 熔断

没有 per-tool 熔断器（无状态机、无失败计数、无冷却窗口）。现有的是 run 级刹车：

| 机制 | 默认 | 效果 |
|---|---|---|
| `ProgressGuard` | nudge 3 / stop 5 | 连续无新证据（错误结果不算证据）→ `stop_reason=no_progress` |
| `max_iterations` | 10 | 硬上限 |
| `max_seconds` | 300 | 整轮超时 |
| 子 Agent 预算 | 4 次调用 / 120 秒 | 防委派失控 |
| `tool_error_mode="raise"` | server 未用 | 第一次工具失败即终止 |

问题在于：坏工具和好工具共享同一预算，错误类型不区分，重试间隔由模型决定。

### 8.3 幂等

工具层没有幂等保证：

- `tool_call_id` 只用于消息配对和 trace 归并，不是幂等键；
- 没有参数哈希去重、没有结果缓存；
- 文件写入是直接 `write_text`，非临时文件 + 原子替换；
- shell 工具任意副作用，`/exec` 没有 `Idempotency-Key`；
- 子 Agent 的 `resume` 是“续接”，不是“重放去重”。

服务端 run 层有部分互斥与幂等：

| 机制 | 作用 |
|---|---|
| `uq_agent_runs_active_conversation` | 同会话只允许一个活跃 run |
| `repo.claim` 的 `UPDATE ... WHERE status='queued'` | 多 worker 只有一个能认领 |
| assistant 消息预生成 id | 重复插入不产生第二行 |
| `repo.finish` 条件更新 | 重复 finalize 无副作用 |

但 `client_run_id` 没有唯一约束，也没有“按它复用已有 run”的查询：客户端超时重发在 run 结束后会**真的再跑一遍**。

### 8.4 加固顺序建议

1. 在 registry 层做错误分类（可重试：超时/连接/5xx/429；不可重试：参数/权限/4xx）；
2. 有界重试 + 指数退避 + 抖动，只对声明为幂等的工具默认开启；
3. `/exec` 与写类工具支持 `Idempotency-Key`（可用 `run_id + tool_call_id`），文件写入改为临时文件 + 原子替换；
4. per-tool 失败计数与临时移除（半开试探），把失败率写进 span；
5. `client_run_id` 加唯一索引，实现请求级幂等。

注意：自动重试会产生新的 `tool_call_id`，失败记录仍进历史；`ProgressGuard` 把错误当“无进展”，重试成功也可能已经消耗了 no-progress 计数，需要同步调整口径。

---

## 9. 工具数量增长：检索与按需加载

### 9.1 现状

**没有检索，也没有按需加载。** 所有注册工具每轮全量下发。当前默认规模约 13–15 个（基础 8 + 可选 shell 3 + office + 记忆 2 + 子 Agent 2），尚未到需要检索的程度；但接入业务工具或 MCP 后会立刻遇到两个问题：

1. schema 占上下文：工具定义参与 token 预算；
2. 路由准确率：描述重叠时模型更容易选错。

### 9.2 可选方案

| 方案 | 做法 | 改动量 | 适用 |
|---|---|---|---|
| 配置分组 | 按 agent profile 选一套工具集 | 最小 | 几十个工具、场景边界清晰 |
| 意图预路由 | 先分类，再构造对应工具集 | 小 | 场景少 |
| `search_tools` 元工具 | 检索返回 schema，加入本 run 的激活集合；`specs()` 只发激活集 | 中 | 上百个工具、长尾明显 |
| 命名空间/分组 | 先选组再展开 | 中 | 工具有天然领域 |
| MCP / 远程工具 | 接外部工具服务 | 大 | 第三方维护 |

### 9.3 这个代码库实现动态加载的有利条件

- `tools.specs()` 每轮调用（`runtime.py:393`），激活集合可以做成 run 状态，每轮过滤；
- 压缩预算每轮按当时 `tools_tokens` 重算（`runtime.py:394-399`），中途加载工具会自动反映到预算；
- `ToolRegistry.register` 运行中可调用，重名报错，天然防重复加载；
- `MODEL_REQUEST` 事件带 `tool_names`，可观测每轮实际暴露了哪些工具。

### 9.4 坑

1. 子 Agent 读的是全量 `parent_tools.tools`，不是父级激活集，需要同步传入；
2. `READ_ONLY_TOOL_NAMES` 等策略依赖硬编码名字，动态命名/命名空间会让策略失真；
3. 动态加载必须复用 `allow_shell`、workspace 限制、office 开关等安全门禁，不能绕过；
4. 检索本身要成本，失败要有回退到默认组的路径。

---

## 10. 风险清单

| 风险 | 影响 | 现状 |
|---|---|---|
| 写类工具并发 | 文件互相覆盖 | 默认串行，尚不存在 |
| 工具结果过大 | 模型只看到头尾，`_fit_budget` 可能压到几十字符 | 五层截断，无分页协议 |
| `tool_trace` 64 KB 预算方向 | 最新结果先被清空 | 见 P-24 |
| 压缩摘要失败 | 被折叠段落只剩省略提示 | 水位仍推进 |
| 无自动重试 | 瞬时故障直接失败 | 模型自愈 |
| 无幂等键 | 重试/重复调用产生重复副作用 | 见 P-23 |
| 无 per-tool 熔断 | 坏工具拖垮整轮预算 | run 级刹车 |
| 工具全量下发 | schema 挤压上下文、路由变差 | 静态裁剪 |
| 多副本部署 | 取消信号只在本进程生效 | 见 `runtime-behavior.md` |

---

## 11. 文件索引

| 主题 | 文件 |
|---|---|
| 工具注册、schema、dispatch | `agent-loop/src/bokaclaw_agent/tools/registry.py` |
| 循环内的工具执行 | `agent-loop/src/bokaclaw_agent/agent/runtime.py:449-512` |
| 取消时的协议补齐 | `agent-loop/src/bokaclaw_agent/agent/runtime.py:1080-1162` |
| 消息构造与截断 | `agent-loop/src/bokaclaw_agent/agent/context.py` |
| 压缩 | `agent-loop/src/bokaclaw_agent/agent/compression.py` |
| 事实台账 | `agent-loop/src/bokaclaw_agent/agent/ledger.py` |
| 无进展判定 | `agent-loop/src/bokaclaw_agent/agent/progress.py` |
| 子 Agent | `agent-loop/src/bokaclaw_agent/agent/subagent.py` |
| server 工具装配 | `server/app/agent/runtime.py:904-912`、`server/app/agent/dev_tools.py` |
| 工具 trace 与持久化 | `server/app/agent/trace.py`、`server/app/agent/payloads.py` |
| 历史重建 | `server/app/agent/store.py:207-380` |
| 执行器与沙箱 | `server/app/agent/executor.py`、`server/app/agent/executor_app.py` |
| run 生命周期与取消 | `server/app/agent/runner.py`、`server/app/routers/agent.py` |
