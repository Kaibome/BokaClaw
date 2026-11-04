# Dashboard：Agent 执行与连接解耦设计

> 状态：已实现（Phase 1，2026-11-04）
>
> 适用范围：`dashboard/` 前端（`/dashboard/agent`）
>
> 目标读者：前端开发者
>
> 契约来源：状态值、事件名、端点、错误码以 [`../server/agent-execution-decoupling.md`](../server/agent-execution-decoupling.md) 第 3、9 节为准。本文只描述前端如何消费这份契约，不重新定义它。

## 1. 背景与现状差异

当前 `dashboard/src/hooks/use-agent-chat.ts` 把一次 Agent 执行完全绑定在一次 `POST /agent` 的 SSE 读取上：

- `send()` 用 `postStream()` 发起请求，`reader.read()` 循环消费事件。
- `stop()` 只调用 `abortRef.current?.abort()`；`AbortController` 同时表达"用户停止"和"组件卸载/切会话"。
- `handleEvent()` 处理 `meta`、`message.delta`、`tool.call`、`tool.result`、`message.completed`、`error`、`done`，但不读取 `event.id`。
- 流异常结束时，本地把气泡标为 `failed` 或 `cancelled`；没有断线重连，也没有"后台继续执行"的概念。
- 刷新页面后，未完成的助手消息不在 `conversation_messages` 里，前端无法恢复。

新设计下：

| 场景 | 现状 | 目标 |
| --- | --- | --- |
| 关页面/切网络 | 服务端取消执行 | 服务端继续，前端可回来续接 |
| Stop | `abort()`，服务端标记 cancelled | 调用 cancel 接口，继续消费到 `done` |
| 刷新 | 丢失进行中的输出 | 从 `active_run` 恢复订阅 |
| 多标签 | 第二个发送会并发 | 409 后保留草稿并订阅现有 run |
| 事件去重 | 无 cursor | 用 SSE `id`/`seq` 去重与续传 |

## 2. 共享契约（摘要）

完整定义见 [`../server/agent-execution-decoupling.md`](../server/agent-execution-decoupling.md) 第 3 节。前端需要固定记住以下内容：

### 2.1 状态

```text
run:     queued | running | completed | failed | cancelled | interrupted
message: completed | cancelled | failed
```

`interrupted` 只存在于 run 状态；落库消息是 `failed`，metadata 带 `run_status=interrupted`。

### 2.2 事件

持久化事件（带 `id: <seq>`）：`message.delta`、`tool.call`、`tool.result`、`message.completed`、`error`。

合成事件（无 `id`）：`meta`（每次订阅开始）、`done`（最后一条）。

所有事件 payload 带 `run_id`、`conversation_id`、`seq`。终态顺序：

- 成功：`message.completed` → `done`
- 失败/取消/中断：`error` → `done`

### 2.3 端点

```text
POST /agent
GET /agent/runs/{run_id}
GET /agent/runs/{run_id}/events?after_seq=N
POST /agent/runs/{run_id}/cancel
GET /conversations/{id}          # 增加 active_run
```

`after_seq` 排他；SSE `id` 是十进制 `seq`；也接受 `Last-Event-ID` 头，但前端统一用查询参数。

### 2.4 错误码

`agent_run_active`（409）、`agent_run_not_found`（404）、`agent_events_expired`（410）、`agent_cancelled`、`agent_interrupted`（SSE `error`）。

## 3. 前端状态模型

### 3.1 类型

```ts
export type RunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type ConnectionState =
  | "idle"        // 没有活跃 run
  | "connecting"  // 首次订阅中
  | "open"        // 正在接收事件
  | "reconnecting"// 断线，等待重试
  | "closed";     // 已终态或放弃

export type BubbleStatus =
  | "streaming"
  | "reconnecting"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export interface ActiveRun {
  id: string;
  status: RunStatus;
  last_seq: number;
  assistant_message_id: string;
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
```

`MessageStatus` 保持 `"completed" | "cancelled" | "failed"` 不变。`ConversationDetail` 增加 `active_run?: ActiveRun | null`；`AgentInvokeResponse` 增加可选 `run_id`。

