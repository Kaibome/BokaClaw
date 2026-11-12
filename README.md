# BokaClaw

> 自托管的 AI Agent 工作台：持久记忆、ReAct 运行时、沙箱工具、链路追踪与离线评测，运行在你自己的基础设施上。

BokaClaw 是一个自托管的 AI Agent 工作台：工作区 Markdown 是记忆的权威存储，Postgres 只承载可重建的检索索引与业务数据。Next.js 控制台负责交互与运营，FastAPI 服务端负责鉴权、会话、执行与观测，`bokaclaw-agent` 负责可嵌入的 ReAct 循环。

项目当前处于快速演进阶段，接口与配置项仍可能调整；可独立安装的 SDK 为 `bokaclaw-agent`。

---

## 能力概览

| 模块 | 能力 |
|---|---|
| **持久记忆** | 工作区 Markdown（`USER.md` / `MEMORY.md` / 每日情节）为唯一权威存储；Postgres 全文+向量索引可重建；每用户隔离，支持信任级别、历史审计与轮末自动情节捕获 |
| **Agent 运行时** | `bokaclaw-agent`：OpenAI 兼容模型的 ReAct 循环，支持 `run()`、`stream()`、`arun()`、协作式取消、同步/异步工具 |
| **多轮会话** | 服务端持久化会话与消息；SSE 增量输出；断线重连、事件回放与 `after_seq` 游标 |
| **工具生态** | 文件读写、Glob/Grep、网页搜索与抓取、PowerShell/Bash、PPTX 生成；工具结果按预算回填模型 |
| **沙箱执行** | Shell 工具可委派给独立 executor 容器：只读根文件系统、非 root、无外网、仅挂载工作区 |
| **子代理** | 子 Agent 作为普通工具被调用，拥有独立上下文与工具白名单，结果结构化回传，父上下文只保留结论 |
| **上下文治理** | Token 预算压缩、滚动摘要、事实账本、步数提示、无进展检测与强制收尾 |
| **可观测性** | `agent / llm / tool / memory` 四类 span、完整事件流、Token 与成本统计、请求审计日志，Dashboard 可下钻 |
| **离线评测** | 基于 DeepEval 的 golden case 评测：循环检测、任务完成度、步骤效率、工具正确性、答案相关性 |
| **多租户与安全** | JWT 登录、`X-API-Key`、首启向导、API Key 管理、按 IP 限流、密钥脱敏、安全响应头 |

---

## 架构

```mermaid
flowchart LR
    Browser["浏览器"] --> Dashboard["Dashboard<br/>Next.js 15"]
    Dashboard -->|"HTTP / SSE"| Server["Server<br/>FastAPI"]

    Server --> Manager["AgentRunManager<br/>队列 · 租约 · 事件持久化"]
    Manager --> Runtime["bokaclaw-agent<br/>ReAct 运行时"]

    Runtime -->|"OpenAI 兼容协议"| Model["LLM / 压缩模型"]
    Server -->|"读写文件 + 索引"| Memory["工作区 Markdown"]
    Memory --> PG[("Postgres + pgvector<br/>派生索引")]

    Server -->|"Bash / PowerShell / Monitor"| Executor["Executor<br/>沙箱容器"]
    Server -->|"span / event"| PG
    Server -->|"评测任务"| Eval["DeepEval Worker"]
    Eval --> PG
```

职责边界：

- **Dashboard 只和 Server 通信**，从不直接调用模型，也不持有模型密钥。
- **Server 是唯一的写入方**：会话、消息、run、span、评测结果都由服务端落库。
- **`bokaclaw-agent` 是进程内库**，不负责排队、重试或跨重启续跑；这些由 Server 的 `AgentRunManager` 承担。
- **执行与连接解耦**：关闭 SSE 不会取消 run；重连可通过 `GET /agent/runs/{id}/events?after_seq=N` 继续消费。

---

## 记忆系统

