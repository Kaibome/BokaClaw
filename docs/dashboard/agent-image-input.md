# BokaClaw 接入多模态（Phase 1：用户上传图片）

## Context

BokaClaw 目前是纯文本 Agent：`POST /agent` 只接受 `message: str`，`conversation_messages.content`
是 `TEXT`，SDK 的上下文预算/压缩/工具结果都按字符串处理，Dashboard 输入框也只有 Textarea。
模型侧当前主要用 DeepSeek 文本模型；OpenAI 兼容 provider 本身是 `messages` 透传，因此“把图片
送给模型”在协议层并不难，成本主要来自贯穿 API、历史、token 预算、追踪和前端的一整套文本假设。

本次范围（已与用户确认）：

- **只做图片输入**：用户在 Agent 对话中点击上传、粘贴或拖拽图片；
- **只做用户上传**：不改 `Read` 工具读取工作区图片，不做文档/音频/图片输出；
- **视觉模型需要一起选型接入**：现有 DeepSeek 配置不足以看图，需要新增 OpenAI 兼容的视觉端点。

目标：让用户能在现有 Agent 对话里发图并得到基于图片内容的回答，同时不破坏纯文本链路、历史
回放、断线重连、取消和记忆。

## 结论：改造成本

**中等，不是架构重写，但跨层改动较多。** 生产级 Phase 1 约 **15–18 人日（单人约 3 周）**；
如果砍掉部分体验与治理（见“MVP 裁剪”），可压到 **10–12 人日**。

| 工作流 | 人日 | 说明 |
|---|---:|---|
| 上传/存储/归一化/鉴权下载 | 3 | multipart、magic bytes、EXIF、缩放、content-addressed 路径、孤儿清理 |
| Server 消息链路 | 3 | schema、payload、history、runtime content parts、memory、span 脱敏 |
| agent-loop token/压缩 | 2 | 图片 token 估算、压缩时不展开 base64、超预算先丢图 |
| Dashboard | 4 | 上传/粘贴/拖拽/预览、乐观消息、图片渲染、重试、i18n |
| 视觉模型接入与联调 | 2 | `AGENT_VISION_*`、按 run 切换、capabilities、真实图片 E2E |
| 测试/文档/部署 | 2.5 | 回归测试、docs、problem-record、本地栈 |

为什么不是更大：

- `OpenAICompatibleProvider` 不解析 `content`，OpenAI 风格的 `image_url` content parts 可以直接
  透传到模型端点；
- `AgentRuntime.run/stream` 和 `normalize_input` 已经接受 `list[dict]`；
- **不需要新表、不需要迁移**：附件描述符放进现有 `conversation_messages.metadata`，文件按
  `用户目录 + 内容哈希` 存放，归属由路径隔离；
- 不需要改 SSE 协议（输出仍是文本增量），不需要改工具协议。

为什么仍不能算小：

- `build_messages`、`build_history`、token 计数、压缩摘要、span 捕获、记忆捕获分别假设字符串；
  其中 token 计数和压缩是**上线前必须修**的阻塞项：base64 被 JSON 序列化后会让上下文预算瞬间
  爆掉，摘要 prompt 也会塞进整段 base64。

更便宜的替代（不推荐作为“多模态”交付）：先用视觉模型把图片描述成文字，再把描述注入
DeepSeek 的普通文本链路。约 **6–8 人日**，不需要改 SDK 的 content parts/token/压缩，但模型
看不到原始像素，追问细节和图表/截图理解会明显受限。若视觉模型的 function calling 质量不够，
可作为降级方案。

## 核心决策

| 决策 | 结论 |
|---|---|
| 附件持久化 | **不加表、不加迁移**。描述符写入 `conversation_messages.metadata.attachments`；文件按 `AGENT_ATTACHMENTS_ROOT/<user_id>/<sha256>.<ext>` 存放 |
| 附件 ID | 归一化后字节的 `sha256` + 扩展名；服务端用正则校验后再拼路径 |
| 模型输入 | OpenAI `image_url` content parts，data URL；只有 user 消息带图片 |
| 历史图片 | 保留最近 `AGENT_HISTORY_MAX_IMAGES`（默认 4）张，超出时从最旧开始丢弃并写文本占位符；base64 永不落库 |
| 模型路由 | 只要本次模型视图里含图片，**整个 run 切换到视觉模型**；纯文本 run 继续用 DeepSeek |
| 图片展示 | 前端用 Bearer + blob URL；不做公开 URL/签名 URL |

