# BokaClaw Dashboard Agent 对话设计文档

> 状态：已实现（v1，默认进程内执行层；`AGENT_LOOP_URL` 保留为外部执行器扩展点）
>
> 适用范围：`dashboard/` 前端、`server/` FastAPI 后端、`agent-loop/` SDK
>
> 目标读者：前端、后端、Agent 执行层开发者

## 1. 背景与目标

`dashboard/` 原有记忆相关页面：Requests、Memories、Entities、Categories、Webhooks、Analytics、Export、API Keys、Configuration、Settings。后端 `server/` 提供 mem0 记忆 CRUD 与配置接口。根目录 `agent-loop/` 已实现为可独立发布的 ReAct SDK（`bokaclaw-agent`），由 `server/` 进程内调用；`AGENT_LOOP_URL` 仅作为外部执行器的可选覆盖。

本次改造落地一个与「记忆功能」平级的 **Agent 对话** 能力：

- **信息架构**：`Agent` 作为 `ACTIVITY` 导航分组的直接子项，与 `Requests`、`Memories`、`Entities` 同级，不挂在 Memories 之下。
- **页面**：`/dashboard/agent`，左侧会话列表，右侧消息流，底部输入框，SSE 打字机输出。
- **后端**：按现有记忆接口的扁平命名风格实现 `POST /agent` 与 `/conversations*`；服务端负责鉴权、会话持久化、SSE 转发。
- **执行层**：`server/agent_runtime.py` 在进程内调用 `bokaclaw-agent` 完成 ReAct 循环，并向模型暴露 `search_memories` / `save_memory` 两个记忆工具。
- **视觉**：完全复用现有 shadcn/Radix 组件、语义颜色 token、`font-fustat`、`typo-*` 工具类，不引入新的 UI 库或视觉语言。

## 2. 关键决策

| 决策点 | 结论 |
| --- | --- |
| 前端导航层级 | `ACTIVITY` 分组新增 `Agent`，与 `Memories` 平级 |
| Agent 执行入口 | `POST /agent`；浏览器只访问 `server/`，不直连 agent-loop |
| 会话持久化 | 新增 Postgres 表 `conversations`、`conversation_messages` |
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
│  fetch + ReadableStream      │
└──────────────┬───────────────┘
               │ POST /agent (SSE)
               │ GET/POST/PUT/DELETE /conversations*
               ▼
┌──────────────────────────────┐
│  server (FastAPI)            │
│  - JWT / API Key 鉴权        │
│  - 会话与消息持久化           │
│  - SSE 解析 / 转发 / 落库     │
└──────────────┬───────────────┘
               │ 进程内调用（默认）
               ▼
┌──────────────────────────────┐
│  bokaclaw-agent (ReAct SDK)  │
│  - Agent 循环 / 工具调用      │
│  - 记忆检索与写入             │
└──────────────────────────────┘
```

需要独立部署执行层时，设置 `AGENT_LOOP_URL`，`server/` 改为向 `POST {AGENT_LOOP_URL}/invoke` 发起 SSE 请求，前端契约不变。

服务端是唯一公开入口。这样做的好处：

- 鉴权、会话归属、限流集中在 `server/`；
- 会话历史由 Postgres 统一持久化，Agent 执行层可以无状态；
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
  "metadata": {},
  "stream": true
}
```

字段说明：

- `conversation_id`：为空时自动创建会话，标题取首条消息前 80 个字符。
- `message`：去空白后不能为空，服务端上限 32,000 字符。
- `agent_id` / `run_id`：透传给执行层，用于记忆作用域或运行标识。
- `stream`：`false` 返回 JSON，`true` 返回 SSE。默认 `false`，Dashboard 固定传 `true`。

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
  }
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

事件顺序固定为：

