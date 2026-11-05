# BokaClaw Dashboard Agent 对话设计文档

> 状态：已实现（v2：后台 run + 可续接订阅 + 工具轨迹；默认进程内执行层，`AGENT_LOOP_URL` 保留为外部执行器扩展点）
>
> 适用范围：`dashboard/` 前端、`server/` FastAPI 后端、`agent-loop/` SDK
>
> 目标读者：前端、后端、Agent 执行层开发者

> **执行模型已升级**：`POST /agent` 现在创建持久化的 `agent_run`，由服务端后台 worker 执行；SSE 只是可断开、可按 `seq` 续接的只读订阅，Stop 走独立取消接口。执行、订阅、重放与取消的完整契约见 [`agent-execution-decoupling.md`](./agent-execution-decoupling.md)，其服务端契约权威为 [`../server/agent-execution-decoupling.md`](../server/agent-execution-decoupling.md)。本文其余部分描述页面 UI、基础 REST 资源与进程内执行器配置；凡与解耦文档冲突，以解耦文档为准。

## 1. 背景与目标

`dashboard/` 原有记忆相关页面：Requests、Memories、Entities、Categories、Webhooks、Analytics、Export、API Keys、Configuration、Settings。后端 `server/` 提供 mem0 记忆 CRUD 与配置接口。根目录 `agent-loop/` 已实现为可独立发布的 ReAct SDK（`bokaclaw-agent`），由 `server/` 进程内调用；`AGENT_LOOP_URL` 仅作为外部执行器的可选覆盖。

本次改造落地一个与「记忆功能」平级的 **Agent 对话** 能力：

- **信息架构**：`Agent` 作为 `ACTIVITY` 导航分组的直接子项，与 `Requests`、`Memories`、`Entities` 同级，不挂在 Memories 之下。
- **页面**：`/dashboard/agent`，左侧会话列表，右侧消息流，底部输入框，SSE 打字机输出。
- **后端**：按现有记忆接口的扁平命名风格实现 `POST /agent` 与 `/conversations*`；服务端负责鉴权、会话持久化、SSE 转发。
- **执行层**：`server/app/agent/runtime.py` 在进程内调用 `bokaclaw-agent` 完成 ReAct 循环，并向模型暴露 `search_memories` / `save_memory` 两个记忆工具。
- **视觉**：完全复用现有 shadcn/Radix 组件、语义颜色 token、`font-fustat`、`typo-*` 工具类，不引入新的 UI 库或视觉语言。

## 2. 关键决策

| 决策点 | 结论 |
| --- | --- |
| 前端导航层级 | `ACTIVITY` 分组新增 `Agent`，与 `Memories` 平级 |
| Agent 执行入口 | `POST /agent`；浏览器只访问 `server/`，不直连 agent-loop |
| 执行模型 | `POST /agent` 先写入 `agent_runs`（`queued`），由进程内 `AgentRunManager` 后台执行；请求协程只负责创建与订阅 |
| 连接与执行 | SSE 是可断开的订阅；断开只退订，不终止执行。重连用 `GET /agent/runs/{id}/events?after_seq=N` 按 `seq` 重放 |
| 取消 | `POST /agent/runs/{id}/cancel` 设置协作式取消标志；前端继续消费到 `error(agent_cancelled)` + `done` |
| 事件持久化 | `agent_run_events` 保存可重放事件；每个 run 有单调递增的 `seq`，SSE 帧的 `id` 等于 `seq` |
| 工具轨迹 | 服务端把 `tool.call` / `tool.result` 持久化并转发；助手气泡下方用 `ToolTrace` 渲染每次工具调用 |
| 并发发送 | 一个会话同时只允许一个活跃 run；冲突由服务端返回 409 `agent_run_active` 兜底 |
| 会话持久化 | 新增 Postgres 表 `conversations`、`conversation_messages`、`agent_runs`、`agent_run_events` |
| 流式协议 | SSE（`text/event-stream`）；前端用 `fetch` + `ReadableStream`，不用 `EventSource`（无法携带 Bearer 头） |
| 会话归属 | 由 `require_auth` 解析的登录用户决定，客户端不传 `user_id` |
| 记忆作用域 | 传给 AgentRuntime 的 `user_id` = 当前登录用户的 UUID 字符串 |
| 更新风格 | 沿用仓库习惯使用 `PUT /conversations/{id}`（与 `PUT /memories/{id}` 一致） |
| 未配置模型 | 返回 503 `agent_not_configured`；`AGENT_LOOP_MOCK=true` 仅用于本地联调 UI |
| 消息渲染 | MVP 纯文本 + `whitespace-pre-wrap`，不引入 Markdown 渲染依赖 |

## 3. 总体架构

```text
┌──────────────────────────────┐
│  dashboard (Next.js)         │
│  /dashboard/agent            │
│  POST /agent → 订阅 SSE      │
│  断线重连 → /agent/runs/…    │
└──────────────┬───────────────┘
               │ POST /agent
               │ GET /agent/runs/{id}/events?after_seq=N
               │ POST /agent/runs/{id}/cancel
               ▼
┌──────────────────────────────────────────────┐
│  server (FastAPI)                            │
│  - JWT / API Key 鉴权、会话归属              │
│  - POST /agent 写入 agent_runs（queued）     │
│  - AgentRunManager 后台执行、事件写库        │
│  - SSE 订阅：after_seq 重放 + 实时转发       │
└──────────────┬───────────────────────────────┘
               │ 进程内调用（默认）
               ▼
┌──────────────────────────────┐
│  bokaclaw-agent (ReAct SDK)  │
│  - Agent 循环 / 工具调用      │
│  - 记忆检索与写入             │
└──────────────────────────────┘
```

执行不在 `POST /agent` 的请求协程里完成。handler 只做四件事：校验、写入 `conversations` / `conversation_messages` / `agent_runs`、把 run 交给 `AgentRunManager`、把当前 SSE 连接注册成该 run 的一个订阅者。请求协程退出、连接断开或浏览器关闭，都只移除订阅；后台 task 继续执行并把事件写入 `agent_run_events`。

需要独立部署执行层时，设置 `AGENT_LOOP_URL`，`server/` 改为向 `POST {AGENT_LOOP_URL}/invoke` 发起 SSE 请求，前端契约不变。

服务端是唯一公开入口。这样做的好处：

- 鉴权、会话归属、限流集中在 `server/`；
- 会话历史与 run 事件由 Postgres 统一持久化，刷新或断网后可以恢复；
- 执行层可在进程内与远程之间切换，前端无需改动；
- 不把模型密钥或 agent 运行时暴露到浏览器，避免跨域、密钥泄漏和额外鉴权。

## 4. API 契约

### 4.1 Agent 调用

```http
POST /agent
Authorization: Bearer <jwt>        # 或 X-API-Key
Content-Type: application/json
```

请求体：

```json
{
  "conversation_id": "uuid | null",
  "message": "用户这一轮输入",
  "agent_id": "string | null",
  "run_id": "string | null",
  "client_run_id": "string | null",
  "metadata": {},
  "stream": true
}
```

字段说明：