> 以后如果需要按用户配额、上传审计、附件删除/回收站，再补 `019_agent_attachments.py`。当前
> 设计的描述符仍在 message metadata 里，未来新增表时不需要迁移历史消息。

## 总体链路

```text
Dashboard composer
  ├─ 选择/粘贴/拖拽图片
  ├─ POST /agent/attachments (multipart) ──► magic bytes + EXIF + 缩放 + 落盘
  │                                          返回 { id: "<sha256>.<ext>", ... }
  └─ POST /agent { message, attachment_ids }
        └─ 校验 id/路径 → metadata.attachments = 描述符
              └─ 执行时读取文件 → base64 data URL
                    └─ user message content = [text, image_url, ...]
                          └─ OpenAI 兼容视觉模型（按 run 切换）
                                └─ 文本增量 SSE（协议不变）
```

`agent_runs.request`、`agent_run_spans`、SSE 事件、记忆和日志里只出现描述符/尺寸/字节数，
**绝不出现 base64 或文件路径**。

## 设计

### 1. 存储、ID 与归一化

**根目录**：`AGENT_ATTACHMENTS_ROOT`，默认 `/app/history/attachments`（复用现有持久化
`history` 卷，无需新增挂载）。启动时校验它不在 `AGENT_WORKSPACE_ROOT` / `AGENT_MEMORY_ROOT`
内部；不在就拒绝上传并记录错误。

**路径与 ID**：

- 布局：`<root>/<user_uuid>/<sha256>.<jpg|png>`；目录 `0700`，文件 `0600`。
- ID 正则：`^[0-9a-f]{64}\.(jpg|png)$`；服务端用 `created_by` 拼前缀，再 `resolve()` 校验
  仍在用户目录内，绝不接受客户端路径。
- 内容寻址：同一用户重复上传同一张图得到同一个 ID，天然去重；不加引用计数。
- **同名 sidecar**：每个附件旁写 `<sha256>.json`（`name`、`mime_type`、`bytes`、`width`、
  `height`、`created_at`），用于发送时重建描述符和下载时的展示名；否则内容寻址会丢失原始
  文件名。sidecar 与主文件同样 `0600`，由孤儿清理一起删除。
- 原子写：先写 `.<id>.tmp` / `.<id>.json.tmp`，`fsync` 后 `os.replace`。

**归一化**（上传时完成一次）：

- 先用 magic bytes 校验 PNG / JPEG / GIF87a-89a / RIFF-WEBP，再交给 Pillow；
- `ImageOps.exif_transpose` 后重新编码，**剥离 EXIF/GPS**；CMYK/RGBA 转 RGB（透明图保留 PNG）；
- 长边缩到 `AGENT_ATTACHMENT_MAX_EDGE`（默认 1568），JPEG quality 88 / PNG 优化；
- 动图只取第一帧；HEIC/HEIF 默认拒绝（如需 iPhone 原图再加可选 `pillow-heif`）；
- `Image.MAX_IMAGE_PIXELS` 限到约 40 MP，捕获 decompression-bomb 错误；
- 归一化后单图上限 3 MB、单轮总上限 6 MB；原始上传上限 `AGENT_ATTACHMENT_MAX_BYTES`（默认
  10 MB）。单图限制在上传时校验；单轮总量在 `POST /agent` 汇总 sidecar 后校验（上传时还不知道
  用户会组合哪几张）。

**生命周期**：

- 上传成功但未发送：文件留在磁盘；没有 DB 行，也没有 pending 状态。
- 发送时只校验“文件存在 + 属于当前用户”，不移动文件。
- 重试复用同一 ID；同一图片可被多条消息引用（内容寻址）。
- 孤儿清理：`server/scripts/prune_attachments.py` 先用一条查询把
  `conversation_messages.metadata->'attachments'` 里所有 `id` 读成集合，再扫描各用户目录，删除
  24h 前创建且不在集合中的文件（主文件 + sidecar）；启动时扫一次，之后按
  `AGENT_ATTACHMENT_PRUNE_INTERVAL_SECONDS`（默认 86400）周期执行。Phase 1 不做 DELETE 接口，
  避免“删了文件但消息还引用”的一致性问题。

