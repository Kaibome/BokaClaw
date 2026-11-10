# BokaClaw AgentRuntime 运行机制与边界

> 状态：现状梳理（对照当前 `main`）
>
> 适用范围：`agent-loop/`（`bokaclaw_agent`）与 `server/app/agent/`（BokaClaw 集成层）
>
> 目标读者：SDK 维护者、server 集成开发者、需要排查 run 行为的一线同学

本文回答一组高频问题：ReAct 循环靠什么停下来、`cancel_event` 是什么、工具调用有没有参数/权限校验、shell 怎么进沙箱、无进展怎么判定、上下文压缩会不会丢信息、`session_id` 和 `run_id` 什么关系、历史到底怎么重放。

除特别说明外，行号对应当前仓库版本。

---

## 0. 一图看懂

```
POST /agent（带或不带 conversation_id）
   │
   ▼
AgentRun(status="queued") + Conversation
   │  run_id = uuid4()
   ▼
AgentRunManager worker
   │  build_history(conversation_id) → payload["history"]
   ▼
build_messages(payload) → OpenAI wire format
   │
   ▼
AgentRuntime.stream(messages, session_id=str(conversation_id), cancel_event=...)
   │
   ├─ for iteration in 1..max_iterations
   │    ├─ 构造视图：build() → 截断 → 压缩 → 追加 runtime context
   │    ├─ 调模型
   │    ├─ 无 tool_calls → 结束
   │    └─ 有 tool_calls → 参数校验 → 执行 → 记录 → 无进展检查
   │
   └─ 结束：final / length / content_filter / max_iterations / no_progress / cancelled
   │
   ▼
runner 从事件流重建 tool_trace → conversation_messages.meta["tool_trace"]
```

---

## 1. ReAct 主循环

入口 `run()` / `stream()` / `arun()` 共用同一个生成器 `_execute`（`agent-loop/src/bokaclaw_agent/agent/runtime.py:312`）。

循环本体：

```python
for iteration in range(1, self.config.max_iterations + 1):
    ...
```

循环体内只有 3 处 `break`，其余靠异常退出。

---

## 2. 循环的终止条件

### 2.1 模型不再调用工具（正常结束）

`runtime.py:447-449`

```python
if not response.tool_calls:
    stop_reason = _map_stop_reason(response.finish_reason)
    break
```

`_map_stop_reason`（`runtime.py:87-92`）：

| `finish_reason` | `stop_reason` |
|---|---|
| `length` | `length` |
| `content_filter` | `content_filter` |
| 其他 / `None` | `final` |

注意：只有"没有 tool_calls"才算结束。若 `finish_reason="length"` 但仍返回 tool_calls，循环继续，最终由 `max_iterations` 兜底。

### 2.2 迭代预算耗尽（for-else）

`runtime.py:520-589`

- `max_iterations` 默认 **10**（`core/config.py:33`）。
- `final_answer_on_max_iterations=True`（默认）时，丢弃工具再发一次无工具请求（附 `_STEP_LIMIT_NOTICE`）：
  - 有非空回答 → 运行成功，`stop_reason="max_iterations"`，status `completed`
  - 回答为空或请求抛异常 → 持久化 `failed`，抛 `MaxIterationsExceeded`
- 设为 `False` 时直接走失败路径。
- provider 即使仍返回 tool_calls，也会被丢弃，保证落库消息合法（`runtime.py:568-573`）。

### 2.3 无进展守卫强制停止

仅在 `progress_guard=True` 时启用（**SDK 默认 `False`，BokaClaw server 默认 `True`**）。详见第 6 节。

`runtime.py:514-519`：

```python
if guard_stop:
    # 本轮 tool_calls 已全部执行完，assistant/tool 消息成对，此时才停
    stop_reason = "no_progress"
    break
```

随后再发一次无工具请求（`_NO_PROGRESS_NOTICE`）：