工作区 Markdown 是记忆的唯一权威来源；Postgres 中的 `memory_index_*` 与 `memory_history` 只是可随时重建的检索索引和审计记录。

```text
<AGENT_MEMORY_ROOT>/users/<user_id>/
  USER.md                # 身份与稳定偏好，常驻注入
  MEMORY.md              # 显式保存的长期事实
  memory/YYYY-MM-DD.md   # 轮末情节
```

- **轮末捕获**：仅在 run `completed` 后追加一条经过噪声过滤与密钥脱敏的原始情节，不调用模型。已使用记忆写工具、子代理、cron/heartbeat 等非交互轮次会跳过。
- **显式写入**：`save_memory` 写入 `MEMORY.md`，`kind=identity` 写入 `USER.md`；`update_memory` / `delete_memory` 可通过 `AGENT_MEMORY_REQUIRE_APPROVAL=true` 走人工确认。
- **召回**：每轮加载小画像并执行混合检索（语义 + 全文 + 重要度 + 时间衰减），结果作为 user 角色的 `<recalled-memories>` 块注入，绝不写入 system prompt；`trust="untrusted"` 内容永不自动注入。
- **一致性**：先原子写文件，再更新索引；索引写入失败会把用户标记为 dirty。启动时和每 5 分钟后台对账，召回/搜索也会按文件 mtime/size 检测外部编辑并即时 reconcile。
- **每用户隔离**：目录为 `0o700`、文件为 `0o600`；`AGENT_MEMORY_ROOT` 不得位于 `AGENT_WORKSPACE_ROOT` 内，也不会挂载进 executor 容器。

维护命令（Compose 栈）：

```bash
docker compose exec server python -m app.memory.cli reconcile --all
docker compose exec server python -m app.memory.cli reindex --user <user_id>
docker compose exec server python -m app.memory.cli prune --retention-days 180
```

---

## 仓库结构

| 目录 | 说明 |
|---|---|
| `server/` | FastAPI 服务端：鉴权、记忆 API、Agent 执行、会话、span、评测、Docker 编排 |
| `dashboard/` | Next.js 15 控制台：Agent 对话、Trace、Eval、Memories、Requests、Entities、配置与密钥 |
| `agent-loop/` | 独立 Python SDK `bokaclaw-agent`：ReAct 循环、工具、子代理、上下文压缩、会话存储 |
| `tests/` | 跨包产品测试：Agent、鉴权、记忆 API、Trace、评测 |
| `server/app/memory/` | 工作区 Markdown 存储、派生索引、召回、capture、迁移与 reconcile |
| `server/app/eval/` | DeepEval 评测适配、指标、golden 数据与后台 worker |
| `docs/` | 架构与设计文档；`docs/memory/` 覆盖记忆格式、运维与迁移 |
| `.github/` | CI Gate 与三条产品流水线；详见 [`.github/AGENTS.md`](.github/AGENTS.md) |
| `deepeval/` | DeepEval 4.3.3 源码参考副本，供评测实现对照 |

---

## 快速开始

### 环境要求

- **运行整套服务**：Docker 24+ 与 Docker Compose v2
- **开发/测试 Python 包**：Python 3.10+（server 以 Docker 为唯一运行方式；SDK 与评测可直接用 Python）
- **开发 Dashboard**：Node.js 20+、pnpm 10+
- 可选：`hatch`（管理 `agent-loop` 的 Python 环境）

### 一键启动（推荐）

```bash
cd server
cp .env.example .env
```

至少填写：

```dotenv
POSTGRES_PASSWORD=<强随机密码>
JWT_SECRET=<openssl rand -base64 48>
OPENAI_API_KEY=<你的模型与 Embedding Key>
```

然后启动并完成首次初始化：

```bash
make bootstrap
```

该命令会：

1. 启动 Postgres（pgvector）、API 与 Dashboard；
2. 等待 API 与 Dashboard 就绪；
3. 创建第一个管理员账号；
4. 生成第一个 API Key。