### 2. API 与消息契约

新增 `server/app/routers/agent_attachments.py`（`APIRouter(prefix="/agent/attachments")`，全部
`Depends(require_auth)`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/agent/attachments` | `multipart/form-data`，字段 `file`；返回 `AttachmentOut`；限流 30/min/user |
| GET | `/agent/attachments/{id}` | 按当前用户目录读取；不存在/不属于当前用户都返回 404；`Cache-Control: private, max-age=31536000, immutable`、`ETag`、`X-Content-Type-Options: nosniff` |

`AttachmentOut`：

```json
{"id": "<sha256>.jpg", "name": "photo.jpg", "mime_type": "image/jpeg",
 "bytes": 412345, "width": 1568, "height": 1045}
```

不返回磁盘路径、sha256 之外的内部信息；`name` 只做展示，已清洗。

新增 `GET /agent/capabilities`（`server/app/routers/agent.py`）：

```json
{"image_input": true, "max_images": 4, "max_bytes": 10485760,
 "allowed_mime_types": ["image/jpeg", "image/png", "image/webp", "image/gif"]}
```

`image_input` 由 `AGENT_VISION_MODEL` 和附件根目录可写共同决定；Dashboard 用它决定是否显示
附件入口。端点不能证明视觉模型会 function calling，部署验证里保留一个手工 smoke test。

扩展 `AgentInvokeRequest`（`server/app/agent/schemas.py`）：

```python
message: str = Field("", max_length=32_000)
# schema 只做 8 的硬上限防滥用；实际数量以 capabilities.max_images / 环境变量为准
attachment_ids: list[str] = Field(default_factory=list, max_length=8)
```

用 `model_validator` 替代原来的非空校验：`message.strip()` 或 `attachment_ids` 至少有一个；
只有空白文字 + 附件时把 `message` 规范化为 `""`。附件 ID 逐个用上面的正则校验。

`_create_background_turn`（以及 legacy `_prepare_turn`）的改动：

1. 标题：文字为空时用 `Image: <首个附件 name>`；
2. 用户消息 `content` 仍是文字（可为 `""`），把描述符写入
   `message_metadata["attachments"]`；
3. `payload["message"]` 仍是文字，`payload["attachment_ids"]` 放 ID 列表（不是 base64）；
4. `agent_runs.request` 只存 ID/描述符；
5. 纯文本请求完全不受影响。

`ConversationMessageOut` 增加 `attachments: list[AttachmentOut] = []`；`serialize_message` 从
`message.meta["attachments"]` 读取并过滤畸形项。这样会话详情、`meta.user_message`、非流式
响应、刷新/重连都会自动带出附件。

### 3. 历史与模型输入

**持久化描述符**（每条用户消息）：

```json
{"id": "<sha256>.jpg", "name": "photo.jpg", "mime_type": "image/jpeg",
 "bytes": 412345, "width": 1568, "height": 1045}
