# Eval 控制台设计：从 Dashboard 触发在线评测

> 状态：设计中（2026-11-05）
>
> 适用范围：`dashboard/`、`server/`、`server/alembic/`、本地 Docker 栈
>
> 目标读者：Agent 执行层、后端、前端、评测开发者
>
> 前置阅读：[`agent-span-evaluation.md`](./agent-span-evaluation.md)。本文复用它的 span
> 采集与 deepeval 适配结论，并**取代**其中「golden 由评测脚本文件提供、不回写数据库」的
> 部分（见 §7）。

## 1. 背景与目标

### 1.1 现状

- 每次后台 agent run 已经落 `agent_runs`、`agent_run_events`、`agent_run_spans`；span 树为
  `agent → llm / tool`，包含 prompt、模型输出、工具入参/结果与 token usage。
- `tests/agent_eval/` 可以把 span 还原成 deepeval 用例并跑指标，但只能：

  ```bash
  python tests/agent_eval/run.py --run-id <uuid> --goldens tests/agent_eval/goldens.example.json
  ```

  结果只打印到 stdout，不落库、没有历史、没有失败重试，也无法在浏览器里使用。
- Dashboard 的 ACTIVITY 下已有 Agent、Requests、Trace、Memories、Entities，没有评测入口。

### 1.2 目标

1. 在 ACTIVITY 下新增 **Eval** 页面（`/dashboard/eval`）。
2. 页面选择 `eval_case_id`，可选勾选 LLM judge；点击 Run eval 后先按该 case 的 input
   跑一次 Agent，再评测这次 run。
3. 评测在独立容器中异步执行；页面轮询任务状态并展示：
   - 总体通过/失败；
   - 每个指标的分数、阈值、通过与否、理由；
   - 实际 judge 模型、token 成本、耗时；
   - 跳过/失败的指标与错误原因。
4. 结果持久化，页面提供历史列表；可重新运行、取消、跳转 Trace。
5. 全链路默认不把用户内容发给外部 judge；只有显式勾选且部署方开启开关时才调用。

### 1.3 非目标

- 批量/定时回归评测、CI 门禁、benchmark 集成（`evaluation/` submodule）。
- Golden 的批量导入与版本历史（单条 CRUD 已实现，见 §7.5；seed 脚本仍用于初始导入）。
- 自动挑选 run、自动生成 golden、在线告警。
- 修改 `POST /agent` 的 SSE 契约、修改 span 采集结构、修改 `agent-loop/` SDK。
- 对接 Confident AI 或任何评测结果上传服务。

## 2. 现状与约束

### 2.1 可复用的资产

| 资产 | 位置 | 复用方式 |
| --- | --- | --- |
| run 状态与终态集合 | `server/app/models.py` `AgentRun` / `TERMINAL_RUN_STATUSES` | 入队前校验 run 已结束 |
| span 读取 | `server/app/agent/repo.py` `list_spans`；路由 `server/app/routers/traces.py` | worker 直接按 `run_id` 读 span |
| 队列模式 | `RunRepository.claim/heartbeat/request_cancel/finish` | `eval_runs` 采用同一套租约语义 |
| 鉴权 | `server/app/core/auth.py` `require_admin` | `/evals` 全部 admin-only，与 `/traces` 一致 |
| 迁移风格 | `server/alembic/versions/009_create_agent_run_spans.py` | 010 沿用 BigInteger Identity、JSONB、CHECK、显式 index |
| deepeval 适配 | `tests/agent_eval/adapter.py` | 迁入 `server/app/eval/adapter.py`，单文件隔离私有 `_trace_dict` |
| Dashboard 表格/请求 | `dashboard/src/components/shared/data-table.tsx`、`dashboard/src/hooks/use-api-query.ts` | 历史表与初始加载 |
| 端点/类型 | `dashboard/src/utils/api-endpoints.ts`、`dashboard/src/types/api.ts` | 新增 `EVAL_ENDPOINTS` 与类型 |

### 2.2 硬约束

1. **依赖冲突不可调和。** `server/requirements.txt` 固定 `pydantic[email]==2.10.4`
   （FastAPI 0.115.8），deepeval 4.3.3 要求 `pydantic>=2.11.7,<3`。deepeval 不能装进
   server 进程，也不能装进 server 镜像的同一 site-packages。因此评测必须由独立镜像
   `bokaclaw-eval` 运行，server 只做校验、入队、查询。
2. **span 可能缺失。** `AGENT_SPAN_CAPTURE=false` 或 run 未被采样（`AGENT_SPAN_SAMPLE_RATE`）
   时没有 span，无法评测；必须在入队时返回明确错误，而不是入队后失败。
3. **run 必须是终态。** 运行中的 run 的 span 尚未结算，评测结果没有意义；只接受
   `completed / failed / cancelled / interrupted`。
4. **judge 会外发内容。** deepeval 的 LLM 指标会把 input、actual output、工具调用与
   golden 的 expected output 发给 judge。默认关闭；开启后 UI 必须明示会外发到哪个模型。
5. **DeepSeek base URL 固定。** deepeval 4.3.3 的 `DeepSeekModel` 在
   `deepeval/models/llms/deepseek_model.py` 中硬编码 `https://api.deepseek.com`；本期不
   支持自定义 base URL，也不为此 patch deepeval。
6. **结果随 run 删除。** `eval_runs.run_id` 是 `ON DELETE CASCADE`，run 清理后历史评测也会
   消失；这是可接受的语义，需在文档和 UI 中说明。

## 3. 总体架构

### 3.1 组件

```
┌──────────────────────┐        POST /evals (202)        ┌──────────────────────┐
│ Dashboard            │ ──────────────────────────────▶ │ FastAPI /evals       │
│ /dashboard/eval      │ ◀──── GET /evals/{id} 轮询 ──── │ admin-only           │
└──────────────────────┘                                 └──────────┬───────────┘
                                                                     │ INSERT queued
                                                                     ▼
                                                          ┌──────────────────────┐
                                                          │ Postgres             │
                                                          │ eval_goldens         │
                                                          │ eval_runs (队列)     │
                                                          │ agent_runs/spans     │
                                                          └──────────┬───────────┘
                                                                     │ claim / heartbeat
                                                                     │ finish / cancel
                                                          ┌──────────▼───────────┐
                                                          │ bokaclaw-eval worker │
                                                          │ deepeval 4.3.3       │
                                                          │ 独立镜像 / 独立容器  │
                                                          └──────────────────────┘
```

| 组件 | 职责 | 不负责 |
| --- | --- | --- |
| Dashboard Eval 页 | 输入、提交、轮询、展示、历史 | 不直接读 span，不计算指标 |
| FastAPI `/evals` | 参数校验、权限、入队、查询、取消 | 不 import deepeval，不执行评测 |
| `eval_runs` 表 | 持久化队列 + 结果 + 租约 | 不存 span 副本 |
| `eval_goldens` 表 | 参考数据源 | 不随 run 删除 |
| `bokaclaw-eval` | claim、执行 deepeval、写回结果 | 不接收外部 HTTP，不暴露端口 |

### 3.2 为什么用数据库队列

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| FastAPI `BackgroundTasks` / 线程 | 拒绝 | deepeval 无法与 server 共存（pydantic 冲突）；进程重启丢任务 |
| 新增 Redis/Celery | 拒绝 | 多一个基础设施；现有 stack 已有 Postgres |
| `eval_runs` + `FOR UPDATE SKIP LOCKED` | 采用 | 无新依赖；任务可重试、可恢复、可取消；与 `agent_runs` 模式一致 |
| 页面同步等待 | 拒绝 | judge 指标通常 10–60s，会触发网关/浏览器超时 |

### 3.3 请求时序

1. 用户在页面选择 `eval_case_id`，可选勾选 judge，点击 Run eval；页面先按该 case 的
   input 新建会话跑一次 Agent，轮询到 run 终态后才继续。
