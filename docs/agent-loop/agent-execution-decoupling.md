# Agent 执行与连接解耦：SDK 层设计

> 状态：已实现（2026-11-04）
>
> 适用范围：`agent-loop/`（`bokaclaw-agent`）Python SDK
>
> 目标读者：SDK 维护者、服务端集成者
>
> 契约来源：HTTP、SSE、run 状态、事件 payload、错误码以 [`../server/agent-execution-decoupling.md`](../server/agent-execution-decoupling.md) 第 3、9 节为准。本文只描述 SDK 自己的能力与边界。

## 1. 定位与边界

`bokaclaw-agent` 是一个**进程内 ReAct 库**，不是后台执行器，也不拥有 run 生命周期。

它负责：

- 组装上下文、调用模型、执行工具、推进 ReAct 循环。
- 通过 `on_event` 向调用方发出七种生命周期事件。
- 在传入了 `SessionStore` 时，把**一轮结束后的**消息与会话状态一次性写入存储。
- 接受 `threading.Event` 作为协作式取消信号，在安全检查点停止并抛出 `RunCancelled`。

它不负责：

- `agent_runs` / `agent_run_events` 表、run 的排队、领取、终态与重启恢复。
- SSE 订阅、事件回放、`after_seq` / `Last-Event-ID`。
- HTTP 请求生命周期、断连检测、连接级取消。
- 增量助手消息持久化；这是服务端 `AgentRunManager` 的职责。
- 后台进程、租约、多副本协调。

服务端集成方式：每个 run 创建一个 `AgentRuntime`，在 worker 线程中消费 `runtime.stream(...)`。服务端使用自己的 `RunEventWriter` 分配持久化 `seq`，**不复用、也不转发** SDK 的 `AgentEvent.seq`。服务端也不使用 SDK 的 `SessionStore`：它有自己的 `conversation_messages` 与 `agent_runs`。

## 2. 已实现能力总览

| 能力 | 入口 / 符号 | 代码位置 |
| --- | --- | --- |
| 同步执行 | `AgentRuntime.run(..., cancel_event=None)` | `src/bokaclaw_agent/runtime.py` |
| 流式执行 | `AgentRuntime.stream(..., cancel_event=None)` | 同上 |
| 异步执行 | `AgentRuntime.arun(..., cancel_event=None)` | 同上 |
| 生命周期事件 | `on_event: Callable[[AgentEvent], Any]` | `AgentRuntime.__init__` |
| 事件序号 | `AgentEvent.seq` | `src/bokaclaw_agent/types.py` |
| 取消异常 | `RunCancelled` | `src/bokaclaw_agent/exceptions.py` |
| 取消终止原因 | `RunResult.stop_reason="cancelled"` | `types.py` |
| 会话状态 | `active / completed / failed / cancelled` | `sessions/postgres.py` |
| 状态约束升级 | `_ensure_status_check()` | 同上 |

公开导出（`bokaclaw_agent/__init__.py`）包含 `AgentRuntime`、`AgentConfig`、`ModelConfig`、`AgentEvent`、`RunResult`、`RunCancelled`、`MaxIterationsExceeded` 等。SDK 版本不拥有服务端 run id；`RunResult.session_id` 由调用方传入（服务端传 `conversation_id`）。

## 3. 公共 API

### 3.1 `run`

```python
def run(
    self,
    input: str | list[dict[str, Any]],
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
    **model_kwargs: Any,
) -> RunResult
```

- `input` 是用户字符串或 OpenAI wire-format 消息列表。
- `session_id` 为空时生成新的 `uuid4().hex`。
- `cancel_event` 在安全检查点被检查；命中时抛出 `RunCancelled`。
- 返回 `RunResult`，包含 `content`、`stop_reason`、`new_messages`、`tool_results`、`iterations`、`usage`。

### 3.2 `stream`

```python
def stream(
    self,
    input: str | list[dict[str, Any]],
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
    **model_kwargs: Any,
) -> Generator[str, None, RunResult]
```

- 只 `yield` 助手文本增量（`str`），不 `yield` 工具事件；工具信息通过 `on_event` 获取。
- 生成器耗尽时以 `return RunResult` 返回最终结果。
- 提前 `close()` 生成器等于**放弃本轮**：不写会话、不产生终态事件，也**不是**取消。
- `cancel_event` 在整个模型响应完成后才检查，不在流式 chunk 循环内部检查。