### 3.2 `ChatMessageView` 扩展

```ts
export interface ChatMessageView extends ChatMessage {
  streaming?: boolean;
  failed?: boolean;
  toolTrace?: ToolTraceEntry[];
  runStatus?: RunStatus;       // 仅助手气泡使用
  reconnecting?: boolean;      // 连接层状态，不影响消息终态
  cancelRequested?: boolean;   // Stop 已点击，等待服务端收尾
}
```

原则：

- `streaming` 表示"这段输出还在增长"，由 run 状态驱动。
- `reconnecting` 只表示"订阅暂时断开"，**不**把消息标成失败或取消。
- `runStatus` 只有服务端给出的终态才写入；本地推断只用于临时展示。

### 3.3 Hook 内部引用

```ts
const runIdRef = useRef<string | null>(null);
const lastSeqRef = useRef(0);
const connectionRef = useRef<ConnectionState>("idle");
const abortRef = useRef<AbortController | null>(null); // 只用于关闭订阅
const assistantIdRef = useRef<string | null>(null);
const cursorKeyRef = useRef<string | null>(null);
const retryTimerRef = useRef<number | null>(null);
const watchdogRef = useRef<number | null>(null);
```

与现状的区别：

- `abortRef` 不再表示"取消执行"，只表示"停止读取本地流"。
- `streamingRef` 拆成 `runIdRef + connectionRef`；`isStreaming` 由二者派生。
- `lastSeqRef` 是去重和续传的唯一游标。

### 3.4 sessionStorage

按标签页隔离，记录进行中的 run：

```ts
const CURSOR_VERSION = 1;
const cursorKey = (conversationId: string) =>
  `bokaclaw.agent.run.v${CURSOR_VERSION}:${conversationId}`;

interface RunCursor {
  v: 1;
  conversationId: string;
  runId: string;
  assistantMessageId: string;
  lastSeq: number;
  updatedAt: string;
}
```

规则：

- 收到 `meta.run_id` 后写入；每个带 `id` 的事件更新 `lastSeq`；终态、404、410、手动清除时删除。
- 只存游标，不存消息正文、不存 token。JWT 仍然只在内存（见 `dashboard/CLAUDE.md`）。
- `sessionStorage` 可能不可用（隐私模式、配额满）：所有读写包 `try/catch`，失败时退化为纯内存。
- 这份记录是"提示 + 诊断"，不是权威数据。**页面刷新后助手消息尚未落库，正文只能通过从 `after_seq=0` 回放重建**；不能只从保存的 `lastSeq` 续接，否则会缺前半段。只有当前 hook 实例仍有气泡内容时，才用内存里的 `lastSeqRef` 续接。

### 3.5 派生 UI 状态

```ts
const bubbleStatus: BubbleStatus =
  runStatus === "completed" ? "completed"
  : runStatus === "cancelled" ? "cancelled"
  : runStatus === "interrupted" ? "interrupted"
  : runStatus === "failed" ? "failed"
  : connection === "reconnecting" ? "reconnecting"
  : "streaming";
```

`queued` 与 `running` 都显示为"执行中"；`cancelRequested` 时显示"正在停止"。

## 4. 发送、订阅与事件归并

### 4.1 发送流程

```text
send(text)
  ├─ 若当前会话已有 runIdRef：不发送，转为 ensureSubscribed()
  ├─ 插入乐观 user/assistant 气泡
  ├─ POST /agent { stream: true }
  │    ├─ 409 agent_run_active → 本次消息未写入：
  │    │     复位 streamingRef/abortRef/isStreaming，移除乐观气泡、恢复草稿
  │    │     采用 body.run_id，ensureSubscribed(from=0) 观察现有 run
  │    └─ 200 → 读取 SSE
  ├─ meta：采纳 conversation/user_message/run_id/assistant_message_id/status/last_seq
  ├─ 持久化事件：按 seq 去重后归并
  └─ done：终态，清 cursor
```

`send` 仍然使用 `postStream`；它负责创建 run 并作为第一个订阅者。**不要**再为发送单独发一次 GET 订阅，否则会重复。