- `conversation_id`：为空时自动创建会话，标题取首条消息前 80 个字符。
- `message`：去空白后不能为空，服务端上限 32,000 字符。
- `agent_id`：透传给执行层，用于记忆作用域。
- `run_id`：已废弃的 mem0 记忆作用域字段，不再表示执行标识；执行的 run id 由服务端生成并在 `meta` 中返回。
- `client_run_id`：客户端自带的关联标识，仅用于日志排查，不参与去重。
- `metadata`：随 run 持久化到 `agent_runs.request_payload.metadata`，同时写入本轮用户消息。普通对话由 Agent 页面发起时不带该字段，run 不会被任何 case 绑定；评测回归由 Eval 页面的 `Run eval` 流水线发起（先按 case 输入跑 Agent，再评测），请求体附加 `{"eval_case_id": "<case>"}`。该值也会出现在 span 根节点 `input.eval_case_id`，`POST /evals` 据此校验 run 与所选 golden 是否一致，不一致返回 409 `eval_case_mismatch`。
- `stream`：`false` 返回 JSON，`true` 返回 SSE。默认 `false`，Dashboard 固定传 `true`。

执行语义：

- 服务端在一个短事务里写入用户消息与 `agent_runs` 行（状态 `queued`），再把 run 交给后台 `AgentRunManager`；
- 响应中的 `run_id` / `meta.run_id` 是该次执行的唯一标识，后续重连、查询、取消都以它为准；
- 同一会话已存在 `queued` 或 `running` 的 run 时返回 **409**，body 为 `agent_run_active` 并带现有 `run_id`；dashboard 据此改为订阅已有 run，而不是再次执行；
- `stream=false` 仍走同一个后台 run，handler 等待 run 结束后返回完整助手消息；连接断开不会取消 run。

非流式响应（`stream=false`）：

```json
{
  "conversation": {
    "id": "uuid",
    "title": "你好，帮我记住...",
    "agent_id": null,
    "metadata": {},
    "created_at": "2026-11-03T10:00:00Z",
    "updated_at": "2026-11-03T10:00:01Z"
  },
  "user_message": {
    "id": "uuid",
    "conversation_id": "uuid",
    "role": "user",
    "content": "你好",
    "status": "completed",
    "metadata": {},
    "created_at": "2026-11-03T10:00:00Z"
  },
  "message": {
    "id": "uuid",
    "conversation_id": "uuid",
    "role": "assistant",
    "content": "你好！有什么可以帮你？",
    "status": "completed",
    "metadata": {},
    "created_at": "2026-11-03T10:00:01Z"
  },
  "run_id": "uuid"
}
```

### 4.2 SSE 事件

响应头：

```text
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
Connection: keep-alive
X-Accel-Buffering: no
```

事件分两类：

- **持久化事件**：`message.delta`、`tool.call`、`tool.result`、`message.completed`、`error`。写入 `agent_run_events`，可重放；每帧带 `id: <seq>`，`seq` 在该 run 内单调递增，从 1 开始。
- **合成事件**：`meta`（订阅建立时由服务端生成）、`done`（订阅收尾）。它们不落库，没有 `id`。

正常顺序示例：

```text
event: meta
data: {
  "run_id": "uuid",
  "conversation_id": "uuid",
  "conversation": {...},
  "user_message": {...},
  "assistant_message_id": "uuid",
  "status": "queued | running",
  "last_seq": 0,
  "run": {...精简 run 快照...}
}

id: 1
event: tool.call
data: {"run_id":"uuid","conversation_id":"uuid","seq":1,"message_id":"uuid","tool_call_id":"call_1","name":"search_memories","iteration":1,"status":"running","arguments":{...},"raw_arguments":"{...}","arguments_error":null,"started_at":"...","truncated":false}

id: 2
event: message.delta
data: {"run_id":"uuid","conversation_id":"uuid","seq":2,"message_id":"uuid","delta":"增量文本"}

id: 3
event: tool.result
data: {"run_id":"uuid","conversation_id":"uuid","seq":3,"message_id":"uuid","tool_call_id":"call_1","name":"search_memories","iteration":1,"status":"completed","content":"...","is_error":false,"duration_ms":42,"finished_at":"...","truncated":false}

id: 4
event: message.completed
data: {"run_id":"uuid","conversation_id":"uuid","seq":4,"message_id":"uuid","message":{...完整 assistant 行...}}

event: done
data: {"run_id":"uuid","conversation_id":"uuid","status":"completed","last_seq":4}
```

失败、取消或中断时，不发送 `message.completed`，顺序为：悬挂工具补发的 `tool.result(status="aborted")` → `error` → `done`。`done` 不是成功信号，必须读 `status`。

助手消息的 `metadata` 会写入本次执行的 `run_id`（`agent_runs.id`），以及中断时的 `run_status`、`error_code` 和 `tool_trace`。dashboard 从消息行读取 `metadata.run_id`，在助手消息下方显示短 id 并提供一键复制完整 UUID；该字段随 `/conversations/{id}` 和历史消息一起返回，刷新页面后仍可复制。

```text
event: error
data: {
  "code": "agent_unreachable | agent_timeout | agent_upstream_error | agent_protocol_error | agent_cancelled | agent_interrupted | conversation_deleted",
  "detail": "可读错误信息",
  "request_id": "abcd1234",
  "conversation_id": "uuid",
  "run_id": "uuid",
  "seq": 5
}
```

约定：

- `meta` 永远是第一个事件；`done` 永远是最后一个事件（成功和失败路径都要发）；
- 持久化帧的 `id` 等于 payload 中的 `seq`；前端用 `seq` 去重；
- 重连使用 `GET /agent/runs/{id}/events?after_seq=N`，`after_seq` 是**独占**的，只返回 `seq > N` 的事件；也接受 `Last-Event-ID`；
- 等待上游期间每 15 秒发送一次 `: ping` 注释心跳，防止代理断开空闲连接；
- 所有 `data:` 使用 JSON，非 ASCII 字符不转义。

完整 payload 字段、两种 `meta` 入口的差异、`done` 的两种来源以及错误码表见服务端契约文档第 3 节；本文不重复定义。

### 4.3 会话资源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/conversations?limit=50&offset=0` | 当前用户会话列表，按 `updated_at desc` |
| POST | `/conversations` | 创建空会话，`title` 默认 `"New conversation"`，返回 201 |
| GET | `/conversations/{id}` | 会话详情，按 `(created_at, id)` 升序带完整消息 |
| PUT | `/conversations/{id}` | 重命名，body `{ "title": "..." }` |
| DELETE | `/conversations/{id}` | 删除会话及其消息（FK cascade）；存在活跃 run 时返回 409 `agent_run_active` |
| GET | `/conversations/{id}/messages?limit=100&offset=0` | 分页读取消息 |
| GET | `/agent/runs/{id}` | 读取单个 run 的状态、`last_seq`、错误与部分内容 |
| GET | `/agent/runs/{id}/events?after_seq=N` | 订阅 run：先重放 `seq > N` 的持久化事件，再转发实时事件，直到 `done` |
| POST | `/agent/runs/{id}/cancel` | 请求取消；只设置协作式取消标志，幂等 |