2. `POST /evals` 校验 run 存在、终态、有 span；校验 golden 存在；校验 case id 与 run
   metadata 一致；校验 judge 开关；检查是否已有同目标的活动任务。
3. 通过后把 golden 固化成 `golden_snapshot`（含 hash），插入 `eval_runs(status='queued')`，
   返回 `202` 与任务行。
4. 页面开始 1.5s 轮询 `GET /evals/{id}`。
5. worker 用 `SKIP LOCKED` claim 一条 queued 任务，置 `running`，启动心跳线程。
6. worker 读取 `agent_runs` / `agent_run_spans` 与入队快照，构造 `LLMTestCase`，执行指标。
7. worker 把指标、总体结果、成本、耗时写入 `eval_runs`，置终态；页面轮询到终态后停止。
8. 用户可从结果跳到 Trace（`/dashboard/trace?run_id=...`）或重新运行。

## 4. 数据模型与迁移 010

迁移文件：`server/alembic/versions/010_create_eval_tables.py`，
`revision = "010"`，`down_revision = "009"`。ORM 模型追加到 `server/app/models.py`。

### 4.1 `eval_goldens`

参考数据，按 `eval_case_id` 自然键，不随 run 删除。

| 列 | 类型 | 约束/默认 | 说明 |
| --- | --- | --- | --- |
| `eval_case_id` | `varchar(255)` | PK | 人工维护的稳定标识，如 `minimax-pricing-golden-01` |
| `input` | `text` | NOT NULL | 原始用户输入；deepeval `Golden.input` |
| `expected_output` | `text` | NULL | 参考输出 |
| `expected_tools` | `jsonb` | NOT NULL, `'[]'` | `ToolCall` 数组，见 §7.2 |
| `context` | `jsonb` | NOT NULL, `'[]'` | 字符串数组；本期不映射 retrieval_context |
| `notes` | `text` | NULL | 维护备注 |
| `created_at` | `timestamptz` | NOT NULL, `now()` | |
| `updated_at` | `timestamptz` | NOT NULL, `now()` | seed upsert 时刷新 |

可选 CHECK：`jsonb_typeof(expected_tools) = 'array'` 且 `jsonb_typeof(context) = 'array'`。
不在数据库层校验每个 ToolCall 的结构，由 seed 脚本与 worker 的 Pydantic 校验负责。

### 4.2 `eval_runs`

| 列 | 类型 | 约束/默认 | 说明 |
| --- | --- | --- | --- |
| `id` | `uuid` | PK，应用侧 `uuid4` | 任务 id |
| `run_id` | `uuid` | FK `agent_runs.id` ON DELETE CASCADE, NOT NULL | 被评测的 run |
| `created_by` | `uuid` | FK `users.id` ON DELETE SET NULL, NULL | 触发人；admin 删除后保留结果 |
| `eval_case_id` | `varchar(255)` | NOT NULL | 入队时使用的 case id（不设 FK，golden 可被删除） |
| `golden_snapshot` | `jsonb` | NOT NULL, `'{}'` | 入队时的 golden 内容，保证可复现 |
| `golden_hash` | `char(64)` | NOT NULL | 快照规范化后的 SHA-256 |
| `use_llm_judge` | `boolean` | NOT NULL, `false` | 本次是否调用外部 judge |
| `status` | `varchar(20)` | NOT NULL, `'queued'` | `queued/running/completed/failed/cancelled` |
| `metrics` | `jsonb` | NOT NULL, `'[]'` | 指标结果数组，见 §6.3 |
| `warnings` | `jsonb` | NOT NULL, `'[]'` | 字符串数组；截断、降级等非致命提示 |
| `raw_result` | `jsonb` | NULL | 脱敏后的 deepeval 结果摘要（不含 trace） |
| `overall_success` | `boolean` | NULL | 所有非跳过指标通过才为 true |
| `judge_model` | `varchar(128)` | NULL | 实际使用的模型（来自 metric data） |
| `token_cost` | `double precision` | NULL | 各指标 `evaluation_cost` 之和；未知为 NULL |
| `duration_ms` | `integer` | NULL | worker 执行耗时（不含排队） |
| `error_code` | `varchar(64)` | NULL | 失败分类，见 §6.2 |
| `error_detail` | `text` | NULL | 截断到 2000 字符，已脱敏 |
| `attempt` | `integer` | NOT NULL, `0` | claim 时 +1；达到上限后不再重试 |
| `worker_id` | `text` | NULL | claim 的 worker |
| `heartbeat_at` | `timestamptz` | NULL | 租约心跳 |
| `cancel_requested_at` | `timestamptz` | NULL | 协作取消标记 |
| `started_at` | `timestamptz` | NULL | claim 时间 |
| `finished_at` | `timestamptz` | NULL | 终态时间 |
| `created_at` | `timestamptz` | NOT NULL, `now()` | |
| `updated_at` | `timestamptz` | NOT NULL, `now()` | |

约束与索引：

```sql
CHECK (status IN ('queued','running','completed','failed','cancelled'))

-- 同一 run + case + judge 模式只允许一个活动任务；完成后可重跑。
CREATE UNIQUE INDEX uq_eval_runs_active_target
  ON eval_runs (run_id, eval_case_id, use_llm_judge)
  WHERE status IN ('queued','running');

CREATE INDEX ix_eval_runs_status_heartbeat ON eval_runs (status, heartbeat_at);
CREATE INDEX ix_eval_runs_run_created ON eval_runs (run_id, created_at DESC);
CREATE INDEX ix_eval_runs_created_at ON eval_runs (created_at DESC);
```

### 4.3 Golden 快照与 hash

入队时把 `eval_goldens` 行规范化后写入 `golden_snapshot`：

```json
{
  "eval_case_id": "minimax-pricing-golden-01",
  "input": "查询 MiniMax 最新模型的价格，并给出官方定价页链接。",
  "expected_output": "给出 MiniMax 最新模型（如 MiniMax-M3.1-Flash）的官方定价，包括每百万 tokens 的输入/输出价格，并附官方定价页链接。",
  "expected_tools": [{"name": "WebSearch"}, {"name": "WebFetch"}],
  "context": []
}
```

- 规范化：`json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`。
- `golden_hash = sha256(normalized.encode("utf-8")).hexdigest()`。
- worker **只使用快照**，不回查 `eval_goldens`。这样 golden 后续修改不会让历史结果失去可复现性；
  用户想用新 golden 重跑，会产生一条新的 `eval_runs`。
- worker 在评测前重算 hash，不一致则失败 `eval_golden_snapshot_corrupt`（理论上不会发生，
  用于发现手工改库）。

### 4.4 迁移要点

- 先建 `eval_goldens`，再建 `eval_runs`（后者逻辑上依赖前者但不设 FK）。
- 所有 JSONB 列显式 `server_default=text("'{}'::jsonb")` / `'[]'::jsonb`。
- `downgrade()` 先删 `eval_runs` 的索引与表，再删 `eval_goldens`；不触碰 `agent_runs` /
  `agent_run_spans`。

## 5. 任务生命周期

### 5.1 状态机

```
queued ──claim──▶ running ──成功/指标完成──▶ completed
  │                  │
  │                  ├──异常（依赖/执行）──▶ failed
  │                  ├──取消确认──────────▶ cancelled
  │                  └──租约过期──────────▶ queued（attempt+1，超限则 failed）
  │
  └──取消──────────▶ cancelled
```

- `completed` 表示评测流程跑完，不代表所有指标通过；是否通过看 `overall_success`。
- `failed` 表示无法得到结果（run 不存在于 worker 视角、golden 快照损坏、deepeval 崩溃等）。
- `cancelled` 表示用户取消。

### 5.2 入队校验（`POST /evals`）

按顺序执行，任一失败立即返回：