```text
event: meta
data: {"conversation": {...}, "user_message": {...}}

event: message.delta
data: {"conversation_id": "uuid", "message_id": "assistant-uuid", "delta": "增量文本"}

event: message.completed
data: {"conversation_id": "uuid", "message": {...完整 assistant 行...}}

event: done
data: {"conversation_id": "uuid"}
```

失败时在 `done` 之前发送 `error`：

```text
event: error
data: {
  "code": "agent_unreachable | agent_timeout | agent_upstream_error | agent_protocol_error | conversation_deleted",
  "detail": "可读错误信息",
  "request_id": "abcd1234",
  "conversation_id": "uuid"
}
```

约定：

- `meta` 永远是第一个事件；
- `done` 永远是最后一个事件（成功和失败路径都要发）；
- 失败或被取消时不发 `message.completed`；
- 等待上游期间每 15 秒发送一次 `: ping` 注释心跳，防止代理断开空闲连接；
- 所有 `data:` 使用 JSON，非 ASCII 字符不转义。

### 4.3 会话资源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/conversations?limit=50&offset=0` | 当前用户会话列表，按 `updated_at desc` |
| POST | `/conversations` | 创建空会话，`title` 默认 `"New conversation"`，返回 201 |
| GET | `/conversations/{id}` | 会话详情，按 `(created_at, id)` 升序带完整消息 |
| PUT | `/conversations/{id}` | 重命名，body `{ "title": "..." }` |
| DELETE | `/conversations/{id}` | 删除会话及其消息（FK cascade） |
| GET | `/conversations/{id}/messages?limit=100&offset=0` | 分页读取消息 |

规则：

- 所有接口使用 `Depends(require_auth)` + `Depends(get_db)`；
- 查询必须过滤 `created_by == user.id`；
- 访问他人会话返回 404，而不是 403，避免泄漏资源是否存在；
- 列表返回裸数组，与 `/api-keys`、`/entities`、`/requests` 保持一致；
- 不接受客户端传入的 `user_id` 作为会话归属。

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

进程内执行时，除 `search_memories` / `save_memory` 外，还注册与 Claude Code 对齐的开发工具（`server/agent_dev_tools.py`）：

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

新增两张表，迁移文件为 `server/alembic/versions/007_create_conversations_and_messages.py`。

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

## 6. 后端实现

### 6.1 文件清单

| 文件 | 变更 |
| --- | --- |
| `server/models.py` | 新增 `Conversation`、`ConversationMessage` |
| `server/alembic/versions/007_create_conversations_and_messages.py` | 新建表、索引、CHECK、downgrade |
| `server/agent_schemas.py` | 新增请求/响应 Pydantic 模型 |
| `server/agent_store.py` | 会话/消息读写、UUID 解码、归属校验、标题生成、历史截断 |
| `server/agent_loop.py` | 执行层选择（mock / 远程 / 进程内）、上游 HTTP/SSE 客户端、SSE 编解码、错误映射 |
| `server/agent_events.py` | `AgentDelta` / `AgentCompleted` / `AgentLoopError` |
| `server/agent_runtime.py` | 进程内 `bokaclaw-agent` 包装、模型配置解析、记忆工具 |
| `server/agent_dev_tools.py` | Read / Write / Edit / NotebookEdit / Glob / Grep / Bash / PowerShell / WebFetch / Monitor |
| `server/routers/conversations.py` | `/conversations*` 路由 |
| `server/routers/agent.py` | `POST /agent` 路由与 SSE 生成器 |
| `server/main.py` | 注册两个 router |
| `server/requirements.txt` | 显式加入 `httpx`、`pypdf` |
| `tests/test_agent_runtime.py`、`tests/test_agent_dev_tools.py`、`tests/test_agent_router.py` | 进程内执行、工具、路由与 SSE 测试 |
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
- 可加 `@limiter.limit("30/minute")`，与 `server/routers/auth.py` 一致。

### 6.3 `POST /agent` 处理流程