- 有回答 → 成功返回，`stop_reason="no_progress"`，status `completed`
- 空/异常 → 持久化 `failed`，抛 `NoProgressError`

### 2.4 协作式取消

`cancel_event` 在 6 个安全点被轮询（见第 3 节）。命中后抛 `RunCancelled`，持久化 status `cancelled`。

### 2.5 异常终止

provider / 工具（`error_mode="raise"` 时）/ 存储抛异常 → `_handle_failure`：

1. 为未执行的 tool_call 补 `is_error=True` 的 tool 消息
2. 持久化 `failed`
3. emit `ERROR`
4. 原样抛出

`MaxIterationsExceeded` / `NoProgressError` / `RunCancelled` 直接 re-raise，不做二次处理（`runtime.py:671-676`）。

### 2.6 调用方提前关闭 stream 生成器

`runtime.py:271-278` 文档明确：提前关闭 generator 是"放弃本轮"，**不是取消，也不会持久化**（`GeneratorExit` 不进入 `_handle_failure`）。

### 2.7 stop_reason 汇总

`RunResult.stop_reason` 的取值（`core/types.py:87`）：

```
final | length | content_filter | max_iterations | no_progress | cancelled
```

| stop_reason | 结果 | 持久化 status |
|---|---|---|
| `final` / `length` / `content_filter` | 正常返回 | completed |
| `max_iterations`（最终回答成功） | 正常返回 | completed |
| `max_iterations`（无回答） | `MaxIterationsExceeded` | failed |
| `no_progress`（最终回答成功） | 正常返回 | completed |
| `no_progress`（无回答） | `NoProgressError` | failed |
| `cancelled` | `RunCancelled` | cancelled |
| 其他异常 | 原异常 | failed |

### 2.8 主 runtime 没有的东西

- 没有整体 wall-clock 超时
- 没有全局 token / 费用预算（只有子代理有 `token_budget`）
- 没有"重复调用完全相同工具"的硬限制（只有内容级无进展检测）

实际兜底只有三条硬线：`max_iterations`、`progress_stop_after`、`cancel_event`。

---

## 3. `cancel_event` 是什么

### 3.1 定义

`run()` / `stream()` / `arun()` 的可选参数：

```python
cancel_event: threading.Event | None = None
```

它是 runtime 唯一的外部刹车。语义是**协作式**：runtime 在安全点检查 `is_set()`，不会强杀正在执行的模型请求或工具。

### 3.2 为什么是 `threading.Event`

- 循环是同步阻塞的
- `arun()` 通过 `asyncio.to_thread` 在工作线程执行
- server 的 `RunManager` 在事件循环里调用 `event.set()`，跨线程安全

### 3.3 检查点

| 时机 | 行号 |
|---|---|
| 每轮 ReAct 开始前 | `runtime.py:391` |
| 模型响应后、工具执行前 | `runtime.py:445` |
| 每个 tool call 前 | `runtime.py:454` |
| 每个 tool call 后 | `runtime.py:512` |
| max_iterations 最终回答前 | `runtime.py:526` |
| no_progress 最终回答前 | `runtime.py:594` |

### 3.4 命中后做什么

`_cancel_run`（`runtime.py:1080`）：

1. 为 `pending_calls` 补 `{"error": "run aborted: ..."}` 的 tool 消息，保证会话合法
2. `_persist_best_effort(..., status="cancelled")`
3. emit `RUN_END({"stop_reason": "cancelled"})`
4. `raise RunCancelled(session_id, result, iteration)`，异常上挂 `result`

### 3.5 语义边界

- **不抢占**：正在执行的 HTTP 请求/工具会跑完，最长受 `ModelConfig.timeout`（默认 60s）或工具自身超时约束
- **流式延迟**：chunk 循环内部不检查，`set()` 后仍会消费完剩余 chunk，等 assistant 消息拼装完整才抛
- **单向**：runtime 不清除 flag；同一个 `Event` 复用给第二次 run 会立刻取消
- **向子代理传播**：`SubagentTool` 通过看门狗线程把父 cancel 镜像到子运行（`agent/subagent.py:472-481`）