### 3.3 `arun`

```python
async def arun(
    self,
    input: str | list[dict[str, Any]],
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
    **model_kwargs: Any,
) -> RunResult
```

- 通过 `asyncio.to_thread(self.run, ...)` 包装同步实现。
- `cancel_event` 原样转发。
- 适合 async 框架调用；不提供异步流式版本。

### 3.4 `RunResult`

```python
class RunResult(BaseModel):
    session_id: str
    content: str | None = None
    stop_reason: Literal["final", "length", "content_filter", "max_iterations", "cancelled"]
    new_messages: list[dict[str, Any]]
    tool_results: list[ToolResult]
    iterations: int
    usage: dict[str, int]
```

`stop_reason` 映射：

| 模型 `finish_reason` | `RunResult.stop_reason` |
| --- | --- |
| `length` | `length` |
| `content_filter` | `content_filter` |
| 其他/空 | `final` |
| 达到最大迭代 | `max_iterations`（同时抛 `MaxIterationsExceeded`） |
| `cancel_event` 命中 | `cancelled`（同时抛 `RunCancelled`） |

取消时的部分结果通过 `RunCancelled.result` 暴露，结构与 `RunResult` 相同。

### 3.5 `RunCancelled`

```python
class RunCancelled(AgentError):
    session_id: str
    result: RunResult | None
    iteration: int
```

- 继承 `AgentError`，不是普通错误事件。
- 在 `_cancel_run()` 中构造，先填好部分结果与持久化，再抛出。
- 调用方必须捕获；未捕获时会向上传播。

## 4. 取消模型

### 4.1 信号

`cancel_event` 是标准库 `threading.Event`。SDK 只读取 `is_set()`，不负责设置或清除。设置方通常是服务端的 `AgentRunManager`：

- `running` run 收到 cancel 请求时 `set()`。
- 正常完成后不需要清除；每个 run 使用独立 Event。

### 4.2 四个检查点

```text
for iteration in 1..max_iterations:
    check_cancelled()            # 1. 每次 ReAct 迭代前
    response = model(...)
    check_cancelled()            # 2. 模型响应后、执行本次工具前
    for call in response.tool_calls:
        check_cancelled()        # 3. 每个工具调用前
        result = invoke(call)
        check_cancelled()        # 4. 每个工具调用后
```

明确的行为：

- **流式 chunk 循环内部不检查**。`_stream_response` 持续消费 provider 的 chunk，直到模型响应完整结束；取消要等这次模型响应结束才生效。
- 模型请求本身是阻塞的；取消不能中断一个正在进行的模型请求。
- 工具调用是同步阻塞的；取消不能中断一个正在执行的工具。
- 检查点 2 会 abort 本次响应中**所有**尚未执行的工具调用。
- 检查点 3 会 abort 当前调用及其后所有调用。
- 检查点 4 会 abort 后续尚未执行的调用；当前调用已经产生 `tool_result`。
- 检查点 1 通常没有待执行的调用（上一轮工具循环已清空 `pending_calls`）。

### 4.3 取消路径

`_cancel_run()` 的顺序：

1. 构造 `RunCancelled(session_id, None, iteration)`。
2. `_abort_pending_tool_calls()`：
   - 对每个待执行调用生成 `ToolResult(is_error=True, content='{"error": "run aborted: ..."}')`。
   - 发出 `tool_result` 事件，`status="aborted"`。
   - 把对应的 `tool` 消息追加到上下文与 `new_messages`，保证持久化的对话里每个 assistant tool call 都有对应 tool message。
3. 构造 `RunResult(stop_reason="cancelled", content=<最后一次模型响应内容>, ...)`。
4. 把结果挂到 `cancelled.result`。
5. `_persist_best_effort(status="cancelled", error="RunCancelled: ...")`。持久化失败只记日志，不影响取消。
6. 发出最后一个事件 `run_ended`，`data={"stop_reason": "cancelled"}`。
7. `raise cancelled`。

取消**不发出** `error` 事件。SDK 侧的事件序列以 `run_ended(stop_reason="cancelled")` 结束；服务端会据此或根据自身取消信号写自己的 `error(agent_cancelled)`。

### 4.4 取消延迟

取消的延迟等于当前阻塞单元：

- 如果正在等待模型响应：要等完整响应（包括所有流式 chunk）结束。
- 如果正在执行工具：要等该工具返回。
- 如果正在两个检查点之间：立即生效。

