# AgentLoop：执行与连接解耦设计

> 状态：Phase 1 已实现（2026-11-04）；Phase 2（RunHandle / CheckpointStore / 续跑）未实现
>
> 适用范围：`agent-loop/` 包（发行名 `bokaclaw-agent`，import 名 `bokaclaw_agent`）
>
> 目标读者：SDK 维护者、`server/` 集成开发者、需要嵌入 Agent 能力的业务方
>
> 契约来源：与 `server/` 共享的 run 状态、事件、seq、错误码以 [`../server/agent-execution-decoupling.md`](../server/agent-execution-decoupling.md) 第 3 节为准。

## 1. 背景与 SDK 定位

`bokaclaw-agent` 是一个可独立发布、可嵌入任意 Python 服务的 ReAct 运行时。v1 负责：

1. ReAct 循环（模型请求 → 工具调用 → 工具结果回填 → 最终答案）。
2. `stream()` 逐段转发最终答案的文本增量。
3. 一次 run 内的上下文管理与历史截断。
4. run 结束后把消息写入 `SessionStore`（内存或 Postgres）。

`server/` 当前**不使用** SDK 的 `SessionStore`：它把 `conversations` / `conversation_messages` 作为唯一事实来源，每次请求把历史显式传给 runtime。因此本轮"执行与连接解耦"的 Phase 1 可以在**不改 SDK 公共 API**的前提下完成，SDK 文档需要说明这一边界，避免夸大现有能力。

### 1.1 本轮的位置

| 层 | 本期改动 |
| --- | --- |
| `server/` | 新增 run 表、后台 worker、事件日志、取消接口（见 server 文档） |
| `dashboard/` | 重连、Stop 走 cancel、刷新恢复（见 dashboard 文档） |
| `agent-loop/` | **Phase 1 不强制改动**；本文定义约束与后续增量 |

Phase 1 的取消依赖服务端已经存在的 `threading.Event` + task cancellation 组合；SDK 只提供"迭代边界可检查"的增强，使取消能产生一条合法的 `cancelled` 会话记录。

## 2. 现状能力与缺口

### 2.1 已有能力

| 能力 | 现状 |
| --- | --- |
| run 标识 | `_execute()` 内部生成 `session_id = session_id or uuid4().hex`，通过 `RunResult.session_id` 返回 |
| 事件回调 | `on_event(AgentEvent)`，类型 `run_started`、`model_request`、`model_response`、`tool_call`、`tool_result`、`run_ended`、`error` |
| 流式文本 | `stream()` 逐段 yield；工具调用分片在内部重组 |
| 失败持久化 | `_handle_failure()` 补齐 pending tool 消息，`persist_run(status="failed")` |
| 最大步数 | `AgentConfig.max_iterations`，默认 10；超出抛 `MaxIterationsExceeded` |
| 历史管理 | `ContextManager` 只做截断（`max_history_messages`），无摘要/压缩 |
| 工具错误 | `tool_error_mode="return"`（默认）或 `"raise"` |

### 2.2 缺口

| 缺口 | 影响 | 归属 |
| --- | --- | --- |
| 没有取消令牌 | 调用方只能关闭生成器；关闭发生在 `stream()` 消费端，`_execute` 的 `finally` 之前不会主动检查 | Phase 1 |
| 事件没有单调 `seq` | 回调消费者无法去重/续接；只有 `iteration` 和事件类型 | Phase 1 |
| run id 在流结束前不可见 | 调用方无法在 run 开始时就持久化/订阅 | Phase 1（可选） |
| 只在 run 结束持久化 | 进程崩溃丢失整轮进度；`stream()` 提前关闭明确不持久化 | Phase 2 |
| 没有 checkpoint | 重启后无法知道已完成哪些工具、pending 哪些调用 | Phase 2 |
| 工具没有幂等声明 | 恢复时无法安全决定是否重放 | Phase 2 |
| 没有事件回放 | 事件只经回调发出，回调失败被吞掉，无法补读 | Phase 2 |

