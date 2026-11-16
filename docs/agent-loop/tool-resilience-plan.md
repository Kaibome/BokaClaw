# 工具调用韧性改造方案（错误分类 / 有界重试 / 熔断 / 幂等）

> 状态：已实现（P0–P4）
>
> 日期：2026-11-16
>
> 实施记录（2026-11-16）：P0 错误分类、P1 重试、P2 熔断、P3 原子写 + executor 幂等键 + run token、P4 `client_run_id` 唯一索引与请求重放均已落地。SDK 侧新增 `tools/errors.py`、`tools/retry.py`、`tools/breaker.py`；server 侧新增 `AGENT_TOOL_RETRY_*` / `AGENT_TOOL_BREAKER_*` / `AGENT_EXECUTOR_IDEMPOTENCY_*` 配置，migration `018`。验证结果见 P-23 记录。
>
> 对应问题：`docs/building-problem-record/problem-record.md` P-23（已解决）
>
> 范围：`agent-loop/`（分类、重试、熔断、幂等键）、`server/app/agent/`（工具声明、原子写、executor 幂等、run 级幂等）、`docs/`
>
> 前置阅读：`docs/agent-loop/tool-execution.md` §8.4、`docs/agent-loop/tool-concurrency-plan.md`

---

## 1. 现状与问题

当前框架对工具失败只有一种处理：把异常转成 `ToolResult(is_error=True)` 交还给模型。重试、熔断、幂等三件套全部缺失，且三者的缺失互相放大：

| 缺口 | 现状代码 | 后果 |
|---|---|---|
| 无重试 | `Tool.invoke`（`tools/registry.py:232`）捕获所有异常直接抛 `ToolExecutionError`；`ToolRegistry.invoke`（`registry.py:337`）转成错误结果 | 一次瞬时故障（超时/连接重置/5xx/429）就让模型重新规划，付出一次完整模型往返 |
| 无错误分类 | 异常被统一包成字符串 `Tool 'X' failed: ...` | 上层无法区分「重试有用」和「参数错了」，只能不做 |
| 无熔断 | 只有 run 级 `ProgressGuard`（`agent/progress.py`）、`max_iterations`、`max_seconds` | 连续失败表现为 `no_progress` 而不是「这个工具坏了」；每个 run 都会重新打一遍已经宕掉的服务 |
| 无幂等 | `tool_call_id` 只做消息配对；`client_run_id` 无唯一约束；`/exec` 无 `Idempotency-Key`；文件写入直接 `write_text` | 一旦开启重试，可能重复执行命令；进程崩溃/重试可能留下被截断的文件；客户端重发请求会创建重复 run |

`ProgressGuard` 的判定口径也需要注意：它把 `is_error` 和空结果都算作「无新证据」（`agent/progress.py:74-82`）。如果重试发生在框架内部，一次逻辑调用只应产生一条结果，否则重试会白白吃掉 no-progress 预算。

---

## 2. 目标与非目标

### 目标

1. **错误分类**：在工具边界把失败分成「可重试（瞬时）」与「不可重试（确定性）」两类，并保留结构化原因。
2. **有界重试**：只对声明为幂等的工具、只对瞬时错误，做带指数退避 + 抖动的有界重试；遵守取消信号与总时长上限。
3. **per-tool 熔断**：连续失败达到阈值后快速失败一段时间，半开时只放一个探测请求；任何一次成功即恢复。
4. **幂等**：
   - 文件写入改为「临时文件 + `os.replace`」原子替换；
   - `/exec` 与 `/monitor start` 支持 `Idempotency-Key`，同一 run 的同一次工具调用重试不会重复执行；
   - `client_run_id` 加部分唯一索引，`POST /agent` 对同一用户重复请求返回同一个 run。
5. **可观测**：重试次数、重试原因、熔断状态进入 tool span 属性与 `ToolResult.metadata`，不新增 `agent_run_events` 类型、不做数据库迁移（除 `client_run_id` 索引）。
6. **默认安全**：默认只对 `effect="read"` 的工具开启重试；写类与外部副作用工具必须显式声明。`tool_concurrency` 默认值不变。

### 非目标（本阶段不做）

- 持久化重试队列 / 跨进程重试（run 结束即结束）。
- 跨副本共享熔断状态（进程内状态即可，重启清零）。
- 工具结果的语义缓存（WebFetch 现有的 run 内缓存不动）。
- 让 `Edit` 在「可能已成功但响应丢失」后安全重放（需要内容版本号，另行设计）。
- 子 Agent 整体重跑（代价高、副作用不可判定），本阶段子 Agent 工具默认不重试。
- 新的 SSE 事件类型（`tool.retry`）；先用 metadata 与 span 表达，见 §10。

