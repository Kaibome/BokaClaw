# BokaClaw Agent 异常处理机制

> 状态：现状梳理（对照当前 `main`）
>
> 适用范围：`agent-loop/`（`bokaclaw_agent`）与 `server/app/agent/`（BokaClaw 集成层）
>
> 目标读者：SDK 维护者、server 集成开发者、需要排查 run 失败或超时的一线同学
>
> 相关：[`runtime-behavior.md`](runtime-behavior.md)（循环终止、取消、压缩、历史重放）、[`tool-execution.md`](tool-execution.md)（工具注册、执行、权限）、[`subagents-reference.md`](subagents-reference.md)、[`exception-handling-comparison.md`](exception-handling-comparison.md)（与 claw-code、Hermes 的对比）

本文按五类异常整理当前实现：上下文压缩失败、模型调用失败、工具调用失败、子 Agent 调用失败、ReAct 循环超时。重点说明每类异常在哪里被捕获、是否中断 run、用户最终看到什么，以及当前实现还有哪些边界。

除特别说明外，默认值和行号对应当前仓库版本。

---

## 0. 一图看懂

```text
ReAct run
  │
  ├─ 每轮构建模型视图
  │    ├─ 预算压缩
  │    │    └─ 摘要模型失败 → 降级截断，run 继续
  │    └─ 调主模型
  │         ├─ SDK 重试 → 仍失败 → ModelError → run failed
  │         └─ 正常返回 tool_calls
  │
  ├─ 执行工具
  │    ├─ 未知工具 / 参数错误 / 执行异常 → tool result error，run 继续
  │    ├─ 工具自身超时 → 返回 timed_out / tool error，run 继续
  │    └─ 子 Agent 失败 → 返回 SubagentResult，父 Agent 继续
  │
  └─ 循环终止
       ├─ 正常回答 → completed
       ├─ max_iterations → 尝试最后回答，或 failed
       ├─ no_progress → 尝试最后回答，或 failed
       ├─ agent_timeout → failed
       └─ 用户取消 / 服务关闭 → cancelled / interrupted
```

总体原则：

- **压缩失败 fail-open**：摘要失败就退化为截断，不阻塞主流程。
- **工具失败 fail-soft**：错误作为 tool result 回给模型，由模型自行纠正。
- **子 Agent 失败故障隔离**：子 run 失败不把父 run 一起打崩。
- **主模型失败 / ReAct 超时 fail-run**：结束当前 run，保留已生成的部分内容。
- 所有失败都会进入 run 事件、trace/span 和 run 状态，便于前端展示和事后排查。

## 1. 总览

| 异常 | 是否中断 run | run 结果 | 典型错误码 / 标记 |
|---|---|---|---|
| 上下文压缩摘要失败 | 否 | 继续，降级截断 | `context.compressed.fallback=true` |
| 主模型调用失败 | 是 | `failed` | `agent_upstream_error` |
| 工具执行失败 | 否 | 继续 | tool result `is_error=true` |
| 子 Agent 失败 | 否 | 父 Agent 继续 | 子结果 `status=failed/timeout` |
| ReAct 整体超时 | 是 | `failed` | `agent_timeout` |
| ReAct 步数耗尽 | 视情况 | 可能 `completed`，也可能 `failed` | `agent_upstream_error` |
| 无进展保护触发 | 视情况 | 可能 `completed`，也可能 `failed` | `agent_no_progress` |

---

## 2. 上下文压缩失败

### 2.1 触发与降级

压缩由 `agent-loop/src/bokaclaw_agent/agent/compression.py` 实现。当模型视图的估算 token 超过 `context_max_tokens` 时：

1. 保留最近 `context_keep_recent_tokens`。
2. 对更早的消息调用压缩模型生成滚动摘要。
3. 用摘要替换被折叠的消息。

压缩模型调用异常、返回格式非法、返回空摘要时：

- `_summarize()` 捕获异常；
- 记录 `_summary_error` 和 warning 日志；
- 返回 `None`，`compress()` 设置 `fallback=True`；
- 不生成摘要，直接丢弃旧消息，并插入：
  `[Note: N earlier messages were omitted to fit the context window.]`；
- 主 run 继续，不影响当前模型调用。

如果 `context_compression="truncate"`，则根本不调用摘要模型，直接走截断。