这些缺口解释了为什么 **Phase 1 的解耦只是"连接级"的**：服务端可以断开 HTTP 连接而继续执行，但 SDK 仍然是一次连续的函数调用；进程重启后不能从中间步骤续跑。

## 3. 与 server 的契约关系

SDK 的 `session_id` 与 server 的 `run_id` 是不同层次的概念：

| 概念 | 所属 | 生命周期 | 用途 |
| --- | --- | --- | --- |
| `session_id` | SDK | 同一会话可跨多次 `run()` 复用 | 加载/持久化历史 |
| `run_id` | server | 一次执行 | 订阅、取消、事件重放 |
| SDK `iteration` | SDK | 一次 run 内的 ReAct 轮次 | 工具事件归属 |
| server `seq` | server | 一次 run 内的事件序号 | SSE 去重/续传 |

映射关系（Phase 1，服务端当前用法）：

```text
server agent_runs.id        → 仅存在于 server，不传给 SDK
server payload.conversation_id → SDK session_id
server payload.message_id   → 用户消息 id（server 自己的表）
server agent_run_events.seq → server 分配，不复用 SDK 事件序号
```

因此：

- SDK **不需要**知道 server 的 `run_id`，也不需要把 `seq` 写进模型请求。
- server 的 `agent_run_events` 是权威事件日志；SDK 的 `on_event` 是进程内观察点。
- SDK 后续若增加 `seq`，它是 SDK 自己的序号，**不能**直接当作 server 的 `seq` 使用；两者独立分配，避免耦合。

### 3.1 server 契约摘要（只列与 SDK 相关的部分）

完整定义见 [`../server/agent-execution-decoupling.md`](../server/agent-execution-decoupling.md) 第 3 节。SDK 侧需要对齐的只有以下几点：

| 项 | 值 |
| --- | --- |
| run 状态 | `queued / running / completed / failed / cancelled / interrupted` |
| SDK 会触发的终态 | `completed`、`failed`、`cancelled`；`interrupted` 由 server 重启回收产生 |
| 取消错误码 | `agent_cancelled` |
| 中断错误码 | `agent_interrupted` |
| 事件订阅游标 | `after_seq`，排他（只返回 `seq > N`） |
| 消息状态映射 | `interrupted` 落库为消息 `failed`，metadata 写 `run_status=interrupted` |
| server 持久化事件 | `message.delta`、`tool.call`、`tool.result`、`message.completed`、`error` |
| server 合成事件 | `meta`（每次订阅开始）、`done`（最后一条，`status` 为终态） |

其余错误码（`agent_run_active`、`agent_run_not_found`、`agent_events_expired`）属于 server 的 run 管理，SDK 不产生也不解析。

## 4. Phase 1：取消令牌、事件序号、持久化时机

Phase 1 的目标是在不改变 `run()` / `stream()` 基本形态的前提下，让取消可预期、事件可去重、取消后的会话记录合法。

### 4.1 取消令牌

新增可选参数：

```python
import threading

def stream(
    self,
    input: str | list[dict[str, Any]],
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
    **model_kwargs: Any,
) -> Generator[str, None, RunResult]: ...
```

`run()` 和 `arun()` 同样接受 `cancel_event`。它是**协作式**的：runtime 在安全边界检查，不强制中断阻塞中的网络请求或工具。

检查点：

1. 进入每次 `for iteration in range(...)` 之前。
2. 模型响应返回后、执行该轮的 `tool_calls` 之前。
3. 每个工具调用开始之前。
4. 工具调用结束、回到下一轮之前。

不要在流式 chunk 循环内部抛取消异常：此时 assistant 消息尚未组装完成，直接中断会产生不完整的 tool_calls，破坏会话合法性。取消生效时，当前模型请求会自然结束，然后在下一次检查点退出。

取消时的行为：