1. 校验请求，读取当前用户；把 `request_id_var.get()` 保存到局部变量，供生成器使用。
2. 若 `AGENT_LOOP_MOCK != true`、`AGENT_LOOP_URL` 为空，且进程内运行时无法解析模型（`AGENT_MODEL` 与 mem0 LLM 均不可用，或 SDK 未安装），在开始流之前返回：

   ```json
   {
     "detail": "Agent execution is not configured. Set AGENT_MODEL and AGENT_API_KEY (or OPENAI_API_KEY), or set AGENT_LOOP_URL for an external executor.",
     "code": "agent_not_configured",
     "request_id": "abcd1234"
   }
   ```

3. 短事务内加载或创建会话；校验归属；按需生成标题。
4. 保存用户消息并提交；此时才调用上游。
5. 构造历史：只取已完成的 user/assistant 消息，默认最多 40 条、约 24,000 字符，避免超长上下文。
6. `stream=false`：完整消费上游后落库 assistant 消息，返回 JSON。
7. `stream=true`：返回 `StreamingResponse`，按 4.2 节发送事件。

### 6.4 落库时机与取消

| 场景 | 用户消息 | Assistant 消息 |
| --- | --- | --- |
| 正常完成 | 调用上游前已提交 | upstream 结束后提交，`status=completed` |
| 用户点 Stop / 浏览器断连 | 已提交 | 有部分内容时提交，`status=cancelled` |
| 上游超时/报错 | 已提交 | 有部分内容时提交，`status=failed`；空内容不插入 |
| 会话被删除 | 已提交 | 落库前重新校验归属；不存在则发送 `conversation_deleted` 并丢弃输出 |

其他要求：

- assistant 消息的 UUID 在开始流之前生成，并在每个 delta 中复用，保证前端消息 ID 稳定；
- 生成器 `finally` 中关闭上游连接，避免连接泄漏；
- 循环中检查 `await request.is_disconnected()`，捕获 `asyncio.CancelledError` 后保存部分内容并重新抛出；
- 心跳用 `asyncio.wait_for(..., timeout=15)` 实现，超时后发送 `: ping` 并继续等待。

### 6.5 错误码

| code | HTTP / 事件 | 含义 |
| --- | --- | --- |
| `agent_not_configured` | 503 | 无法解析模型（`AGENT_MODEL` / mem0 LLM 均不可用），或 SDK 未安装 |
| `agent_unreachable` | error / 502 | 外部执行器连接失败、DNS 失败 |
| `agent_timeout` | error / 504 | 读取超时或单轮总时长超限 |
| `agent_upstream_error` | error / 502 | 上游返回非 2xx，或模型调用失败 / 超过最大迭代轮数 |
| `agent_protocol_error` | error | 上游 SSE/JSON 格式不合法 |
| `conversation_deleted` | error | 流式过程中会话被删除 |

错误体与 `server/errors.py` 保持一致：`{ "detail", "code", "request_id" }`。

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
- `server/dev.Dockerfile`、`server/Dockerfile.all-in-one`、`server/Dockerfile` 都会 `COPY agent-loop /opt/agent-loop` 并安装，三套镜像都自带进程内执行层。

## 7. 前端实现

### 7.1 文件清单

