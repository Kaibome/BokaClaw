# Agent 执行与连接解耦：前端设计

> 状态：已实现（2026-11-04）
>
> 适用范围：`dashboard/`（Next.js 15 + React）Agent 聊天页
>
> 目标读者：前端开发者
>
> 契约来源：run 状态、消息状态、事件 payload、端点、错误码以 [`../server/agent-execution-decoupling.md`](../server/agent-execution-decoupling.md) 第 3、9 节为准。本文只描述前端如何消费契约，不重新定义契约。
>
> UI 基础结构见 [`agent-chat-design.md`](agent-chat-design.md)。

## 1. 背景与目标

旧前端把一次 Agent 执行绑在 `POST /agent` 的 reader 上：

- 关闭标签页、切换会话、网络中断都会终止 reader；服务端在 legacy 模式下会把本轮标记为取消。
- 没有 run id、没有事件游标；刷新后只能看到最终落库的消息，看不到进行中的文本与工具轨迹。
- Stop 只能 `AbortController.abort()`，无法区分“停止执行”和“停止接收”。

新前端把执行与订阅分开：

| 场景 | 行为 |
| --- | --- |
| 网络抖动 | reader 失败后按退避重连，带 `after_seq` 续传 |
| 刷新页面 | 读取会话详情的 `active_run`，从 seq 0 重放 |
| 切换会话 | 只关闭本地订阅，服务端 run 继续 |
| 关闭标签页 | 只关闭本地订阅，服务端 run 继续 |
| 第二个标签发送 | 服务端 409；前端订阅已有 run，不重复发送 |
| Stop | 调用 cancel 接口，继续消费到 `error + done` |
| 服务端重启 | 重连看到 `interrupted` 终态；展示 `INTERRUPTED` |
| 工具调用 | 实时渲染在助手气泡下方，刷新后从消息 metadata 恢复 |

目标不是“永不掉线”，而是**任何一次断线都能从事件日志续上，且断线本身不终止执行**。

## 2. 共享契约摘要（非权威）

以下只列前端实际消费的部分。完整定义见 [`../server/agent-execution-decoupling.md`](../server/agent-execution-decoupling.md) 第 3、9 节。

### 2.1 状态

```text
RunStatus:        queued | running | completed | failed | cancelled | interrupted
MessageStatus:    completed | cancelled | failed
ToolTraceStatus:  running | completed | failed | aborted
ConnectionState:  idle | connecting | open | reconnecting | closed   # 前端本地状态
```

终态：`completed | failed | cancelled | interrupted`。活跃：`queued | running`。

### 2.2 事件

| 事件 | 前端用途 |
| --- | --- |
| `meta` | 取 `conversation`、`run_id`、`status`、`user_message`、`assistant_message_id`；终态时可能带 `message` |
| `message.delta` | 追加正文 |
| `tool.call` | 新增/更新工具条目（`status=running`） |
| `tool.result` | 结算工具条目（`completed/failed/aborted`） |
| `message.completed` | 用服务端消息替换本地气泡，优先使用 `metadata.tool_trace` |
| `error` | 终态错误；按 `code` 映射为 cancelled/interrupted/failed |
| `done` | 最终状态；读取 `status`，不是“成功”的同义词 |

`meta` / `done` 没有 `seq`；持久化事件带 `id: <seq>`。