1. `run_id` 是合法 UUID，且 `agent_runs` 中存在。
2. `run.status` 属于 `TERMINAL_RUN_STATUSES`；否则 `agent_run_not_terminal`。
3. `EVAL_ENABLED=true`，否则 `eval_disabled`。
4. `SELECT count(*) FROM agent_run_spans WHERE run_id = :id` > 0；否则 `eval_no_spans`。
5. `eval_goldens` 中存在 `eval_case_id`；否则 `eval_golden_not_found`。
6. run 的 `request.metadata.eval_case_id` 非空且与请求不同 → `eval_case_mismatch`
   （防止用错误的参考数据打分）。tag 由 Dashboard Eval 页面的 Run eval 流水线写入（先按 case 输入跑 Agent，再评测）；普通对话不带该字段。
7. `use_llm_judge=true` 时：
   - `EVAL_JUDGE_ENABLED=true` 且 `EVAL_JUDGE_API_KEY` 非空，否则 `eval_judge_disabled` /
     `eval_judge_unavailable`。
8. 插入 `eval_runs`。若命中 `uq_eval_runs_active_target`，查询已有活动任务并返回
   `409 eval_already_running` + `eval_id`。
9. 返回 `202`。

### 5.3 claim 与心跳

worker 每轮执行一条单语句 CTE，避免先查后改的竞态：

```sql
WITH next AS (
  SELECT id FROM eval_runs
  WHERE status = 'queued' AND cancel_requested_at IS NULL
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE eval_runs e
SET status = 'running',
    worker_id = :worker_id,
    attempt = e.attempt + 1,
    started_at = now(),
    heartbeat_at = now(),
    updated_at = now()
FROM next
WHERE e.id = next.id
RETURNING e.*;
```

- `EVAL_HEARTBEAT_SECONDS`（默认 10）由 worker 的守护线程写 `heartbeat_at`。
  单次 judge 调用可能超过租约，不能只在阶段之间心跳。
- `EVAL_LEASE_SECONDS`（默认 60）是判定失联的阈值。
- 每次 claim 前先做一次 stale recovery：

```sql
-- attempt 未超限：重新排队
UPDATE eval_runs
SET status = 'queued', worker_id = NULL, heartbeat_at = NULL,
    cancel_requested_at = NULL, updated_at = now()
WHERE status = 'running'
  AND heartbeat_at < now() - make_interval(secs => :lease)
  AND attempt < :max_attempts;

-- attempt 已超限：置 failed
UPDATE eval_runs
SET status = 'failed',
    error_code = 'eval_lease_expired',
    error_detail = 'Worker heartbeat expired; max attempts reached.',
    finished_at = now(), updated_at = now()
WHERE status = 'running'
  AND heartbeat_at < now() - make_interval(secs => :lease)
  AND attempt >= :max_attempts;
```

### 5.4 取消

- `queued`：直接 `status='cancelled'`，`finished_at=now()`。
- `running`：写 `cancel_requested_at=now()`，状态仍为 `running`。worker 在以下时机检查：
  - claim 之后、开始评测之前；
  - 评测返回之后、写结果之前。
  deepeval 的 `evaluate()` 是阻塞调用，无法在单次 judge 请求内部中断；UI 把
  `running + cancel_requested_at != null` 显示为「取消中」。
- 终态任务取消返回 `409 eval_not_cancellable`。

### 5.5 完成写回与幂等

`finish` 只在 `status='running' AND worker_id=:worker_id` 时更新，重复调用无副作用。
租约过期后旧 worker 的迟到写回不会覆盖新 worker 的结果。

优雅停机（SIGTERM/SIGINT）不结算正在执行的任务：worker 停止 claim、不再写终态并退出，
行保持 `running`，由下一次启动的 stale recovery 重新排队。停机不是超时，不应写成
`eval_timeout`。

### 5.6 级联与保留

- 删除 `agent_runs` 行会级联删除该 run 的所有 `eval_runs`。
- `eval_goldens` 不自动清理。
- 可选后续脚本 `server/scripts/prune_eval_runs.py --days EVAL_RETENTION_DAYS`（默认 30），
  删除终态且 `finished_at < cutoff` 的行；本期不阻塞功能上线，但文档保留该扩展点。

## 6. API 契约

路由文件：`server/app/routers/evals.py`，`APIRouter(prefix="/evals", tags=["evals"])`，
在 `server/app/main.py` 与 `traces_router` 并列注册。全部依赖 `require_admin`。

### 6.1 端点

| 方法 | 路径 | 状态码 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/evals/config` | 200 | 是否启用、judge 是否可用、模型名、golden 数量、租约参数 |
| `GET` | `/evals/goldens` | 200 | DB 中的 case 列表（id、输入预览、工具名、更新时间） |
| `GET` | `/evals/goldens/{eval_case_id}` | 200 | 单条 case 完整内容（编辑前读取） |
| `POST` | `/evals/goldens` | 201 | 新建 case；重复 `eval_case_id` 返回 409 `eval_golden_exists` |
| `PUT` | `/evals/goldens/{eval_case_id}` | 200 | 覆盖更新已有 case；不存在返回 404 |
| `DELETE` | `/evals/goldens/{eval_case_id}` | 204 | 删除 case；历史评测保留冻结快照 |
| `POST` | `/evals` | 202 | 入队；响应为任务摘要 |
| `GET` | `/evals` | 200 | 历史列表；`limit`、`run_id`、`status`、`eval_case_id` 过滤 |
| `GET` | `/evals/{eval_id}` | 200 | 任务详情，含指标与错误 |
| `POST` | `/evals/{eval_id}/cancel` | 202 | 协作取消 |

路由声明顺序：静态路径 `/config`、`/goldens` 放在 `/{eval_id}` 之前；`eval_id` 参数声明为
`uuid.UUID`，非法值由 FastAPI 返回 422。

### 6.2 错误码

错误使用结构化 `detail`，便于前端映射文案：

```json
{
  "detail": {
    "code": "agent_run_not_terminal",
    "message": "Run 33ddd007-… is still running.",
    "run_id": "33ddd007-183a-4f6d-a1ad-698c6c5719e2"
  }
}
```

| HTTP | `code` | 触发条件 |
| --- | --- | --- |
| 401/403 | — | 非 admin；由 `require_admin` 统一返回字符串 detail，不属于本表的结构化错误 |
| 404 | `agent_run_not_found` | `run_id` 不存在 |
| 409 | `agent_run_not_terminal` | run 仍在 queued/running |
| 422 | `eval_no_spans` | run 没有 span（关闭采集、未采样、或远程 executor 未落 span） |
| 404 | `eval_golden_not_found` | `eval_case_id` 不存在 |
| 409 | `eval_case_mismatch` | run metadata 的 case id 与请求不一致 |
| 503 | `eval_disabled` | `EVAL_ENABLED=false` |
| 422 | `eval_judge_disabled` | 请求 judge，但 `EVAL_JUDGE_ENABLED=false` |
| 503 | `eval_judge_unavailable` | 请求 judge，但没有配置可用的 judge key |
| 409 | `eval_already_running` | 同目标已有活动任务；`detail.eval_id` 给出已有任务 |
| 409 | `eval_not_cancellable` | 取消终态任务 |

### 6.3 请求/响应模型

```python
class EvalCreateRequest(BaseModel):
    run_id: uuid.UUID
    eval_case_id: str = Field(min_length=1, max_length=255)
    use_llm_judge: bool = False


class MetricResult(BaseModel):
    name: str
    score: float | None
    threshold: float | None
    success: bool | None
    reason: str | None
    evaluation_model: str | None
    evaluation_cost: float | None
    skipped: bool = False
    error: str | None