> 终端 `=== Ready ===` 区块中的密码与 API Key 只打印一次，请立即保存；API Key 无法再次恢复。

也可以覆盖默认生成的凭据：

```bash
make bootstrap EMAIL=admin@company.com PASSWORD='strong-password' NAME='Admin'
```

### 浏览器向导

如果不使用 `make bootstrap`，可以自行启动后打开向导：

```bash
cd server
make up
```

访问 <http://localhost:3000>，按向导完成管理员注册与模型配置。

### 访问地址

| 服务 | 地址 |
|---|---|
| Dashboard | <http://localhost:3000> |
| REST API | <http://localhost:8888> |
| OpenAPI 文档 | <http://localhost:8888/docs> |
| Postgres（宿主机） | `localhost:8432` |

### 带沙箱执行器的四容器栈

默认 Compose 栈不包含 Shell executor。仓库内提供了独立脚本，额外启动只读根文件系统、无外网、仅挂载工作区的 `bokaclaw-executor`：

```bash
.claude/skills/bokaclaw-docker-stack/scripts/build-images.sh
.claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh
```

重建镜像后让容器使用新镜像：

```bash
RECREATE=1 .claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh
```

该栈会把 `server/.env` 中的 `AGENT_EXECUTOR_URL` 指向 `http://bokaclaw-executor:8099`，并把记忆工作区以 bind mount 方式挂载到宿主机：默认 `MEMORY_HOST=/Users/pankaibo/BokaClaw/memory` → 容器 `/app/memory`（只挂载到 server，不挂载到 executor）。可通过 `MEMORY_HOST=/path/to/memory` 覆盖。注意此脚本使用端口 **8000**（Compose 栈为 8888）。

### 单容器部署

`server/Dockerfile.all-in-one` 把 API、Dashboard 与 PostgreSQL 打进一个镜像，内置本地 fastembed 中文 Embedding 模型，适合单机演示或轻量部署：

```bash
docker build -f server/Dockerfile.all-in-one -t bokaclaw-all-in-one:test .
docker run -d --name bokaclaw \
  -p 3000:3000 -p 8000:8000 \
  -e POSTGRES_PASSWORD='<strong-password>' \
  -e JWT_SECRET="$(openssl rand -base64 48)" \
  -e OPENAI_API_KEY='<your-key>' \
  -v bokaclaw-data:/var/lib/postgresql/data \
  -v bokaclaw-memory:/app/memory \
  bokaclaw-all-in-one:test
```

### 常用运维命令

```bash
cd server

make down                    # 停止栈
make clean                   # 停止并删除数据卷（不可恢复）
make health                  # 检查 API / Dashboard / Postgres
make logs                    # 跟踪日志
make reset-admin-password EMAIL=admin@example.com PASSWORD='new-password'
make prune-logs              # 清理 30 天前的请求日志
```

---

## 核心配置

完整变量见 [`server/.env.example`](server/.env.example) 与 [`server/README.md`](server/README.md)。

### 基础