```text
cancel_event.set()
  → 在下一个检查点抛 RunCancelled
  → 复用 _handle_failure 的补齐逻辑：为 pending tool calls 写 aborted 的 tool 消息
  → persist_run(status="cancelled", error="RunCancelled")
  → emit run_ended {stop_reason: "cancelled"}（或 error 事件）
  → 抛出 RunCancelled（携带 session_id / 部分 RunResult）
```

实现上应把 `_handle_failure` 中"补齐 pending tool 消息"的部分抽成共享函数；取消路径复用它，但以 `cancelled` 持久化，不能直接调用会写 `failed` 的整个函数。

服务端已有的 `stop` 机制：

- `server/app/agent/runtime.py` 的 `stream_local_agent()` 已经维护一个 `threading.Event`，在生成器 `finally` 中 `stop.set()`，worker 线程在下一个 `next(turn)` 后检查并 `turn.close()`。
- 这条路径能停止"继续消费"，但 `turn.close()` 会让 `_execute` 的生成器提前结束，**不会走到 `persist_run`**，因为没有 store 时无影响；有 store 时不会留下 cancelled 记录。
- Phase 1 接入 `cancel_event` 后，服务端把 manager 的取消信号传给它，SDK 就能在安全边界收尾，而不是只关闭生成器。

### 4.2 `RunCancelled`

新增异常：

```python
class RunCancelled(AgentError):
    """Raised when cancel_event was set at a safe boundary."""

    def __init__(
        self,
        session_id: str,
        result: RunResult | None = None,
        iteration: int = 0,
    ) -> None: ...
```

- 继承 `AgentError`，与 `MaxIterationsExceeded` 同级。
- `result` 包含已产生的消息与工具结果，便于调用方检查。
- 不要把它映射成 `ModelError` 或普通 `Exception`；服务端据此映射 `agent_cancelled`。

### 4.3 事件序号

给 `AgentEvent` 增加：

```python
seq: int = 0          # run 内单调递增，从 1 开始
```

分配规则：

- 每次 `_emit` 前 `self._event_seq += 1`，写入事件。
- `run_started` = 1；`run_ended` / `error` 是最后一个。
- 序号在 run 内单调；不同 run 独立从 1 开始。
- 回调异常仍然被吞掉，不影响 run；`seq` 让消费者至少能检测缺口。

不要复用 `iteration` 作为 `seq`：一轮内可能有多条事件（model_request、model_response、多个 tool_call/tool_result）。

### 4.4 尽早暴露 run 标识

现状 `RunResult.session_id` 只在 `stream()` 耗尽后可读。但 `AgentEvent` 已经带 `session_id` 字段，`run_started` 又是每条 run 的第一条事件，因此调用方可以在 `on_event` 里立即拿到标识，**无需新增 API 或修改 payload**。只需要在文档中明确这一用法。

可选增强：若调用方希望在 runtime 生成 id 之前就掌控标识，显式传入 `session_id`；这也是 server 当前的做法（传 `conversation_id`）。

### 4.5 持久化时机

Phase 1 **不改变**"run 结束才 `persist_run`"的整体策略，只补充取消路径：

| 路径 | 现状 | Phase 1 |
| --- | --- | --- |
| 正常完成 | `persist_run(completed)` | 不变 |
| 异常 | `_handle_failure` + `persist_run(failed)` | 不变 |
| 超出步数 | `persist_run(failed)` + 抛异常 | 不变 |
| 调用方提前关闭 `stream()` | **不持久化**（文档明确） | 不变；这不是取消，是放弃 |
| `cancel_event` | 不存在 | 补齐 tool 消息后 `persist_run(cancelled)` |

为什么不在 Phase 1 做步级持久化：`persist_run` 是整轮追加，重复调用会产生重复消息；要做增量写必须同时引入 checkpoint 与幂等键，属于 Phase 2。Phase 1 的定位是"取消语义正确"，不是"崩溃可恢复"。