规则：

- 所有接口使用 `Depends(require_auth)` + `Depends(get_db)`；
- 查询必须过滤 `created_by == user.id`；
- 访问他人会话或 run 返回 404，而不是 403，避免泄漏资源是否存在；
- 列表返回裸数组，与 `/api-keys`、`/entities`、`/requests` 保持一致；
- 不接受客户端传入的 `user_id` 作为会话归属；
- `GET /conversations/{id}` 的响应额外包含 `active_run`（`queued` / `running` 的 run，或 `null`），字段为 `id`、`status`、`last_seq`、`assistant_message_id`、`cancel_requested_at`、`created_at`；刷新页面时前端据此恢复订阅；
- `POST /agent` 的 409 响应体带现有 `run_id`，前端应转为订阅该 run，而不是提示失败。

### 4.4 可选：远程执行器契约

默认 `server/` 在进程内调用 `bokaclaw-agent`。仅当设置 `AGENT_LOOP_URL` 时，`server/` 才作为客户端调用外部执行器，契约如下。

```http
POST {AGENT_LOOP_URL}/invoke
Accept: text/event-stream
Content-Type: application/json
Authorization: Bearer {AGENT_LOOP_API_KEY}   # 仅配置时发送
X-Request-ID: <rid>
```

请求体：

```json
{
  "conversation_id": "uuid",
  "message_id": "uuid",
  "message": "当前用户输入",
  "history": [
    { "role": "user", "content": "..." },
    { "role": "assistant", "content": "..." }
  ],
  "user_id": "<当前登录用户 uuid>",
  "agent_id": null,
  "run_id": null,
  "client_run_id": null,
  "metadata": {}
}
```

响应要求：

- 正常返回 HTTP 200 + `Content-Type: text/event-stream`；
- 至少发送 `event: message.delta` + `data: {"delta": "..."}`；
- 可选发送 `event: message.completed`，服务端只读取 `usage`、`finish_reason`、`run_id` 等元数据；
- 可选发送 `event: error`，服务端转换后继续向浏览器发送统一错误事件；
- 也接受非 SSE JSON `{"content": "..."}` 作为单次返回兜底，便于早期实现；
- 执行层不需要访问 Postgres，服务端会把本轮完整上下文传过去。

服务端以累计的 delta 文本为准落库；若上游没有 delta 但 `message.completed` 带 `content`，才使用该内容兜底。

### 4.5 内置工具

进程内执行时，除 `search_memories` / `save_memory` 外，还注册与 Claude Code 对齐的开发工具（`server/app/agent/dev_tools.py`）：

| 工具 | 作用 | 注册条件 |
| --- | --- | --- |
| `Read` | 读取文本、Notebook、PDF；图片返回元数据 | `AGENT_DEV_TOOLS=true` |
| `Write` | 新建或整体覆盖文件 | 同上 |
| `Edit` | 精确字符串替换，要求唯一匹配或 `replace_all` | 同上 |
| `NotebookEdit` | 按 cell id/索引替换、插入、删除 Notebook cell | 同上 |
| `Glob` | 按文件名模式查找 | 同上 |
| `Grep` | 正则搜索文件内容，支持 content/files/count 模式 | 同上 |
| `WebFetch` | 抓取公网 URL 并提取文本，默认拒绝私网地址 | 同上 |
| `Bash` | 在 workspace 执行 `bash -lc` | `AGENT_TOOLS_ALLOW_SHELL=true` |
| `PowerShell` | 执行 `pwsh -NoProfile -NonInteractive` | 同上 |
| `Monitor` | 后台启动命令，按 start/poll/stop 读取增量输出 | 同上 |

约束：

- 文件工具只能访问 `AGENT_WORKSPACE_ROOT` 内的路径，`..` 与指向外部的 symlink 都会被拒绝；
- `Bash` / `PowerShell` / `Monitor` 没有 OS 级沙箱，可访问 server 进程能访问的一切（包括 `.env` 与数据库凭据），必须显式开启；
- 图片无法作为多模态输入，`Read` 只返回类型、尺寸和说明；PDF 依赖 `pypdf`；
- 所有命令有超时与输出大小上限，`Monitor` 的缓冲保留最近 5000 行。

## 5. 数据库设计

迁移 007 新增会话两张表（`conversations`、`conversation_messages`），迁移 008 新增执行状态两张表（`agent_runs`、`agent_run_events`）。完整字段语义与生命周期见服务端契约文档第 5 节。

### 5.1 `conversations`

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | UUID | PK |
| `created_by` | UUID | FK `users.id`，`ON DELETE CASCADE` |
| `title` | VARCHAR(200) | NOT NULL |
| `agent_id` | VARCHAR(255) | NULL |
| `metadata` | JSONB | NOT NULL，默认 `{}` |
| `created_at` | TIMESTAMPTZ | NOT NULL，默认 `now()` |
| `updated_at` | TIMESTAMPTZ | NOT NULL，默认 `now()`，追加消息时显式更新 |

索引：`(created_by, updated_at)`，覆盖“按用户列出最新会话”。

> 注意：SQLAlchemy Declarative 已保留 `metadata` 属性名，ORM 中必须写成 `meta: Mapped[dict] = mapped_column("metadata", JSONB, ...)`。

### 5.2 `conversation_messages`

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | UUID | PK |
| `conversation_id` | UUID | FK `conversations.id`，`ON DELETE CASCADE` |
| `role` | VARCHAR(20) | CHECK `user/assistant/system/tool` |
| `content` | TEXT | NOT NULL，默认空串 |
| `status` | VARCHAR(20) | CHECK `completed/cancelled/failed`，默认 `completed` |
| `metadata` | JSONB | NOT NULL，默认 `{}` |
| `created_at` | TIMESTAMPTZ | NOT NULL，默认 `now()` |

索引：`(conversation_id, created_at)`；排序始终追加 `id` 作为稳定的次级排序键。

### 5.3 标题规则

- 显式创建会话未传标题：`"New conversation"`；
- `POST /agent` 未带 `conversation_id`：用首条消息生成，压缩空白、截取前 80 字符，超长追加 `...`；
- 已存在但仍为 `"New conversation"` 且没有消息的会话，在下一条消息到来时替换标题。

### 5.4 `agent_runs`

迁移 `008_create_agent_runs_and_events.py`。

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | UUID | PK，即 API 中的 `run_id` |
| `conversation_id` | UUID | FK `conversations.id`，`ON DELETE CASCADE` |
| `created_by` | UUID | FK `users.id`，`ON DELETE CASCADE` |
| `user_message_id` | UUID | FK `conversation_messages.id`，`ON DELETE SET NULL` |
| `assistant_message_id` | UUID | 无 FK，终态前由服务端预生成 |
| `request` | JSONB | 请求快照（ORM 属性名 `request_payload`） |
| `client_run_id` / `agent_id` | VARCHAR(255) | 可选关联信息 |
| `status` | VARCHAR(20) | CHECK `queued/running/completed/failed/cancelled/interrupted` |
| `error_code` / `error_detail` | VARCHAR(64) / TEXT | 终态错误 |
| `partial_content` | TEXT | 已累积的助手文本 |
| `partial_metadata` | JSONB | 工具轨迹等增量元数据 |
| `last_seq` | BIGINT | 已写入的最大事件序号 |
| `attempt` | INT | 预留，当前恒为 1 |
| `worker_id` / `heartbeat_at` | TEXT / TIMESTAMPTZ | 心跳字段，当前写入但不参与回收 |
| `cancel_requested_at` | TIMESTAMPTZ | Stop 请求时间 |
| `started_at` / `finished_at` | TIMESTAMPTZ | 执行起止 |
| `created_at` / `updated_at` | TIMESTAMPTZ | 时间戳 |