```

**历史**（`server/app/agent/store.py::build_history`）：

- 用户消息有 `metadata.attachments` 时，即使 `content == ""` 也不能丢弃；输出
  `{"role": "user", "content": caption, "attachments": descriptors}`（`attachments` 是内部键，
  发给 SDK 前要剥掉）。
- 按从新到旧保留最多 `AGENT_HISTORY_MAX_IMAGES`（默认 4）张；被丢弃的图片在对应消息的
  caption 后追加 `[Earlier image omitted]`，没有 caption 时用该占位符作为 content。
- `_history_block_cost` 每保留一张图加固定字符成本（建议 4000），防止历史字符预算被图片撑爆。
- 历史图片文件缺失：跳过图片、追加 `[Image unavailable]`；**当前轮**图片缺失：返回
  `agent_attachment_missing`，不静默降级。

**模型视图**（`server/app/agent/runtime.py::build_messages`）：

- 当前用户消息和带附件的历史用户消息都展开为：
  ```python
  {"role": "user", "content": [
      {"type": "text", "text": caption},                       # 空文字时省略
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
  ]}
  ```
- `detail` 来自 `AGENT_VISION_IMAGE_DETAIL`（`auto|low|high`，默认空；空表示不传，兼容
  DashScope/GLM/vLLM）。OpenAI 端点可显式配置 `auto`。
- 数据 URL 在**执行时**从磁盘读取并 base64；不进入 `agent_runs.request`、SSE、span。
- 展开后剥掉内部 `attachments` 键，确保 provider 只看到标准字段。
- 工具结果、assistant 消息、流式 delta 保持字符串，不改工具协议。

**记忆**：

- 图片二进制不进入 Markdown/索引/embedding。
- 召回 query：文字为空时 query 留空（`recall_for_turn` 仍注入 profile，但跳过语义检索）；
- 轮末 `turn_memory` 用描述性占位符（如 `用户上传了图片：<name>`）拼进用户侧文本，并在
  `capture_turn` 里允许“带附件”通过 `_too_short` 检查，避免纯图片轮次不产生 episode。

### 4. 视觉模型

**路由规则**：只要本次模型视图里含 `image_url` part（当前轮或历史保留图），整个 run 使用
`AGENT_VISION_*` 的 `ModelConfig`；纯文本 run 继续用 `AGENT_MODEL`。压缩模型仍用
`AGENT_COMPRESSION_MODEL`（纯文本，只看到占位符）；子 Agent 不接收图片，继续用文本模型。

**配置**：

```bash
AGENT_VISION_MODEL=
AGENT_VISION_BASE_URL=
AGENT_VISION_API_KEY=
AGENT_VISION_IMAGE_DETAIL=       # 留空；OpenAI 可设 low|auto|high
AGENT_VISION_CONTEXT_WINDOW=     # 视觉模型窗口；缺省回退 AGENT_CONTEXT_WINDOW
AGENT_HISTORY_MAX_IMAGES=4
AGENT_IMAGE_TOKEN_ESTIMATE=1200
AGENT_REMOTE_VISION_SUPPORTED=false
```

- `AGENT_VISION_API_KEY` / `AGENT_VISION_BASE_URL` 为空时回退到 `AGENT_API_KEY` /
  `AGENT_BASE_URL`（同一网关同时提供文本和视觉模型时只需配模型名）。
- DashScope 快捷配置：设置 `DASHSCOPE_API_KEY` 和 `AGENT_VISION_MODEL`（实测
  `qwen3-vl-plus`）即可，`AGENT_VISION_BASE_URL` 会自动填
  `https://dashscope.aliyuncs.com/compatible-mode/v1`。模型可用性按地域/工作区不同：
  本机北京地域下 `qwen-vl-max-latest` 返回 403 `access_denied`、`qwen-vl-max` 返回 404，
  因此默认示例改用已验证的 `qwen3-vl-plus`。
- `LocalAgentConfig` 增加 `vision_model/vision_api_key/vision_base_url/vision_image_detail/
  vision_context_window/history_max_images/remote_vision_supported`；
  `build_runtime(use_vision=...)` 只把主模型切到视觉端点，子 Agent 仍用文本模型/凭据；
  `_completion_metadata` 接收实际使用的 window，确保 Dashboard 的 context meter 在图片 run 上
  显示视觉模型的窗口。
- 未配置 `AGENT_VISION_MODEL` 但请求带附件：在创建 run 之前返回 **503
  `agent_vision_not_configured`**；上传接口本身可以成功，避免用户先传图再失败。新增错误码
  `agent_attachment_missing` / `agent_vision_not_configured` /
  `agent_remote_vision_unsupported`，同步 `server/app/agent/events.py`、错误响应映射和文档错误表。
- 远程执行器（`AGENT_LOOP_URL`）默认不支持图片：`AGENT_REMOTE_VISION_SUPPORTED=false` 时带图
  请求返回 **400 `agent_remote_vision_unsupported`**；开启后要求远程执行器与 server 共享
  `AGENT_ATTACHMENTS_ROOT`，文档写明。

**选型建议**：