---

## 4. 工具调用的参数校验与权限

### 4.1 完整链路

```
模型返回 tool_calls
  ↓
registry.invoke(call, error_mode)
  ├─ call.arguments_error? → ToolArgumentError → error ToolResult
  ├─ tool = _tools.get(name) → None → ToolNotFoundError → error ToolResult
  └─ Tool.invoke
       ├─ args_model(**arguments)   # Pydantic
       ├─ func(**kwargs)            # 业务校验在函数体内
       └─ 异常 → ToolExecutionError → error ToolResult（return 模式）
```

默认 `tool_error_mode="return"`（`core/config.py:43`），参数错误不会终止 run，只把 `{"error": "..."}` 喂回模型。

### 4.2 三层参数校验

| 层 | 位置 | 内容 |
|---|---|---|
| JSON 解析 | `core/utils.py:11` `parse_tool_arguments` | 非 JSON / 顶层非 object → `arguments_error` |
| Pydantic 模型 | `tools/registry.py:97-133, 230-238` | 由签名 + 类型注解动态 `create_model`，同时生成 JSON Schema |
| 工具内部 | 各工具函数 | 路径、URL、大小、范围等业务规则 |

Pydantic 层的三个局限：

1. **多余字段静默丢弃**：`create_model` 未设 `extra="forbid"`，Pydantic v2 默认 `ignore`
2. **类型宽松**：默认 lax 模式，`"5"` → `5`
3. **取值范围取决于注解**：普通 `int` 不限制范围，只有 `Annotated[..., Field(ge=...)]` 才生效

### 4.3 权限：只有围栏，没有权限层

明确没有：人工审批（HITL）、RBAC、命令白名单、per-run 工具子集、统一策略钩子、主 run 的工具配额。

现有的是：

| 机制 | 位置 | 说明 |
|---|---|---|
| 构建期工具裁剪 | `server/app/agent/dev_tools.py:1189` | `AGENT_TOOLS_ALLOW_SHELL`（默认 false）决定是否注册 Bash/PowerShell/Monitor |
| 文件路径围栏 | `dev_tools.py:146` | `resolve()` 解开 `..`/symlink，必须落在 workspace 内 |
| SSRF 防护 | `dev_tools.py:330, 427-428` | 只允许 http/https；拒绝凭据、私有/回环/保留地址；**每次重定向重新校验** |
| 子进程环境 | `dev_tools.py:1200` | 白名单 env，不传 API key / DB 凭据 / executor token |
| 子代理 scope 校验 | `server/app/agent/subagents.py:144` | 绝对路径必须在 workspace 内，URL scheme 只允许 http/https |

---

## 5. shell 如何进沙箱

只有 `Bash` / `PowerShell` / `Monitor` 走沙箱；`Read/Write/Edit/Glob/Grep/WebFetch/WebSearch` 都在 **server 进程内**执行。

### 5.1 调用链

```
Bash(command, description, timeout_seconds)          dev_tools.py:1093
  → execute_command(cfg, ...)                        dev_tools.py:760
      ├─ cfg.executor 存在 → agent_executor.run_command()
      │      payload = {command, description, timeout_seconds, shell}
      │      POST {AGENT_EXECUTOR_URL}/exec
      │      Authorization: Bearer <AGENT_EXECUTOR_TOKEN>
      │        → executor_app.py:66  POST /exec
      │             ├─ authorize(): secrets.compare_digest
      │             ├─ ExecRequest: timeout 1..3600, shell ∈ {bash, powershell}
      │             └─ run_command() → subprocess.run([bash, "-lc", command], cwd=workspace, env=command_env)
      │
      └─ 未配置 → 直接在 server 进程内 subprocess.run（无沙箱）
```

关键点：