约束与索引：

- 部分唯一索引 `uq_agent_runs_active_conversation`：`conversation_id` 在 `status IN ('queued','running')` 时唯一，保证一个会话同时只有一个活跃 run；
- `ix_agent_runs_created_by_created`、`ix_agent_runs_status_heartbeat`。

### 5.5 `agent_run_events`

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | BIGINT | Identity PK |
| `run_id` | UUID | FK `agent_runs.id`，`ON DELETE CASCADE` |
| `seq` | BIGINT | run 内单调递增 |
| `event_type` | VARCHAR(32) | CHECK `message.delta/tool.call/tool.result/message.completed/error` |
| `payload` | JSONB | 事件载荷，含 `run_id`、`conversation_id`、`seq` |
| `created_at` | TIMESTAMPTZ | 写入时间 |

约束与索引：`UNIQUE (run_id, seq)`、`ix_agent_run_events_run_seq`。

## 6. 后端实现

### 6.1 文件清单

| 文件 | 变更 |
| --- | --- |
| `server/app/models.py` | 新增 `Conversation`、`ConversationMessage`、`AgentRun`、`AgentRunEvent` |
| `server/alembic/versions/007_create_conversations_and_messages.py` | 新建会话表、索引、CHECK、downgrade |
| `server/alembic/versions/008_create_agent_runs_and_events.py` | 新建 `agent_runs`、`agent_run_events`、部分唯一索引 |
| `server/app/agent/schemas.py` | 请求/响应模型、`ActiveRunOut`、`AgentRunOut` |
| `server/app/agent/store.py` | 会话/消息读写、UUID 解码、归属校验、标题生成、历史截断 |
| `server/app/agent/runner.py` | `AgentRunManager`、`RunEventWriter`、订阅交接、启动/关闭恢复、取消 |
| `server/app/agent/repo.py` | run/事件持久化：领取、心跳、终止、`append_event`、清理 |
| `server/app/agent/payloads.py` | 持久化事件 payload 构造与字段裁剪 |
| `server/app/agent/trace.py` | `ToolTrace`：按 `tool_call_id` 记录工具调用、预算与状态 |
| `server/app/agent/loop.py` | 外部执行器 HTTP/SSE 客户端、SSE 编解码、错误映射 |
| `server/app/agent/events.py` | `AgentDelta` / `AgentCompleted` / `AgentLoopError` / 工具事件 |
| `server/app/agent/runtime.py` | 进程内 `bokaclaw-agent` 包装、模型配置解析、记忆工具、`on_event` 桥接 |
| `server/app/agent/dev_tools.py` | Read / Write / Edit / NotebookEdit / Glob / Grep / Bash / PowerShell / WebFetch / Monitor |
| `server/app/agent/executor.py`、`executor_app.py` | 可选独立执行器与 shell 命令委托 |
| `server/app/routers/conversations.py` | `/conversations*` 路由，详情带 `active_run`，删除活跃会话返回 409 |
| `server/app/routers/agent.py` | `POST /agent`、`/agent/runs/*` 路由与 SSE 订阅生成器 |
| `server/app/main.py` | 注册两个 router、创建并关闭 `AgentRunManager` |
| `server/requirements.txt` | 显式加入 `httpx`、`pypdf` |
| `tests/test_agent_runs.py`、`tests/test_agent_trace.py`、`tests/test_agent_upstream.py`、`tests/test_agent_runtime.py`、`tests/test_agent_dev_tools.py`、`tests/test_agent_executor.py`、`tests/test_agent_router.py` | run 生命周期/重放/取消、工具轨迹、远程上游、进程内执行、工具、执行器委托、legacy 路由与 SSE |
| `agent-loop/README.md` | 记录 4.4 节的下游契约 |
| `server/README.md`、`docs/open-source/features/rest-api.mdx` | 补充 Agent Chat 文档与 curl 示例 |

### 6.2 路由与鉴权

```python
router = APIRouter(prefix="/conversations", tags=["conversations"])
router = APIRouter(prefix="/agent", tags=["agent"])
```

- 会话接口：`require_auth` 保证有真实用户，用于归属校验；
- Agent 接口：同样使用 `require_auth`；使用 `ADMIN_API_KEY` 或 `AUTH_DISABLED` 时归属到默认用户，与 `/api-keys` 行为一致；
- 流式端点不要注入 `Depends(get_db)`，避免长连接一直占用 session；在 handler 与生成器内部使用 `with SessionLocal() as db:` 短事务；
- 可加 `@limiter.limit("30/minute")`，与 `server/app/routers/auth.py` 一致。

### 6.3 `POST /agent` 处理流程

默认 `AGENT_RUN_BACKGROUND=true`，走后台 run 模型：

1. 校验请求，读取当前用户；把 `request_id_var.get()` 保存到局部变量，供后续日志使用。
2. 若 `AGENT_LOOP_MOCK != true`、`AGENT_LOOP_URL` 为空，且进程内运行时无法解析模型（`AGENT_MODEL` 与 mem0 LLM 均不可用，或 SDK 未安装），在创建 run 之前返回：

   ```json
   {
     "detail": "Agent execution is not configured. Set AGENT_MODEL and AGENT_API_KEY (or OPENAI_API_KEY), or set AGENT_LOOP_URL for an external executor.",
     "code": "agent_not_configured",
     "request_id": "abcd1234"
   }
   ```

3. 在一个短事务（`asyncio.to_thread`）里创建本轮数据：
   - 加载或创建会话，校验归属，按需生成标题；
   - 插入用户消息；
   - 构造历史：只取 `status=completed` 的 user/assistant 消息，最多 40 条、约 24,000 字符；
   - 预生成 `assistant_message_id`（此时不写消息行）；
   - 插入 `agent_runs`，状态 `queued`，保存请求快照与历史快照。
   部分唯一索引冲突时返回 **409** `agent_run_active`，body 带现有 `run_id`。
4. `manager.submit(run_id, request_id)`：把 run 交给后台 `AgentRunManager`，由 worker 领取并执行。此后请求协程与执行生命周期无关。
5. `stream=false`：等待 run 终态；`completed` 返回 assistant 消息 JSON，失败/取消/中断返回对应错误，仍在执行返回 504 `agent_timeout`。
6. `stream=true`：`manager.open_subscription(run_id, 0)` 注册订阅，构造 `meta`，返回 `StreamingResponse`。客户端断开只会移除订阅，不会取消 run。