| 选项 | 接入方式 | 优点 | 风险/成本 |
|---|---|---|---|
| **vLLM 自托管 Qwen3-VL-8B / Qwen2.5-VL-7B** | 任意 OpenAI 兼容 `base_url` | 数据不出域，单卡可跑，中文/OCR 好 | 需要 GPU/运维；必须实测 tool parser |
| **Qwen VL（DashScope 兼容模式）** | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 无需 GPU，国内网络稳定，接入快 | 图片出境到阿里云；需确认所选型号 function calling |
| **GPT-4o-mini / GPT-4.1-mini** | 官方 OpenAI 兼容端点 | content parts/工具调用兼容性最好 | 网络与单价；图片出境 |
| **GLM-4V / GLM-4.1V** | 智谱 OpenAI 兼容 `/api/paas/v4` | 国内可用 | 视觉细节与工具调用需实测 |
| **Claude** | 需新增 Anthropic provider | 视觉能力强 | 当前 SDK 只有 OpenAI 兼容 provider，Phase 1 不选 |

推荐：**有 GPU 优先 vLLM 自托管 Qwen3-VL/Qwen2.5-VL；没有 GPU 用 DashScope 或 GPT-4o-mini**。

> Phase 1 已选定 **DashScope Qwen-VL（OpenAI 兼容模式）**，实测型号 `qwen3-vl-plus`；vLLM
> 自托管保留为后续数据不出域选项。已用“看图 + WebSearch”场景验证 function calling：模型能读出
> 图中文字并正确发起搜索；直接 API 测试也返回了带参数的 `tool_calls`。

### 5. SDK token 预算与压缩（上线前必须修）

1. `agent-loop/src/bokaclaw_agent/agent/context.py::count_message_tokens`
   - `content` 为 list 时逐 part 计数：text 用 tokenizer；`image_url` 用
     `AgentConfig.image_token_estimate`（默认 1200）；**不 JSON 序列化 base64**；
   - 未知 part 用有界 JSON 预览计数。
2. `compression.py`
   - `_format_transcript`：图片 part 渲染为 `[image: <id>]`，不把 base64 放进摘要 prompt；
   - `_messages_chars`：图片按固定字符数计，避免 `len(json.dumps(...))` 爆炸；
   - `_fit_budget`：超预算时先移除最旧消息里的图片 part（保留最新用户轮的图），再截断文本；
     结构（tool_call/tool 配对）不能破坏。
3. `AgentConfig` 增加 `image_token_estimate`（默认 1200）；server 从
   `AGENT_IMAGE_TOKEN_ESTIMATE` 传入。
4. `sessions/postgres.py` 的 `content TEXT` 不在 server 链路（server 不使用 SDK SessionStore）；
   若公开 SDK 支持 content parts，该列需要 JSON 序列化或迁移，作为已知限制写进 README。

### 6. 追踪、Span 与用量

- `_model_request_event` 在 `AGENT_SPAN_PROMPT_MODE=full` 下，把 `image_url.url` 替换为
  `data:image/...;base64,<redacted:N bytes>`，保留 mime/size/sha256；
- `spans.py::_redact` 增加 `data:image/...;base64,` 的兜底脱敏；
- `_message_hash` 对脱敏后的消息做哈希，避免每次哈希几 MB base64；
- SSE/usage 不变：输出仍是文本 delta；provider 返回的 `prompt_tokens` 已包含图片 token；
- 带图 run 的 `context.window_tokens` 用 `AGENT_VISION_CONTEXT_WINDOW`，不要错用文本模型窗口。

### 7. Dashboard