class EvalRunItem(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    run_status: str              # join agent_runs 得到
    eval_case_id: str
    status: str
    use_llm_judge: bool
    overall_success: bool | None
    judge_model: str | None
    token_cost: float | None
    duration_ms: int | None
    error_code: str | None
    error_detail: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class EvalRunDetail(EvalRunItem):
    metrics: list[MetricResult]
    golden_hash: str
    attempt: int
    warnings: list[str]
```

`warnings` 直接写入 `eval_runs.warnings`（§4.2），详情接口原样返回，避免从 `raw_result`
二次解析。

请求示例：

```http
POST /evals
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "run_id": "33ddd007-183a-4f6d-a1ad-698c6c5719e2",
  "eval_case_id": "minimax-pricing-golden-01",
  "use_llm_judge": true
}
```

`202` 响应：

```json
{
  "id": "6f0c2c8e-6f4d-4f0e-9d3b-6a6f0f3f9a11",
  "run_id": "33ddd007-183a-4f6d-a1ad-698c6c5719e2",
  "run_status": "completed",
  "eval_case_id": "minimax-pricing-golden-01",
  "status": "queued",
  "use_llm_judge": true,
  "overall_success": null,
  "judge_model": null,
  "token_cost": null,
  "duration_ms": null,
  "error_code": null,
  "error_detail": null,
  "created_at": "2026-11-05T03:12:44Z",
  "started_at": null,
  "finished_at": null
}
```

完成后的详情（分数来自真实 run `33ddd007-183a-4f6d-a1ad-698c6c5719e2` 的一次验证，
仅作示例）：

```json
{
  "id": "6f0c2c8e-6f4d-4f0e-9d3b-6a6f0f3f9a11",
  "run_id": "33ddd007-183a-4f6d-a1ad-698c6c5719e2",
  "run_status": "completed",
  "eval_case_id": "minimax-pricing-golden-01",
  "status": "completed",
  "use_llm_judge": true,
  "overall_success": false,
  "judge_model": "deepseek-chat",
  "token_cost": 0.0051,
  "duration_ms": 12060,
  "golden_hash": "1f0d3b0f0d9b4c1e9a1e4c3b2d5f6a7b8c9d0e1f2a3b4c5d6e7f8091a2b3c4d5",
  "attempt": 1,
  "warnings": [],
  "metrics": [
    {"name": "Agent Loop Detection", "score": 1.0, "threshold": 1.0, "success": true,
     "reason": null, "evaluation_model": null, "evaluation_cost": null,
     "skipped": false, "error": null},
    {"name": "Answer Relevancy", "score": 0.24, "threshold": 0.5, "success": false,
     "reason": "The answer includes pricing but does not link the official pricing page.",
     "evaluation_model": "deepseek-chat", "evaluation_cost": 0.0012,
     "skipped": false, "error": null},
    {"name": "Task Completion", "score": 0.9, "threshold": 0.5, "success": true,
     "reason": "…", "evaluation_model": "deepseek-chat", "evaluation_cost": 0.0018,
     "skipped": false, "error": null},
    {"name": "Argument Correctness", "score": 1.0, "threshold": 0.5, "success": true,
     "reason": "…", "evaluation_model": "deepseek-chat", "evaluation_cost": 0.0009,
     "skipped": false, "error": null},
    {"name": "Tool Correctness", "score": 1.0, "threshold": 0.5, "success": true,
     "reason": "All expected tools were called.", "evaluation_model": null,
     "evaluation_cost": null, "skipped": false, "error": null},
    {"name": "Step Efficiency", "score": 0.5, "threshold": 0.5, "success": true,
     "reason": "…", "evaluation_model": "deepseek-chat", "evaluation_cost": 0.0012,
     "skipped": false, "error": null}
  ],
  "error_code": null,
  "error_detail": null,
  "created_at": "2026-11-05T03:12:44Z",
  "started_at": "2026-11-05T03:12:45Z",
  "finished_at": "2026-11-05T03:12:57Z"
}
```

`GET /evals/config`：

```json
{
  "enabled": true,
  "judge_enabled": false,
  "judge_provider": "deepseek",
  "judge_model": "deepseek-chat",
  "golden_count": 1,
  "max_attempts": 3,
  "lease_seconds": 60
}
```

`GET /evals/goldens`：

```json
[
  {
    "eval_case_id": "minimax-pricing-golden-01",
    "input_preview": "查询 MiniMax 最新模型的价格，并给出官方定价页链接。",
    "expected_tools": ["WebSearch", "WebFetch"],
    "updated_at": "2026-11-05T02:40:00Z"
  }
]
```

`input_preview` 截断到 200 字符；完整 input 不下发到列表，避免页面一次拉取大量文本。

## 7. Golden 管理（数据库）

### 7.1 数据源

- 运行时唯一来源是 `eval_goldens` 表；不再读取 `tests/agent_eval/goldens.example.json`。
- 仓库里的 JSON 只是**导入格式示例**，由 seed 脚本写入数据库。
- 入队时固化快照（§4.3），worker 不回查表。

### 7.2 字段映射

| `eval_goldens` / JSON | deepeval `Golden` | 说明 |
| --- | --- | --- |
| `eval_case_id` | — | 我方连接键；不传给 `Golden` |
| `input` | `input` | 必填字符串 |
| `expected_output` | `expected_output` | 可空 |
| `expected_tools[].name` | `expected_tools[].name` | 必填 |
| `expected_tools[].input_parameters` | `expected_tools[].input_parameters` | 可选 dict |
| `expected_tools[].output` | `expected_tools[].output` | 可选 |
| `expected_tools[].reasoning` / `description` / `type` | 同名 | 可选 |
| `context` | `context` | 字符串数组；本期只存储，不参与指标 |

seed 与 worker 都用 Pydantic 校验；worker 侧再构造 `deepeval.test_case.ToolCall`。

### 7.3 seed 脚本

`server/scripts/seed_eval_goldens.py`：

```bash
docker cp tests/agent_eval/goldens.minimax.json bokaclaw-server:/tmp/goldens.json
docker exec bokaclaw-server python scripts/seed_eval_goldens.py --file /tmp/goldens.json
```

- 输入：JSON 数组，每项必须有 `eval_case_id` 与 `input`。
- 行为：`INSERT ... ON CONFLICT (eval_case_id) DO UPDATE`，刷新 `updated_at`。
- 选项：`--dry-run` 只校验并打印；`--delete-missing` 删除文件中未出现的 case（默认关闭）。
- 退出码：0 成功；2 校验失败（打印第几项、哪个字段）。

示例文件 `tests/agent_eval/goldens.minimax.json`（实施阶段 1 新增；内容如下）：

```json
[
  {
    "eval_case_id": "minimax-pricing-golden-01",
    "input": "查询 MiniMax 最新模型的价格，并给出官方定价页链接。",
    "expected_output": "给出 MiniMax 最新模型（如 MiniMax-M3.1-Flash）的官方定价，包括每百万 tokens 的输入/输出价格，并附官方定价页链接。",
    "expected_tools": [
      { "name": "WebSearch" },
      { "name": "WebFetch" }
    ]
  }
]
```

### 7.4 匹配规则

- 页面必须显式提交 `eval_case_id`（不从 run 自动推断，避免误用参考数据）。
- 如果 run 的 `request.metadata.eval_case_id` 非空，必须与提交值一致；否则
  `eval_case_mismatch`。
- 如果 run 没有 `eval_case_id`（例如线上真实用户请求），允许用任意已存在的 case 评测，
  但 UI 应标注「该 run 未关联 case，使用手动选择的参考数据」。
- 后续若需要「一个 run 对应多个 case」，把唯一索引中的 `eval_case_id` 去掉即可；
  当前保持一对一，语义更清晰。

### 7.5 Case 管理

Eval 页右上角的 **Case management** 进入 `/dashboard/eval/cases`，提供单条 CRUD：

- 列表：`GET /evals/goldens`，列 case id、输入预览、期望工具、更新时间。
- 新建 / 编辑：`POST /evals/goldens`、`PUT /evals/goldens/{eval_case_id}`；
  `expected_tools` 以 JSON 数组编辑（如 `[{"name": "WebSearch"}]`），`context` 每行一条。
- 删除：`DELETE /evals/goldens/{eval_case_id}`，删除前二次确认。
- 入队时仍会冻结 `golden_snapshot`，所以编辑或删除 case 都不会改写历史评测结果。
- case id 有 URL 组成部分的限制：UI 中不要使用 `/`；如需支持任意字符，应改为按主键
  （非路径参数）访问。批量导入/版本历史仍是后续扩展。

## 8. 指标与 judge 策略

### 8.1 judge 可用性

生产环境不要把 deepeval 的 provider 环境变量直接散落在容器里；用统一的 `EVAL_JUDGE_*`
描述，worker 启动时再映射成 deepeval 认识的变量：

| 我方变量 | DeepSeek 映射 | OpenAI 映射 |
| --- | --- | --- |
| `EVAL_JUDGE_PROVIDER=deepseek` | `USE_DEEPSEEK_MODEL=true` | — |
| `EVAL_JUDGE_PROVIDER=openai` | — | — |
| `EVAL_JUDGE_MODEL` | `DEEPSEEK_MODEL_NAME` | `OPENAI_MODEL_NAME` |
| `EVAL_JUDGE_API_KEY` | `DEEPSEEK_API_KEY` | `OPENAI_API_KEY` |
| `EVAL_JUDGE_LANGUAGE=zh` | — | — |

- `EVAL_JUDGE_LANGUAGE` 控制 judge 生成的 `reason` / `analysis` 等说明文本语言，默认 `zh`（简体中文）；`en` 使用 deepeval 原始英文模板。实现方式是为 Answer Relevancy、Task Completion、Step Efficiency、Argument Correctness 和 Tool Correctness 的 reason 生成提示注入中文模板，JSON 字段名与 `yes/no/borderline` 等枚举值不受影响。
- 该设置只影响 judge 生成的说明文本。Agent Loop Detection 与 Tool Correctness 中由代码拼接的确定性说明仍是英文。
- `EVAL_JUDGE_ENABLED=false`（默认）时，`POST /evals` 拒绝 `use_llm_judge=true`。
- `EVAL_JUDGE_API_KEY` 只用于服务端判断「是否配置」和 worker 实际调用；不写入日志、
  不下发前端、不写进 `raw_result`。
- deepeval 4.3.3 的 DeepSeek 走 `https://api.deepseek.com`；OpenAI 走其默认 base URL。
  需要自定义网关时，必须升级/修改 deepeval，不在本期范围。