### 4.2 订阅流程

统一封装：

```ts
async function subscribe(runId: string, afterSeq: number, signal: AbortSignal) {
  const response = await getStream(
    AGENT_ENDPOINTS.RUN_EVENTS(runId, afterSeq),
    { signal },
  );
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    for (const event of parser.push(decoder.decode(value, { stream: true }))) {
      handleEvent(event);
    }
  }
  for (const event of parser.flush()) handleEvent(event);
}
```

`getStream` 是新增的 GET 版流式请求，复用 `postStream` 的 Bearer 注入与 401 刷新逻辑（见 8.2）。不能使用 `EventSource`，它无法携带 `Authorization`。

### 4.3 事件归并

`handleEvent` 只做纯状态更新，不做网络请求：

| 事件 | 处理 |
| --- | --- |
| `meta` | 采纳 `conversation`、`user_message`、`run_id`、`assistant_message_id`、`status`、`last_seq`；先 `adoptAssistantId` 重命名乐观气泡，若仍没有对应气泡则插入一个空的助手气泡；若 `status` 已终态且 `message` 非空，直接用最终消息替换气泡并结束 |
| `message.delta` | `seq <= lastSeqRef` 丢弃；否则追加文本，`lastSeqRef = seq` |
| `tool.call` | 去重后 `upsertToolTrace`，保留 arguments；`lastSeqRef = seq` |
| `tool.result` | 去重后更新条目；`lastSeqRef = seq` |
| `message.completed` | 用服务端完整消息替换气泡，`streaming=false`，`runStatus=completed`；`lastSeqRef = seq` |
| `error` | 记录 `errorCode`；按 code 映射 `cancelled` / `interrupted` / `failed`；保留已收到的正文与工具轨迹；**不立即清 cursor** |
| `done` | `runStatus = payload.status`；`streaming=false`；把仍为 running 的工具标为 `aborted`；清 cursor；关闭连接；刷新会话列表 |
| 心跳 `: ping` | 只重置 watchdog，不改变状态 |

去重函数：

```ts
function acceptSeq(event: SseMessage, payload: { seq?: number }): boolean {
  const seq = Number(event.id ?? payload.seq ?? 0);
  if (!Number.isFinite(seq) || seq <= 0) return true; // meta/done 无 seq
  if (seq <= lastSeqRef.current) return false;
  lastSeqRef.current = seq;
  persistCursor();
  return true;
}
```

注意：`meta` 和 `done` 没有 `id`，不要用它们推进 `lastSeq`。`meta.last_seq` 只用于校准显示，不直接写入 `lastSeqRef`，否则会跳过 replay 事件。

### 4.4 工具轨迹

复用现有 `normalizeToolTrace` / `upsertToolTrace` / `finalizeRunningToolTrace`。新增两点：

- 终态是 `interrupted`/`cancelled` 时，把所有仍为 `running` 的条目标为 `aborted`，`is_error=true`。
- 断线重连后，`tool.call`/`tool.result` 会按 seq 重放；去重逻辑保证不会重复插入。

### 4.5 完成后以谁为准

- 收到 `message.completed`：以该消息为准，替换本地气泡；metadata 里的 `tool_trace` 覆盖本地轨迹。
- 失败/取消/中断：服务端已把部分内容落库为 `failed`/`cancelled` 消息，但事件里没有完整消息。前端先保留本地拼接内容；`done` 后可以惰性调用 `loadMessages()` 校准（下一次打开会话也会以服务端为准）。
- 不要在 `done` 后立刻清空消息列表再重拉，否则会闪屏并丢失错误提示。

## 5. 断连与重连算法

### 5.1 什么算断连

- `reader.read()` 抛错（网络中断、服务端重启）。
- 流结束但没收到 `done`（代理超时、连接被截断）。
- 45 秒内没有收到任何事件或心跳（watchdog）。
- `visibilitychange` 从隐藏变可见，且距上次事件超过 45 秒。
- `online` 事件触发，且当前处于 `reconnecting`。