| 文件 | 改动 |
|---|---|
| `dashboard/src/types/api.ts` | 新增 `Attachment` / `AgentCapabilities`；`ChatMessage.attachments?: Attachment[]`；`AgentInvokeResponse` 同步 |
| `dashboard/src/utils/api-endpoints.ts` | `AGENT_ATTACHMENTS`、`AGENT_ATTACHMENT(id)`、`AGENT_CAPABILITIES` |
| `dashboard/src/utils/api.ts` | `uploadAttachment(file, onProgress, signal)`：axios FormData，复用 Bearer/401 刷新；`getAttachmentBlob(id)` |
| `dashboard/src/components/agent/chat-composer.tsx` | 回形针 + 隐藏 `<input type=file multiple>`；`onPaste` 读 `clipboardData.files`；`onDragOver/onDrop`；预览条（缩略图、名称、移除、上传中/失败）；`onSend(text, attachments)`；`text || attachments.length` 才允许发送；保留 Enter/Shift+Enter/IME |
| `dashboard/src/hooks/use-agent-chat.ts` | `send(text, attachments)`；允许空文字；乐观用户消息带描述符；POST `attachment_ids`；`meta.user_message.attachments` 到达后替换；`loadMessages` 从响应顶层 `attachments` 恢复；失败时保留草稿+附件 |
| `dashboard/src/components/agent/message-bubble.tsx` | 用户消息在文字上方渲染 1–4 张缩略图；点击用现有 `Dialog` 打开大图；空文字时只显示图片 |
| `dashboard/src/components/agent/authenticated-image.tsx`（新） | `getAttachmentBlob` → `URL.createObjectURL`，卸载时 revoke；失败显示占位与文件名 |
| `dashboard/src/app/(root)/dashboard/agent/page.tsx` | `handleRetry` 把上一条用户消息的 `attachments` 一起重发 |
| `dashboard/src/i18n/messages/en.ts` / `zh.ts` | 上传、拖拽、类型/大小/数量超限、上传失败、预览、移除、图片 alt 等 key |

交互约束：

- 选择文件后立即上传，附件条目显示进度/失败态；有附件仍在上传时禁用发送；
- 前端做类型/大小/数量校验，服务端必须独立再校验；
- 上传失败可单独重试或移除，不阻塞文字消息；
- 不在 `localStorage` 存图片或 token；blob URL 只在组件生命周期内存在；
- 不引入新 UI/图片库；用现有 `Button`、`Dialog`、`Spinner`、`toast`、语义色 token；
- `next/image` 不适用于需要 Bearer 的私有资源，直接用 `<img>` + blob URL。现有 CSP 只限制
  `frame-ancestors`，无需改；如果以后加 `img-src`，必须允许 `blob:`。

### 8. 配置、依赖与部署

`server/requirements.txt` 新增：

```text
python-multipart>=0.0.9,<1.0   # POST /agent/attachments multipart parsing
Pillow>=10.0,<12.0            # image validation/normalization/EXIF stripping
```

环境变量（同步 `server/.env.example`、`server/docker-compose.yaml`、
`server/entrypoint.all-in-one.sh`、`.claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh`）：

```bash
AGENT_ATTACHMENTS_ROOT=/app/history/attachments
AGENT_ATTACHMENT_MAX_BYTES=10485760          # 原始上传，单文件
AGENT_ATTACHMENT_MAX_NORMALIZED_BYTES=3145728  # 归一化后，单图
AGENT_ATTACHMENT_MAX_TOTAL_BYTES=6291456       # 归一化后，单轮合计
AGENT_ATTACHMENT_MAX_FILES=4
AGENT_ATTACHMENT_MAX_EDGE=1568
AGENT_ATTACHMENT_PRUNE_INTERVAL_SECONDS=86400
AGENT_HISTORY_MAX_IMAGES=4
AGENT_IMAGE_TOKEN_ESTIMATE=1200
AGENT_VISION_MODEL=
AGENT_VISION_API_KEY=
AGENT_VISION_BASE_URL=
AGENT_VISION_IMAGE_DETAIL=
AGENT_VISION_CONTEXT_WINDOW=
AGENT_REMOTE_VISION_SUPPORTED=false
```

部署注意：

- 复用现有 `history` 卷（`./history:/app/history`），不新增挂载；备份 history 卷时必须包含
  `attachments/`。
- 反向代理允许 10 MB 以上的请求体（nginx `client_max_body_size` 示例写进文档）。
- 当前 server 是单副本假设；多副本时 `AGENT_ATTACHMENTS_ROOT` 必须是共享存储。
- `AGENT_ATTACHMENTS_ROOT` 不得位于 `AGENT_WORKSPACE_ROOT` 或 `AGENT_MEMORY_ROOT` 内，也不
  挂载进 executor。

## 关键文件清单

**Server**

- 新增 `server/app/agent/attachments.py`：校验、EXIF 剥离、缩放重编码、content-addressed
  落盘/读取、content-part 构造、孤儿扫描。