因此“Stop 后立刻停止”并不成立；服务端文档把它描述为协作式取消。

### 4.5 与服务端桥接的关系

服务端 `stream_local_agent()` 会：

- 把 `cancel_event` 传给 `runtime.stream(...)`。
- 同时在消费协程里每 0.5 秒检查一次 `cancel_event`；命中就抛 `AgentLoopError("agent_cancelled")`。
- 在 `finally` 中 `stop.set()`、`cancel_event.set()`、关闭 runtime。

这意味着服务端可能先于 SDK 检查点观察到取消：

- 若 SDK 先命中检查点：抛 `RunCancelled`，桥接层按类型名映射为 `"cancelled"`。
- 若桥接层先观察到：直接结束订阅，`turn.close()` 放弃生成器，SDK 的 `_cancel_run()` 可能不会执行。
- 两条路径在服务端都归一为 `AgentLoopError("agent_cancelled")`，最终 run 状态为 `cancelled`。

SDK 的 `RunCancelled` 语义只在“调用方让生成器继续运行到检查点”时成立；桥接层的提前关闭属于“放弃生成器”，不是 SDK 取消。

## 5. 事件与可观测性

### 5.1 七种事件

`EventType`：

| 值 | 时机 | `data` |
| --- | --- | --- |
| `run_started` | `_execute` 开始 | `input`：字符串原文，或 `"[messages]"` |
| `model_request` | 每次模型请求前 | `messages`、`tools`（完整 wire-format 与工具 schema） |
| `model_response` | 每次模型响应后 | `content`、`tool_calls`（`model_dump()` 列表）、`finish_reason` |
| `tool_call` | 工具调用前 | 见 5.2 |
| `tool_result` | 工具返回/abort 后 | 见 5.3 |
| `run_ended` | 正常结束、最大迭代、取消 | `stop_reason` |
| `error` | 非取消异常 | `error`（字符串）、`type`（异常类名） |

`model_request` / `model_response` 携带完整消息与工具 schema，长对话下体积可能很大；服务端桥接层只消费工具事件，不转发这两类事件。

### 5.2 `tool_call` 的 `data`

```json
{
  "tool_call_id": "call_abc",
  "name": "search_files",
  "arguments": { "query": "agent run" },
  "raw_arguments": "{\"query\":\"agent run\"}",
  "arguments_error": null,
  "status": "running"
}
```

- `arguments` 是 provider 解析后的对象；解析失败时可能为空对象，`arguments_error` 给出原因。
- `raw_arguments` 是原始 JSON 字符串（若 provider 提供）。
- `status` 恒为 `running`。

### 5.3 `tool_result` 的 `data`

```json
{
  "tool_call_id": "call_abc",
  "name": "search_files",
  "content": "{\"matches\": [\"server/app/agent/runner.py\"]}",
  "is_error": false,
  "status": "completed"
}
```

- 正常完成：`status="completed"`，`is_error=false`。
- 工具抛错且 `tool_error_mode` 为 `observe`：`status="failed"`，`is_error=true`，`content` 是错误文本。
- 取消/失败中断：`status="aborted"`，`is_error=true`，`content` 是 `{"error": "run aborted: ..."}` 的 JSON 字符串。

### 5.4 序号

- `AgentEvent.seq` 从 1 开始，每个 `_execute` 重置为 0。
- 无论是否注册 `on_event`，`_event_seq` 都会递增；未注册时只是不构造事件对象。
- 顺序严格等于事件发出顺序，包含 `run_started` 与 `run_ended`。
- `seq` 是 **runtime 实例状态**，不是全局或会话级状态。共享同一个 `AgentRuntime` 并发执行多个 run 会导致序号交错；服务端每个 run 新建 runtime，因此不受影响。
- SDK `seq` 与服务端 `agent_run_events.seq` **没有任何对应关系**；服务端按自己的事件写入顺序重新编号。

### 5.5 回调异常

`_emit()` 用 `try/except Exception` 包住 `on_event(event)`：

- 回调抛错只记录 `logger.exception`，不终止 run。
- 回调返回 awaitable 时会 `resolve_awaitable` 等待（用于异步回调）；其异常同样被吞掉。

## 6. 终止路径

### 6.1 正常结束

```text
构建 RunResult(stop_reason=final/length/content_filter)
_persist(status="completed")        # 非 best-effort
_emit(run_ended, {stop_reason})
return RunResult
```