## 5. Phase 2：RunHandle、CheckpointStore、事件回放

### 5.1 `RunHandle`

新增 `start()`，与 `run()` / `stream()` 并存：

```python
handle = runtime.start(
    input,
    session_id=session_id,
    user_id=user_id,
    metadata=metadata,
)

handle.run_id          # SDK run id（可与 session_id 不同）
handle.events()        # Iterator[AgentEvent]，支持从 seq 回放
handle.cancel()        # 等价于设置 cancel_event
handle.result()        # 阻塞等待 RunResult；取消/失败抛对应异常
handle.checkpoint()    # 最近一次 Checkpoint
```

语义：

- 后台执行（线程或 asyncio task），调用方通过 `events()` 读取。
- `events()` 可多次调用；从 `seq > after` 回放，实时事件继续追加。
- `cancel()` 是幂等的；取消后 `result()` 抛 `RunCancelled`。
- 不改变 `run()` / `stream()` 的行为；`start()` 是加法。

### 5.2 `CheckpointStore`

```python
class Checkpoint(BaseModel):
    run_id: str
    session_id: str
    seq: int                 # 对应的最后一个 SDK 事件序号
    iteration: int
    messages: list[dict[str, Any]]
    pending_tool_calls: list[ToolCall]
    tool_results: list[ToolResult]
    usage: dict[str, int]
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"]
    created_at: datetime


class CheckpointStore(Protocol):
    def save(self, checkpoint: Checkpoint) -> None: ...
    def load(self, run_id: str) -> Checkpoint | None: ...
    def latest_for_session(self, session_id: str) -> Checkpoint | None: ...
```

建议实现：

- `InMemoryCheckpointStore`：测试与单进程使用。
- `PostgresCheckpointStore`：新表 `bokaclaw_agent_checkpoints`，`run_id` 主键，`(session_id, created_at DESC)` 索引；与现有 `bokaclaw_agent_sessions/messages` 同库，但独立表，避免改变已有 schema。
- checkpoint 写入时机：每次模型响应后、每个工具结果后、终态。写入必须与事件追加保持同一顺序；至少保证"checkpoint 的 seq ≤ 已发出事件的最大 seq"。
- checkpoint 不替代 `persist_run`：前者是执行进度，后者是会话消息的最终事实。恢复成功后仍由 `persist_run` 把新增消息补进会话。

### 5.3 恢复

```python
result = runtime.resume(run_id, checkpoint_store=store)
```

恢复流程：

1. 加载 checkpoint；没有则报 `CheckpointNotFoundError`。
2. 重建 `ContextManager`：历史 = store 中该 session 的已持久化消息 + checkpoint 的增量消息（去重）。
3. 已完成工具调用：直接复用 checkpoint 中的 `ToolResult`，不重新执行。
4. 未完成工具调用（模型已请求、没有结果）：
   - 标记为 `pending`。
   - 默认**不自动重试**，把 run 置为 `interrupted`，要求调用方决定。
   - 只有工具声明 `idempotent=True` 时才允许自动重放。
5. 从下一个 ReAct 轮次继续；`seq` 继续递增。
6. 成功或失败后按正常路径 `persist_run`。

### 5.4 事件回放

- `RunHandle.events(after=seq)` 先回放内存/存储中的事件，再挂接实时。
- 事件存储可复用 checkpoint 表的一个 JSONB 列，或新增 `bokaclaw_agent_events` 表（`run_id, seq, type, data, created_at`，`UNIQUE(run_id, seq)`）。
- 事件保留策略由宿主决定；SDK 只提供读写接口，不自动清理。
- 与 server 的 `agent_run_events` **不共享表**：两者的 seq 空间、事件类型、保留策略不同，强行复用会把两套生命周期绑死。

## 6. 恢复语义与工具幂等

### 6.1 三种工具状态