- 新增 `server/app/routers/agent_attachments.py`：上传/下载。
- 新增 `server/scripts/prune_attachments.py`。
- 修改 `server/app/agent/schemas.py`、`server/app/routers/agent.py`、
  `server/app/agent/store.py`、`server/app/agent/runtime.py`、`server/app/agent/turn_memory.py`、
  `server/app/memory/capture.py`、`server/app/agent/spans.py`、`server/app/agent/events.py`、
  `server/app/main.py`。
- `server/requirements.txt`、`server/.env.example`、`server/docker-compose.yaml`、
  `server/entrypoint.all-in-one.sh`、`.claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh`。

**agent-loop**

- `agent-loop/src/bokaclaw_agent/core/config.py`（`image_token_estimate`）、
  `agent-loop/src/bokaclaw_agent/agent/context.py`、
  `agent-loop/src/bokaclaw_agent/agent/compression.py`、`agent-loop/README.md`。

**Dashboard**

- `dashboard/src/types/api.ts`、`dashboard/src/utils/api.ts`、`dashboard/src/utils/api-endpoints.ts`。
- `dashboard/src/components/agent/chat-composer.tsx`、`message-bubble.tsx`、新增
  `authenticated-image.tsx`；`dashboard/src/hooks/use-agent-chat.ts`；
  `dashboard/src/app/(root)/dashboard/agent/page.tsx`。
- `dashboard/src/i18n/messages/en.ts` / `zh.ts`。

**文档**

- `docs/dashboard/agent-chat-design.md`、新增 `docs/dashboard/agent-image-input.mdx`、
  `docs/docs.json`、`docs/llms.txt`、`docs/building-problem-record/problem-record.md`（P-40）、
  `server/README.md`、`agent-loop/README.md`。

## 实施顺序

1. **附件工具 + 路由**：`attachments.py`、`agent_attachments.py`、归一化、鉴权下载、孤儿脚本；
   用 curl 验证 PNG/JPEG/WebP/GIF、超限和越权。
2. **契约**：`schemas.py`、`_create_background_turn`/`_prepare_turn`、`payload`、
   `GET /agent/capabilities`、`ConversationMessageOut.attachments`；补 router/附件测试。
3. **历史与模型输入**：`build_history` 描述符 + 保留预算、`build_messages` content parts、
   title/memory 占位；补 history/runtime 测试。
4. **SDK 预算/压缩**：`count_message_tokens`、`_format_transcript`、`_messages_chars`、
   `_fit_budget`；补 agent-loop 测试。
5. **视觉模型**：`AGENT_VISION_*`、按 run 切换、503/400 错误码、context window；真实图片跑一次
   “图片 + 工具调用”。
6. **Span/记忆**：脱敏、哈希、episode 占位、`_too_short` 旁路；补 spans/turn_memory 测试。
7. **Dashboard**：types/endpoints/api → composer → hook → bubble → retry → i18n。
8. **文档与部署**：契约文档、新页面 + docs.json + llms.txt、problem-record P-40、env/compose/
   stack 脚本。
9. **回归**：全量 server / agent-loop 测试、`ruff`、`tsc`、`prettier`、`next build`。

## 必须新增/扩展的测试

- `tests/test_agent_attachments.py`（新）：magic bytes、超限、EXIF 剥离、缩放、alpha/GIF、去重、
  路径穿越、用户目录隔离、孤儿扫描、data URL 构造。
- `tests/test_agent_attachment_router.py`（新）：上传鉴权/限流、capabilities、下载 200/404/他人
  404、响应不泄漏路径。
- `tests/test_agent_history.py`（扩展）：空 caption 的图片消息不被丢弃；描述符回放；超过
  history 图片上限时丢最旧；字符成本；不含 base64。
- `tests/test_agent_runtime.py`（扩展）：current/history 图片展开；纯文本路径不变；vision 配置
  选择；缺文件行为；remote guard。
- `tests/test_agent_runs.py`（扩展）：POST 只持久化描述符；SSE meta 带 attachments；空 caption
  允许；未配置视觉模型 503；`request_payload` 不含 base64。