### 2.2 可观测性

压缩失败会发出 `context.compressed` 事件，stats 包含：

- `before_tokens` / `after_tokens`
- `dropped_messages`
- `summarized`
- `fallback`
- `model_calls`
- `usage_unknown_calls`

压缩还会生成独立的 `compression` span，记录 input/output 描述。

当前边界：

- `_summary_error` 只写日志，**没有进入 stats 或 span**；前端只能看到 `fallback=true`，看不到具体失败原因。
- 摘要失败后本轮仍会推进 `_folded_until`，同一批消息不会自动重试；下一轮压缩只处理新消息。
- 只有摘要调用被捕获；`compress()` 内部其他未预期异常没有额外兜底，会走 run 级失败流程。

---

## 3. 模型调用失败

### 3.1 Provider 重试

模型调用由 `OpenAICompatibleProvider` 负责：

- 单次请求超时：`AGENT_REQUEST_TIMEOUT_SECONDS`，默认 **60s**。
- 重试次数：`AGENT_MAX_RETRIES`，默认 **2 次**，由 OpenAI SDK 对网络错误、429、5xx 等临时错误重试。
- 401/400 等不可重试错误通常直接失败。
- 重试耗尽后统一包装为 `ModelError`。

流式调用额外有一个特殊重试：如果服务端不支持 `stream_options`，会去掉该参数重试一次；其他流式错误不会在 runtime 层再次重试。

### 3.2 Runtime 处理

`AgentRuntime._handle_failure()` 会：

1. 中止尚未执行的 pending tool calls，为每个调用补一条错误 tool result，保证 assistant tool call 与 tool message 成对。
2. 以 `failed` 状态持久化当前 session。
3. 发出 `ERROR` 事件并抛出异常。
4. 已流式输出的文本不会丢。

模型返回“空内容且无工具调用”不是异常，但 server 会判为：

```text
agent_protocol_error: Agent produced no content.
```

### 3.3 Server 处理

后台 run 捕获 `AgentLoopError` 后：

- run 状态置为 `failed`；
- 写入 `error_code` / `error_detail`；
- 有部分文本或工具轨迹时，写一条 `status=failed` 的 assistant 消息；
- 没有任何内容且没有工具轨迹时，不写 assistant 消息，只保留失败的 run；
- SSE 推送 `error`，随后 `done(status=failed)`；
- 非流式请求返回 HTTP **502**，错误码 `agent_upstream_error`。

远程 executor 路径会把上游错误映射为 `agent_upstream_error`、`agent_unreachable` 或 `agent_timeout`。

### 3.4 部分输出

如果失败发生在流式输出之后：

- `RunEventWriter` 已经累计了部分文本；
- `_finalize` 在 `should_persist_reply()` 为真时写 assistant 消息；
- 消息 `status=failed`，`metadata` 中保留错误码和工具轨迹；
- 前端可以同时看到部分回答和错误提示。

---

## 4. 工具调用失败

### 4.1 工具选择与参数错误

`ToolRegistry.invoke()` 在真正执行前做检查：

| 情况 | 返回 |
|---|---|
| 工具名不存在 | `{"error": "Unknown tool 'foo'. Available tools: [...]"}` |
| 工具存在但 deferred | `{"error": "Tool 'foo' is deferred; load it with ToolSearch before calling it."}` |
| 参数 JSON 非法 | `{"error": "Invalid JSON arguments: ..."}` |
| 参数不符合 schema | `{"error": "Invalid arguments for tool 'foo': ..."}` |

这些都变成 `is_error=true` 的 tool result，**run 继续**，模型下一轮可以纠正。

### 4.2 工具执行异常

工具内部抛异常时：

1. `Tool.invoke()` 统一转成 `ToolExecutionError`；
2. registry 再转成：
   ```json
   {"error": "Tool 'X' failed: ..."}
   ```
3. `is_error=true`，run 继续。

默认 `tool_error_mode="return"`；如果显式配置为 `"raise"`，异常会向上抛，run 失败。

### 4.3 工具超时

没有统一的工具超时，由各工具自己控制：