- 命令内容**不过滤**，安全性完全依赖容器隔离
- `Monitor` 用 `Popen(start_new_session=True)`，monitor 表在 executor 进程内存中，容器重启即丢失
- 调用是同步阻塞的，`cancel_event` 无法中断正在执行的沙箱命令

### 5.2 executor 容器隔离配置

`start-stack.sh:196-208`：

```bash
docker run --name bokaclaw-executor --network bokaclaw-exec \
  --read-only --tmpfs /tmp:rw,size=256m \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 256 --memory 1g --cpus 2 \
  --user 1000:1000 \
  -v "$WORKSPACE_HOST:$WORKSPACE_CONTAINER" \
  ... uvicorn app.agent.executor_app:app --port 8099
```

| 保护 | 效果 |
|---|---|
| `--network`（`--internal`） | 无外网出口 |
| `--read-only` | 根文件系统不可写 |
| `--cap-drop ALL` | 无特权能力 |
| 非 root + `no-new-privileges` | 不能提权 |
| `--pids-limit` / `--memory` / `--cpus` | 防 fork 炸弹与资源耗尽 |
| 只挂 workspace | `/app` 只读 |

---

## 6. 无进展（no-progress）如何判定

实现：`agent/progress.py` 的 `ProgressGuard`。

核心思路不是"调用是否重复"，而是**对每次工具返回的内容做指纹**，统计"连续多少次没有带来本 run 从未见过的新证据"。

### 6.1 证据指纹

`_evidence_signature`（`progress.py:74-109`），按优先级：

| 条件 | 指纹 | 含义 |
|---|---|---|
| `is_error=True` | `None` | 错误永不算进展 |
| 内容去空白为空 | `None` | 空结果不算 |
| JSON object 且有 `error` | `None` | JSON 内错误不算 |
| JSON object 且 `cached` 为真 | `None` | 缓存命中不算 |
| 有 `results` 列表 | `search:<sha256(排序去重后的 URL 集合)>` | 搜索只看 URL 集合 |
| 有 `content` 字符串 | `content:<sha256(content)>` | 抓取只看正文，与 URL 无关 |
| 有 `stdout` | `stdout:<sha256(stdout)>` | shell 只看 stdout |
| 文本为 `{}` / `[]` | `None` | 空容器不算 |
| 其他 | `text:<sha256(全文)>` | 兜底全文哈希 |

推论：

- 同一 URL 抓两次 → 无进展
- 不同 URL、正文相同 → 无进展
- 搜索顺序变化 → 无进展
- 报错重试 5 次 → 直接 stop
- 命中 server 的 run 内缓存（`cached: true`）→ 无进展

### 6.2 计数状态机

`progress.py:49-71`：

```python
if signature is None or signature in self._evidence:
    self._no_progress += 1
else:
    self._evidence.add(signature)
    self._no_progress = 0
    self._nudged = False

if self._no_progress >= self._stop_after:
    return "stop"
if self._no_progress >= self._nudge_after and not self._nudged:
    self._nudged = True
    return "nudge"
return "ok"
```

- 首次出现的新证据不计数，只有重复才累加
- 只统计**连续**，新证据到达立即归零
- 阈值下限为 2
- 默认 `nudge_after=3`、`stop_after=5`

### 6.3 runtime 侧的消费

`runtime.py:474-485`：

- `stop` → `guard_stop=True`，但**等本轮所有 tool_calls 执行完**才 break
- `nudge` → 注入 `_NO_PROGRESS_NUDGE`，只读视图、不落库、只发一次
- 新证据到达 → 清除 nudge，允许下次重新提示

### 6.4 边界

- 只看内容，不看调用本身
- guard 看到的是**未截断**的完整结果（`record` 在 `truncate_text` 之前）
- 子代理工具也参与计数
- **子运行没有开启 `progress_guard`**（`subagent.py:558-571`），只受 `max_turns` 和 timeout 约束
- `_evidence` 是 run 级：同一会话第二次 run 从空集合开始