- 没有工具调用、模型返回最终文本时进入此路径。
- 成功路径的会话持久化失败会抛异常，进入 6.4 的通用失败处理。

### 6.2 达到最大迭代

```text
构建 RunResult(stop_reason="max_iterations")
_persist_best_effort(status="failed", error="max_iterations exceeded")
_emit(run_ended, {stop_reason="max_iterations"})
raise MaxIterationsExceeded(session_id, result)
```

- `MaxIterationsExceeded.result` 携带部分结果。
- 这是 `failed`，不是 `cancelled`。

### 6.3 取消

见 4.3。最后一个事件是 `run_ended(stop_reason="cancelled")`，没有 `error` 事件，抛出 `RunCancelled`。

### 6.4 其他异常

```text
_handle_failure():
  abort 仍在 pending 的 tool calls（发出 aborted tool_result）
  _persist_best_effort(status="failed", error="<Type>: <message>")
_emit(error, {error, type})
raise
```

- 包括模型错误、工具错误、配置错误、成功路径持久化失败等。
- 失败持久化是 best-effort；失败本身不会被持久化错误掩盖。

### 6.5 提前关闭生成器

`stream()` 的生成器在耗尽前被 `close()`：

- `GeneratorExit` 不会被 `except Exception` 捕获。
- 不执行 `_persist`，不发 `run_ended`，不抛 `RunCancelled`。
- 会话不会被写入；调用方视为“本轮被放弃”。
- 这与 `cancel_event` 是两种不同语义。

## 7. 会话持久化

### 7.1 SessionStore

`AgentRuntime(config, store=...)` 可注入 `SessionStore`：

```python
class SessionStore(Protocol):
    def load_messages(self, session_id: str) -> list[dict[str, Any]]: ...
    def persist_run(self, session_id, new_messages, *, agent_name, user_id=None,
                    metadata=None, status="completed", error=None) -> None: ...
```

- `InMemorySessionStore` 用于测试/单进程。
- `PostgresSessionStore` 用于持久化；需要 `postgres` extra（`psycopg` 延迟导入）。
- 服务端 `build_runtime()` **没有传 store**，因此服务端路径上 SDK 不会写会话表；消息由服务端 `conversation_messages` 与 run 事件单独管理。

### 7.2 写入时机

- 每轮开始时 `store.load_messages(session_id)` 读取历史。
- 一轮结束时一次性 `persist_run`：会话行 upsert + 本轮 `new_messages` 批量插入。
- 没有增量写入；流式过程中的文本只通过 `AgentDelta` 交给调用方。
- `title` 由 `derive_title(new_messages)` 生成；已有标题不会被覆盖。
- 消息 `seq` 从当前最大值继续递增。

### 7.3 状态

`SessionRecord.status` 与 Postgres CHECK：

```text
active | completed | failed | cancelled
```

映射：

| 路径 | 写入状态 |
| --- | --- |
| 正常结束 | `completed` |
| 最大迭代 | `failed` |
| 取消 | `cancelled` |
| 其他异常 | `failed` |
| 提前关闭生成器 | 不写 |

### 7.4 Postgres CHECK 升级

旧版本的 `sessions.status` CHECK 只允许 `active/completed/failed`。为了让旧库能写入 `cancelled`：

- `PostgresSessionStore.ensure_schema()` 在建表后调用 `_ensure_status_check(cursor)`。
- 它从 `pg_constraint` 读取 `sessions` 表上 `status` 列的实际 CHECK 约束名与定义。
- 如果定义中缺少 `cancelled`，删除旧约束并按固定名重新添加：

```sql
CHECK (status IN ('active','completed','failed','cancelled'))
```

- 如果约束已包含 `cancelled`，不执行任何操作。
- 升级是幂等的；旧数据无需回填，状态列之外的列不变。
- `ensure_schema()` 使用 advisory lock 串行化多进程初始化。

## 8. 与服务端契约的关系