| 变量 | 说明 |
|---|---|
| `POSTGRES_PASSWORD` | 必填，Compose 缺少该值会拒绝启动 |
| `JWT_SECRET` | 启用鉴权时必填，建议 `openssl rand -base64 48` |
| `OPENAI_API_KEY` | 默认 LLM 与 Embedding 的密钥 |
| `MEMORY_BACKEND` | 固定为 `markdown`（工作区 Markdown 权威存储） |
| `AGENT_MEMORY_ROOT` | 记忆工作区根目录，默认 `/app/memory`，必须独立于 `AGENT_WORKSPACE_ROOT` |
| `AGENT_MEMORY_RECALL` | 是否启用每轮记忆召回，默认 `true` |
| `AGENT_MEMORY_RECALL_TOKENS` | 查询召回 Token 预算，默认 `1500` |
| `AGENT_MEMORY_RECALL_MAX` | 查询召回最大条数，默认 `15` |
| `AGENT_MEMORY_RECALL_THRESHOLD` | 最低语义相似度，默认 `0.4` |
| `AGENT_MEMORY_CAPTURE` | 是否在轮末追加情节，默认 `true` |
| `AGENT_MEMORY_EPISODIC_RETENTION_DAYS` | 情节保留天数，`prune` 使用，默认 `180` |
| `AGENT_MEMORY_RECONCILE_ON_START` | 启动时对账 dirty 工作区，默认 `true` |
| `AGENT_MEMORY_RECONCILE_INTERVAL_SECONDS` | 后台对账间隔，默认 `300` |
| `AGENT_MEMORY_REQUIRE_APPROVAL` | 更新/删除记忆是否走人工确认，默认 `false` |
| `MEMORY_DEFAULT_LLM_PROVIDER` | `openai` / `anthropic` / `gemini` / `deepseek` |
| `MEMORY_DEFAULT_LLM_MODEL` | 默认 `gpt-5-mini` |
| `MEMORY_DEFAULT_EMBEDDER_PROVIDER` | `openai` / `gemini` / `fastembed` |
| `MEMORY_DEFAULT_EMBEDDER_MODEL` | 默认 `text-embedding-3-small`；向量统一归一化并补齐到 `halfvec(3072)` |
| `AUTH_DISABLED` | 仅限本地开发，生产环境保持 `false` |
| `BOKACLAW_TELEMETRY` | 匿名遥测开关，`false` 可退出 |