---

## 7. 上下文压缩与信息保留

### 7.1 压缩只改视图

`agent/compression.py:1-5`：

> The compressed view is built on every request and never written back: session stores, `new_messages`, and `RunResult.tool_results` keep full content.

`compress()` 返回新列表，只用于该次 `MODEL_REQUEST`。

### 7.2 四层防丢机制

1. **最近窗口 + 最后一条 user 固定保留**（`compression.py:82-89, 124-126`）
   - 默认保留一半预算
   - 最后一条 user 消息无论落在哪都会被重新插入 tail
2. **Fact Ledger**（`agent/ledger.py`）
   - 专门为"压缩后忘事"设计
   - 压缩之后追加，永不被摘要
   - 每条约 240 字符、最多 24 条、总长默认 2,500 字符
   - SDK 默认关闭，BokaClaw server 默认开启
3. **`_fit_budget` 只截内容不拆结构**（`compression.py:256-286`）
   - 反复缩短最大的消息，最多 64 轮
   - 不会破坏 tool_call / tool 消息配对
4. **摘要 prompt 要求保留事实**（`compression.py:34-39`）
   - goals / constraints / decisions / facts（路径、ID、数字、错误）/ next steps
   - 滚动增量摘要

### 7.3 明确的丢失窗口

| # | 窗口 | 位置 | 后果 |
|---|---|---|---|
| 1 | 摘要失败仍推进 `_folded_until` | `compression.py:104-122` | 该段历史**永久不再尝试摘要**，只剩一行 "N messages omitted" |
| 2 | 摘要输入被截断 | `compression.py:289-309` | 单条 2,000 字符，整体只留最后 24,000 字符 |
| 3 | 摘要自身有上限 | `compression.py:28-29` | 1,024 tokens / 8,000 字符 |
| 4 | Ledger 也会遗忘 | `ledger.py:19-22, 86-91` | 超过 24 条 FIFO，单条 240 字符 |
| 5 | `max_history_messages` 静默丢弃 | `context.py:209-215` | 无摘要、无提示；server 未设置该参数 |
| 6 | 摘要不跨 run 复用 | `runtime.py:349-350` | 每次 run 重新压缩、重新调摘要模型 |
| 7 | 模型无法主动回忆 | — | 没有 recall 工具；重复调用还会被 progress guard 判为无进展 |

**没有任何"自动把被压缩掉的原文找回来"的机制。** 真正的补回只能在应用层：完整消息在 store / `RunResult` 里，由调用方重新注入。

---

## 8. `session_id` 与 `run_id`

### 8.1 层级关系

```
Conversation（会话）
  conversation_id  ←→  SDK 的 session_id
  │
  ├── AgentRun #1     run_id = uuid        ← 一次 run() / stream()
  │     ├── iteration 1
  │     ├── iteration 2
  │     └── iteration 3 → 结束
  │
  ├── AgentRun #2     run_id = uuid        ← 下一轮提问
  └── ...
```

| 层级 | 标识 | 生成位置 | 生成方式 |
|---|---|---|---|
| 会话 | `Conversation.id` / `session_id` | `server/app/models.py:96` | `uuid.uuid4()`，首次请求时生成 |
| 执行 | `AgentRun.id` / `run_id` | `server/app/models.py:143` | `uuid.uuid4()`，**入队时**生成 |
| SDK 直调 | `session_id` | `agent-loop/.../runtime.py:326` | `session_id or uuid4().hex` |
| 子代理 | `subagent_id` | `agent/subagent.py:435` | `"sub_" + uuid4().hex` |

### 8.2 server 的映射

```python
# server/app/agent/runtime.py:1033, 1067
conversation_id = payload.get("conversation_id")
kwargs = {"session_id": conversation_id, ...}
```

即 **`session_id` ≡ `str(conversation_id)`**。`run_id` 只用于调度、取消、事件流，不进入 SDK。