| 服务端概念 | SDK 对应 | 说明 |
| --- | --- | --- |
| `conversation_id` | `session_id` | 服务端桥接层把会话 id 作为 SDK 会话 id 传入；SDK 不生成它 |
| `run_id` | 无 | SDK 不感知执行 run；服务端的 `run_id` 不传给 SDK |
| `message_id` | 无 | SDK 不生成助手消息 id；服务端预生成 |
| `agent_run_events.seq` | 无 | 服务端 `RunEventWriter` 独立编号 |
| `AgentEvent.seq` | SDK 事件序号 | 只用于 SDK 回调顺序，服务端不持久化、不转发 |
| `AgentToolCall` / `AgentToolResult` | 服务端事件模型 | 桥接层把 SDK `tool_call` / `tool_result` 转为服务端事件 |
| `tool_trace` | 无 | 服务端 `ToolTrace` 聚合；SDK 只发原始工具事件 |
| run 终态 | `RunResult.stop_reason` | 只表达本轮停止原因，不表达服务端 run 状态 |
| 取消 | `cancel_event` / `RunCancelled` | 服务端设置信号；桥接层归一到 `agent_cancelled` |

事件映射（桥接层）：

| SDK 事件 | 服务端事件 | 备注 |
| --- | --- | --- |
| `tool_call` | `AgentToolCall` → 持久化 `tool.call` | `status="running"` |
| `tool_result(status=completed/failed)` | `AgentToolResult` → 持久化 `tool.result` | 保留 `is_error` |
| `tool_result(status=aborted)` | `AgentToolResult` → 持久化 `tool.result(status=aborted)` | 取消/失败时补齐 |
| 文本 delta | `AgentDelta` → 合并后的 `message.delta` | 服务端按 150ms/4096B 合并 |
| 正常结束 | `AgentCompleted` | 携带 `usage` / `finish_reason` metadata |
| `RunCancelled` / 取消 | `AgentLoopError("agent_cancelled")` | 服务端写 `error(agent_cancelled)` |
| `MaxIterationsExceeded` | `AgentLoopError("agent_upstream_error")` | 服务端写 `error(agent_upstream_error)` |

服务端不转发 `model_request` / `model_response` / `run_started` / `run_ended` / `error` 这几类 SDK 事件；它们要么体积过大，要么由服务端自己的事件模型表达。

## 9. 已知限制与后续阶段

| 限制 | 现状 | 后续方向 |
| --- | --- | --- |
| 无 `RunHandle` | 调用方只能通过 `cancel_event` 与 `on_event` 观察 run | 引入显式 `RunHandle` 持有的取消与事件接口 |
| 无 `CheckpointStore` / `resume` | 中断后只能重新开始；SDK 不保存步级状态 | 步级 checkpoint 与恢复 |
| 无事件回放 | `on_event` 是推送式，错过即错过 | 可选的 run 事件缓冲/查询接口 |
| 仅终态持久化 | 流式过程中不写会话；崩溃会丢失整轮 | 增量持久化或与调用方协作 |
| 取消非抢占 | 模型/工具阻塞期间无法中断；流式 chunk 内不检查 | 工具级取消令牌、provider 级中断 |
| `seq` 是实例状态 | 共享 runtime 并发执行会交错 | 每个 run 独立 runtime（服务端已这样做） |
| 取消不发 `error` | SDK 侧以 `run_ended(stop_reason="cancelled")` 结束 | 保持；错误语义由调用方决定 |
| 提前关闭 ≠ 取消 | 关闭生成器不持久化、不发终态事件 | 明确文档化（本文） |
| 持久化失败 best-effort | 取消/失败路径只记日志 | 可选的重试或错误上报 |
| 无分布式协调 | SDK 进程内库，不感知其他副本 | 由服务端负责 |

后续阶段的服务端 side 设计见 [`../server/agent-execution-decoupling.md`](../server/agent-execution-decoupling.md) 第 13 节。SDK 内部结构见 [`architecture.md`](architecture.md)。

## 10. 测试覆盖

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_cancel.py` | 迭代前取消、模型响应后取消 abort 全部工具、工具执行中取消保留已完成结果、流式取消等待模型响应、未设置 `cancel_event` 时行为不变、`arun` 转发取消信号 |
| `tests/test_runtime.py` | 事件顺序、`seq` 从 1 开始且每次 run 重置、跨工具调用单调递增、工具生命周期 metadata、`failed` / `aborted`、回调异常被吞、终态持久化 |
| `tests/test_streaming.py` | delta 转发、工具调用片段重组、工具事件、不支持流式的 provider 回退、提前关闭不持久化、空文本 |
| `tests/test_session_postgres.py` | 建表、`cancelled` 状态、旧 CHECK 升级、实际约束名、幂等升级、取消状态持久化、状态往返 |

验收命令：

```bash
cd agent-loop
hatch run test
hatch run lint
```