| 工具 | 超时行为 |
|---|---|
| `Bash` / `PowerShell` | 返回 `timed_out=true`、`exit_code=null` 和部分输出；不是 tool error |
| `SkillRun` | executor 杀掉进程组，返回 `status=timed_out`；不是 tool error |
| MCP 工具 | 抛 `MCPError`，转成 `is_error=true` 的 tool result |
| `WebFetch` / `WebSearch` | httpx 超时，转成 tool error |
| Subagent | 返回 `status=timeout`，父 Agent 继续 |
| 自定义工具 | 没有通用超时，可能阻塞 worker 线程 |

`Monitor` 是长驻进程：`start` / `poll` 本身很快返回，`wait_seconds` 最大 30s，命令不会因为工具调用结束而自动终止，需要显式 `stop`。

### 4.4 审批被拒绝

- MCP 审批拒绝：返回 `is_error=true` 的 tool result，内容为拒绝原因。
- `SkillRun` 审批拒绝：返回 `{"status": "denied", "reason": ...}`，不是 error，但脚本不会执行。

### 4.5 run 失败时的 pending 工具

run 因模型失败、超时或取消而终止时：

- `_abort_pending_tool_calls()` 为每个未执行的工具补一条 aborted tool result；
- `trace.abort_open()` 把未闭合的 trace 条目标记为 aborted；
- 保证历史里每个 assistant tool call 都有对应的 tool message。

---

## 5. 子 Agent 调用失败

子 Agent 是父 Agent 的一个普通工具调用，故障被严格隔离：

- 子 runtime 的模型失败、工具失败、异常，都会被 `SubagentTool._delegate()` 捕获；
- 返回 `SubagentResult`，不会把异常抛给父 Agent；
- 父 Agent 收到一条普通 tool result。

状态映射：

| 子 Agent 结果 | 父 Agent 看到 |
|---|---|
| `completed` | 正常结果 |
| `partial` | 部分结果，非 error |
| `failed` | `is_error=true` |
| `timeout` | `is_error=true` |
| `cancelled` | `is_error=true` |

### 5.1 超时

- 默认 `AGENT_SUBAGENT_TIMEOUT_SECONDS`，当前默认 **120s**；
- 定时器设置子 Agent 的 `cancel_event`，子 run 在安全边界退出；
- 返回 `status=timeout`，可能带 partial 内容。

### 5.2 步数与预算

- `MaxIterationsExceeded` / `NoProgressError` 会转为 `partial` 或 `failed`；
- 父 Agent 可以决定重试、换子 Agent、或直接回答；
- 超过最大嵌套深度、每次 run 的最大调用次数、token 预算时，返回 failed tool result；
- 只有 `completed` / `partial` 且满足条件时才可 resume。

子 Agent 的 token 使用会合并到父 run 的总 usage 中；子 Agent 的中间消息不会进入父上下文。

---

## 6. ReAct 循环超时与终止

### 6.1 四层预算

容易混淆的四类限制：

| 限制 | 默认值 | 结果 |
|---|---|---|
| 单次模型请求 | `AGENT_REQUEST_TIMEOUT_SECONDS=60s` | `agent_upstream_error` |
| 单次工具 | 各工具自己控制，通常 30–120s | tool result，run 继续 |
| 整个 run 墙钟时间 | 后台 `AGENT_RUN_MAX_SECONDS=1800s`；legacy `AGENT_LOOP_MAX_SECONDS=300s` | `agent_timeout`，run `failed` |
| ReAct 最大迭代 | `AGENT_MAX_ITERATIONS`，SDK 默认 10 | 尝试最后回答；否则 `agent_upstream_error` |
| 无进展保护 | 连续多次工具调用没有新证据 | `agent_no_progress` |

### 6.2 整体超时：`agent_timeout`

- `stream_local_agent` 用 deadline 检查队列，超过后抛 `AgentLoopError("agent_timeout", ...)`；
- server 将 run 标记为 `failed`，`error_code=agent_timeout`；
- 非流式请求返回 **504**；
- `_finalize` 会把未闭合的 tool trace 标记为 aborted，并补写 tool result。

注意：**正在执行的工具线程不保证被强制杀死**。run 结束后，工具可能仍继续产生副作用，只是后续事件被丢弃。

### 6.3 最大迭代：`max_iterations`

到达上限且模型仍在调用工具时：