---

## 3. 设计总览

```text
模型返回 tool_call
        │
        ▼
ToolRegistry.invoke(call, cancel_event)
        │
        ├─ 参数解析失败 ──────────────────────────► 不可重试，返回错误结果
        │
        ├─ 熔断器 open？ ────────────────────────► 快速失败，metadata.breaker="open"
        │
        ▼
   第 1..N 次尝试（N = 该工具的重试策略）
        │  Tool.invoke（参数校验 + 执行 + ContextVar 设置）
        │
        ├─ 成功 ──► breaker.success() ──► 返回结果（metadata.attempts=n）
        │
        └─ 失败 ──► 分类
                     ├─ 不可重试 ──► breaker.failure() ──► 返回错误结果
                     └─ 可重试
                          ├─ 还有预算且未取消 ──► 退避等待（可被取消打断）──► 下一次尝试
                          └─ 预算耗尽/已取消 ──► breaker.failure() ──► 返回错误结果（metadata.retry_exhausted=true）

重试在「一次逻辑调用」内部完成：tool_call_id 不变、只发一条 TOOL_CALL 与一条 TOOL_RESULT、
ProgressGuard 只记录最终结果。
```

三层是独立的：分类是前提，重试是消费方，熔断是可以单独开关的保险丝。幂等不参与重试决策，但决定了「哪些工具允许重试」。

---

## 4. 错误分类

### 4.1 新增异常类型

`agent-loop/src/bokaclaw_agent/core/exceptions.py`：

```python
class TransientToolError(ToolError):
    """Raised by a tool when the failure is likely to succeed on a retry.

    ``retry_after`` lets the tool pass a server-provided delay (for example an
    HTTP Retry-After header); the framework treats it as a lower bound.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)
```

同时在 `ToolExecutionError` 上增加只读属性，由 `Tool.invoke` 在包装异常时填写：

```python
class ToolExecutionError(ToolError):
    kind: Literal["transient", "permanent", "unknown"] = "unknown"
    cause_type: str = ""
    retry_after: float | None = None
    def __init__(self, message, *, kind="unknown", cause_type="", retry_after=None): ...
```

现有调用方（server 只读 `str(exc)`）不受影响。

### 4.2 分类规则

新增 `agent-loop/src/bokaclaw_agent/tools/errors.py`，提供纯函数 `classify_tool_error(exc: BaseException) -> ErrorClass`：

| 判定 | 类别 | 说明 |
|---|---|---|
| `TransientToolError` | transient | 工具显式声明；`retry_after` 透传 |
| `TimeoutError` / `subprocess.TimeoutExpired` / `socket.timeout` | transient | 标准库超时 |
| `ConnectionError`（含 `ConnectionResetError` / `ConnectionAbortedError` / `BrokenPipeError`） | transient | 连接层瞬时故障 |
| `OSError` 且 `errno in {ECONNRESET, ECONNREFUSED, ETIMEDOUT, EPIPE, EHOSTUNREACH, ENETUNREACH}` | transient | 网络/管道 |
| `httpx.TimeoutException` / `httpx.TransportError` | transient | 延迟导入，未安装 httpx 时跳过 |
| `urllib.error.URLError`（非 HTTPError） | transient | 标准库 |
| `ValueError` / `TypeError` / `KeyError` / `ToolArgumentError` | permanent | 参数或数据问题，重试必然再错 |
| `PermissionError` / `FileNotFoundError` / `IsADirectoryError` | permanent | 确定性环境问题 |
| 其余 | unknown | **默认不重试**（失败关闭） |

HTTP 状态码不在这里解析：工具已经知道状态码，应主动抛 `TransientToolError`（429/5xx）或普通 `RuntimeError`（4xx）。禁止在分类层做字符串匹配。

### 4.3 现有工具的错误归类（server）

多数工具已经用 `raise ValueError` / `raise RuntimeError` 表达失败。本次只改「瞬时」语义明确的位置：