`AGENT_RUN_BACKGROUND=false` 时退回 legacy 请求绑定路径（4.1 节之外的旧行为）：handler 同时持有执行与连接，断开会取消上游。该路径仅用于回滚，不是默认契约。

### 6.4 落库时机与取消

| 场景 | 用户消息 | Assistant 消息 | run |
| --- | --- | --- | --- |
| 正常完成 | 创建 run 前已提交 | 终态时写入，`status=completed` | `completed` |
| 用户点 Stop | 已提交 | 终态时写入已累积内容，`status=cancelled`；无文本且无工具轨迹时不插入 | `cancelled` |
| 浏览器断连 / 关标签 | 已提交 | 后台继续执行，按最终结果写入 | 继续执行，不因断连改变 |
| 上游超时 / 报错 | 已提交 | 写入部分内容，`status=failed` | `failed` |
| 服务重启 | 已提交 | 启动恢复时按已有消息状态或 `interrupted` 处理 | `completed` / `cancelled` / `failed` / `interrupted` |
| 会话被删除 | 已删除 | 不再写入 | 级联删除 |

其他要求：

- assistant 消息的 UUID 在创建 run 时生成，并在每个 delta、`tool.call`、`tool.result` 中复用，保证前端消息 ID 稳定；
- Stop 只调用 `POST /agent/runs/{id}/cancel`，设置 `cancel_requested_at`；worker 在下一个检查点协作式停止，没有 `task.cancel()` 硬中断；
- 生成器 `finally` 只关闭订阅，不关闭上游执行；
- 心跳用 `asyncio.wait_for(..., timeout=15)` 实现，超时后发送 `: ping` 并继续等待；
- 终态事件顺序见 4.2 节；run 状态、助手消息与 `message.completed` 的写入顺序见服务端契约文档第 6 节。

### 6.5 错误码

| code | HTTP / 事件 | 含义 |
| --- | --- | --- |
| `agent_not_configured` | 503 | 无法解析模型（`AGENT_MODEL` / mem0 LLM 均不可用），或 SDK 未安装 |
| `agent_unreachable` | error / 502 | 外部执行器连接失败、DNS 失败 |
| `agent_timeout` | error / 504 | 读取超时或单轮总时长超限 |
| `agent_upstream_error` | error / 502 | 上游返回非 2xx，或模型调用失败 |
| `agent_protocol_error` | error | 上游 SSE/JSON 格式不合法 |
| `agent_failed` | 502 | 非流式 `POST /agent` 的终态兜底错误码 |
| `agent_cancelled` | error | 用户取消；run 终态 `cancelled`，消息 `cancelled` |
| `agent_interrupted` | error | 服务重启时回收的活跃 run；消息 `failed`，`metadata.run_status="interrupted"` |
| `agent_run_active` | 409 | 会话已有 `queued` / `running` 的 run，body 带 `run_id` |
| `agent_run_not_found` | 404 | run 不存在或不属于当前用户 |
| `agent_events_expired` | 410 | `after_seq` 早于已清理的最早事件，无法完整重放 |
| `agent_invalid_cursor` | 400 | `after_seq` 为负数（非法的 `Last-Event-ID` 会被当作 0） |
| `conversation_deleted` | error / 404（legacy） | 执行过程中会话被删除，输出被丢弃 |

错误体与 `server/app/core/errors.py` 保持一致：`{ "detail", "code", "request_id" }`；run 相关错误额外带 `run_id`。完整映射与出现位置见服务端契约文档第 3.7 节。

### 6.6 环境变量

```bash
# ---- Agent chat ----
# 默认进程内执行，复用 mem0 的 openai/deepseek LLM 配置
# AGENT_MODEL=
# AGENT_API_KEY=
# AGENT_BASE_URL=
# AGENT_SYSTEM_PROMPT=
AGENT_MAX_ITERATIONS=10
AGENT_TEMPERATURE=0.2
# AGENT_MAX_TOKENS=
AGENT_REQUEST_TIMEOUT_SECONDS=60
AGENT_MAX_RETRIES=2

# ---- Agent 执行、事件与取消（迁移 008）----
AGENT_RUN_BACKGROUND=true          # false 时退回 legacy 请求绑定路径
AGENT_RUN_CONCURRENCY=2            # 每进程同时执行的后台 run 数
AGENT_RUN_MAX_SECONDS=1800
AGENT_RUN_HEARTBEAT_SECONDS=10
AGENT_RUN_LEASE_SECONDS=60         # 预留，当前写入心跳但不参与回收
AGENT_RUN_EVENT_RETENTION_DAYS=7
AGENT_RUN_RETENTION_DAYS=30
AGENT_RUN_DELTA_FLUSH_MS=150
AGENT_RUN_DELTA_FLUSH_BYTES=4096

# ---- 工具轨迹预算 ----
AGENT_TOOL_TRACE_MAX_BYTES=16384
AGENT_TOOL_TRACE_MAX_ARGUMENT_BYTES=4096
AGENT_TOOL_TRACE_MAX_TOTAL_BYTES=65536

# ---- 开发工具 ----
AGENT_DEV_TOOLS=true
AGENT_WORKSPACE_ROOT=/app/workspace
AGENT_TOOLS_ALLOW_SHELL=false   # 开启后才有 Bash/PowerShell/Monitor
AGENT_TOOL_TIMEOUT_SECONDS=120
AGENT_TOOL_MAX_OUTPUT_BYTES=200000
AGENT_TOOL_MAX_FILE_BYTES=2000000
AGENT_WEB_FETCH_TIMEOUT_SECONDS=30
AGENT_WEB_FETCH_MAX_BYTES=1000000
AGENT_WEB_FETCH_ALLOW_PRIVATE=false

# ---- 可选：远程执行器 ----
AGENT_LOOP_URL=                    # 不含 /invoke；设置后优先于进程内执行
# AGENT_LOOP_API_KEY=
AGENT_LOOP_MOCK=false              # 仅本地联调：内置流式假回复
AGENT_LOOP_CONNECT_TIMEOUT_SECONDS=5
AGENT_LOOP_READ_TIMEOUT_SECONDS=120
AGENT_LOOP_MAX_SECONDS=300
AGENT_LOOP_HISTORY_MESSAGES=40
```

部署注意：

- `server/docker-compose.yaml` 的 `mem0` 已使用 `env_file: .env`，变量会自动透传；增加 `extra_hosts: ["host.docker.internal:host-gateway"]` 便于 Linux 访问宿主机上的远程执行器；
- `server/entrypoint.all-in-one.sh` 透传上述变量并给默认值，不设置默认 `AGENT_LOOP_URL`；
- `server/dev.Dockerfile`、`server/Dockerfile.all-in-one`、`server/Dockerfile` 都会 `COPY agent-loop /opt/agent-loop` 并安装，三套镜像都自带进程内执行层；
- `AgentRunManager` 是进程内单例，当前假设 `server/` **单副本**部署；多副本会各自执行与持有订阅，尚未实现分布式租约或外部队列；
- 事件与 run 的保留清理需要手工运行 `server/scripts/prune_agent_runs.py`，没有内置定时任务。

## 7. 前端实现

### 7.1 文件清单