- 设置 `DEEPEVAL_TELEMETRY_OPT_OUT=1`，且不注入 `CONFIDENT_AI_API_KEY`；评测结果不上传。

### 8.2 指标矩阵

| 指标 | deepeval 类 | 需要 judge | 需要 golden | 数据依赖 | 默认阈值 |
| --- | --- | --- | --- | --- | --- |
| 循环检测 | `AgentLoopDetectionMetric` | 否 | 否 | `_trace_dict` | 1.0 |
| 工具正确性 | `ToolCorrectnessMetric` | 是* | 是 | `tools_called` + `expected_tools` | 0.5 |
| 回答相关性 | `AnswerRelevancyMetric` | 是 | 否 | input + actual_output | 0.5 |
| 任务完成度 | `TaskCompletionMetric` | 是 | 可选 | input + output + tools + trace | 0.5 |
| 步骤效率 | `StepEfficiencyMetric` | 是 | 否 | `_trace_dict` | 0.5 |
| 参数正确性 | `ArgumentCorrectnessMetric` | 是 | 否 | tool input_parameters | 0.5 |
| 目标达成度 | `GoalAccuracyMetric` | 是 | 否 | `ConversationalTestCase` turns + tools_called | 0.5 |

规则：

- 未勾选 judge：只跑 `AgentLoopDetectionMetric`。不产生外部调用与费用。
- 勾选 judge：追加 AnswerRelevancy、TaskCompletion、StepEfficiency、ArgumentCorrectness；
  如果当前 case 同时具备 `expected_tools` 与 `tools_called`，再加入 ToolCorrectness。
- `GoalAccuracyMetric` 需要 `ConversationalTestCase`，因此单独跑第二轮 `evaluate()`，再把两轮
  的 `metrics_data` 合并；对话 turns 由 `adapter.to_conversational_test_case()` 从
  `request_payload.history`、当前用户消息和最终 assistant 输出构造。
- \* deepeval 4.3.3 的 `ToolCorrectnessMetric.__init__` 会构造 judge 模型（即使只比较工具名），
  因此它必须和有 key 的 judge 一起启用；这与旧文档「确定性、无需 judge」的假设不同。
- ToolCorrectness 只在**当前 case**具备两侧工具数据时加入，而不是按文件是否有 golden 决定。
  这修复了旧 CLI 在批量时让缺参数的 case 触发 `MissingTestCaseParamsError` 的问题。
- 结果中的指标名来自 deepeval 的 `metric.__name__`（人类可读标签，如 `Agent Loop Detection`），
  不是类名；判断「被跳过」时必须用同一标签比对。
- 阈值本期沿用 0.5（循环检测 1.0），不做页面配置。

### 8.3 错误与跳过

`evaluate()` 使用：

```python
ErrorConfig(ignore_errors=True, skip_on_missing_params=True)
```

- `skip_on_missing_params=True`：缺少 retrieval_context 等参数的指标标记为跳过，不中断整轮。
- `ignore_errors=True`：单个 judge 调用失败不丢失其他指标结果；失败指标写 `error`，页面红色
  展示。若所有指标都失败，`overall_success=false`，任务仍是 `completed`。
- `skipped_metrics`：由 worker 根据 `MetricData.error` 是否属于缺参错误、或指标未加入来填充；
  响应中以 `skipped: true` + `error` 表达。

### 8.4 成本与耗时

- `token_cost` = 各 `MetricData.evaluation_cost` 之和；全部为 `None` 时为 `None`（页面显示
  「—」）。
- `judge_model` 取第一个非空 `MetricData.evaluation_model`。
- `duration_ms` 为 worker 从 claim 到写回结果的实际耗时；入队等待时间不计入。
- DeepSeek 的单价可通过 `DEEPSEEK_COST_PER_INPUT_TOKEN` /
  `DEEPSEEK_COST_PER_OUTPUT_TOKEN` 提供给 deepeval，否则成本可能为 `None`。
- 参考量级：上文真实 run 的 6 个指标约 12 秒、$0.005；judge 指标越多越慢，本期不设并发。

### 8.5 隐私与截断

- 只有 `use_llm_judge=true` 才把内容发往外部；未勾选时 worker 不初始化任何 LLM provider。
- 勾选时，worker 在构造测试用例前按 `EVAL_JUDGE_MAX_CHARS`（默认 20000）截断
  `input`、`actual_output`、`expected_output`、工具入参与输出，并在 `warnings` 里记录被截断
  的字段。确定性指标仍使用完整 span（已由 `AGENT_SPAN_MAX_BYTES` 截断）。
- 结果页展示 `judge_model` 与费用，让用户知道这次评测调用了什么。

## 9. 评测执行重构

### 9.1 模块划分

```
server/app/eval/
├── __init__.py
├── config.py        # 读取 EVAL_*，纯 stdlib，不 import deepeval
├── goldens.py       # GoldenPayload/ToolCallPayload 校验、快照、hash、DB 读取
├── adapter.py       # 从 tests/agent_eval/adapter.py 迁入，唯一 deepeval 私有接触点
├── metrics.py       # 按 case/开关构造指标，修复 ToolCorrectness 门控
├── evaluator.py     # evaluate_run(run, spans, golden_snapshot, use_llm_judge) -> EvalOutcome
├── repo.py          # EvalRepository：create/claim/heartbeat/finish/cancel/recover/list/get
├── worker.py        # 主循环、心跳线程、信号处理
├── cli.py           # python -m app.eval.cli，手工/离线触发
└── healthcheck.py   # DB 连通 + 心跳文件新鲜度
```

约束：

- `config.py` / `repo.py` / `goldens.py` 不能 import deepeval，server 可以安全导入它们。
- `adapter.py` / `metrics.py` / `evaluator.py` 只在 eval 容器中导入。
- server 路由只使用 `config.py`、`goldens.py`、`repo.py` 与 ORM 模型。
- 不把 `app.agent.repo` 作为 worker 的依赖（避免未来它引入 `agent-loop`）；worker 的
  `EvalRepository` 直接查询 `AgentRun` / `AgentRunSpan`。