- `server/app/agent/executor.py`：连接失败、请求超时 → `TransientToolError`；executor 返回 429/5xx → `TransientToolError`；4xx → `RuntimeError`。
- `server/app/agent/dev_tools.py` 的 WebFetch：
  - `httpx.TimeoutException` / `httpx.TransportError` → `TransientToolError`；
  - 5xx → `TransientToolError`；
  - 429：保留 `Retry-After` 解析，改为抛 `TransientToolError(retry_after=...)`，**删除内部 `MAX_RATE_LIMIT_RETRIES` 循环**，避免框架重试与工具内重试叠加（这是一个小行为变更，需同步测试）；
  - 404/其他 4xx、非 JSON、解析失败 → 保持 `RuntimeError`（不重试）。
- `_request_search`：超时/连接失败 → `TransientToolError`；4xx → `RuntimeError`。
- 文件工具（Read/Glob/Grep/Write/Edit）：本地文件系统的瞬时故障极少，保持原样（unknown → 不重试）。
- 记忆工具、子 Agent、`ask_user`：不重试。
- 记忆后端 / DB 的瞬时错误（连接池耗尽等）暂不纳入：它们发生在工具函数之外的部分路径上，先保持现状。

> 注意：工具如果捕获异常后返回 `{"error": ...}` 而不是抛出，框架无法重试。约定写入文档：**需要重试的工具必须抛出异常，而不是返回错误载荷。**

---

## 5. 有界重试

### 5.1 策略模型

`agent-loop/src/bokaclaw_agent/tools/retry.py`：

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3          # 含首次尝试
    base_delay: float = 0.4        # 秒
    max_delay: float = 8.0
    max_elapsed: float = 15.0      # 单次逻辑调用的重试总预算（不含最后一次尝试的执行时间）
    jitter: float = 0.3            # 0 表示不加抖动
    respect_breaker: bool = True
```

退避公式（full jitter）：

```text
delay = min(max_delay, base_delay * 2 ** (attempt - 1))
delay = delay * random.uniform(1 - jitter, 1)
delay = max(delay, retry_after or 0)
```

`_backoff_delay(attempt, policy, retry_after, rng)` 做成纯函数，便于测试；等待用 `cancel_event.wait(delay)` 实现可中断 sleep。

### 5.2 工具声明

`Tool`（`tools/registry.py:159`）增加字段 `retry: RetryPolicy | bool | None`：

| 声明 | 语义 |
|---|---|
| `None`（默认） | 自动：`effect == "read"` 用 AgentConfig 的默认策略；`write` / `external` 不重试 |
| `True` | 显式开启，使用 AgentConfig 默认策略 |
| `RetryPolicy(...)` | 使用该策略 |
| `False` | 禁用 |

`tool()` 装饰器与 `Tool.from_callable` 透传该参数。本次显式开启的外部工具：`WebFetch`、`WebSearch`（`external` + 幂等 GET）。子 Agent、`Bash`、`PowerShell`、`Monitor`、写类工具保持默认关闭；`Bash` 可在 `/exec` 幂等键落地后单独评估（§7.1、§14）。

### 5.3 执行位置与语义

重试循环放在 `ToolRegistry.invoke`（`registry.py:337`），不放进 `Tool.invoke`：

- `ToolRegistry.invoke` 能看到 `Tool.retry`、`error_mode` 与熔断器；
- `Tool.invoke` 保持「一次执行」的单一职责，参数校验失败天然 `permanent`，不会进入循环；
- `error_mode="raise"` 时，重试耗尽后抛出最后一次的 `ToolExecutionError`（保留 `kind` / `cause_type`），与现有契约一致。

关键约束：

1. **一次逻辑调用一条结果**：`tool_call_id` 不变，`TOOL_CALL` 只发一次，`TOOL_RESULT` 只发一次；尝试历史放进 `metadata`。
2. **取消可打断**：`ToolRegistry.invoke` 新增 `cancel_event=None` 参数；`invoke_batch` / `_invoke_parallel` 把已有 `cancel_event` 透传（当前 `registry.py:415`、`495` 未传）。等待期间被取消 → 返回 `_aborted(call, "run aborted: cancelled")`，语义与现有取消一致。
3. **总时长上限**：`max_elapsed` 只统计重试等待与失败尝试，耗尽即返回最后一次错误；避免尾部工具把整个 run 拖过 `AGENT_LOOP_MAX_SECONDS`。
4. **并行批次不受影响**：重试发生在各自 worker 线程内，退避 sleep 不阻塞其他工具；`tool_concurrency` 不变。
5. **不重试的情况**：参数 JSON 解析失败、未知工具、deferred 未激活、熔断开、`error_mode` 校验前错误——都直接返回，不进入循环。

### 5.4 与 `tool_error_mode` 的关系

`error_mode="return"`（server 当前值）：所有尝试失败 → 返回错误结果（`metadata.attempts` 记录尝试次数）。
`error_mode="raise"`：所有尝试失败 → 抛 `ToolExecutionError(kind=...)`。两种模式的重试决策完全相同。

---

## 6. per-tool 熔断

### 6.1 状态机

新增 `agent-loop/src/bokaclaw_agent/tools/breaker.py`。状态是**进程级、按工具名**的（模块级字典 + `threading.Lock`），因为 server 每个 run 都会重建 `Tool` 实例，状态不能挂在实例上：

```text
closed ──(连续 transient 失败 ≥ threshold)──► open
open ──(cooldown 到期)──► half_open（只允许 1 个探测请求）
half_open ──探测成功──► closed，计数清零
half_open ──探测失败──► open，cooldown = min(cooldown × 2, max_cooldown)
```

接口：

```python
class ToolCircuitBreaker:
    def allow(self, name: str) -> tuple[bool, float]:   # (允许?, 建议等待秒数)
    def record_success(self, name: str) -> None
    def record_failure(self, name: str, *, transient: bool) -> None
    def state(self, name: str) -> str                    # closed/open/half_open