### 8.3 会话复用规则

| 场景 | 行为 |
|---|---|
| 首次请求不带 `conversation_id` | 新建 Conversation，返回 id |
| 再次请求带同一 id | 校验归属（`get_owned_conversation`），复用 |
| 带别人的 / 不存在的 id | 404（防探测） |
| 上次 run 仍 `queued`/`running` | 409 `agent_run_active` |
| 每次都不带 id | 每次都是新会话，历史不共享 |
| 同一会话并发（SDK 直调） | **无互斥**，会读到同一份历史并各自追加 |

数据库级保护：`uq_agent_runs_active_conversation` 偏唯一索引（`models.py:133-137`）。

### 8.4 取消与事件流按 run

- `_cancel_events[run_id]`：取消是 run 级
- `AgentRunEvent.last_seq` / `open_subscription(run_id, after_seq)`：事件流按 run
- 会话本身没有"取消"或"运行状态"

---

## 9. 历史重放：上一轮的工具有没有塞给下一轮

### 9.1 结论

**会，但不是全量，也不是走 SDK store。**

BokaClaw server 构造 `AgentRuntime` 时**没有传 store**，所以 SDK 内部 `history = []`。历史完全由 server 显式重建并作为 `input` 传入。

### 9.2 链路

```python
# routers/agent.py:147
history = build_history(db, conversation.id, limit=config.history_messages)  # 默认 40

# routers/agent.py:159-171
payload = {"conversation_id": ..., "history": history, ...}

# runtime.py:1033 → build_messages(payload)
# runtime.py:1067-1072
runtime.stream(messages, session_id=conversation_id, ...)
```

### 9.3 工具历史如何还原

`build_history` 读取 `conversation_messages`，对带 `meta["tool_trace"]` 的 assistant 行展开（`store.py:207-267`）：

```
assistant(content="", tool_calls=[...])
tool(tool_call_id="call_1", content="...")
tool(tool_call_id="call_2", content="...")
assistant(content="最终回答")          # 仅当 status == completed
```

细节：

- `status == "running"` 的 trace 条目跳过
- `status == "aborted"` 的条目保留，内容是 abort 错误 JSON
- 缺失结果的 tool_call 会合成 `{"status": "...", "truncated": ...}` 占位
- 失败/取消的 run 只保留 tool_trace，不保留回答

### 9.4 四道历史上限

| 机制 | 默认值 |
|---|---|
| `AGENT_LOOP_HISTORY_MESSAGES` | 40 条 DB 行（展开后消息数更多） |
| `MAX_HISTORY_CHARACTERS` | 24,000 字符 |
| `AGENT_LOOP_HISTORY_TOOL_RESULT_MAX_CHARS` | 2,000 字符 |
| `AGENT_LOOP_HISTORY_TOOL_ARGUMENT_MAX_CHARS` | 1,000 字符 |

`AGENT_LOOP_HISTORY_TOOL_TRACE` 默认开启（只有 `0/false/no/off` 才关）。

---

## 10. 压缩是否影响持久化

**不影响。**

### 10.1 持久化来源是事件，不是模型请求

```
SDK TOOL_CALL / TOOL_RESULT 事件
  → server runtime.py:754-808  _bridge_event
  → runner.py:625-652  trace.start() / trace.finish()
  → payloads.py:43-46  attach_trace()
  → metadata["tool_trace"] = entries
  → runner.py:738-746  repo.insert_assistant_message(metadata=final_metadata)
```

压缩产生 `CONTEXT_COMPRESSED` 事件：转成 `agent` 下的独立 `compression` span（`compression:<iteration>:<occurrence>`，属性保存完整 stats）做观测，同时由 server 转成可重放的 `context.compressed` SSE 事件推给前端；它不参与 `tool_trace` 的构建。

### 10.2 但持久化另有 4 道上限