### 2.3 端点

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/agent` | 创建 run 并订阅首条流 |
| `GET` | `/agent/runs/{id}/events?after_seq=N` | 重连/恢复订阅 |
| `GET` | `/agent/runs/{id}` | 轮询兜底，只读 `status` |
| `POST` | `/agent/runs/{id}/cancel` | Stop |
| `GET` | `/conversations/{id}` | 刷新恢复，读 `active_run` 与已落库消息 |
| `DELETE` | `/conversations/{id}` | 删除会话；活跃 run 时 409 |

### 2.4 错误码

前端显式分支处理：

| code / HTTP | 处理 |
| --- | --- |
| `409 agent_run_active` | `POST`：删除乐观气泡、保留草稿、订阅 body 中的 `run_id`；`DELETE`：提示先停止/等待 |
| `404 agent_run_not_found` | 标记 run 过期，清游标，重新加载会话 |
| `410 agent_events_expired` | 标记 run 过期，清游标，重新加载最终消息 |
| `403` | 停止本地订阅，显示无权限 |
| `401` | 停止本地订阅（由全局鉴权处理） |
| `agent_not_configured` | 显示固定配置提示 |
| `agent_cancelled` | 消息标记为 `cancelled`，不显示错误横幅 |
| `agent_interrupted` | 消息标记为 `interrupted`，显示 `INTERRUPTED` |
| 其他 `error` | 显示服务端 `detail` 或通用文案 |

## 3. 类型与状态模型

### 3.1 类型

`src/types/api.ts`：

```ts
export type RunStatus =
  | "queued" | "running" | "completed" | "failed" | "cancelled" | "interrupted";

export type ConnectionState = "idle" | "connecting" | "open" | "reconnecting" | "closed";

export interface ActiveRun {
  id: string;
  status: RunStatus;
  last_seq: number;
  assistant_message_id: string | null;   // 可空
  cancel_requested_at: string | null;
  created_at: string;
}