| 状态 | 含义 | 恢复动作 |
| --- | --- | --- |
| 已完成且有结果 | checkpoint 中有 `ToolResult` | 复用结果，不执行 |
| 已请求、无结果 | 模型已发出 tool_call，执行前崩溃/取消 | 默认不重试；`idempotent=True` 才重放 |
| 未请求 | 尚未进入模型输出 | 正常执行 |

### 6.2 幂等声明

建议给 `@tool` 增加元数据：

```python
@tool(idempotent=True)
def search_memories(query: str) -> dict: ...

@tool(idempotent=False)   # 默认
def save_memory(content: str) -> dict: ...
```

默认 `False` 是安全选择：恢复时不确定就不重放。

分类建议：

| 工具 | 幂等 |
| --- | --- |
| `search_memories`、只读文件、`list_dir`、`read_file` | 是 |
| `save_memory`、写文件、删除文件、`execute_command`、网络写操作 | 否 |

### 6.3 at-least-once 风险

即使声明幂等，恢复仍然可能重复执行"执行到一半"的工具：

- 工具已经产生副作用，但在写 checkpoint 前崩溃。
- 工具执行成功，但结果没有落盘。
- 多个 worker 同时恢复同一 run（Phase 2 仍建议单写者）。

因此恢复语义应表述为 **at-least-once**，不是 exactly-once：

- 幂等工具可以自动重放。
- 非幂等工具：恢复时停止并把 run 置为 `interrupted`，由用户确认后重试或人工处理。
- 文档必须明确这一点，不能承诺"恢复不会重复执行工具"。

### 6.4 与 server Phase 1 的关系

server 本期不自动续跑，重启后把 run 标为 `interrupted`。因此上述 at-least-once 风险在 Phase 1 不会发生；它是 Phase 2 启用 `resume()` 后的前置约束。

## 7. SessionStore 扩展与兼容性

### 7.1 新增 `cancelled` 状态

现状：

- `SessionStore.persist_run(status=...)` 接受 `completed` / `failed`。
- `PostgresSessionStore._VALID_STATUSES = {"active", "completed", "failed"}`。
- 建表 CHECK：`status IN ('active','completed','failed')`。
- `SessionRecord.status` 是普通字符串。

Phase 1 取消要写 `cancelled`，需要：

1. `_VALID_STATUSES` 增加 `"cancelled"`。
2. `SessionRecord.status` 的类型注释/文档增加 `cancelled`（保持 `str`，不引入枚举，避免破坏）。
3. Postgres 建表 DDL 的 CHECK 增加 `cancelled`。
4. 对已存在的表做幂等迁移：检测约束定义，若不含 `cancelled`，执行：

```sql
ALTER TABLE {sessions_table}
    DROP CONSTRAINT IF EXISTS {sessions_table}_status_check;
ALTER TABLE {sessions_table}
    ADD CONSTRAINT {sessions_table}_status_check
    CHECK (status IN ('active','completed','failed','cancelled'));
```

注意：

- 现有 DDL 的 CHECK 是匿名内联约束，Postgres 自动命名为 `{table}_status_check`；迁移前先用 `pg_get_constraintdef` 确认名称，不要盲删。
- 迁移放在 `ensure_schema()` 内、建表之后，使用 advisory lock 保护，保持"自动建表"的现有体验。
- 如果宿主不希望改表，可提供兼容模式：取消写 `failed`，metadata 写 `{"run_status": "cancelled"}`。但默认推荐 `cancelled`，语义更清晰。

### 7.2 与现有数据的兼容

- 旧行状态只有 `active/completed/failed`，新 CHECK 是超集，不需要数据迁移。
- 旧客户端读取 `SessionRecord.status` 时可能只认识三个值；这是 additive 的行为变化，需要 release note。
- `list_sessions` 不按状态过滤，无需改查询。

### 7.3 Phase 2 的 checkpoint 表

独立表，不改 `sessions/messages`：