### 9.2 `EvalOutcome`

```python
@dataclass
class EvalOutcome:
    overall_success: bool
    metrics: list[dict]
    warnings: list[str]
    judge_model: str | None
    token_cost: float | None
    raw_result: dict
    duration_ms: int
```

`evaluator.evaluate_run()` 负责：

1. 反序列化 `golden_snapshot`，校验 hash。
2. `adapter.to_llm_test_case(spans, run=run, golden=Golden(...))`。
3. `metrics.build_metrics(case, use_llm_judge=...)`。
4. `deepeval.evaluate(test_cases=[case], metrics=metrics, error_config=ErrorConfig(...))`。
5. 把 `MetricData` 转成 `MetricResult`；计算 `overall_success`；序列化脱敏的
   `raw_result`（只保留 `name/success/metrics_data/metadata`，剔除 `_trace_dict`、
   `verbose_logs` 与完整 prompt）。
6. 返回 `EvalOutcome`；异常抛 `EvalExecutionError(code, detail)`。

### 9.3 CLI

`server/app/eval/cli.py`：

```bash
python -m app.eval.cli \
  --run-id 33ddd007-183a-4f6d-a1ad-698c6c5719e2 \
  --eval-case-id minimax-pricing-golden-01 \
  --judge \
  --json
```

- `--eval-case-id` 省略时读取 run metadata；再省略则不使用 golden。
- `--judge` 仍需 `EVAL_JUDGE_ENABLED=true`，与 API 一致。
- `--json` 输出 `EvalOutcome`；默认输出人类可读表格。
- 退出码：0 全部通过；1 有指标失败；2 输入/配置错误；3 执行错误。
- `tests/agent_eval/run.py` 保留为薄壳，内部调用 `app.eval.cli.main()`，避免破坏现有
  文档命令；`tests/agent_eval/adapter.py` 改为从 `app.eval.adapter` 重新导出，契约测试
  继续跑。
- 同步更新 `docs/eval/agent-span-evaluation.md` §5 的路径与「golden 文件」描述，避免两份
  文档冲突。

## 10. 前端设计

### 10.1 导航

`dashboard/src/app/(root)/dashboard/components/main-nav.tsx` 在 Trace 之后插入：

```ts
{
  title: "Eval",
  url: "/dashboard/eval",
  icon: FlaskConical,
  active: pathname === "/dashboard/eval",
}
```

`FlaskConical` 来自现有 `lucide-react`，不新增图标库。

### 10.2 页面结构

`dashboard/src/app/(root)/dashboard/eval/page.tsx`，客户端组件：

```
Eval                                  [Case management] [Refresh]
先按 golden case 跑一次 Agent，再对这次 run 打分

┌─ New evaluation ──────────────────────────────────────────────────┐
│ Eval case *   [minimax-pricing-golden-01                 ▼ ]      │
│               用该 case 的 golden input 新建会话并打 tag，        │
│               run 结束后自动提交评测                              │
│ [ ] Use LLM judge (deepseek-chat) · sends run content to the judge│
│                                                                   │
│ [running agent] [View trace]                       [Run eval]     │
└───────────────────────────────────────────────────────────────────┘

┌─ Result dialog（点击历史行或提交后打开）──────────────────────┐
│ completed · FAILED OVERALL · 12.1s · $0.0051 · deepseek-chat │
│ Metric                         Score  Threshold  Status       │
│ AgentLoopDetectionMetric        1.00      1.00    ✓          │
│ AnswerRelevancyMetric           0.24      0.50    ✗ reason…  │
│ …                                                            │
│ [Open trace] [Run again] [Cancel]                            │
└──────────────────────────────────────────────────────────────┘

┌─ History ────────────────────────────────────────────────────┐
│ When        Run        Case                    Status  Overall│
│ 2m ago      33ddd007   minimax-pricing-golden-01 completed FAIL │
└──────────────────────────────────────────────────────────────┘
```

表单只保留 Eval case、Use the LLM judge 和 Run eval 三个元素；Run ID 与独立的
「Run case input」按钮均已移除，跑 Agent 与提交评测由同一个按钮串联。

### 10.3 URL 与状态

**Run eval 是一条流水线。** 点击后依次完成「按 case 输入跑一次 Agent」和「评测这次 run」：

1. `GET /evals/goldens/{eval_case_id}` 取完整 `input`（列表接口只有截断预览）；
2. `POST /agent`，`conversation_id=null`（每次新建会话，保证历史为空）、
   `message=golden.input`、`metadata={"eval_case_id": ...}`；
3. 读取 SSE 第一条 `meta` 拿到 `run_id` 后立即断开 reader，后台 run 继续执行；
4. 轮询 `GET /agent/runs/{run_id}` 直到终态；期间按钮显示 `Running agent…`，
   状态行显示 `running agent` 徽标与 `View trace` 链接；
5. 只有 `completed` 的 run 才提交 `POST /evals {run_id, eval_case_id, use_llm_judge}`；
   其他终态（failed / cancelled / interrupted）提示「Agent run did not complete」，
   保留 Trace 链接且不入队评测；
6. 提交成功后打开结果 Dialog，轮询 `GET /evals/{id}`；`Run eval` 恢复可用。

表单不再接收 `run_id`，只保留 Eval case、Use the LLM judge 与 Run eval。
Trace 页的「Evaluate this case」按钮在 agent span 的 `input.eval_case_id` 非空时出现，
跳转 `/dashboard/eval?eval_case_id=<case>` 预填该 case。

- 使用 `useSearchParams()`，页面必须用 `<Suspense>` 包裹（Next.js 15 构建要求）。
- 页面本地状态：

  ```ts
  const [caseId, setCaseId] = useState("");
  const [useJudge, setUseJudge] = useState(false);
  const [activeEvalId, setActiveEvalId] = useState<string | null>(null);
  const [detail, setDetail] = useState<EvalRunDetail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [phase, setPhase] = useState<"idle" | "running" | "evaluating">("idle");
  const [agentRunId, setAgentRunId] = useState<string | null>(null);
  const [agentRunError, setAgentRunError] = useState("");
  const [pollError, setPollError] = useState("");
  const mountedRef = useRef(true);
  ```

- `useApiQuery` 加载 `/evals/config`、`/evals/goldens`、`/evals?limit=50`；它本身不轮询。
- 提交成功后设置 `activeEvalId` 并启动轮询；页面卸载时停止 Agent 轮询，且不再入队评测。

### 10.4 轮询

```ts
const POLL_MS = 1500;
const MAX_POLL_MS = 15 * 60 * 1000;
```

- Agent run：`Run eval` 启动 run 后每 1.5s 轮询 `GET /agent/runs/{id}`，直到终态；
  `mountedRef` 在页面卸载时停止轮询，且不再提交评测。
- Eval 任务：`activeEvalId` 变化后用 `setTimeout` 链式轮询（不是 `setInterval`），
  避免慢请求堆叠；终态停止。
- 两者都设 15 分钟上限，超时后停止并提示「任务仍在后台运行，可稍后刷新」。
- `running + cancel_requested_at` 显示「取消中」，继续轮询直到终态。

### 10.5 结果展示

- 顶部状态：`queued / running / completed / failed / cancelled`，完成时叠加
  `overall_success` 的 PASS/FAIL。
- 指标表：名称、分数（保留两位）、阈值、状态、模型、费用；点击行展开 `reason`。
- 跳过指标与 `warnings` 单独列出，不混入失败。
- 失败任务显示 `error_code` 与 `error_detail`，提供「重试」。
- 链接：
  - `Open trace` → `/dashboard/trace?run_id=<run_id>`
  - `Run again` → 以当前表单值重新 POST
  - `Cancel` → `POST /evals/{id}/cancel`，成功后继续轮询
- 费用未知显示「—」，不要显示 0。