### Agent

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_MODEL` | 复用 LLM 配置 | 覆盖 Agent 使用的模型 |
| `AGENT_API_KEY` / `AGENT_BASE_URL` | 复用 LLM 配置 | 接入 Qwen、vLLM、Ollama、OpenRouter 等兼容端点 |
| `AGENT_MAX_ITERATIONS` | `10` | 单轮 ReAct 最大步数 |
| `AGENT_FINAL_ANSWER_ON_MAX_ITERATIONS` | `true` | 步数耗尽时再做一次无工具请求，尽力作答 |
| `AGENT_CONTEXT_MAX_TOKENS` | `32000` | 模型视图的 Token 预算 |
| `AGENT_CONTEXT_COMPRESSION` | `summary` | `summary` 摘要压缩 / `truncate` 仅截断 |
| `AGENT_COMPRESSION_MODEL` | `AGENT_MODEL` | 压缩摘要专用模型，可指向更便宜的模型 |
| `AGENT_FACT_LEDGER` | `true` | 保留 run 内事实账本，避免压缩后遗忘 |
| `AGENT_PROGRESS_GUARD` | `true` | 检测无新证据的工具调用，nudge 后强制收尾 |
| `AGENT_SUBAGENTS_ENABLED` | `false` | 注册 `explore` / `plan` / `research_agent` 子代理 |
| `AGENT_DEV_TOOLS` | `true` | 注册文件、搜索、抓取、PPTX 等开发工具 |
| `AGENT_TOOLS_ALLOW_SHELL` | `false` | 注册 `Bash` / `PowerShell` / `Monitor` |
| `AGENT_EXECUTOR_URL` | — | 设置后 Shell 工具委派给沙箱 executor |
| `AGENT_WORKSPACE_ROOT` | `/app/workspace` | 文件工具可访问的唯一根目录 |
| `AGENT_SUBAGENT_MAX_CALLS_PER_RUN` | `4` | 每次父 run 允许的子代理调用次数 |

> 生产环境不要直接开启进程内 Shell。保留 `AGENT_EXECUTOR_URL` 指向隔离容器，才能获得文件系统与网络的边界。

---

## 使用 Dashboard

登录后可用的页面：

| 页面 | 用途 |
|---|---|
| **Agent** | 多轮对话、流式输出、停止生成、上下文用量与 Token 成本、工具调用展开、子代理嵌套展示 |
| **Requests** | API 调用审计：方法、路径、状态码、耗时、请求 ID |
| **Trace** | 按 run 查看 `agent / llm / tool / memory` span，下钻查看输入输出、耗时、错误与子代理层级 |
| **Eval** | 管理 golden case，发起评测，查看指标、通过率、成本与对应的 Agent run |
| **Memories** | 浏览、搜索、过滤、删除记忆；查看记忆历史 |
| **Entities** | 汇总所有 `user_id` / `agent_id` / `run_id`，支持级联删除 |
| **Configuration** | 运行时覆盖 LLM 与 Embedder，持久化后重启仍生效 |
| **API Keys** | 创建、标注、吊销用户级 API Key |
| **Settings** | 账号资料与密码 |

侧边栏中的 Categories、Webhooks、Analytics、Export 仅显示占位页，当前自托管栈不提供对应后端能力。

---

## REST API

除健康检查与文档外，默认都需要鉴权：`Authorization: Bearer <JWT>`、`X-API-Key: <key>`，或旧版 `ADMIN_API_KEY`。

| 分组 | 端点 |
|---|---|
| 鉴权 | `POST /auth/register`、`POST /auth/login`、`POST /auth/refresh`、`GET/PATCH /auth/me`、`POST /auth/change-password`、`GET /auth/setup-status` |
| API Key | `GET/POST /api-keys`、`DELETE /api-keys/{key_id}` |
| 记忆 | `POST/GET /memories`、`GET/PUT/DELETE /memories/{id}`、`GET /memories/{id}/history`、`POST /search`、`POST /reset` |
| 实体 | `GET /entities`、`DELETE /entities/{type}/{id}` |
| Agent | `POST /agent`、`GET /agent/runs/{id}`、`GET /agent/runs/{id}/events`、`POST /agent/runs/{id}/cancel`、`POST /agent/runs/{id}/answers` |
| 会话 | `GET/POST /conversations`、`GET/PUT/DELETE /conversations/{id}`、`GET /conversations/{id}/messages` |
| 观测 | `GET /requests`、`GET /traces`、`GET /traces/{span_pk}` |
| 评测 | `POST/GET /evals`、`GET /evals/{id}`、`POST /evals/{id}/cancel`、`GET/POST /evals/goldens`、`GET/PUT/DELETE /evals/goldens/{case_id}` |
| 配置 | `GET/POST /configure`、`GET /configure/providers`、`POST /generate-instructions` |

### 调用 Agent（SSE）

```bash
curl -N -X POST http://localhost:8888/agent \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "message": "总结一下我保存的发布计划，并列出风险",
    "stream": true
  }'
```

典型事件顺序：

```text
meta → memory.recall? → (tool.call → tool.result)*
     → message.delta* → message.completed → done → memory.save?
```

失败、取消或中断时不会发送 `message.completed`，而是：

```text
悬挂工具补发 tool.result(status="aborted") → error → done
```

`done` 不是成功信号，必须读取其中的 `status`。`meta` 最先出现，`done` 最后出现；每个持久化事件都带单调递增的 `id`，可用于重连去重。

### 调用 Agent（一次性 JSON）

去掉 `"stream": true` 即可在 run 完成后返回完整助手消息。

---

## Agent Runtime SDK

`agent-loop/` 可独立安装和嵌入任意 Python 服务。当前尚未发布到 PyPI，从仓库安装：

```bash
pip install ./agent-loop                # 仅内存会话
pip install "./agent-loop[postgres]"    # 增加 psycopg3 + 连接池
```

```python
import os

from bokaclaw_agent import AgentConfig, AgentRuntime, ModelConfig, tool


@tool
def get_weather(city: str) -> str:
    """Return the current weather for a city.

    Args:
        city: The city to look up.
    """
    return f"{city}: sunny, 25C"


runtime = AgentRuntime(
    AgentConfig(
        name="weather-agent",
        system_prompt="Use tools when they help. Answer concisely.",
        model=ModelConfig(
            model="deepseek-chat",
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com/v1",
        ),
    ),
    tools=[get_weather],
)