```sql
CREATE TABLE IF NOT EXISTS {prefix}_checkpoints (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES {prefix}_sessions(id) ON DELETE CASCADE,
    seq BIGINT NOT NULL,
    iteration INT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','completed','failed','cancelled','interrupted')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS {prefix}_checkpoints_session_idx
    ON {prefix}_checkpoints (session_id, created_at DESC);
```

`payload` 存 `messages` / `pending_tool_calls` / `tool_results` / `usage`，避免列爆炸。

## 8. 与 `server/app/agent/runtime.py` 的集成方式

### 8.1 当前调用链

```text
server/routers/agent.py
  → stream_agent(payload)
    → stream_local_agent(payload)
      → build_runtime(...)             # 不传 store
      → runtime.stream(messages, session_id=conversation_id, user_id=...)
      → on_event → _bridge_tool_event → asyncio queue
```

server 不传 `store`，所以：

- 不会写 SDK 的 `sessions/messages` 表。
- `persist_run` 不会执行；`AgentRuntime` 的持久化逻辑对 server 无效。
- 取消只能通过关闭生成器和 `stop` Event。

### 8.2 Phase 1 接入建议

`stream_local_agent` 增加可选参数：

```python
async def stream_local_agent(
    payload: dict[str, Any],
    *,
    cancel_event: threading.Event | None = None,
) -> AsyncIterator[AgentEvent]: ...
```

manager 的取消路径（推荐）：

```text
POST /cancel
  → 设置 cancel_event（不 task.cancel）
  → runtime.stream 在下一个检查点抛 RunCancelled
  → stream_local_agent 转成 AgentLoopError("agent_cancelled")
  → manager 按服务端 6.4 收尾
```

回退路径（SDK 尚未支持 `cancel_event`）：

```text
POST /cancel
  → task.cancel()
  → stream_local_agent 的 finally: stop.set()
  → manager 捕获 CancelledError 后自行写 cancelled 终态
```

两条路径都必须走到同一个收尾函数，保证 run 状态、事件和消息一致。不要只依赖 `turn.close()`：它会让 `_execute` 提前结束，但不产生合法的 cancelled 会话记录。

注意两点：

- 即使 SDK 不支持 `cancel_event`，现有 `stop` + `turn.close()` 仍能停止消费；Phase 1 的增强是"产生合法的取消记录"，不是"才能取消"。
- `cancel_event` 与 manager task 的 `CancelledError` 是两条路径，必须都走到同一个收尾函数；不要让异常直接穿透而不写 run 终态。

### 8.3 事件映射

SDK 事件到 server 事件的现有映射在 `server/app/agent/runtime.py::_bridge_tool_event`：只转发 `tool_call` / `tool_result`。Phase 1 不需要新增映射：

- 文本增量由 `stream()` 的 yield 产生，server 映射为 `message.delta`。
- 工具事件已映射为 `tool.call` / `tool.result`。
- 终态由 `AgentCompleted` / `AgentLoopError` 映射为 `message.completed` / `error`。
- `RunCancelled` 需要新增映射：`AgentLoopError("agent_cancelled", ...)` 或独立分支。

### 8.4 不在 server 中使用 SessionStore

明确保持：server 的 `conversations` 是权威存储，SDK 的 `SessionStore` 只服务独立使用 SDK 的场景。Phase 2 的 checkpoint 如果要在 server 生效，应该由 server 的 run manager 实现自己的 `CheckpointStore`（写 `agent_run_checkpoints`），而不是让 SDK 直接写 server 的表。这样：

- 两套 run 生命周期不会互相污染。
- server 的 checkpoint 可以包含 dashboard 需要的工具轨迹格式。
- SDK 仍然是可独立发布的通用组件。

## 9. 测试方案

### 9.1 Phase 1