### 5.2 什么不算断连

- 用户切会话（主动关闭本地流，run 继续）。
- 组件卸载（关闭本地流，run 继续）。
- Stop 点击（继续消费，不重连）。

### 5.3 退避

```text
第 1 次：1s
第 2 次：2s
第 3 次：4s
第 4 次：8s
第 5 次及以后：15s
每档 ±20% 抖动
```

- 成功收到 `meta` 后重置退避。
- 连续 8 次失败后，切换到轮询兜底：每 5 秒 `GET /agent/runs/{run_id}`，直到终态；页面重新可见或 `online` 时回到 SSE。
- 所有定时器在组件卸载、会话切换、终态时清理。

### 5.4 重连伪代码

```ts
function scheduleReconnect() {
  if (!runIdRef.current || terminalRef.current) return;
  connectionRef.current = "reconnecting";
  setConnection("reconnecting");

  const delay = nextBackoff(); // 1s..15s + jitter
  retryTimerRef.current = window.setTimeout(async () => {
    if (!runIdRef.current || terminalRef.current) return;
    abortRef.current?.abort(); // 关闭上一次残留读取
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await subscribe(runIdRef.current, lastSeqRef.current, controller.signal);
    } catch (err) {
      if (controller.signal.aborted) return;
      if (err instanceof ApiStreamError) {
        if (err.status === 404) return handleRunGone();
        if (err.status === 410) return handleEventsExpired();
        if (err.status === 401) return handleAuthFailure();
      }
      scheduleReconnect();
    }
  }, delay);
}
```

`subscribe` 正常返回（没有 `done`）也要当作断连处理，调用 `scheduleReconnect()`。

### 5.5 恢复时从哪个 seq 开始

| 场景 | 起点 |
| --- | --- |
| 同一页面内网络抖动 | `lastSeqRef.current`（内存气泡已完整） |
| 切会话后返回，详情返回 `active_run` | `0`（本地气泡已卸载，必须重建） |
| 刷新页面/新标签，详情返回 `active_run` | `0`（助手消息尚未落库） |
| 收到 409 后订阅现有 run | `0` |
| 当前实例只重连、气泡还在 | `lastSeqRef.current` |

判定标准：**本地是否有这段 run 的完整文本**。有就续接，没有就从头回放。不要用 sessionStorage 的 `lastSeq` 在没有正文时续接。

## 6. Stop、断连与取消的区别

| 动作 | 触发 | 前端行为 | 服务端行为 |
| --- | --- | --- | --- |
| 断连 | 切会话、关页面、网络中断、组件卸载 | 关闭流；气泡显示 `reconnecting` 或保持原状；**不**标 cancelled | run 继续 |
| Stop | 用户点击停止 | `POST /agent/runs/{run_id}/cancel`；`cancelRequested=true`；继续消费到 `done` | run → `cancelled` |
| 重试 | 终态后点击重试 | 重新 `send` 同文本，创建新 run | 新 run |

实现规则：

```ts
const stop = useCallback(async () => {
  const runId = runIdRef.current;
  if (!runId) {
    abortRef.current?.abort(); // 兼容旧服务端：没有 run_id 时只能 abort
    return;
  }
  setCancelRequested(true);
  try {
    await api.post(AGENT_ENDPOINTS.RUN_CANCEL(runId));
  } catch (err) {
    if (err instanceof ApiStreamError && err.status === 404) {
      // run 已终态或被清理，保持订阅/恢复即可
    } else {
      toast({ title: "Failed to stop the agent", variant: "destructive" });
      setCancelRequested(false);
    }
  }
}, []);
```

关键点：

- **不要**在 `stop()` 里调用 `abortRef.current?.abort()`。那只会断开订阅，让用户以为停止了，实际后台还在跑。
- Stop 之后仍要接收 `error(agent_cancelled)` 和 `done`；此时 `done.status === "cancelled"`。
- 取消是协作式的，长工具调用不会立刻停；Stop 按钮显示"正在停止"，不要立即禁用整个输入框。
- 取消请求失败（网络）时提示用户重试，不要静默。
- 若服务端是旧版本（`meta` 没有 `run_id`）：退回 `abort()`，并保持现有 `cancelled` 展示。