| 文件 | 作用 |
| --- | --- |
| `dashboard/src/app/(root)/dashboard/agent/page.tsx` | 页面编排 |
| `dashboard/src/components/agent/conversation-list.tsx` | New chat、会话列表、active/hover、重命名/删除菜单 |
| `dashboard/src/components/agent/message-thread.tsx` | 消息列表、自动滚动、错误提示、首 token 前 loading |
| `dashboard/src/components/agent/message-bubble.tsx` | user/assistant 气泡、状态 caption、流式光标 |
| `dashboard/src/components/agent/chat-composer.tsx` | 自适应高度输入框、发送/停止、Enter/Shift+Enter、IME 兼容 |
| `dashboard/src/components/agent/agent-empty-state.tsx` | 未选会话 / 空会话状态 |
| `dashboard/src/components/agent/rename-conversation-dialog.tsx` | 重命名弹窗 |
| `dashboard/src/hooks/use-agent-chat.ts` | 会话加载、发送、SSE 状态机、停止、重命名、删除 |
| `dashboard/src/utils/sse.ts` | 增量 SSE 解析 |
| `dashboard/src/utils/api.ts` | 扩展既有 `postStream`：`AbortSignal`、SSE Accept、401 刷新、错误读取 `detail` |
| `dashboard/src/utils/api-endpoints.ts` | 新增 `AGENT_ENDPOINTS` |
| `dashboard/src/types/api.ts` | 新增 Conversation / ChatMessage 等类型 |
| `dashboard/src/app/(root)/dashboard/components/main-nav.tsx` | 新增 `Agent` 导航项 |

### 7.2 类型与端点常量

```ts
export type ChatRole = "user" | "assistant" | "system" | "tool";
export type MessageStatus = "completed" | "cancelled" | "failed";

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
}
```

```ts
export const AGENT_ENDPOINTS = {
  INVOKE: "/agent",
  CONVERSATIONS: "/conversations",
  CONVERSATION: (conversationId: string) =>
    `/conversations/${conversationId}`,
  MESSAGES: (conversationId: string) =>
    `/conversations/${conversationId}/messages`,
} as const;
```

### 7.3 SSE 解析

`dashboard/src/utils/sse.ts`：

```ts
export interface RawSSEEvent {
  event: string;
  data: string;
  id?: string;
}

export async function consumeSSE(
  response: Response,
  onEvent: (event: RawSSEEvent) => void,
  signal?: AbortSignal,
): Promise<void>;
```

实现要求：

- 使用 `response.body.getReader()` + `TextDecoder("utf-8")`；
- 按 `\n` 分行，兼容 CRLF；
- 空行结束一个事件；
- 以 `:` 开头的行是注释，忽略且不派发；
- 多个 `data:` 行用 `\n` 拼接；
- 没有 `event:` 时默认事件名为 `message`；
- `AbortError` 静默返回，其他读取错误重新抛出；
- 退出时释放 reader 并取消 body。

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
- streaming 时发送按钮切换为 Stop（`Square` 图标），调用 `AbortController.abort()`。

### 7.6 状态机

`use-agent-chat.ts` 维护：

```ts
conversations: Conversation[];
activeConversation: Conversation | null;
messages: ChatMessage[];
isStreaming: boolean;
isLoadingConversation: boolean;
error: { code?: string; message: string } | null;
```

行为：

- `send(text)`：空白或 streaming 中直接返回；乐观插入用户消息和 assistant 占位；
- `meta`：用服务端消息替换临时用户消息；新会话插入列表顶部；
- `message.delta`：按 `message_id` 追加文本，首次 delta 时把占位替换为服务端 ID；
- `message.completed`：替换为完整消息行并更新 `updated_at`；
- `error`：显示内联错误；`agent_not_configured` 在消息区提示“Agent execution is not configured yet. Set AGENT_MODEL and AGENT_API_KEY (or OPENAI_API_KEY) on the API server, or AGENT_LOOP_MOCK=true for local UI work.”，不能只弹 toast；
- `done`：结束 streaming；
- `stop()`：中断连接并保留已收到文本，服务端会以 `cancelled` 落库；
- 切换会话：`GET /conversations/{id}`，加载期间显示 `Skeleton`；
- 删除当前会话：清空选择并刷新列表；
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

# mock：期望 meta → message.delta… → message.completed → done
curl -N -sS -X POST http://localhost:8888/agent \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"message":"hi","stream":true}'