| 层 | 默认上限 | 环境变量 |
|---|---|---|
| SDK 事件内容 | `tool_result_max_chars` = 16,000 字符 | `AGENT_TOOL_RESULT_MAX_CHARS` |
| ToolTrace 单条 | 16,384 字节 | `AGENT_TOOL_TRACE_MAX_BYTES` |
| ToolTrace 参数 | 4,096 字节 | `AGENT_TOOL_TRACE_MAX_ARGUMENT_BYTES` |
| ToolTrace 单轮总量 | 65,536 字节 | `AGENT_TOOL_TRACE_MAX_TOTAL_BYTES` |

⚠️ SDK 的 `new_messages` / `RunResult.tool_results` 保留完整内容，但 **server 不使用它们**，只读事件。因此超过 16,000 字符的工具输出，从 server 视角永久丢失尾部。

### 10.3 完整截断链

```
工具真实输出（可能 100KB）
  ↓ ① SDK 事件 cap 16,000 字符
  ↓ ② ToolTrace cap 16,384 字节
  ↓ ③ 单轮总 cap 65,536 字节
conversation_messages.meta["tool_trace"]          ← 压缩不参与
  ↓ ④ build_history cap 2,000 字符/条、24k 总字符、40 条
payload["history"] → 发给 SDK
  ↓ ⑤ context_max_tokens=32,000 时压缩
模型请求                                          ← 压缩只在这里生效
```

---

## 11. 已知风险与加固建议

按影响排序：

1. **摘要失败后永久跳过**
   - `compression.py:104-122`：失败也推进 `_folded_until`
   - 建议：失败时不推进，下一轮重试
2. **`extra="ignore"` 静默吞参数**
   - 模型拼错字段名不会报错，工具可能带默认值执行
   - 建议：`create_model(..., __config__=ConfigDict(extra="forbid"))`
3. **事件层截断导致 server 侧无法拿到完整工具输出**
   - 建议：需要完整输出时从 `RunResult.tool_results` 取，或调大 `AGENT_TOOL_RESULT_MAX_CHARS`
4. **没有工具级策略钩子**
   - 建议：在 `ToolRegistry.invoke` 增加 `policy(call) -> str | None`
5. **没有 recall 机制**
   - 建议：提供按 `tool_call_id` / message index 取回原文的工具
6. **SDK 层同一 `session_id` 无并发保护**
   - 建议：应用层加锁，或文档明确要求串行
7. **shell 开启后无命令级策略**
   - 当前完全依赖容器隔离

---

## 12. 关键文件索引

| 主题 | 文件 |
|---|---|
| ReAct 循环、终止、取消、持久化编排 | `agent-loop/src/bokaclaw_agent/agent/runtime.py` |
| 无进展判定 | `agent-loop/src/bokaclaw_agent/agent/progress.py` |
| 上下文压缩 | `agent-loop/src/bokaclaw_agent/agent/compression.py` |
| Fact Ledger | `agent-loop/src/bokaclaw_agent/agent/ledger.py` |
| 历史视图与截断 | `agent-loop/src/bokaclaw_agent/agent/context.py` |
| 工具注册、参数校验、错误模式 | `agent-loop/src/bokaclaw_agent/tools/registry.py` |
| 配置默认值 | `agent-loop/src/bokaclaw_agent/core/config.py` |
| 子代理委派与预算 | `agent-loop/src/bokaclaw_agent/agent/subagent.py` |
| run/会话/历史重放 | `server/app/agent/runner.py`、`server/app/agent/store.py` |
| 工具实现与围栏 | `server/app/agent/dev_tools.py` |
| shell 沙箱客户端 | `server/app/agent/executor.py` |
| shell 沙箱服务 | `server/app/agent/executor_app.py` |
| 工具调用 trace | `server/app/agent/trace.py`、`server/app/agent/payloads.py` |
| 数据模型 | `server/app/models.py` |
| 容器启动 | `.claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh` |