## 7. 刷新、多标签与会话切换

### 7.1 刷新/重新打开

`loadMessages(conversationId)` 拿到 `ConversationDetail` 后：

```ts
if (detail.active_run) {
  ensureAssistantBubble(detail.active_run.assistant_message_id);
  runIdRef.current = detail.active_run.id;
  lastSeqRef.current = 0;
  connectionRef.current = "connecting";
  subscribe(detail.active_run.id, 0, controller.signal);
} else {
  clearCursor(conversationId);
}
```

- `active_run` 不存在：不要订阅；已终态的消息已经在 `messages` 里。
- `active_run` 存在但 `messages` 里没有对应助手消息（正常，因为终态才落库）：插入一个空助手气泡，`streaming=true`，id 用 `assistant_message_id`。
- 订阅从 0 开始，回放全部历史事件重建正文。
- 若回放返回 410：放弃重建，重新 `loadMessages`，提示"历史事件已过期，已显示最终保存内容"。

### 7.2 多标签

- 每个标签独立订阅、独立渲染；服务端支持一个 run 多个订阅者。
- 标签 B 在已有活跃 run 的会话里发送：服务端返回 409 `agent_run_active`，body 带 `run_id`。**这条消息没有写入**：标签 B 应移除乐观气泡、把文本恢复到输入框，并提示"当前回复尚未结束"；同时可以采用该 run，从 0 订阅，观察另一个标签正在产生的输出。不要把 409 静默当成发送成功。
- 两个标签都可能写 sessionStorage（各自独立），互不影响。
- 同一标签重复发送由 `runIdRef` 挡住；服务端 409 是最后兜底。

### 7.3 会话切换

```ts
function selectConversation(nextId: string) {
  abortRef.current?.abort();   // 只关闭本地订阅
  clearRetryTimer();
  // 不调用 cancel，不修改 run 状态
  activeIdRef.current = nextId;
  setMessages([]);
  void loadMessages(nextId);
}
```

- 切走再切回：详情里的 `active_run` 会让前端重新订阅并从 0 重建。
- 切走期间 run 完成：回来时 `active_run` 为 null，消息已落库，直接显示。
- 新建会话同理，旧 run 留在后台。

### 7.4 删除会话

- 当前会话有活跃 run 时，服务端对 `DELETE /conversations/{id}` 返回 409 `agent_run_active`。
- 前端提示"请先停止或等待当前回复完成"，不要静默失败。
- 若删除成功，清理该会话的 cursor 与本地气泡。

## 8. 组件、Hook、类型与端点改动清单

### 8.1 `src/hooks/use-agent-chat.ts`（主要改动）

- 新增 `runIdRef`、`lastSeqRef`、`connection`、`cancelRequested`、`runStatus`。
- `send()`：改用 `meta.run_id`；处理 409；不再把 abort 当取消。
- `stop()`：改为调用 cancel 接口（见第 6 节）。
- 新增 `ensureSubscribed(runId, fromSeq)`、`scheduleReconnect()`、`handleRunGone()`、`handleEventsExpired()`。
- `handleEvent()`：读取 `event.id` 去重；新增 `meta.status` / `meta.message` 处理；`done.status` 决定终态。
- `selectConversation()` / `startNewChat()` / 卸载：只 abort 订阅，不取消 run。
- `loadMessages()`：处理 `active_run`，自动恢复。
- 返回值新增 `connectionState`、`runStatus`、`cancelRequested`、`activeRunId`；`isStreaming` 保留为派生值，避免页面大改。

建议把订阅/重连逻辑抽成内部函数或单独的 `use-agent-run.ts`，但不要引入新的状态库；现有 hook 足够。

### 8.2 `src/utils/api.ts`

新增 GET 流式请求：

```ts
export const getStream = async (
  url: string,
  options: { signal?: AbortSignal; headers?: Record<string, string> } = {},
): Promise<Response> => {
  // 与 postStream 相同的 Bearer 注入、401 刷新、错误解析
};
```