# 会话持久化
curl -sS http://localhost:8888/conversations -H "X-API-Key: $ADMIN_API_KEY"
curl -sS http://localhost:8888/conversations/<id> -H "X-API-Key: $ADMIN_API_KEY"
```

新增 `tests/test_agent_router.py`，沿用 `tests/test_api_keys_router.py` 的 `sys.path` + dependency override 模式，覆盖：

- 未配置时 503；
- 非法 UUID / 他人会话返回 404；
- 未认证返回 401；
- 上游错误映射；
- SSE 编解码、标题截断、历史截断等纯函数测试。

### 10.2 前端

```bash
cd dashboard
pnpm typecheck
pnpm lint
pnpm build
```

### 10.3 手工 E2E（`AGENT_LOOP_MOCK=true`）

1. 打开 `http://localhost:3000/dashboard/agent`，导航中 `Agent` 与 `Memories` 同级；
2. 新建会话 → 发送 → 打字机输出；
3. Stop 后保留部分文本，刷新后可见（`cancelled`）；
4. 会话自动命名、重命名、删除、切换；刷新后历史仍在；
5. 关闭 mock，清空 `AGENT_LOOP_URL` 并移除所有模型配置（`AGENT_MODEL` 与 mem0 LLM 都不可用）后，页面显示 `agent_not_configured` 内联提示；
6. 切换浅色/深色主题，颜色、边框、滚动条与现有页面一致；
7. 缩到 390px，检查 Sheet 侧栏与输入区可用。

### 10.4 进程内执行与远程切换

关闭 mock 后，默认由进程内 `bokaclaw-agent` 执行，模型来自 mem0 LLM 配置或 `AGENT_MODEL` / `AGENT_API_KEY` / `AGENT_BASE_URL`。要验证远程路径，启动一个实现 4.4 节契约的 stub，设置：

```bash
AGENT_LOOP_URL=http://host.docker.internal:8890   # 或 compose 内的 http://agent-loop:8890
```

验证服务端正确转发 delta、忽略上游 `message.completed` 中的完整正文（以累计 delta 为准），并正确落库。

## 11. 实施顺序

1. `server/models.py` + Alembic 007，跑通 upgrade → downgrade → upgrade；
2. `server/agent_schemas.py`、`server/agent_store.py`；
3. `server/agent_loop.py`（SSE 编解码、上游客户端、mock）；
4. `server/routers/conversations.py`、`server/routers/agent.py`，在 `main.py` 注册；
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
| 失败后手动重发产生重复用户消息 | MVP 文档说明；后续可加 `client_message_id` 唯一约束 |
| 多端并发发送同一会话 | 前端 streaming 时禁用输入；后端不加长事务锁，后续可加 `active_run_id` 条件更新 |
| 流式过程中会话被删除 | 落库前用新 session 重新校验归属，不存在则发 `conversation_deleted` 并丢弃输出 |
| 客户端断连/取消 | 保存部分内容为 `cancelled`，空内容不落库，`finally` 关闭上游连接 |
| XSS | Agent 输出按纯文本渲染，绝不 `dangerouslySetInnerHTML` |
| 超长对话 | 详情接口 MVP 返回完整线程；超长时使用 `/messages` 分页 |
| 未配置执行层 | 页面必须优雅展示 503 提示，不能出现半截流或未捕获异常 |
| all-in-one 打包 agent-loop | 三套 Dockerfile 已 `COPY agent-loop /opt/agent-loop` 并安装；修改打包方式时需同时验证 dev、production、all-in-one 镜像 |

## 13. 本次不做

- 不把 `agent-loop/` 作为独立服务部署（默认进程内运行），但保留 `/invoke` HTTP/SSE 契约作为远程扩展点；
- 不引入 Markdown 渲染、代码高亮、文件上传、多模态、工具调用可视化（后续可加 `react-markdown` + `remark-gfm`）；
- 不做多用户/多租户权限模型；会话归属固定为登录用户；
- 不新增全局状态管理；会话状态放在页面级 hook 中；
- 不新增图片资源或全局 CSS。