| 文件 | 作用 |
| --- | --- |
| `dashboard/src/app/(root)/dashboard/agent/page.tsx` | 页面编排 |
| `dashboard/src/components/agent/conversation-list.tsx` | New chat、会话列表、active/hover、重命名/删除菜单 |
| `dashboard/src/components/agent/message-thread.tsx` | 消息列表、自动滚动、错误提示、首 token 前 loading |
| `dashboard/src/components/agent/message-bubble.tsx` | user/assistant 气泡、状态 caption、流式光标；助手气泡下方渲染 `ToolTrace` |
| `dashboard/src/components/agent/tool-trace.tsx` | 工具轨迹列表：按 `tool_call_id` 合并的调用/结果；默认只显示最新一条，顶部 `Show all` 按钮展开全部；行内折叠展开、状态与耗时 |
| `dashboard/src/components/agent/chat-composer.tsx` | 自适应高度输入框、发送/停止、Enter/Shift+Enter、IME 兼容 |
| `dashboard/src/components/agent/agent-empty-state.tsx` | 未选会话 / 空会话状态 |
| `dashboard/src/components/agent/rename-conversation-dialog.tsx` | 重命名弹窗 |
| `dashboard/src/hooks/use-agent-chat.ts` | 会话加载、发送、run 订阅与重连、取消、重命名、删除 |
| `dashboard/src/utils/sse.ts` | 增量 SSE 解析（`SseParser`） |
| `dashboard/src/utils/api.ts` | 扩展既有 `postStream` / `getStream`：`AbortSignal`、SSE Accept、401 刷新、错误读取 `detail` |
| `dashboard/src/utils/api-endpoints.ts` | 新增 `AGENT_ENDPOINTS`（含 run 查询、事件、取消） |
| `dashboard/src/types/api.ts` | 新增 Conversation / ChatMessage / `ActiveRun` / `AgentRun` / `ToolTraceEntry` 等类型 |
| `dashboard/src/app/(root)/dashboard/components/main-nav.tsx` | 新增 `Agent` 导航项 |

### 7.2 类型与端点常量

```ts
export type ChatRole = "user" | "assistant" | "system" | "tool";
export type MessageStatus = "completed" | "cancelled" | "failed";

export type RunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type ConnectionState =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed";

export type ToolTraceStatus = "running" | "completed" | "failed" | "aborted";

export interface ActiveRun {
  id: string;
  status: RunStatus;
  last_seq: number;
  assistant_message_id: string | null;
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

export interface Conversation {
  id: string;
  title: string;
  agent_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id: string;
  conversation_id: string;
  role: ChatRole;
  content: string;
  status: MessageStatus;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: ChatMessage[];
  active_run?: ActiveRun | null;
}

export interface AgentInvokeResponse {
  conversation: Conversation;
  user_message: ChatMessage;
  message: ChatMessage;
  run_id?: string;
}
```

```ts
export const AGENT_ENDPOINTS = {
  INVOKE: "/agent",
  CONVERSATIONS: "/conversations",
  CONVERSATION: (conversationId: string) => `/conversations/${conversationId}`,
  MESSAGES: (conversationId: string) =>
    `/conversations/${conversationId}/messages`,
  RUN: (runId: string) => `/agent/runs/${runId}`,
  RUN_EVENTS: (runId: string, afterSeq = 0) =>
    `/agent/runs/${runId}/events?after_seq=${afterSeq}`,
  RUN_CANCEL: (runId: string) => `/agent/runs/${runId}/cancel`,
} as const;
```

### 7.3 SSE 解析

`dashboard/src/utils/sse.ts`：

```ts
export interface SseMessage {
  event: string;
  data: string;
  id?: string;
}

export class SseParser {
  push(chunk: string): SseMessage[];
  flush(): SseMessage[];
}
```

实现要求：

- 使用 `response.body.getReader()` + `TextDecoder("utf-8")`，把解码后的片段喂给 `SseParser.push()`；
- 先把 CRLF 归一化为 `\n`，按空行切分事件，支持事件跨 chunk 边界；
- 以 `:` 开头的行是注释，忽略且不派发；
- 多个 `data:` 行用 `\n` 拼接；
- 没有 `event:` 时默认事件名为 `message`；
- `flush()` 处理流结束时残留的最后一个 block；
- 读取循环由 hook 管理：`AbortError` 静默退出，其他错误进入重连逻辑；退出时释放 reader。

`SseMessage.id` 即持久化事件的 `seq`。hook 用 `Number(event.id ?? payload.seq ?? 0)` 去重，不使用事件名做状态判断；`meta` / `done` 没有 `id`，不参与去重。

### 7.4 页面布局

```text
┌──────────────────────────────────────────────────────────┐
│ Agent                              [New chat]            │
├───────────────┬──────────────────────────────────────────┤
│ 会话列表       │ 会话标题                                  │
│ (w-260px)     ├──────────────────────────────────────────┤
│ · 会话 A       │  assistant 气泡                          │
│ · 会话 B       │                          user 气泡       │
│ · ...         │  (内部 ScrollArea，自动滚到底部)          │
│               ├──────────────────────────────────────────┤
│               │ [Textarea ......................] [↑/■]  │
└───────────────┴──────────────────────────────────────────┘
```

关键尺寸：

- 页面根容器：`flex h-[calc(100vh-118px)] min-h-0 flex-col gap-4`（标题行与聊天区都放在这个固定高度内，聊天区再 `flex-1 min-h-0`）；
- 右侧：`Card className="flex min-w-0 flex-1 flex-col overflow-hidden border-memBorderPrimary"`；
- 消息区：`ScrollArea className="min-h-0 flex-1"`；
- 输入区：`shrink-0 border-t border-memBorderPrimary p-3`；
- `min-h-0` 必须保留，确保只有消息区内部滚动，不触发外层 dashboard `ScrollArea` 的第二个滚动条。

`calc(100vh-118px)` 的来源：Dashboard 外壳提供 48px 顶部偏移、`calc(100vh-56px)` 容器和 `px-6 py-6` 内边距，可用高度为 `100vh - 70px - 48px`。

移动端（`< md`）：隐藏左栏，header 增加 `Menu` 按钮打开现有 `Sheet side="left"`，内部复用同一个会话列表。

### 7.5 交互细节