实现方式：把 `requestStream` 泛化为接受 `method` 与可选 `body`，`postStream` 与 `getStream` 共用。错误仍抛 `ApiStreamError`，保留 `status` 与 `code`。

### 8.3 `src/utils/api-endpoints.ts`

```ts
export const AGENT_ENDPOINTS = {
  INVOKE: "/agent",
  CONVERSATIONS: "/conversations",
  CONVERSATION: (id: string) => `/conversations/${id}`,
  MESSAGES: (id: string) => `/conversations/${id}/messages`,
  RUN: (runId: string) => `/agent/runs/${runId}`,
  RUN_EVENTS: (runId: string, afterSeq = 0) =>
    `/agent/runs/${runId}/events?after_seq=${afterSeq}`,
  RUN_CANCEL: (runId: string) => `/agent/runs/${runId}/cancel`,
} as const;
```

### 8.4 `src/utils/sse.ts`

`SseMessage` 已经暴露 `id`，`SseParser` 已经解析 `id:`，无需改动。不要新增第二套解析器。

### 8.5 `src/types/api.ts`

- 新增 `RunStatus`、`ConnectionState`、`ActiveRun`、`AgentRun`、`RunEvent` 类型。
- `ConversationDetail` 增加 `active_run?: ActiveRun | null`。
- `AgentInvokeResponse` 增加 `run_id?: string`。
- `ChatMessageView`（在 hook 内）增加 `runStatus`、`reconnecting`、`cancelRequested`。
- `MessageStatus` 不变。

### 8.6 组件

| 文件 | 改动 |
| --- | --- |
| `components/agent/message-bubble.tsx` | 支持 `reconnecting`、`interrupted`、`cancelRequested` 文案；终态显示重试按钮；工具轨迹在中断时标记 aborted |
| `components/agent/message-thread.tsx` | 传递新状态；不要在重连时隐藏气泡 |
| `components/agent/tool-trace.tsx` | 复用现有状态色；新增 `aborted` 在中断场景的展示（如果尚未覆盖） |
| `components/agent/chat-composer.tsx` | Stop 按钮在 `runId` 存在时调用 `stop`；`cancelRequested` 时显示"正在停止"并禁用重复点击；输入框在 `queued/running` 时保持现状 |
| `components/agent/conversation-list.tsx` | 可选：对本地已知的活跃 run 显示一个小圆点；数据来自 hook 的内存映射，不新增列表接口 |
| `app/(root)/dashboard/agent/page.tsx` | 接入新增返回值；无需改路由 |

### 8.7 明确不改

- 不引入 `EventSource`、不把 token 存 `localStorage`。
- 不新增 UI/图标/状态管理库。
- 不改 `docs/dashboard/agent-chat-design.md` 的 v1 内容；本文件描述增量设计。

## 9. 边界情况与错误码处理

| 场景 | 前端处理 |
| --- | --- |
| `POST /agent` 返回 409 `agent_run_active` | 本次消息未写入：移除乐观气泡、恢复草稿；读 `body.run_id`，从 0 订阅现有 run；提示等待或 Stop |
| 订阅 404 `agent_run_not_found` | 清 cursor，`loadMessages()`，提示"该回复已结束或不存在" |
| 订阅 410 `agent_events_expired` | 清 cursor，`loadMessages()`，提示"历史事件已过期" |
| `meta.status` 已是终态且 `message` 非空 | 直接渲染最终消息，不发请求 |
| run `interrupted` | 气泡显示"已中断"；保留部分内容；提供"重试"（重新发送，不续跑） |
| `error(agent_cancelled)` 后连接中断 | 保持本地 `cancelled` 状态，不必重连；若 run 未终态则重连确认 |
| 取消请求 404 | 视为已终态，继续等待/恢复订阅 |
| 取消请求网络失败 | 提示重试；不要把气泡标成 cancelled |
| 401 | 走现有刷新逻辑；刷新失败跳登录 |
| 403 | 视为无权限，停止订阅并提示 |
| 5xx/网络错误 | 进入重连退避；8 次后轮询 `GET /agent/runs/{id}` |
| 心跳缺失 45s | 主动断开并重连 |
| 浏览器离线 | 显示 `reconnecting`，监听 `online` 立即重试 |
| 页面隐藏 | 保持订阅；浏览器可能冻结计时器，恢复可见时用 watchdog 检查 |
| 事件重复（重放与实时重叠） | 按 `seq` 丢弃 |
| `meta` 无 `run_id`（旧服务端） | 退回现有行为：`AbortController` 即取消，不显示"后台继续" |
| 会话被其他标签删除 | 订阅返回 404；关闭当前会话，刷新列表 |
| 事件表已清理但 run 行还在 | 按 410 处理，以落库消息为准 |
| sessionStorage 不可用 | 纯内存运行，刷新后依赖 `active_run` 从 0 重建 |