### 10.6 历史表

复用 `DataTable`，列：创建时间、run id（截断 + 可复制）、case、状态、overall、耗时、费用。
默认 `limit=50`，不做分页（与 Trace 页一致）；行点击后在 Dialog 中加载详情，关闭弹窗
停止轮询并刷新历史，不跳页。
状态用现有 Badge 语义色；不新增颜色 token。

### 10.7 端点和类型

`dashboard/src/utils/api-endpoints.ts`：

```ts
export const EVAL_ENDPOINTS = {
  BASE: "/evals",
  CONFIG: "/evals/config",
  GOLDENS: "/evals/goldens",
  GOLDEN_BY_ID: (evalCaseId: string) => `/evals/goldens/${encodeURIComponent(evalCaseId)}`,
  BY_ID: (evalId: string) => `/evals/${evalId}`,
  CANCEL: (evalId: string) => `/evals/${evalId}/cancel`,
} as const;
```

`dashboard/src/types/api.ts`：新增 `EvalStatus`、`EvalMetricResult`、`EvalRunSummary`、
`EvalRunDetail`、`EvalGoldenItem`、`EvalConfig`、`EvalCreateRequest`，与 §6.3 一一对应。

### 10.8 Trace 页联动

- `trace/page.tsx` 用 `useSearchParams` 读取 `run_id`，作为初始过滤条件；同样需要
  `<Suspense>`（或把读取逻辑放进子组件）。
- agent span 详情的 `Evaluate this case` 按钮只在 span `input.eval_case_id` 非空时显示，
  跳转 `/dashboard/eval?eval_case_id=<case>` 预填 case；Eval 页面会按该 case 重新跑一次
  Agent 再评测，不再直接评测当前 run。
- 顺手修正该文件中的无效类名 `border-memBorderPrimary` → `border-memBorder-primary`。

### 10.9 无新依赖

不引入 react-query、SWR、zustand 或图表库；轮询手写，表格复用 `DataTable`，样式只用
现有 shadcn 组件与语义 token。

## 11. 部署与配置

### 11.1 新镜像

`server/eval-requirements.txt`（deepeval 本身从仓库内 `./deepeval` 源码安装，不走 PyPI）：

```
sqlalchemy>=2.0,<3.0
psycopg[binary]>=3.2.8,<4.0.0
```

`server/eval.Dockerfile`（构建上下文仍是仓库根）：

```dockerfile
FROM python:3.12-slim

WORKDIR /app
ARG PIP_INDEX_URL=https://pypi.org/simple

COPY server/eval-requirements.txt .
COPY deepeval/ /tmp/deepeval-src/
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
        --index-url "${PIP_INDEX_URL}" -r eval-requirements.txt \
    && pip install --no-cache-dir --retries 10 --timeout 120 \
        --index-url "${PIP_INDEX_URL}" /tmp/deepeval-src \
    && rm -rf /tmp/deepeval-src

COPY server/ .

RUN useradd --system --uid 1000 --create-home eval
USER 1000:1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEEPEVAL_TELEMETRY_OPT_OUT=1

CMD ["python", "-m", "app.eval.worker"]
```

- 不安装 `server/requirements.txt`、不安装 `agent-loop/`、不复制 `dashboard/`。
- 不 `EXPOSE`；worker 不监听端口。
- 不把 `.env` 拷入镜像；部署时显式传环境变量。

### 11.2 容器

`start-stack.sh` 在迁移之后、server 之前启动：

```bash
docker run -d --name bokaclaw-eval --network bokaclaw-net --restart unless-stopped \
  --read-only --tmpfs /tmp:rw,size=256m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 256 --memory 1g --cpus 1 \
  --user 1000:1000 \
  -e POSTGRES_HOST="$PG_CONTAINER" -e POSTGRES_PORT=5432 \
  -e POSTGRES_USER="$POSTGRES_USER" -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  -e APP_DB_NAME="$APP_DB_NAME" \
  -e EVAL_WORKER_ID="${EVAL_WORKER_ID:-eval-1}" \
  -e EVAL_WORKER_POLL_SECONDS="${EVAL_WORKER_POLL_SECONDS:-2}" \
  -e EVAL_HEARTBEAT_SECONDS="${EVAL_HEARTBEAT_SECONDS:-10}" \
  -e EVAL_LEASE_SECONDS="${EVAL_LEASE_SECONDS:-60}" \
  -e EVAL_MAX_ATTEMPTS="${EVAL_MAX_ATTEMPTS:-3}" \
  -e EVAL_MAX_RUN_SECONDS="${EVAL_MAX_RUN_SECONDS:-900}" \
  -e EVAL_JUDGE_ENABLED="${EVAL_JUDGE_ENABLED:-false}" \
  -e EVAL_JUDGE_PROVIDER="${EVAL_JUDGE_PROVIDER:-deepseek}" \
  -e EVAL_JUDGE_MODEL="${EVAL_JUDGE_MODEL:-deepseek-chat}" \
  -e EVAL_JUDGE_API_KEY="${EVAL_JUDGE_API_KEY:-}" \
  -e EVAL_JUDGE_MAX_CHARS="${EVAL_JUDGE_MAX_CHARS:-20000}" \
  -e EVAL_JUDGE_LANGUAGE="${EVAL_JUDGE_LANGUAGE:-zh}" \
  bokaclaw-eval:test
```

- **不**使用 `--env-file server/.env`：避免把 `JWT_SECRET`、`ADMIN_API_KEY`、`OPENAI_API_KEY`、
  `CONFIDENT_AI_API_KEY` 等无关或高权限密钥带进评测容器。
- `EVAL_JUDGE_API_KEY` 为空时 worker 仍可运行，只做确定性指标；API 也会拒绝 judge 请求。
- `RECREATE=1` 时先 `docker rm -f bokaclaw-eval`；`stop-stack.sh` 的 `CONTAINERS` 加入
  `bokaclaw-eval`。
- 健康检查：`server/app/eval/healthcheck.py` 检查数据库可连接，且
  `/tmp/eval-worker-heartbeat` 的 mtime 在 `3 * max(lease, poll)` 秒内；`Dockerfile` 中
  `HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-m", "app.eval.healthcheck"]`。
  worker 每轮循环写该文件。

### 11.3 构建脚本

`build-images.sh`：

- `TARGET` 增加 `eval`，`all` 时构建 `bokaclaw-eval:test`。
- 使用同一 `PIP_INDEX_URL`。
- 冒烟：

  ```bash
  docker run --rm --entrypoint python bokaclaw-eval:test \
    -c 'import deepeval, app.eval.adapter, app.eval.worker; print("eval-ok")'
  ```

- 不构建 eval 时，`start-stack.sh` 不应因缺镜像而失败：只有 `EVAL_ENABLED=true` 才要求
  镜像存在；否则跳过并打印提示。

### 11.4 环境变量

`server/.env.example` 增加：

```bash
# Online evaluation (see docs/eval/eval-console-design.md).
EVAL_ENABLED=true
EVAL_WORKER_POLL_SECONDS=2
EVAL_HEARTBEAT_SECONDS=10
EVAL_LEASE_SECONDS=60
EVAL_MAX_ATTEMPTS=3
EVAL_MAX_RUN_SECONDS=900
EVAL_RETENTION_DAYS=30

# Judge is opt-in per run AND must be enabled here.
EVAL_JUDGE_ENABLED=false
EVAL_JUDGE_PROVIDER=deepseek
EVAL_JUDGE_MODEL=deepseek-chat
EVAL_JUDGE_API_KEY=
EVAL_JUDGE_MAX_CHARS=20000
# zh (default) or en.
EVAL_JUDGE_LANGUAGE=zh
```

说明：

- `EVAL_ENABLED=false` 时 server 返回 `503 eval_disabled`，页面显示禁用原因。
- worker 启动时把 `EVAL_JUDGE_*` 映射为 deepeval 的 provider 变量（§8.1），不要求 `.env`
  里同时出现 `USE_DEEPSEEK_MODEL` / `DEEPSEEK_MODEL_NAME`。