- `tests/test_agent_spans.py`（扩展）：`AGENT_SPAN_PROMPT_MODE=full` 下没有 `data:image`。
- `tests/test_agent_turn_memory.py`（扩展）：纯图片轮次产生 `用户上传了图片：...` episode。
- `agent-loop/tests/test_context.py` / `test_compression.py` / `test_runtime.py`（扩展）：
  图片 token 估算不按 base64、摘要转录用占位符、超预算先丢图、content parts 原样进入
  FakeProvider。

## 验证

```bash
# 后端
cd server && ruff check app tests
make server-test                      # server/tests + 根目录 tests/

# SDK
cd agent-loop && hatch run lint && hatch run test

# Dashboard
cd dashboard && pnpm typecheck && pnpm lint && pnpm build
```

手工 E2E：

1. `GET /agent/capabilities` 返回 `image_input=true`；未配置视觉模型时返回 false 或图片请求
   返回 503。
2. 上传 PNG/JPEG/WebP/GIF，确认归一化尺寸、EXIF 被剥离、`GET /agent/attachments/{id}` 需要
   Bearer，他人 id 返回 404。
3. 发送“这张图里有什么？”+ 图片：SSE 正常、`meta.user_message.attachments` 存在、刷新/断线
   重连后图片仍显示。
4. 纯图片无文字发送：标题、消息渲染、episode 占位符正常。
5. 5 张图 / 超 10MB / 伪造扩展名分别得到 400/413 和可读错误。
6. 图片 + 工具调用（“看图后搜索图中的产品名”），验证视觉模型 function calling。
7. 追问上一轮的图片：确认历史图片按配置保留；超过上限后旧图变为
   `[Earlier image omitted]`，run 回到文本模型。
8. 纯文本会话回归：不出现附件字段，行为与现在一致。
9. Trace 页检查 `data:image` 不出现；SQL 检查
   `request_payload::text NOT LIKE '%base64%'`。
10. 取消/失败/超时路径：部分文本、附件展示、带图重试。

## MVP 裁剪（如果先要 10–12 人日）

- 不做 `GET /agent/capabilities`，前端始终显示附件入口，靠 503 提示（省 0.5d）；
- 不做上传进度条/单附件重试，只保留 spinner（省 0.5d）；
- 不做大图 lightbox，缩略图点开用简单 Dialog（省 0.5d）；
- 不做自动孤儿周期扫描，只保留启动扫描/手工脚本（省 0.5d）；
- 不做历史图片保留，历史一律文本占位（回到“当前轮才发图”，省 0.5–1d，但追问体验下降）；
- 不接远程执行器，`AGENT_LOOP_URL` + 图片直接 400（省 1d）。

不可裁剪：附件校验/用户隔离、token 计数/压缩修复、span 脱敏、纯文本回归测试。这些是正确性
与成本底线。

## 主要风险

| 风险 | 缓解 |
|---|---|
| 视觉模型 tool calling 弱，带图后 ReAct 循环退化 | 选型时用“看图 + 搜索/读文件”实测；不达标降级为“视觉模型描述图片 → DeepSeek 执行工具循环” |
| base64 进入 token 计数/摘要/span，导致预算爆炸或 trace 膨胀 | 三处脱敏/占位作为阻塞项，先用回归测试锁住 |
| 历史图片让后续每轮都走视觉模型、成本上升 | `AGENT_HISTORY_MAX_IMAGES` 默认 4，超限丢最旧并回退文本模型；文档给出成本说明 |
| 不同 OpenAI 兼容端点对 `image_url`/`detail` 支持不一致 | 默认不传 `detail`；用真实端点跑 PNG/JPEG/WebP；保留纯文本回退 |
| 上传安全（伪造类型、EXIF/GPS、压缩炸弹、越权读取、路径穿越） | magic bytes + Pillow 重编码、剥离 EXIF、像素/字节上限、固定 ID 正则、用户目录隔离、鉴权下载 |
| 多副本/远程执行器读不到附件目录 | 文档要求共享存储；`AGENT_REMOTE_VISION_SUPPORTED=false` 默认拒绝 |
| 记忆/评测看不到图片，用户预期不一致 | 文档写清边界；episode 用文本占位；评测 Phase 1 只支持文本 golden |
| 磁盘增长/备份遗漏 | 启动+周期扫描；文档要求 history 卷与 Postgres 一起备份 |