## 10. 测试与验收场景

项目没有前端测试框架，验收以 `pnpm typecheck`、`pnpm build` 和手工场景为准。

### 10.1 手工场景

1. **正常发送**：文字逐段出现，工具轨迹实时更新，完成后消息落库。
2. **断网 10 秒**：气泡显示"重连中"；恢复网络后文本不重复、不缺段，最终 `completed`。
3. **关闭标签页再打开**：会话详情返回 `active_run`，自动从 0 重建并继续增长；完成后消息落库。
4. **切会话再切回**：run 在后台继续；切回后恢复；其他会话不受影响。
5. **Stop**：点击后显示"正在停止"，最终 `cancelled`；服务端 run 状态为 `cancelled`；工具轨迹标 aborted。
6. **Stop 后立刻刷新**：消息以 `cancelled` 落库，不再恢复订阅。
7. **两个标签同时发送**：第二个标签收到 409，确认这条消息未写入、草稿恢复，随后订阅现有 run；两个标签看到同一段输出。
8. **多标签其中一个 Stop**：另一个标签收到 `error(agent_cancelled)` + `done`，同步结束。
9. **服务端重启**：连接断开并重连；`meta.status` 为 `interrupted` 或收到 `error(agent_interrupted)`；显示"已中断"，保留部分内容。
10. **事件过期**：把保留期调成 0 后刷新进行中的 run，收到 410，回退到会话详情。
11. **401 过期**：流中断后刷新 token 重连成功；刷新失败跳登录。
12. **旧服务端**：`meta` 无 `run_id` 时 Stop 仍走 abort，UI 行为与现状一致。

### 10.2 类型与构建

```bash
cd dashboard
pnpm typecheck
pnpm build
pnpm lint
```

注意 `pnpm lint` 是 Prettier 检查，不是 ESLint。

## 11. 分阶段落地与回滚

### 11.1 落地顺序

1. 服务端迁移 008 + run manager（`AGENT_RUN_BACKGROUND=false` 先验证旧路径）。
2. 服务端切 `true`，用 API 工具验证订阅/取消/重启回收。
3. 前端先加类型与 `getStream`，保持旧 `send` 行为。
4. 改 `handleEvent` 读取 `id`、接入 `meta.run_id`；此阶段不启用重连。
5. 接入 `active_run` 恢复与 Stop 接口。
6. 启用重连、watchdog、多标签 409 处理。
7. 全量发布。

每一步都可以独立回滚：前端检测 `meta.run_id`，没有就退回旧路径，因此服务端回滚不影响已发布前端。

### 11.2 回滚

- 前端回滚：恢复旧 hook 版本；服务端新接口保留但无人调用。
- 服务端回滚：`AGENT_RUN_BACKGROUND=false`；前端收到无 `run_id` 的 `meta` 后自动退回旧语义。
- 不要在前端回滚时删除进行中的 run；它们会继续执行到终态，只是没有订阅者。

### 11.3 监控与验收指标

- 前端：重连次数、410/404 次数、409 次数、Stop 到终态的时间。
- 后端（见 server 文档）：活跃 run、排队、事件写入失败、interrupted 数量。
- 灰度期间重点看：刷新恢复成功率、重连后消息重复率、取消后残留 `running` 数。