- 消息内容：`whitespace-pre-wrap break-words text-sm text-onSurface-default-primary`，纯文本渲染，禁止 `dangerouslySetInnerHTML`；
- 用户气泡：`ml-auto max-w-[85%] rounded-xl border border-memBorderPrimary bg-surface-default-secondary px-3 py-2`；
- 助手气泡：`mr-auto max-w-[85%] rounded-xl border border-memBorderPrimary bg-surface-default-primary px-3 py-2`；
- 流式光标：现有 `animate-pulse`；等待首个 token：现有 `.dot` 动画或 `Spinner`；
- 会话行：`rounded-lg px-2.5 py-2 typo-body-sm`，active `bg-surface-default-tertiary`，hover `bg-surface-default-secondary`；
- 会话时间：`formatDistanceToNow` + `typo-caption-sm text-onSurface-default-tertiary`；
- 删除会话复用 `DeleteConfirmationModal`，重命名复用 `Dialog` + `Input`；
- 自动滚动：记录用户是否接近底部（约 80px），仅在接近底部时随 delta 滚动，避免打断回看历史；
- 输入框：复用 `Textarea`，`textareaClassName="h-auto min-h-[44px] max-h-[200px] resize-none py-2.5"`，自动调整高度到 200px 后内部滚动；
- Enter 发送，Shift+Enter 换行；用 `event.nativeEvent.isComposing` 兼容中文输入法；
- `maxLength={32000}`；
- streaming 时发送按钮切换为 Stop（`Square` 图标）：已有 `run_id` 时调用 `POST /agent/runs/{id}/cancel` 并继续消费到 `error(agent_cancelled)` + `done`；仅在 `meta` 之前还没有 `run_id` 时才本地 abort 连接；
- 工具轨迹：助手气泡下方渲染 `ToolTrace`，默认只展示最新一条工具调用；条目多于一条时顶部提供 `Show all N tool calls` 下拉按钮展开全部（再点变为 `Show latest only`）。每行按 `tool_call_id` 展示工具名、状态（Running / Completed / Failed / Aborted）与耗时；展开后显示 ARGUMENTS / OUTPUT 与截断提示。

### 7.6 状态机

`use-agent-chat.ts` 维护：

```ts
conversations: Conversation[];
activeId: string | null;
activeConversation: Conversation | null;
messages: ChatMessageView[];
isLoadingConversations: boolean;
isLoadingMessages: boolean;
connectionState: ConnectionState;
runStatus: RunStatus | null;
cancelRequested: boolean;
activeRunId: string | null;
error: string | null;
isStreaming: boolean; // 派生：run 未终态且 (发送中或存在 activeRunId)
```

内部 refs 还保存 `lastSeqRef`（已接收的最大 `seq`）、`epochRef`（订阅代数，丢弃旧 reader）、`staleRunIdsRef`（该会话已终态的 run，避免重复订阅）、连接/轮询定时器与 `AbortController`。

行为：

- `send(text)`：空白或已有活跃 run 时直接返回；乐观插入用户消息和 assistant 占位；`POST /agent` 返回 SSE 后读取 `meta.run_id` 建立订阅；
- 409：读取 `err.data.run_id`，订阅该已有 run（从 `after_seq=0` 重放），不提示失败；
- `meta`：用服务端消息替换临时用户消息；新会话插入列表顶部；记录 `run_id`、初始 `status` 与 `last_seq`；
- `message.delta`：按 `message_id` 追加文本，首次出现时把占位替换为服务端 ID；
- `tool.call` / `tool.result`：按 `tool_call_id` 在助手消息的 `metadata.tool_trace` 中 upsert；结果到达时保留已有调用参数；
- `message.completed`：替换为完整消息行并更新 `updated_at`；工具轨迹优先采用服务端 `metadata.tool_trace`；
- `error`：显示内联错误；`agent_not_configured` 在消息区提示“Agent execution is not configured yet. Set AGENT_MODEL and AGENT_API_KEY (or OPENAI_API_KEY) on the API server, or AGENT_LOOP_MOCK=true for local UI work.”，不能只弹 toast；`agent_cancelled` 不显示为错误；
- `done`：按 `payload.status` 进入终态（`completed` / `failed` / `cancelled` / `interrupted`），不能把 `done` 当成功信号；
- `stop()`：有 `run_id` 时调用取消接口并保留连接，继续消费部分文本与终态；只有尚无 `run_id` 时本地 abort 并把占位标为 `cancelled`；
- 断线重连：`1/2/4/8/15s` 退避，`after_seq=lastSeqRef.current`，连续 8 次失败改为 5 秒轮询 `GET /agent/runs/{id}`；完整算法见 [`agent-execution-decoupling.md`](./agent-execution-decoupling.md) 第 6 节；
- 切换会话：`GET /conversations/{id}`，加载期间显示 `Skeleton`；若返回 `active_run`，插入占位并从 `after_seq=0` 重放；
- 删除当前会话：先清空选择并刷新列表；服务端返回 409 时提示先停止或等待 run 结束；
- 重命名：乐观更新，失败回滚并 toast。

## 8. 风格一致性约束（硬要求）

- 颜色只用语义 token：`bg-surface-default-{primary,secondary,tertiary}`、`text-onSurface-default-{primary,secondary,tertiary}`、`border-memBorderPrimary`、`text-onSurface-danger-primary`；禁止 `bg-gray-*`、`text-white`、裸十六进制色。
- 复用现有组件，不新增 UI 库：`Button`、`Card`、`Input`、`Textarea`、`ScrollArea`、`Skeleton`、`DropdownMenu`、`Dialog`、`Sheet`、`Tooltip`、`DeleteConfirmationModal`、`Spinner`、`toast`、`getErrorMessage`、`useDebounce`。
- 排版：页面标题 `text-xl font-semibold font-fustat`；正文 `text-sm`（`typo-body-sm` 自带 `font-semibold`，不要用于消息正文）；元信息 `typo-caption-sm`（DM Mono）。
- 图标只用 lucide-react，按钮内 `size-4`，菜单内 `size-3.5`。
- 不新增全局 CSS、不改 Tailwind 主题、不新增图片资源。
- 所有新文件通过 Prettier；`pnpm lint`、`pnpm typecheck`、`pnpm build` 必须通过。

## 9. 导航集成

`dashboard/src/app/(root)/dashboard/components/main-nav.tsx`：

```ts
{
  title: "Agent",
  url: "/dashboard/agent",
  icon: Bot,
  active: pathname === "/dashboard/agent",
}
```

插入位置：`ACTIVITY` 数组中 `Memories` 之后、`Entities` 之前。这样 Agent 与记忆功能处于同一层级，且继承折叠模式的图标渲染与 tooltip。

## 10. 验证方案

### 10.1 后端

```bash
cd server && docker compose up --build

# 迁移可回滚
docker compose exec mem0 alembic downgrade -1
docker compose exec mem0 alembic upgrade head

# 未配置：清空 AGENT_MODEL、OPENAI_API_KEY，且 AGENT_LOOP_URL/AGENT_LOOP_MOCK 为空/false
# 期望 503，code=agent_not_configured
curl -sS -X POST http://localhost:8888/agent \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message":"hi","stream":true}'

# mock：期望 meta → (tool.call / tool.result)* → message.delta… → message.completed → done
curl -N -sS -X POST http://localhost:8888/agent \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"message":"hi","stream":true}'

# 断线重连：从 meta 取 run_id，然后从 after_seq 续接
curl -N -sS "http://localhost:8888/agent/runs/<run_id>/events?after_seq=0" \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Accept: text/event-stream"

# 查询与取消
curl -sS http://localhost:8888/agent/runs/<run_id> -H "X-API-Key: $ADMIN_API_KEY"
curl -sS -X POST http://localhost:8888/agent/runs/<run_id>/cancel -H "X-API-Key: $ADMIN_API_KEY"

# 会话持久化（详情包含 active_run）
curl -sS http://localhost:8888/conversations -H "X-API-Key: $ADMIN_API_KEY"
curl -sS http://localhost:8888/conversations/<id> -H "X-API-Key: $ADMIN_API_KEY"
```