export interface AgentRun {
  id: string;
  conversation_id: string;
  status: RunStatus;
  error_code: string | null;
  error_detail: string | null;
  last_seq: number;
  partial_content: string;
  assistant_message_id: string | null;
  cancel_requested_at: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export type ToolTraceStatus = "running" | "completed" | "failed" | "aborted";

export interface ToolTraceEntry {
  tool_call_id: string;
  name: string;
  iteration?: number;
  status: ToolTraceStatus;
  arguments?: Record<string, unknown> | null;
  raw_arguments?: string | null;
  arguments_error?: string | null;
  content?: string;
  is_error?: boolean;
  duration_ms?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  truncated?: boolean;
}
```

`ConversationDetail` 增加 `active_run?: ActiveRun | null`。

`ChatMessageView` 是 `ChatMessage` 的本地视图，额外字段：

```ts
interface ChatMessageView extends ChatMessage {
  streaming?: boolean;
  reconnecting?: boolean;
  failed?: boolean;
  cancelRequested?: boolean;
  runStatus?: RunStatus | null;
  toolTrace?: ToolTraceEntry[];
}
```

### 3.2 state

`useAgentChat()` 暴露/维护：

| state | 类型 | 用途 |
| --- | --- | --- |
| `conversations` | `Conversation[]` | 侧栏列表 |
| `activeId` | `string \| null` | 当前会话 |
| `activeConversation` | `Conversation \| null` | 派生 |
| `messages` | `ChatMessageView[]` | 当前线程 |
| `isLoadingConversations` / `isLoadingMessages` | `boolean` | 加载态 |
| `isStreaming` | `boolean` | 派生：`messages.some(m => m.streaming)` |
| `connection` | `ConnectionState` | 订阅状态；页面当前未展示 |
| `runStatus` | `RunStatus \| null` | 当前 run 状态；页面当前未展示 |
| `cancelRequested` | `boolean` | Stop 已发出、等待终态 |
| `activeRunId` | `string \| null` | 当前订阅的 run；页面当前未展示 |
| `isSending` | `boolean` | 防重复发送 |
| `error` | `string \| null` | 错误横幅 |

页面（`page.tsx`）只解构 `isStreaming`、`cancelRequested`、`error`、`send`、`stop` 等；`connection` / `runStatus` / `activeRunId` 已返回但未渲染。

### 3.3 refs

| ref | 作用 |
| --- | --- |
| `activeIdRef` | 最新会话 id，避免闭包旧值 |
| `runIdRef` | 当前 run id；恢复/重连的唯一依据 |
| `lastSeqRef` | 已消费的最大 `seq`；重连游标 |
| `assistantIdRef` | 当前助手气泡 id（服务端 `assistant_message_id`） |
| `connectionRef` | 最新连接状态 |
| `abortRef` | 当前 reader 的 `AbortController` |
| `cursorKeyRef` | 当前会话的 sessionStorage key |
| `retryTimerRef` | 退避定时器 |
| `watchdogRef` | 45s 无事件看门狗 |
| `pollTimerRef` | 5s 轮询定时器 |
| `pollingRef` | 是否处于轮询模式 |
| `consecutiveFailuresRef` | 连续重连失败次数 |
| `terminalRef` | 是否已进入终态 |
| `sendingRef` | 是否已有一条发送中的流 |
| `cancelRequestedRef` | 最新 Stop 状态 |
| `runStatusRef` | 最新 run 状态 |
| `lastEventAtRef` | 最后一次收到 chunk 的时间 |
| `epochRef` | reader 代号；旧 reader 的事件按代号丢弃 |
| `staleRunIdsRef` | 已确认过期/不存在的 run id，避免重复恢复 |

## 4. 发送与订阅

### 4.1 `send(text)`

1. `trim` 后为空直接返回；`sendingRef` 为 true 时返回草稿，不重复发送。
2. 如果 `runIdRef` 已有值：说明本标签已有一条流，直接 `connect(runId, lastSeq)` 观察，返回 `{ok:false, draft}`。
3. 清空错误；生成临时 `temp-user-<stamp>` / `temp-assistant-<stamp>`，插入乐观气泡。
4. 重置 `terminalRef=false`、`runIdRef=null`、`lastSeq=0`、`runStatus=null`、连接状态等。
5. `postStream(AGENT_ENDPOINTS.INVOKE, {conversation_id, message, stream:true})`。
6. 成功后异步 `consumeRun(response, controller, epoch)`；`send` 立即返回 `{ok:true}`（不等待整轮结束）。

### 4.2 从 `meta` 建立 run

`handleEvent("meta")`：

- `consecutiveFailures=0`，停止轮询，清退避定时器，连接状态 `open`。
- 若带 `conversation`：更新 `activeId` 与侧栏列表。
- 若带 `run_id`：写入 `runIdRef` / `activeRunId`，并设置 `cursorKey = bokaclaw.agent.run.v1:<conversationId>`。
- 若带 `status`：写入 `runStatus`。
- 若带 `user_message`：用服务端消息替换 `temp-user-*`；若找不到临时用户消息，则插入到助手气泡之前。
- 若带 `assistant_message_id`：确保对应助手气泡存在。
- 若带 `cancel_requested_at`：恢复 `cancelRequested`。当前服务端 `meta` 不提供该字段，这段是防御性读取；刷新恢复时改从 `active_run.cancel_requested_at` 获取。
- 若 `status` 是终态且带 `message`：用服务端消息替换气泡、`finishRun(status)`，本次订阅结束。
- 若 `status` 是终态但没有 `message`：把 streaming 气泡标记为非流式，等待后续 `done` 收尾。

### 4.3 重连

`connect(runId, afterSeq)`：

1. `terminalRef` 为 true 直接返回。
2. abort 上一个 reader；创建新 `AbortController`；`epoch = ++epochRef`。
3. 连接状态从 `idle/closed` 进入 `connecting`。
4. 记录 `lastEventAt=now`，启动 45s 看门狗。
5. `getStream(AGENT_ENDPOINTS.RUN_EVENTS(runId, afterSeq))`。
6. 正常读到流结束但没收到 `done`：`consecutiveFailures++`，安排重连。
7. 异常：404 / 410 / 403 / 401 分支处理；其他情况同样累计失败并安排重连。

`RUN_EVENTS(runId, afterSeq=0)` 生成 `/agent/runs/{id}/events?after_seq=N`。重连始终带 `lastSeqRef.current`，即最后成功消费的 `seq`。

### 4.4 409：已有活跃 run

`POST /agent` 返回 409 时：

- 删除两个临时气泡；`sending=false`；重置 run 状态；保留草稿。
- Toast：`A reply is still in progress` / `Your message was not sent...`。
- 从 `err.data.run_id` 读取已有 run；若存在，重置 `lastSeq=0`，`runIdRef=run`，`connect(run, 0)`。
- 返回 `{ok:false, draft:text}`，输入框保留原文。

这条路径让第二个标签能直接看到进行中的输出，而不是只看到错误。

## 5. 事件归并

### 5.1 去重

```ts
const seq = Number(event.id ?? payload?.seq ?? 0);
if (!Number.isFinite(seq) || seq <= 0) return true; // meta/done 没有 id
if (seq <= lastSeqRef.current) return false;
lastSeqRef.current = seq;
persistCursor();
return true;
```

- 以 SSE `id` 为准，回退到 payload `seq`。
- 小于等于已消费游标的事件直接丢弃；重连回放与实时事件重叠时不会重复追加。
- `meta` / `done` 没有 `id`，不参与去重。
- `done` 不按 seq 去重，依赖 `terminalRef` 防止重复收尾。

### 5.2 逐事件

| 事件 | 处理 |
| --- | --- |
| `meta` | 见 4.2 |
| `message.delta` | 确保助手气泡存在；`content += delta`；`streaming=true`；`runStatus="running"` |
| `tool.call` | 构造 `ToolTraceEntry(status="running", arguments, raw_arguments, arguments_error, started_at)`；按 `tool_call_id` upsert |
| `tool.result` | 构造结果条目（`status`、`content`、`is_error`、`duration_ms`、`finished_at`）；upsert 时保留 `tool.call` 的参数字段 |
| `message.completed` | `toView(finalMessage)` 替换气泡；优先 `metadata.tool_trace`，否则保留本地轨迹；`runStatus="completed"`、`streaming=false` |
| `error` | 按 `code` 映射状态；`agent_cancelled` 不显示错误横幅；`partial=false` 时删除空占位气泡；不直接 `finishRun`，等 `done` |
| `done` | `finalStatus = payload.status ?? runStatusRef`；非终态则回退 `"completed"`；调用 `finishRun(finalStatus)` |

### 5.3 工具条目 upsert

`upsertToolTrace` 以 `tool_call_id` 为键：

- 不存在则追加。
- 已存在则合并：`tool.result` 不带 `arguments/raw_arguments/arguments_error/started_at`，合并时保留 `tool.call` 的值。
- 已终态条目不会被后来的 `running` 覆盖。
- `tool.result` 先于 `tool.call` 到达时也能创建条目；后续 `tool.call` 不会把状态退回 `running`。

`finalizeRunningToolTrace` 在 `error` / `done` / `finishRun` / 本地停止时把仍为 `running` 的条目改为 `aborted`，避免 UI 永远转圈。

### 5.4 `done` 不是成功信号

- `done.status` 才是最终状态；前端读取 `payload.status`，缺失时用 `runStatusRef`。
- 如果两者都不是终态（异常服务端/旧版本），回退为 `"completed"`。
- `finishRun` 会清理 `runId`、游标、定时器、订阅，并刷新侧栏。

### 5.5 `message.completed` 与 `done` 的顺序

服务端保证 `message.completed` 先于 `done`。前端：

- `message.completed` 只更新气泡，不结束订阅。
- `done` 才执行 `finishRun`。
- 如果只收到 `done` 而没有 `message.completed`，气泡保留已流式拼接的文本；服务端最终消息仍可通过刷新恢复。

## 6. 重连、看门狗与轮询

### 6.1 触发条件

- reader 抛错。
- reader 正常结束但 `terminalRef` 仍为 false（没有收到 `done`）。
- 看门狗 45 秒没有收到任何 chunk。

### 6.2 退避

`BACKOFF_MS = [1000, 2000, 4000, 8000, 15000]`：

- 第 1 次失败用 1s，第 2 次 2s，第 3 次 4s，第 4 次 8s，第 5 次及以后 15s。
- 每档乘以 `0.8 + random * 0.4`，即 ±20% 抖动。
- 退避期间助手气泡显示 `RECONNECTING…`。
- `connect` 成功收到 `meta` 后失败计数清零。

### 6.3 轮询兜底

连续失败达到 `MAX_RECONNECT_FAILURES = 8` 后：

- 停止 SSE 重连，进入轮询模式。
- 每 5 秒 `GET /agent/runs/{runId}`。
- 只读取 `status`：
  - 终态 → `finishRun(status)`。
  - 活跃 → 更新 `runStatus`，继续轮询。
  - 404 → `handleRunGone()`。
- 轮询**不读取** `partial_content`，不重新拉消息；它只负责在 SSE 长期不可用时发现终态。
- 因此轮询恢复的会话可能只有本地已收到的内容；完整内容需要刷新页面（走 `active_run` 重放或已落库消息）。

### 6.4 看门狗

- `armWatchdog(epoch)` 设置 45s 定时器；每收到一个 chunk 重置。
- 超时后：`consecutiveFailures++`，`scheduleReconnect()`。
- 旧 reader 由下一次 `connect` 的 `abortRef.current?.abort()` 关闭。
- 定时器在 `meta`、重连、终态、停止时清理。

### 6.5 页面可见性与网络恢复

只监听两个事件：

- `window.online`：如果当前是 `reconnecting` 或轮询中，立即 `retryNow()`（清零失败计数、重新 connect）。
- `document.visibilitychange`：页面变为可见且当前 run 未终态时，如果处于 `reconnecting` / 轮询，或连接状态是 `open` 但超过 45s 没有事件，立即 `retryNow()`。

没有 `offline` 监听。断网时不会立刻切换为 `reconnecting`；要等 reader 报错或看门狗超时。

### 6.6 404 / 410 / 403 / 401

| 状态 | 处理 |
| --- | --- |
| 404 | `handleRunGone()`：标记 run 过期、清游标、`stopLocalRun("closed")`、Toast、重新加载会话 |
| 410 | `handleEventsExpired()`：同上，Toast 说明历史已过期，展示最终保存消息 |
| 403 | `handleForbidden()`：停止本地订阅，显示无权限 |
| 401 | `stopLocalRun("closed")`；不额外提示，交给全局鉴权 |

`staleRunIdsRef` 记录已确认过期的 run；刷新会话时遇到同一个 run 不会再次自动重放。

## 7. Stop、断连与取消

### 7.1 Stop（已有 run id）

```ts
if (cancelRequestedRef.current) return;
cancelRequestedRef.current = true;
setCancelRequested(true);
mark streaming bubbles cancelRequested=true;
await api.post(AGENT_ENDPOINTS.RUN_CANCEL(runId));
```

- 不 abort reader，继续消费到 `error(agent_cancelled)` + `done`。
- cancel 接口返回时 run 通常仍是 `running`，只是 `cancel_requested_at` 已写；UI 显示 `STOPPING…`。
- 重复点击直接忽略。
- 404 视为“已经终态”，保留订阅等 `done`。
- 其他失败：清 `cancelRequested`、Toast，可重试。

### 7.2 Stop（还没有 run id）

只有 `POST /agent` 尚未返回 `meta`（或 legacy 服务端不返回 `run_id`）时：

- `closeSubscription()` abort reader。
- 本地把助手气泡标记为 `cancelled`，`runStatus="cancelled"`。
- 服务端 run 可能仍在执行；前端不再接收。
- 这是回退路径，不是后台模式的正常 Stop。

### 7.3 断连

| 动作 | 前端行为 | 服务端 run |
| --- | --- | --- |
| 切换会话 | `selectConversation` 关闭订阅、清空线程、重置状态 | 继续 |
| 新建会话 | `startNewChat` 关闭订阅、重置状态 | 继续 |
| 组件卸载 | `useEffect` cleanup `closeSubscription()` | 继续 |
| 浏览器断网 | reader 报错/看门狗触发 `scheduleReconnect` | 继续 |
| 关闭标签页 | 连接断开，无前端逻辑 | 继续 |

断连不调用 cancel 接口。

### 7.4 重试按钮

`MessageBubble` 在非流式、非重连、且终态为 `interrupted` / `failed` / `cancelled` 时显示 Retry：

- 页面找到该助手消息之前最近的一条用户消息，调用 `send(previousUser.content)`。
- 这会创建**新的 run**；旧 run 不会被复用或续跑。
- 这依赖服务端“一个会话一个活跃 run”：若旧 run 仍活跃，会收到 409 并转为订阅已有 run。

## 8. 刷新、多标签与恢复

### 8.1 刷新恢复

`loadMessages(conversationId)`：

1. `GET /conversations/{id}`。
2. 读 `active_run`。若存在、状态是 `queued/running`、且不在 `staleRunIdsRef`：
   - 如果当前已经连着同一个 run（`runIdRef === active_run.id`、未终态、有 abort controller），只确保助手气泡存在，不重复连接。
   - 否则用已落库消息构造视图；若 `assistant_message_id` 已有消息，标记 `streaming=true`；没有则插入空占位气泡。
   - `assistantIdRef = active_run.assistant_message_id`。
   - `runIdRef = active_run.id`，`lastSeq = 0`。
   - `runStatus = active_run.status`。
   - `cancelRequested = active_run.cancel_requested_at !== null`。
   - `connect(active_run.id, 0)`：**从 seq 0 完整重放**。
3. 没有活跃 run：清游标，直接用 `detail.messages` 渲染，重置所有 run 状态。

为什么从 0 而不是从游标：

- 助手正文在流式过程中没有写入 `conversation_messages`，只有事件日志。
- 本地 `sessionStorage` 游标不可靠（见 8.3），且新标签页没有内存游标。
- 从 0 重放能保证正文与工具轨迹完整；事件表通常很小。
- 事件已过期时服务端返回 410，前端转为展示最终保存消息。

### 8.2 多标签

- 每个标签有独立的 `useAgentChat` 实例、独立 reader、独立 `epoch`、独立本地状态。
- 没有 BroadcastChannel / localStorage 协调。
- 两个标签都可以订阅同一个 run；服务端允许一个 run 多个订阅者。
- 第二个标签发送消息会收到 409，然后订阅第一个标签创建的 run。
- 两个标签同时点 Stop 是幂等的：第二个 cancel 请求返回当前终态或已取消状态。

### 8.3 sessionStorage 游标

`persistCursor()` 写入：

```json
{
  "v": 1,
  "conversationId": "0b2a...",
  "runId": "6f1c...",
  "assistantMessageId": "c9d3...",
  "lastSeq": 12,
  "updatedAt": "2026-11-04T10:00:00.000Z"
}
```

key 为 `bokaclaw.agent.run.v1:<conversationId>`。

现状：

- 每次 `acceptSeq` 成功后写入。
- `finishRun`、`handleRunGone`、`handleEventsExpired`、切换/新建/删除会话时删除。
- **从不读取**。刷新恢复完全依赖 `active_run` + 从 0 重放。
- 写入包在 `try/catch` 中，隐私模式/禁用存储不会影响功能。

### 8.4 会话切换与删除

- `selectConversation` 不取消服务端 run；它只关闭订阅。切回时会通过 `active_run` 重新订阅。
- `startNewChat` 同样不取消。
- `deleteConversation` 在活跃 run 时收到 409，Toast 提示先 Stop 或等待；不会强制删除。
- 删除成功后清游标；如果删除的是当前会话，进入新会话状态。

## 9. UI 结构

### 9.1 工具轨迹

`MessageBubble` 在助手内容气泡下方渲染 `ToolTrace`（仅非用户消息且 `toolTrace` 非空）：

- **默认只展示最新一条**（`entries[entries.length - 1]`），避免长工具链把回答挤出视野。
- 条目多于一条时，列表上方显示下拉按钮 `Show all N tool calls`；点击后按到达顺序展开全部条目，按钮文案变为 `Show latest only`，箭头 `ChevronDown` 旋转 180°。
- 展开状态是 `ToolTrace` 组件本地 `useState`，每条助手消息独立；流式过程中若用户未手动展开，新增工具调用仍只显示最新一条。
- 每个 `tool_call_id` 一行，使用 `Collapsible`。
- 收起时显示：状态图标、工具名、状态文案、耗时（`duration_ms`，如有）、展开箭头。
- 状态文案：`Running` / `Completed` / `Failed` / `Aborted`。
- 图标：running 旋转；completed 对勾；failed/aborted 警告圆。
- 展开显示：
  - `arguments_error`（如有）。
  - `ARGUMENTS`：`arguments` 格式化 JSON；为空时回退 `raw_arguments`；都没有时 `No arguments`。
  - `OUTPUT`：`content`；空且 running 时 `Waiting for result...`，否则 `No output`。
  - `truncated` 为 true 时显示 `Output truncated for display.`
- 不做按 iteration 分组，不做运行中自动展开（默认折叠，只显示最新一条），不提供复制按钮。
- 条目顺序是事件到达顺序，即 `tool.call` 的首次出现顺序；展开后从最早到最新排列。

### 9.2 状态标记

`MessageBubble` 的非用户消息标记：

| 条件 | 文案 |
| --- | --- |
| `reconnecting === true` | `RECONNECTING…` |
| `cancelRequested === true && streaming === true` | `STOPPING…` |
| `runStatus === "interrupted"` | `INTERRUPTED` + 警告图标 |
| `runStatus === "cancelled"` 或 `status === "cancelled"` | `STOPPED` |
| `runStatus === "failed"` 或 `failed === true` | `FAILED` + 警告图标 |
| 终态且非流式 | Retry 按钮（interrupted/failed/cancelled） |

### 9.3 Composer 与线程

- `chat-composer.tsx`：流式中显示 Stop；`cancelRequested` 时按钮禁用并显示加载，`aria-label="Stopping"`。
- `message-thread.tsx`：自动滚动采用“贴底才跟随”，用户上滚时不强制拉回；顶部显示错误横幅。
- `page.tsx`：只传 `isStreaming`、`cancelRequested`、`error`、`send`、`stop`；不展示 `connection` / `runStatus` / `activeRunId`。

## 10. 错误与边界

### 10.1 发送阶段

| 情况 | 行为 |
| --- | --- |
| 空输入 | 忽略 |
| 已有流 | 订阅已有 run，返回草稿 |
| 409 | 删除临时气泡，保留草稿，订阅已有 run |
| `agent_not_configured` | 固定文案，标记失败，删除空占位 |
| 其他 4xx/5xx | 错误横幅，标记失败，删除空占位，保留草稿 |
| 新会话创建失败 | 删除两个临时气泡（因为会话不存在） |

### 10.2 订阅阶段

| 情况 | 行为 |
| --- | --- |
| 无 `run_id` 的流结束 | 视为 legacy，`handleLegacyFailure` |
| 有 `run_id` 的流结束但无 `done` | 重连 |
| `error(partial=false)` | 删除空助手气泡 |
| `error(partial=true)` | 保留已流式正文/工具轨迹，标记终态 |
| `done` 缺少 `status` | 用 `runStatusRef`；仍非终态则回退 `completed` |
| 410 | 提示历史过期，展示最终消息 |

### 10.3 工具轨迹边界

- `tool.call` 参数被服务端截断时 `truncated=true`、`arguments=null`，UI 显示 `raw_arguments` 预览并提示截断。
- `tool.result` 的 `content` 被截断时同样提示。
- 服务端补齐的 `aborted` 条目会显示 `Aborted`。
- 若 `tool.result` 先到，条目仍会创建；后续 `tool.call` 不会把状态改回 running。

## 11. 已知限制与后续阶段

| 限制 | 现状 | 后续方向 |
| --- | --- | --- |
| sessionStorage 游标只写不读 | 刷新从 0 重放 | 若要减少重放量，可读游标并从 `lastSeq` 续接 |
| `meta.last_seq` 未使用 | 客户端只信 SSE `id`/payload `seq` | 可用于校准，但需处理缺口 |
| 轮询不恢复正文 | `pollRun` 只读 `status` | 轮询时拉取 `partial_content` 或消息 |
| meta 之前 Stop 只本地 abort | 服务端 run 继续 | 不返回 meta 时无法拿到 run id；需要服务端更早下发 |
| 无 `offline` 监听 | 依赖 reader 报错/看门狗 | 可加离线提示 |
| 页面未展示连接/运行状态 | `connection`/`runStatus`/`activeRunId` 未渲染 | 增加状态条 |
| 无多标签协调 | 各标签独立订阅 | BroadcastChannel 同步本地状态 |
| 工具轨迹无分组/复制 | 默认折叠显示最新一条，可 `Show all` 展开全部；按 `tool_call_id` 排列 | 按 iteration 分组、复制按钮另开 UI 任务 |
| `ActiveRun.assistant_message_id` 可空 | 无 id 时不插占位气泡，等 `message.delta` 创建 | 由服务端保证非空，或前端用 run id 生成临时 id |
| `MessageStatus` 与 run 状态不完全对齐 | 消息只有 `completed/cancelled/failed`；`interrupted` 只存在于 run | 展示层用 `runStatus` |
| 无测试运行器 | 验证靠 `pnpm typecheck` + `pnpm build` + 手工场景 | 引入组件/ hook 测试 |

后续阶段的服务端设计见 [`../server/agent-execution-decoupling.md`](../server/agent-execution-decoupling.md) 第 13 节。

## 12. 文件清单与手工验收

### 12.1 文件

| 文件 | 职责 |
| --- | --- |
| `src/hooks/use-agent-chat.ts` | 发送、订阅、事件归并、重连、轮询、Stop、恢复 |
| `src/utils/sse.ts` | `SseParser` / `SseMessage`；解析 `id`、`event`、`data` |
| `src/utils/api.ts` | `api`、`postStream`、`getStream`、`ApiStreamError` |
| `src/utils/api-endpoints.ts` | `INVOKE`、`RUN`、`RUN_EVENTS`、`RUN_CANCEL`、`CONVERSATION` |
| `src/types/api.ts` | `RunStatus`、`ConnectionState`、`ActiveRun`、`AgentRun`、`ToolTraceEntry` |
| `src/components/agent/message-bubble.tsx` | 内容气泡、工具轨迹、状态标记、Retry |
| `src/components/agent/tool-trace.tsx` | 工具条目折叠行 |
| `src/components/agent/message-thread.tsx` | 线程、自动滚动、错误横幅 |
| `src/components/agent/chat-composer.tsx` | 输入、Stop/Stopping |
| `src/app/(root)/dashboard/agent/page.tsx` | 页面装配、Retry 取上一条用户消息 |

### 12.2 手工验收

| 场景 | 期望 |
| --- | --- |
| 发送一条会调用工具的消息 | 气泡下方依次出现 Running → Completed 的工具行；耗时可展开；最终正文出现 |
| 流式中断网 10 秒再恢复 | 气泡显示 `RECONNECTING…`；恢复后正文与工具轨迹不重复、不缺段 |
| 流式中刷新页面 | 从 `active_run` 恢复，从 seq 0 重放，正文与工具轨迹完整 |
| 第二个标签发送 | 409 Toast；临时气泡被移除；草稿保留；自动订阅已有 run |
| 流式中 Stop | 按钮禁用并显示加载；最终 `STOPPED`；消息状态 `cancelled`；无错误横幅 |
| Stop 后立即刷新 | 显示已落库的 `cancelled` 消息；`active_run` 为 null 或非活跃 |
| 服务重启 | 刷新/重连后显示 `INTERRUPTED`；Retry 可重新发送 |
| 删除有活跃 run 的会话 | 409 Toast；取消或等待后可删除 |
| 事件过期（410） | 提示历史过期；展示最终保存消息 |
| 切会话再切回 | 执行继续进行；切回后恢复订阅 |
| 关闭标签页再打开 | 会话详情返回 `active_run`；从 0 重放；执行未中断 |

### 12.3 静态验证

```bash
cd dashboard
pnpm typecheck
pnpm build
```