def reset_tool_breakers() -> None                        # 测试用
```

### 6.2 计数口径

- **只统计 `transient` 类的终止失败**：重试耗尽仍为 transient、或分类为 transient 但策略禁止重试。参数错误、权限错误、未知工具**不计数**（一个用户传错参数不该熔断所有人）。
- **一次逻辑调用最多计一次失败**：重试的每次尝试不计，避免一次抖动直接打满阈值。
- 成功（包括重试后成功）清零连续计数。
- open 状态下的快速失败**不再次加分**，只返回剩余冷却时间。

### 6.3 快速失败的返回

```json
{"error": "tool 'WebFetch' is temporarily unavailable after 5 consecutive transient failures; retry in 42s"}
```

`is_error=True`，`metadata={"breaker": "open", "retry_after_s": 42}`。该结果按现有规则被 `ProgressGuard` 视为「无新证据」，模型会转向其他工具或停止，这正是期望行为。

### 6.4 开关与作用域

- `AgentConfig.tool_breaker_enabled`（默认 `True`）、`tool_breaker_failure_threshold`（默认 5）、`tool_breaker_cooldown_seconds`（默认 60）、`tool_breaker_max_cooldown_seconds`（默认 600）。
- 熔断器在处理 `invoke` 时读取当前 runtime 传入的配置；状态键只有工具名。
- 进程重启清零；多副本之间不共享（文档写明）。
- 预留 `reset_tool_breakers()` 给测试和未来的管理接口。

---

## 7. 幂等

### 7.1 `/exec` 与 `/monitor start` 的幂等键

**协议**（`server/app/agent/executor_app.py`）：

```python
class ExecRequest(BaseModel):
    command: str = Field(min_length=1)
    description: str = ""
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    shell: Literal["bash", "powershell"] = "bash"
    idempotency_key: str | None = Field(default=None, max_length=200)