同时验证：断开 `events` 的 curl 不影响 run 继续执行；重新连接能补齐断开期间的事件；对同一会话再次 `POST /agent` 在活跃 run 期间返回 409，body 带现有 `run_id`。

后端测试以 `tests/test_agent_router.py` 为基础（沿用 `tests/test_api_keys_router.py` 的 `sys.path` + dependency override 模式），并新增：

- `tests/test_agent_runs.py`：创建/领取/终态、事件重放与 `after_seq`、409 活跃 run、取消、启动恢复；
- `tests/test_agent_trace.py`：工具轨迹合并、状态、预算截断；
- `tests/test_agent_upstream.py`：远程执行器 SSE、错误映射；
- `tests/test_agent_executor.py`：独立执行器的命令委托与隔离参数。

`test_agent_router.py` 覆盖未配置 503、非法 UUID / 他人会话 404、未认证 401、legacy SSE 与标题/历史截断等纯函数。

### 10.2 前端

```bash
cd dashboard
pnpm typecheck
pnpm lint
pnpm build
```

### 10.3 手工 E2E（`AGENT_LOOP_MOCK=true`）

1. 打开 `http://localhost:3000/dashboard/agent`，导航中 `Agent` 与 `Memories` 同级；
2. 新建会话 → 发送 → 打字机输出；有工具调用时助手气泡下方出现 `ToolTrace`，运行中状态为 Running，结束后变为 Completed / Failed；
3. Stop：点击后按钮显示停止中，最终保留部分文本，刷新后消息状态为 `cancelled`；run 状态为 `cancelled`；
4. 断线重连：执行中把浏览器切到离线再恢复（或杀死再重启 API 连接），前端应自动用 `after_seq` 续接，消息与工具轨迹不重复、不丢失；
5. 刷新恢复：执行中刷新页面，`GET /conversations/{id}` 返回 `active_run`，页面从 `seq=0` 重放并继续实时更新；
6. 多标签：同一会话在第二个标签发送应收到 409 并转为订阅已有 run，而不是创建第二个执行；
7. 会话自动命名、重命名、删除、切换；刷新后历史仍在；活跃 run 期间删除会话应提示 409；
8. 关闭 mock，清空 `AGENT_LOOP_URL` 并移除所有模型配置（`AGENT_MODEL` 与 mem0 LLM 都不可用）后，页面显示 `agent_not_configured` 内联提示；
9. 切换浅色/深色主题，颜色、边框、滚动条与现有页面一致；
10. 缩到 390px，检查 Sheet 侧栏与输入区可用。

### 10.4 进程内执行与远程切换

关闭 mock 后，默认由进程内 `bokaclaw-agent` 执行，模型来自 mem0 LLM 配置或 `AGENT_MODEL` / `AGENT_API_KEY` / `AGENT_BASE_URL`。要验证远程路径，启动一个实现 4.4 节契约的 stub，设置：

```bash
AGENT_LOOP_URL=http://host.docker.internal:8890   # 或 compose 内的 http://agent-loop:8890
```

验证服务端正确转发 delta、忽略上游 `message.completed` 中的完整正文（以累计 delta 为准），并正确落库。远程执行器同样运行在后台 run 中：SSE 断开只退订，`GET /agent/runs/{id}/events` 可重放，Stop 仍走取消接口。

## 11. 实施顺序

1. `server/app/models.py` + Alembic 007，跑通 upgrade → downgrade → upgrade；
2. `server/app/agent/schemas.py`、`server/app/agent/store.py`；
3. `server/app/agent/loop.py`（SSE 编解码、上游客户端、mock）；
4. `server/app/routers/conversations.py`、`server/app/routers/agent.py`，在 `main.py` 注册；
5. `requirements.txt`、`.env.example`、compose/entrypoint、文档；
6. 前端类型、端点常量、`sse.ts`、`api.ts` 的 `postStream` 扩展；
7. `use-agent-chat.ts` → 组件 → 页面 → 导航项；
8. 后端测试、前端 `typecheck/lint/build`、mock E2E、curl SSE；
9. 默认由进程内 `bokaclaw-agent` 执行；若部署独立执行层，只改 `AGENT_LOOP_URL` 即可切换，前端无需改动。

## 12. 风险与边界

| 风险 | 应对 |
| --- | --- |
| 现有 `@app.middleware("http")` 是 BaseHTTPMiddleware，可能缓冲 SSE | 尽早用 `curl -N` 验证；若被缓冲，将 `/agent` 从日志中间件排除或改为纯 ASGI 中间件 |
| 反向代理缓冲 SSE | 响应头已设 `X-Accel-Buffering: no`；文档要求代理解除 buffering |
| `EventSource` 不能带 Bearer 头 | 坚持 `fetch` + `ReadableStream`，不要改回 `EventSource` |
| 失败后手动重发产生重复用户消息 | 当前仍会新增用户消息；后续可加 `client_message_id` 唯一约束 |
| 多端并发发送同一会话 | 服务端部分唯一索引保证一个会话一个活跃 run，冲突返回 409 并带现有 `run_id`；前端转为订阅 |
| 流式过程中会话被删除 | run 随外键级联删除；worker 检测到 run 消失后停止，订阅收到 `conversation_deleted` 或 404 |
| 客户端断连 | 只移除订阅，后台 run 继续执行；重连按 `after_seq` 重放 |
| 用户取消 | 只设置协作式取消标志；worker 在下一个检查点停止，终态写 `cancelled` 并保留部分内容 |
| 事件写入失败 | 首次 `append_event` 失败即停止该 run 的后续持久化（`writer.available=False`），执行继续；重连可能缺事件，这是当前已知限制 |
| XSS | Agent 输出按纯文本渲染，绝不 `dangerouslySetInnerHTML` |
| 超长对话 | 详情接口 MVP 返回完整线程；超长时使用 `/messages` 分页 |
| 未配置执行层 | 页面必须优雅展示 503 提示，不能出现半截流或未捕获异常 |
| all-in-one 打包 agent-loop | 三套 Dockerfile 已 `COPY agent-loop /opt/agent-loop` 并安装；修改打包方式时需同时验证 dev、production、all-in-one 镜像 |

## 13. 本次不做

- 不把 `agent-loop/` 作为独立服务部署（默认进程内运行），但保留 `/invoke` HTTP/SSE 契约作为远程扩展点；
- 不引入 Markdown 渲染、代码高亮、文件上传、多模态（后续可加 `react-markdown` + `remark-gfm`）；
- 不做工具轨迹的按迭代分组、运行中自动展开、复制按钮；当前默认折叠只显示最新一条，可手动 `Show all` 展开全部；
- 不做跨重启续跑、步级 checkpoint、多副本执行或分布式队列；服务端重启时活跃 run 会被回收为终态；
- 不做多用户/多租户权限模型；会话归属固定为登录用户；
- 不新增全局状态管理；会话状态放在页面级 hook 中；
- 不新增图片资源或全局 CSS。