result = runtime.run("What's the weather in Beijing?", session_id="user-42")
print(result.content)
```

主要入口：

| 方法 | 行为 |
|---|---|
| `run(input, session_id=...)` | 阻塞执行一轮 ReAct，返回 `RunResult` |
| `stream(input, session_id=...)` | 逐段 yield 文本增量，生成器耗尽时返回 `RunResult` |
| `arun(...)` | `run()` 的异步包装 |

运行时还支持：

- `@tool` 自动从类型注解与 docstring 生成 JSON Schema，参数先经 Pydantic 校验；
- 工具失败默认以 `{"error": ...}` 回填给模型，可按需改为抛出；
- `cancel_event` 在安全检查点协作式取消，并落库为 `cancelled`；
- `InMemorySessionStore` 或 `PostgresSessionStore` 持久化会话；
- 子代理：独立 system prompt、上下文窗口与工具白名单，父级只接收结构化结果；
- 上下文压缩、事实账本、步数提示、无进展守卫；
- `on_event` 事件回调，覆盖模型请求、工具调用、压缩、子代理与运行结束。

完整 API、子代理任务包格式、持久化表结构与 HTTP executor 契约见 [`agent-loop/README.md`](agent-loop/README.md)。

---

## 开发

### 服务端

服务端约定使用 Docker 运行，不提供“直接 uvicorn 启动”的路径。

```bash
cd server
docker compose up -d --build
docker compose logs -f server
```

数据库迁移在容器启动时执行：

```bash
docker compose exec server alembic upgrade head
```

测试需要服务端依赖与一个可用的 Postgres/pgvector。CI 会启动 `pgvector/pgvector:pg17`、创建应用数据库并执行迁移；本地等价命令：

```bash
make server-test        # pytest server/tests + tests
make lint               # ruff check server / agent-loop / tests
```

只跑记忆后端：

```bash
pytest server/tests/test_memory_*.py -q
```

### Agent SDK

```bash
cd agent-loop
hatch env create
hatch run lint
hatch run format-check
hatch run test
```

- Ruff line length **120**，isort `profile = black`；
- 单元测试不访问网络，使用 `FakeProvider` 与 `InMemorySessionStore`；
- 真实 Postgres 测试由 `BOKACLAW_AGENT_TEST_DSN` 控制，标记为 `integration`。

### Dashboard

```bash
cd dashboard
pnpm install
pnpm dev          # http://localhost:3000
pnpm typecheck
pnpm build
pnpm lint         # prettier --check .
```

仅使用 pnpm；API 调用统一走 `src/utils/api.ts` 与 `src/utils/api-endpoints.ts`，不要把 token 写入 `localStorage`。

### CI

`.github/workflows/ci-gate.yml` 是 PR 的唯一必需检查，按改动路径调用三条产品流水线：

| 流水线 | 内容 |
|---|---|
| Server | Python 3.12 + pgvector service + Alembic；Ruff + pytest |
| Agent Runtime | Python 3.10 / 3.12 矩阵；Ruff + pytest |
| Dashboard | pnpm + Prettier + `tsc` + `next build` |

另有 `docs-llms-txt-check.yml` 校验 `docs/llms.txt` 覆盖，以及 PR Gate、Vouch、Labeler、Stale 等仓库治理工作流。详见 [`.github/AGENTS.md`](.github/AGENTS.md)。

### 评测

评测代码位于 `server/app/eval/`，依赖与 server 分开，`deepeval` 使用仓库内源码：

```bash
pip install -r server/eval-requirements.txt
pip install ./deepeval
pytest server/tests/test_eval_*.py tests/agent_eval -q
```

评测 worker 镜像为 `server/eval.Dockerfile`；运行中的栈会以 `bokaclaw-eval` 容器消费 DB 队列。

---

## 安全模型

- **默认开启鉴权**：Dashboard 使用 JWT，程序化访问使用 `X-API-Key`；`AUTH_DISABLED=true` 只用于本地开发。
- **工作区约束**：所有文件工具通过 `_resolve()` 解析路径，拒绝 `..` 与符号链接逃逸。
- **记忆隔离**：每个用户一个 `AGENT_MEMORY_ROOT/users/<user_id>/` 工作区，目录 `0o700`、文件 `0o600`；通用 `Write` / `Edit` 工具不能修改记忆文件，只有记忆工具可以。
- **信任级别**：`owner / agent / tool / untrusted / system` 由代码写入，不解析 Markdown 文本；`untrusted` 可存储、可搜索，但永不自动注入模型上下文。
- **Shell 隔离**：executor 容器只读根文件系统、非 root、丢弃全部 capability、仅内网可达、仅挂载工作区；子进程使用净化后的环境变量。
- **SSRF 防护**：`WebFetch` / `WebSearch` 默认禁止访问 localhost 与私有网段，可通过 `AGENT_WEB_FETCH_ALLOW_PRIVATE` 显式放开。
- **敏感信息脱敏**：span 写入前按规则过滤 `sk-*`、`Bearer`、`api_key`、`password`、`token` 等字段。
- **限流**：Agent 接口默认 30 次/分钟/IP；鉴权接口同样受限。
- **安全响应头**：Dashboard 设置 `X-Frame-Options: DENY`、`frame-ancestors 'none'`、`nosniff` 与严格 Referrer Policy。
- **遥测可关闭**：`BOKACLAW_TELEMETRY=false`。

---

## 文档索引

| 主题 | 文档 |
|---|---|
| 记忆系统总览 | [`docs/memory/overview.mdx`](docs/memory/overview.mdx) |
| 记忆 Markdown 格式 | [`docs/memory/markdown-format.mdx`](docs/memory/markdown-format.mdx) |
| 记忆运维与 REST/工具 | [`docs/memory/operations.mdx`](docs/memory/operations.mdx) |
| Agent SDK 使用与 API | [`agent-loop/README.md`](agent-loop/README.md) |
| Agent SDK 架构与分层 | [`docs/agent-loop/architecture.md`](docs/agent-loop/architecture.md) |
| 运行时行为与终止条件 | [`docs/agent-loop/runtime-behavior.md`](docs/agent-loop/runtime-behavior.md) |
| 子代理编排设计 | [`docs/agent-loop/subagent-orchestration-design-v2.md`](docs/agent-loop/subagent-orchestration-design-v2.md) |
| 执行与连接解耦 | [`docs/agent-loop/agent-execution-decoupling.md`](docs/agent-loop/agent-execution-decoupling.md) |
| Dashboard ↔ Server 契约 | [`docs/dashboard/agent-chat-design.md`](docs/dashboard/agent-chat-design.md) |
| DeepEval 评测原理 | [`docs/eval/deepeval-architecture.md`](docs/eval/deepeval-architecture.md) |
| Span 评测设计 | [`docs/eval/agent-span-evaluation.md`](docs/eval/agent-span-evaluation.md) |
| 构建问题记录 | [`docs/building-problem-record/problem-record.md`](docs/building-problem-record/problem-record.md) |
| 服务端运维与配置 | [`server/README.md`](server/README.md) |

---

## 贡献

- 提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/)（`feat:`、`fix:`、`docs:`、`refactor:`、`test:`）。
- 改动前先阅读目标目录下的 `AGENTS.md`；不同包的 linter、行宽与测试命令不同。
- 修改公共 API 时同步更新对应 README 与 `docs/`。
- 不要提交 `.env`、API Key 或任何凭据。
- 提交前运行受影响包的 lint 与测试。

行为准则见 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)，安全问题请按 [`SECURITY.md`](SECURITY.md) 私下报告。

---

## License

Apache-2.0，见 [`LICENSE`](LICENSE)。