```

`/monitor` 的 `start` 动作同样接受 `idempotency_key`（`start` 重复执行会起两个进程，这是最危险的一条）。

**客户端**（`server/app/agent/executor.py`）：`run_command` / `monitor_action` 增加 `idempotency_key: str | None = None`，放进 JSON body。

**键的构造**：`Tool.invoke` 在执行期间设置新的 ContextVar：

```python
CURRENT_IDEMPOTENCY_KEY: ContextVar[str] = ContextVar("bokaclaw_idempotency_key", default="")
```

由 `ToolRegistry.invoke` 计算：`run_token` 是 `AgentRuntime.run()` 开始时生成的 `uuid4().hex`（通过 `ToolRegistry.begin_run(token)` 注入，`_execute` 结束时 `clear`）；key = `f"{run_token}:{call.id}"`。同一逻辑调用的每次重试使用**同一个 key**，这正是去重的依据。`run_token` 为空（直接使用 `Tool` 的嵌入方）时不发送 key。

**executor 侧实现**：

- 内存缓存 `dict[key, (expires_at, response)]`，`threading.Lock` 保护；容量 512、TTL 600 秒，LRU 淘汰。
- 命中 → 直接返回缓存响应；
- 同 key 正在执行 → 等待其完成（`threading.Event`，最多等待请求超时 + 5 秒），返回同一响应；
- 未命中 → 执行，**把任何已完成的响应写入缓存**（包括非零退出码和 `timed_out=True`），保证 at-most-once；只有传输层失败（无响应、进程无法启动）不写缓存，让重试可以继续；
- 缓存只在内存：executor 重启后丢失，退化为 at-least-once。文档写明这是本阶段的边界。

### 7.2 原子写

`server/app/agent/dev_tools.py` 新增：

```python
def _atomic_write_text(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)   # 保留权限位
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
```

替换所有 `path.write_text(...)`：`Write`（`dev_tools.py:1012`）、`Edit`（`:1042`）、`NotebookEdit`（`:1107`）。

效果：重试或进程崩溃最多丢失一次写入，不会留下半截文件；`Write` 是全量覆盖，天然幂等。`Edit` 仍不重试（失败后原文可能已改变），但不会再产生损坏文件。

### 7.3 run 级幂等（`client_run_id`）

**迁移** `server/alembic/versions/018_agent_runs_client_run_id_unique.py`：

1. 清理历史重复：同一 `created_by` 下相同 `client_run_id` 只保留 `created_at` 最早的一行，其余置 `NULL`（并记录日志）；
2. 创建部分唯一索引：
   ```sql
   CREATE UNIQUE INDEX uq_agent_runs_client_run_id
   ON agent_runs (created_by, client_run_id)
   WHERE client_run_id IS NOT NULL;
   ```
   `downgrade()` 删除索引。
   范围选择「每用户唯一」而不是全局唯一：不同用户可能复用相同的客户端 id，不应互相阻塞。

**接口行为**（`server/app/routers/agent.py` 的后台 run 创建路径）：

- 请求带 `client_run_id`（或 `run_id`，现有逻辑是 `body.client_run_id or body.run_id`）时，先按 `(user.id, key)` 查已有 run：
  - 找到 → 不再创建 conversation / user message / run，直接返回该 run 对应的 `BackgroundTurn`（`deduplicated: true`）；
  - 没找到 → 正常创建；
- 并发同 key：靠唯一索引兜底，`IntegrityError` 后回滚并重新查询，返回已有 run；
- `BackgroundTurn` 增加 `deduplicated: bool = False` 字段（**加性变更**，现有调用方不需要改）。这是公共 REST 契约变更，需同步 `docs/server/` 与 `.env.example` 无关、但 `docs/` 必须更新。

**语义**：同一个 `client_run_id` 的重复 POST 是「重放」——返回同一个 run id、conversation id、assistant message id，客户端据此恢复 SSE 订阅，而不是产生第二条用户消息。若原 run 已失败/取消，也返回它（客户端可显式新建 key 重试）。

---

## 8. 与 ProgressGuard / 取消 / 并发的交互

| 机制 | 交互 | 处理 |
|---|---|---|
| ProgressGuard | 重试若每次尝试都进 `guard.record()` 会白吃 no-progress 预算 | 重试在 registry 内部完成，runtime 只看到最终结果，天然只记一次；补充回归用例 |
| ProgressGuard | 熔断快速失败是否算无进展 | 算（`is_error` → 无证据），这是期望行为，不改 |
| 取消 | 退避等待期间 run 被取消 | 用 `cancel_event.wait(delay)`，被设置则立刻返回 `aborted` 结果；`invoke_batch` / `_invoke_parallel` 必须透传 `cancel_event`（当前未传） |
| 并发 | 并行批次里的重试 | 每个 worker 独立退避，不阻塞同批其他调用；线程池大小不变 |
| 超时 | 重试把 run 拖过 `AGENT_LOOP_MAX_SECONDS` | `RetryPolicy.max_elapsed` 封顶（默认 15s）；server 运行时限不受影响 |
| 子 Agent | 子 runtime 的 registry 也会应用策略 | 子 Agent 工具默认 `retry=None` 且 `effect="external"` → 不重试；子 runtime 的熔断状态按工具名与父级共享 |
| 事件契约 | 不新增事件类型 | `TOOL_RESULT.metadata` 增加 `attempts` / `retry_errors` / `breaker` / `duration_ms`；server `AgentToolResult` 增加 `metadata` 字段（dataclass 加性字段） |
| `error_mode` | `raise` 模式下重试耗尽 | 抛最后一次的 `ToolExecutionError`，带 `kind` / `cause_type` |

---

## 9. 配置与默认值

### 9.1 SDK `AgentConfig`（`agent-loop/src/bokaclaw_agent/core/config.py`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `tool_retry_enabled` | `True` | 关闭后所有工具都不重试（熔断仍可独立开启） |
| `tool_retry_max_attempts` | `3` | 含首次 |
| `tool_retry_base_delay` | `0.4` | 秒 |
| `tool_retry_max_delay` | `8.0` | 秒 |
| `tool_retry_max_elapsed` | `15.0` | 秒 |
| `tool_retry_jitter` | `0.3` | 0–1 |
| `tool_breaker_enabled` | `True` | |
| `tool_breaker_failure_threshold` | `5` | 连续 transient 失败次数 |
| `tool_breaker_cooldown_seconds` | `60` | |
| `tool_breaker_max_cooldown_seconds` | `600` | 指数退避上限 |

`AgentConfig` 是 `extra="forbid"`，新增字段都带默认值，属于向后兼容的加性变更。

### 9.2 server 环境变量（`server/app/agent/runtime.py` + `LocalAgentConfig`）

| 环境变量 | 默认 |
|---|---|
| `AGENT_TOOL_RETRY_ENABLED` | `true` |
| `AGENT_TOOL_RETRY_MAX_ATTEMPTS` | `3` |
| `AGENT_TOOL_RETRY_BASE_DELAY_SECONDS` | `0.4` |
| `AGENT_TOOL_RETRY_MAX_ELAPSED_SECONDS` | `15` |
| `AGENT_TOOL_BREAKER_ENABLED` | `true` |
| `AGENT_TOOL_BREAKER_FAILURE_THRESHOLD` | `5` |
| `AGENT_TOOL_BREAKER_COOLDOWN_SECONDS` | `60` |
| `AGENT_EXECUTOR_IDEMPOTENCY_TTL_SECONDS` | `600`（executor 容器读取） |
| `AGENT_EXECUTOR_IDEMPOTENCY_MAX_ENTRIES` | `512`（executor 容器读取） |

更新 `.env.example`、`server/README.md`、`agent-loop/README.md`。

**默认值取舍**：重试默认开启但只作用于 `effect="read"` 的工具（Read / Glob / Grep / 记忆读取），瞬时故障下收益明确、副作用为零；`WebFetch` / `WebSearch` 需要显式声明才重试；写类与 shell 默认关闭。熔断默认开启，只在连续 5 次瞬时失败后生效，误伤面小。

---

## 10. 可观测性

1. **`ToolResult.metadata`**（不进入模型上下文）：

   ```json
   {
     "attempts": 2,
     "duration_ms": 1830,
     "retry_errors": [{"type": "TimeoutError", "kind": "transient", "message": "..."}],
     "retry_delays_ms": [420],
     "breaker": "closed"
   }
   ```

2. **tool span**：`AgentToolResult` 增加 `metadata` 字段，`server/app/agent/spans.py:474` 的 `_on_tool_result` 把 `attempts`、`retry_error_count`、`breaker_state`、`duration_ms` 写进 `attributes`。不新增 span 类型、不做迁移。
3. **trace 记录**：`runner.py:704` 的 `tool.result` 事件继续只写现有字段；`attempts` 不进入 `agent_run_events`（避免改 check constraint）。
4. **日志**：熔断 open / half-open / recovered 用 `logger.warning`；每次重试用 `logger.info` 带 tool 名与原因类型（不记录参数）。
5. **Dashboard**：本阶段不改。若后续要在 UI 上展示「重试了 2 次」，需要新增 `tool.retry` 事件 → migration 018 已用于 `client_run_id` 索引，届时需 019 扩 event type check，另行评估。

---

## 11. 文件变更清单

### agent-loop

| 文件 | 变更 |
|---|---|
| `core/exceptions.py` | `TransientToolError`；`ToolExecutionError.kind / cause_type / retry_after` |
| `core/config.py` | §9.1 的 10 个字段 + 校验（attempts ≥ 1、breaker 阈值 ≥ 1） |
| `core/types.py` | `ToolResult.metadata` 已有，无需改 |
| `tools/errors.py`（新） | `ErrorClass`、`classify_tool_error` |
| `tools/retry.py`（新） | `RetryPolicy`、`_backoff_delay`、`RetryDecision` |
| `tools/breaker.py`（新） | `ToolCircuitBreaker`、`reset_tool_breakers` |
| `tools/registry.py` | `Tool.retry`、`tool()` 透传、`CURRENT_IDEMPOTENCY_KEY`、`ToolRegistry.begin_run/end_run`、`invoke` 重试循环 + 熔断检查、`invoke_batch/_invoke_parallel` 透传 `cancel_event` |
| `agent/runtime.py` | run 开始时 `tools.begin_run(uuid4().hex)` 并在 `finally` 清理；把 `AgentConfig` 的重试/熔断参数传给 registry |
| `README.md` | 公共 API（`RetryPolicy`、`tool(retry=...)`、`TransientToolError`）说明 |
| `tests/test_tool_retry.py`（新） | 见 §13 |
| `tests/test_tool_breaker.py`（新） | 见 §13 |

### server

| 文件 | 变更 |
|---|---|
| `app/agent/dev_tools.py` | `_atomic_write_text`；`Write` / `Edit` / `NotebookEdit` 替换写入；WebFetch/WebSearch 的瞬时错误与 429 改用 `TransientToolError`；`WebFetch` / `WebSearch` 声明重试；Bash/PowerShell/Monitor 传 `idempotency_key` |
| `app/agent/executor.py` | `TransientToolError`；`idempotency_key` 参数 |
| `app/agent/executor_app.py` | `ExecRequest.idempotency_key`；`MonitorRequest.idempotency_key`；幂等缓存 |
| `app/agent/runtime.py` | `LocalAgentConfig` 新字段 + env 读取；`build_runtime` 传给 `AgentConfig` |
| `app/agent/events.py` | `AgentToolResult.metadata: dict \| None = None` |
| `app/agent/spans.py` | tool span attributes 写 attempts / breaker |
| `app/models.py` | 无列变更（索引在迁移里） |
| `alembic/versions/018_agent_runs_client_run_id_unique.py`（新） | 去重 + 部分唯一索引 |
| `app/routers/agent.py` | `(user, client_run_id)` 命中 → 返回已有 run；`BackgroundTurn.deduplicated` |
| `tests/` | 见 §13 |

### docs

| 文件 | 变更 |
|---|---|
| `docs/agent-loop/tool-execution.md` | §8.4 标记为「已按本方案实现」，补 §8.5 错误分类/重试/熔断/幂等细节 |
| 本文件 | 实施后更新状态与实测数据 |
| `docs/building-problem-record/problem-record.md` | P-23 状态、解决时间、解决措施、解决后效果（按仓库约定，落地后补） |
| `docs/server/`（REST） | `POST /agent` 的 `deduplicated` 字段与重放语义 |

---

## 12. 分阶段实施

| 阶段 | 内容 | 可独立合并 | 验收 |
|---|---|---|---|
| **P0** | 错误分类 + `TransientToolError` + `ToolExecutionError` 属性；`classify_tool_error` 单测 | 是，零行为变化 | 现有测试全绿；分类表用例通过 |
| **P1** | `RetryPolicy` + registry 重试循环 + `cancel_event` 透传 + `AgentConfig` 配置 + server env；`WebFetch`/`WebSearch` 声明 | 是 | agent-loop `hatch run test` / `lint`；server `pytest` / `ruff`；无网络单测 |
| **P2** | 熔断器 + span/metadata 可观测 | 是（依赖 P0） | 状态机单测；tool span 断言 |
| **P3** | 原子写 + `/exec`、`/monitor start` 幂等键 + `CURRENT_IDEMPOTENCY_KEY` | 是（与 P1 可并行） | 原子写故障注入用例；executor 去重用例 |
| **P4** | `client_run_id` 唯一索引 + `POST /agent` 重放 + 文档 | 是（server-only） | 迁移升降级；并发重复请求返回同一 run |

建议合并顺序：**P0 → P1 + P3 → P2 → P4**。P3 先于或并行于 P1，才能安全评估把 `Bash` 加入重试白名单（本方案不默认开启）。P4 与其余部分无依赖，可随时插队。

---

## 13. 测试与验收

全部单测禁止真实网络：沿用 `FakeProvider` / `InMemorySessionStore`，工具用可控的假实现与可控时钟。

### agent-loop

| 用例 | 断言 |
|---|---|
| `transient error retried then succeeds` | 工具被调用 2 次；`TOOL_RESULT` 只有 1 条；`metadata.attempts == 2`；`is_error=False` |
| `permanent error not retried` | `ValueError` 只调用 1 次；`metadata` 无重试字段 |
| `retry budget exhausted` | 3 次后返回错误；`retry_exhausted=True`；`retry_errors` 长度 2 |
| `retry shares one progress-guard entry` | 手动记录最终结果；`guard.stalled_calls` 只加 1 |
| `explicit policy overrides auto` | `effect="write"` 且 `retry=RetryPolicy(max_attempts=2)` 会重试 |
| `write tool auto-disabled` | `Write` 瞬时失败只调用 1 次 |
| `cancel during backoff` | `cancel_event` 在等待中置位 → 立即返回 `aborted`；工具只调用 1 次 |
| `raise mode propagates last error` | `error_mode="raise"` 抛 `ToolExecutionError(kind="transient")` |
| `backoff formula` | 纯函数：上界、jitter 范围、`retry_after` 下限 |
| `breaker opens/half-opens/closes` | 连续 5 次 transient → 第 6 次快速失败且工具未被调用；cooldown 后探测成功 → closed |
| `breaker ignores permanent errors` | 连续 `ValueError` 不开熔断 |
| `breaker failure counted once per logical call` | 一次 3 次尝试的重试耗尽只 +1 |
| `breaker shared across runtimes` | 两个 registry 同名工具共享状态 |

### server

| 用例 | 断言 |
|---|---|
| `atomic write survives failure` | monkeypatch `os.replace` 抛错 → 原文件内容不变、无残留 tmp |
| `write preserves mode` | 0o640 文件覆盖后仍为 0o640 |
| `exec idempotency caches response` | 同 key 两次 → 底层 runner 只执行 1 次，响应相同 |
| `exec without key runs twice` | 无 key 保持原行为 |
| `exec failure not cached` | 连接失败不写缓存，下一次重试会再次执行 |
| `monitor start deduplicated` | 同 key 只启动一个 monitor |
| `client_run_id replay` | 同用户同 key 两次 POST → 同一 run id / conversation id / assistant id；`deduplicated=true` |
| `client_run_id race` | 并发两次 → 一个 200 重放，一个创建成功；库中 1 行 |
| `client_run_id different users` | 不同用户相同 key 各自创建 |
| `migration upgrade/downgrade` | 018 升降级可重复执行；重复数据被清理 |
| `tool span attributes` | `attempts` / `breaker_state` 写进 span attributes |

### 命令

```bash
cd agent-loop && hatch run test && hatch run lint
cd server && pytest tests/ -q && ruff check app tests
```

Dashboard 无改动，不需要 `pnpm build`；REST 契约变更需跑 server 侧契约测试。

---

## 14. 风险、回退与待确认

### 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| `effect="read"` 的工具实际上有副作用（例如未来新增的写接口被误标） | 重试放大副作用 | 复核所有 `effect="read"` 声明；文档强调 effect 是重试的安全边界；默认策略只信任 `read` |
| 退避占用 worker 线程 | 并行批次延迟 | `tool_concurrency` 上限 8；`max_elapsed` 15s；退避期间可被取消 |
| 熔断状态进程内共享 | 一个坏工具影响所有用户 short window | 只统计 transient；阈值 5、冷却 60s；可环境变量关闭 |
| 熔断键与 MCP 工具重名 | 误熔断 | registry 已禁止同名注册；MCP 工具带 `mcp__` 前缀 |
| `/exec` 幂等缓存内存占用 | 单进程内存增长 | TTL 600s + 512 条上限 + LRU |
| executor 重启丢缓存 | 极端情况下重复执行一次 | 本阶段接受；后续可换 Redis/文件存储 |
| `client_run_id` 历史重复数据 | 迁移失败 | 迁移先清理（保留最早一行，其余置 NULL）并打日志 |
| 429 内部重试改到框架层 | WebFetch 行为变化 | 同步修改 `tests` 中 rate-limit 用例；`Retry-After` 语义保持不变 |

### 回退

每个阶段都可用配置关闭，无需回滚代码：

```bash
AGENT_TOOL_RETRY_ENABLED=false
AGENT_TOOL_BREAKER_ENABLED=false
```

迁移 018 有完整 `downgrade()`；`deduplicated` 字段是加性的，旧客户端忽略即可。

### 实施决策（2026-11-16）

1. **重试/熔断默认开启**：`AGENT_TOOL_RETRY_ENABLED` / `AGENT_TOOL_BREAKER_ENABLED` 默认 `true`；重试只作用于 read 工具与显式声明的 `WebFetch` / `WebSearch`，误伤面已最小化。
2. **`client_run_id` 唯一性范围**：采用 `(created_by, client_run_id)`（每用户唯一）。
3. **`Bash` 未纳入重试**：`/exec` 幂等键已就绪，但「命令可能已执行」的语义仍需要单独评估，先观察 `WebFetch` / `WebSearch` 一个版本。
4. **不新增实时 `tool.retry` 事件**：重试详情进入 `ToolResult.metadata` 与 tool span attributes，避免 migration 019 与 Dashboard 改动；若产品需要在对话流里展示，单独排期。