1. 如果 `final_answer_on_max_iterations=true`，再发一次**不带工具**的请求；
2. 如果模型给出答案：run 正常完成，`stop_reason=max_iterations`；
3. 如果仍没有答案：run `failed`，server 报：
   `agent_upstream_error: Agent exceeded the maximum number of steps.`

这是“步数耗尽”，不是 `agent_timeout`，HTTP 是 502。

### 6.4 无进展保护

- 连续工具调用没有带来新证据时，先插入 nudge 提示；
- 超过阈值后停止工具循环，要求模型直接回答；
- 有答案则完成；无答案则 `agent_no_progress`，run `failed`。

### 6.5 取消与服务关闭

- 用户主动取消：`agent_cancelled`，run `cancelled`，HTTP 409。
- 服务关闭：`agent_interrupted`，run `interrupted`，HTTP 503。
- 启动恢复时，未完成的 run 也会走 `interrupted` 兜底，并补写 partial assistant 消息（如果有内容或工具轨迹）。

### 6.6 HTTP 状态映射

非流式 `POST /agent`：

| error_code | HTTP |
|---|---|
| `agent_not_configured` | 503 |
| `agent_timeout` | 504 |
| `agent_cancelled` | 409 |
| `agent_interrupted` | 503 |
| `agent_upstream_error` | 502 |
| `agent_protocol_error` | 502 |
| `agent_no_progress` | 502 |

后台 run 仍在 `queued` / `running` 时，非流式请求会返回 504，提示改为订阅 run 事件。

---

## 7. 关键设计特点与缺口

### 7.1 已经具备的保障

- 部分输出不会因为异常丢失；
- tool call / tool result 始终配对，历史不会被破坏；
- 子 Agent 故障不会污染父 run；
- 压缩失败不会阻塞主流程；
- 失败状态、错误码、事件、span 基本可追踪；
- run 终止时会把未闭合的 trace 标记为 aborted，并补写 tool result。

### 7.2 当前缺口

1. **没有通用工具超时**
   - 新增工具如果不自己实现超时，可能永久阻塞 worker 线程。

2. **run 超时不强杀工具线程**
   - `agent_timeout` 只结束 run 状态，不保证停止正在执行的工具。

3. **Shell 超时只杀直接子进程**
   - `subprocess.run(timeout=...)` 只终止直接子进程，孙进程可能残留；
   - `SkillRun` 使用 `start_new_session=True` 并在超时时杀整个进程组，行为更严格。

4. **压缩错误原因不可观测**
   - `_summary_error` 只写日志，没有进入 `context.compressed` stats 或 span。

5. **最大迭代被归为 `agent_upstream_error`**
   - 语义上更像 `agent_max_iterations`，当前无法从错误码直接区分“模型失败”和“步数耗尽”。

6. **工具超时语义不一致**
   - Shell / Skill 返回正常结果，MCP / Web 返回 error；
   - 模型需要自己判断 `timed_out`、`status` 和 `is_error` 的不同组合。

7. **压缩失败后不重试同一批消息**
   - `_folded_until` 仍然推进，失败摘要覆盖的消息不会在下一轮重新尝试。

---

## 8. 关键文件索引

| 主题 | 文件 |
|---|---|
| ReAct 循环、失败处理、pending tool 中止 | `agent-loop/src/bokaclaw_agent/agent/runtime.py` |
| 上下文压缩与摘要降级 | `agent-loop/src/bokaclaw_agent/agent/compression.py` |
| 工具注册、参数校验、错误模式 | `agent-loop/src/bokaclaw_agent/tools/registry.py` |
| 子 Agent 委派、超时、结果封装 | `agent-loop/src/bokaclaw_agent/agent/subagent.py` |
| 模型 provider 与重试 | `agent-loop/src/bokaclaw_agent/providers/openai_compatible.py` |
| server 侧模型错误映射 | `server/app/agent/runtime.py` |
| run worker、finalize、恢复 | `server/app/agent/runner.py` |
| HTTP 错误码与状态码 | `server/app/routers/agent.py` |
| 工具实现与超时 | `server/app/agent/dev_tools.py` |
| shell executor 与 SkillRun 超时 | `server/app/agent/executor_app.py` |
| MCP 超时与调用取消 | `server/app/agent/mcp/manager.py` |
| 工具 trace | `server/app/agent/trace.py`、`server/app/agent/payloads.py` |