- `EVAL_JUDGE_API_KEY` 与 server `.env` 的其他密钥同等对待：gitignored、不打印、不入镜像。

### 11.5 本地验证命令

```bash
set -a; . server/.env; set +a   # 让下面的 ADMIN_API_KEY 可用
.claude/skills/bokaclaw-docker-stack/scripts/build-images.sh eval
RECREATE=1 .claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh

docker cp tests/agent_eval/goldens.minimax.json bokaclaw-server:/tmp/goldens.json
docker exec bokaclaw-server python scripts/seed_eval_goldens.py --file /tmp/goldens.json

curl -s http://localhost:8000/evals/config -H "X-API-Key: $ADMIN_API_KEY" | jq
curl -s -X POST http://localhost:8000/evals \
  -H "X-API-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"run_id":"33ddd007-183a-4f6d-a1ad-698c6c5719e2","eval_case_id":"minimax-pricing-golden-01","use_llm_judge":false}' | jq
docker logs -f bokaclaw-eval
```

## 12. 安全与隐私

- **权限**：`/evals` 全部 `require_admin`；普通用户无法查看 run 内容或触发外部调用。
- **judge opt-in**：默认 `use_llm_judge=false` 且 `EVAL_JUDGE_ENABLED=false`；页面复选框
  必须同时满足两者才可用，并显示将发送到的模型与费用提示。
- **密钥边界**：eval 容器只拿 DB 凭据与 `EVAL_JUDGE_*`；不拿 `JWT_SECRET`、
  `ADMIN_API_KEY`、`AGENT_API_KEY`、`OPENAI_API_KEY`、`CONFIDENT_AI_API_KEY`。
- **不变量**：worker 只写 `eval_runs`，不写 `agent_runs` / `agent_run_spans` /
  `conversations`；查询使用只读语义。
- **Telemetry**：`DEEPEVAL_TELEMETRY_OPT_OUT=1`；不配置 Confident AI；失败时也不会自动上传。
- **脱敏**：span 已在采集端脱敏与截断；worker 写回的错误与 `raw_result` 再截断 2000 字符，
  不写 prompt 全文；judge 截断见 §8.5。
- **数据留存**：评测结果随 run 级联删除；golden 是人工维护数据，单独保留。
- **容器**：只读 rootfs、非 root、无端口、cap-drop、资源限制；仅加入 `bokaclaw-net`。

## 13. 测试与验收

### 13.1 后端单测

`server/` 目前没有测试目录。本设计新增 `server/tests/`，使用 pytest；pytest 只加入
`server/requirements-dev.txt`，不进入生产镜像。纯逻辑测试也可以在 eval venv 中运行。

| 文件 | 覆盖 |
| --- | --- |
| `server/tests/test_eval_goldens.py` | payload 校验、快照规范化、hash 稳定性、seed upsert、`--delete-missing` |
| `server/tests/test_eval_metrics.py` | 未勾选 judge 只出确定性指标；ToolCorrectness 按 case 门控；缺参指标跳过 |
| `server/tests/test_eval_router.py` | 鉴权、run 不存在/非终态/无 span、golden 不存在、case 不匹配、judge 禁用、重复 409、list/get、cancel |
| `server/tests/test_eval_worker.py` | FakeRepo claim、成功写回、stale recovery、attempt 超限、取消、幂等 finish |
| `tests/agent_eval/test_deepeval_adapter.py` | 更新 import，继续守护 `_trace_dict` 契约 |

测试不真正调用 judge：`metrics.build_metrics` 与 `evaluator` 用 fake metric / monkeypatch；
router 测试用现有 FastAPI TestClient 模式。

### 13.2 前端验证

- `cd dashboard && pnpm typecheck`
- `cd dashboard && pnpm build`
- 手工检查：导航高亮、URL 预填、轮询停止、历史行切换、取消、错误码文案、移动端宽度。

### 13.3 端到端验收

使用真实 run `33ddd007-183a-4f6d-a1ad-698c6c5719e2`：

1. seed `minimax-pricing-golden-01`。
2. `EVAL_JUDGE_ENABLED=false` 提交 → 202；worker 完成；详情只有
   `AgentLoopDetectionMetric`；无外部请求。
3. 开启 `EVAL_JUDGE_ENABLED=true` 与 `EVAL_JUDGE_API_KEY`，重建 eval 容器；勾选 judge 重跑
   → 详情包含 5–6 个指标（case 具备工具数据时含 ToolCorrectness），`judge_model`、
   `token_cost`、`duration_ms` 有值。
4. 负例：
   - 运行中的 run → 409 `agent_run_not_terminal`；
   - 同一 run/case/judge 模式重复提交 → 409 `eval_already_running` + 已有 id；
   - 不存在的 case → 404；
   - 关闭采集的 run → 422 `eval_no_spans`；
   - 页面取消 queued 任务 → `cancelled`。
5. 重启 eval 容器时若有 running 任务 → 租约过期后重新排队，最多 `EVAL_MAX_ATTEMPTS` 次。
6. 删除一条测试 `agent_runs` 行 → 对应 `eval_runs` 级联消失（在事务中回滚验证）。

## 14. 风险与后续扩展

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| deepeval 升级改变 `_trace_dict` | 轨迹指标失真或报错 | 固定 4.3.3；契约测试；升级单独任务 |
| judge 成本/延迟不可控 | 长时间占用 worker、费用上升 | 默认关闭；单 worker + 超时；页面展示费用 |
| golden 过期 | 分数失去意义 | 快照记录 hash 与时间；定期人工复核；后续加 `updated_at` 提醒 |
| span/run 保留期短于评测需求 | 历史 run 无法再评测 | 文档说明；评测历史随 run 删除 |
| 单 worker 阻塞 | 队列延迟 | `EVAL_MAX_RUN_SECONDS`；后续可多 worker（`SKIP LOCKED` 已支持） |
| DeepSeek base URL 固定 | 无法接自建网关 | 本期明确不支持；需要时改 deepeval 适配层 |
| 用户内容外发 | 合规风险 | 默认关闭、admin-only、显式提示、截断、可审计 `judge_model` |

后续扩展：

- 批量评测一个 case 的多个 run，或多 case 对同一 run。
- 定时回归与质量趋势图。
- 多租户/按用户隔离（当前 admin 全局）。
- 评测结果导出（JSON/CSV）与 webhook。
- 对接 OpenTelemetry 或外部评测平台（仍保持 opt-in）。

## 15. 实施顺序

| 阶段 | 内容 | 文件 |
| --- | --- | --- |
| 1 | 迁移 010、ORM、golden 校验与 seed | `server/alembic/versions/010_create_eval_tables.py`、`server/app/models.py`、`server/app/eval/{goldens,repo}.py`、`server/scripts/seed_eval_goldens.py`、`tests/agent_eval/goldens.minimax.json` |
| 2 | 评测执行核心 | `server/app/eval/{config,adapter,metrics,evaluator,cli}.py`；`tests/agent_eval/*` 改为薄壳/重导出 |
| 3 | worker 与健康检查 | `server/app/eval/{worker,healthcheck}.py`、`server/eval-requirements.txt`、`server/eval.Dockerfile` |
| 4 | HTTP 契约 | `server/app/routers/evals.py`、`server/app/main.py`、`server/tests/test_eval_*.py` |
| 5 | 本地栈 | `build-images.sh`、`start-stack.sh`、`stop-stack.sh`、`server/.env.example`、stack skill 文档 |
| 6 | Dashboard | `api-endpoints.ts`、`types/api.ts`、`main-nav.tsx`、`eval/page.tsx`、`trace/page.tsx` |
| 7 | 端到端与文档回写 | 真实 run 验证、`docs/eval/agent-span-evaluation.md` 更新、problem record |

阶段 1–4 未上线前不暴露路由；阶段 5 之后页面才可用。阶段 6 可先用 curl 驱动验证 API，
再接前端。