| 用例 | 断言 |
| --- | --- |
| 正常 run | `run_started` seq=1，`run_ended` 最大；`RunResult` 不变 |
| 事件序号 | 一轮内多条事件 seq 连续、单调 |
| 取消于迭代边界 | 抛 `RunCancelled`；pending tool 补齐 aborted；store 状态 `cancelled` |
| 取消于工具调用前 | 该工具不执行；之前的工具结果保留 |
| 取消后会话合法 | 每个 assistant tool_calls 都有对应 tool 消息 |
| 未设置 cancel_event | 行为与现状完全一致 |
| `stream()` 提前关闭 | 仍不持久化（现有语义不变，加回归测试） |
| Postgres 迁移 | 旧表 CHECK 自动升级；重复执行幂等；旧数据可读 |
| 取消与完成竞争 | 先到终态者生效；不重复写消息 |

### 9.2 Phase 2

| 用例 | 断言 |
| --- | --- |
| checkpoint 保存/加载 | 字段完整，seq 单调 |
| 恢复已完成工具 | 不重新调用工具 |
| 恢复 pending 工具（非幂等） | 不自动执行，run 置 interrupted |
| 恢复 pending 工具（幂等） | 重放一次，结果写回 |
| 事件回放 | `after=seq` 只返回更大序号；实时继续 |
| 多订阅者 | 每个订阅者看到相同顺序 |
| 崩溃窗口 | 模拟保存前失败，恢复后至少一次执行，不丢消息 |

测试约束沿用 `agent-loop/AGENTS.md`：

- 无网络，使用 `FakeProvider` 与 `InMemorySessionStore`。
- Postgres 单测注入 fake pool；真实库测试由 `BOKACLAW_AGENT_TEST_DSN` 门控并标 `integration`。
- `pytest-asyncio` 严格模式，显式 `@pytest.mark.asyncio`。

## 10. 迁移与版本策略

### 10.1 Phase 1

| 改动 | 兼容性 | 版本建议 |
| --- | --- | --- |
| `cancel_event` 可选参数 | additive | minor |
| `RunCancelled` 异常 | additive | minor |
| `AgentEvent.seq` | additive 字段，默认 0 | minor |
| `RunResult.stop_reason` 增加 `"cancelled"` | Literal 扩宽；穷举 match 的调用方需要更新 | minor + release note |
| `SessionRecord.status` 允许 `cancelled` | 行为扩展；读取方需容忍新值 | minor + release note |
| Postgres CHECK 迁移 | 自动、幂等 | patch/minor |

不建议在 Phase 1 改 `stream()` 的返回类型或移除 `on_event`；`RunHandle` 留在 Phase 2。

### 10.2 Phase 2

| 改动 | 兼容性 |
| --- | --- |
| `runtime.start()` / `RunHandle` | additive |
| `CheckpointStore` / `InMemoryCheckpointStore` / `PostgresCheckpointStore` | additive |
| `@tool(idempotent=...)` | additive，默认 `False` |
| checkpoint 表 | 新建表，不改旧表 |

### 10.3 发布检查

- 更新 `agent-loop/README.md` 的公共 API 说明（项目规则要求公共 API 变更同 PR 更新 README）。
- 更新 `docs/agent-loop/architecture.md` 的运行时链路与持久化章节（如果实现落地）。
- 运行 `hatch run lint`、`hatch run format-check`、`hatch run test`。
- 不在本期修改 `.github/workflows/`。

## 11. 结论

- **Phase 1**：服务端的执行与连接解耦不依赖 SDK 改动；SDK 只需提供协作式取消、事件 `seq` 与 `cancelled` 持久化，就能让取消语义完整、事件可去重。它**不会**让 SDK 具备长程恢复能力。
- **Phase 2**：`RunHandle` + `CheckpointStore` 才提供步级恢复与事件回放；恢复是 **at-least-once**，非幂等工具必须显式阻止自动重放。
- **长期**：多副本领取、上下文压缩、预算控制仍不在本文范围；它们决定的是"能跑多久"，而本文解决的是"连接断了还继续跑"。
