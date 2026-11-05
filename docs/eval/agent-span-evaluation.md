# Agent 运行 Span 采集与 deepeval 离线评测设计

> 状态：设计中（2026-11-04）
>
> 适用范围：`server/` FastAPI 后端、`server/alembic/` 迁移、`tests/agent_eval/` 评测脚本
>
> 目标读者：Agent 执行层、后端、评测开发者
>
> 契约地位：本文定义 span 的落库结构与评测边界；HTTP/SSE 契约仍以
> [`../server/agent-execution-decoupling.md`](../server/agent-execution-decoupling.md) 为准。

## 1. 背景与目标

`POST /agent` 已经用 `agent_runs` / `agent_run_events` 持久化运行状态与 SSE 事件，
assistant 消息的 `metadata.tool_trace` 只保留工具调用。模型请求、模型响应、上下文压缩、
run 起止这些评测所需的信息在运行结束后不可得，因此无法离线回答「这一轮 agent 用了哪些
步骤、哪一步低效、为什么失败」。

本设计增加一条**旁路采集**链路：

1. 每次后台 run 把 SDK 事件转换成中立 span，写入 `agent_run_spans`。
2. 评测脚本读取落库 span，还原成 deepeval 的测试用例与 trace 结构，跑指标。
3. 采集端不依赖 deepeval；评测端可回放、可重复跑、可替换。

非目标：在线评测、实时告警、修改 SSE 对外契约、修改 `agent-loop/` SDK。

## 2. 生命周期与采集点

| 生命周期阶段 | 现有落点 | 新增 span |
| --- | --- | --- |
| run 入队 / claim | `agent_runs.status` | run 被 claim 时创建根 `agent` span |
| SDK `run_started` | 未转发 | 补根 span 的 input / attributes |
| SDK `model_request` | 未转发 | 创建 `llm:{event.seq}` span（input） |
| SDK `model_response` | 未转发 | 补对应 llm span 的 output / finish_reason |
| SDK `tool_call` | `tool.call` 事件 + `ToolTrace` | 创建 `tool:{tool_call_id}` span |
| SDK `tool_result` | `tool.result` 事件 + `ToolTrace` | 结束 tool span |
| SDK `context_compressed` | 仅调试日志 | 缓存，附加到下一次 `model_request` 生成的 llm span |
| SDK `run_ended` | 未转发 | 补根 span 的 `stop_reason` |
| `_finalize` | 消息 / 事件 / run 终态 | 结束根 span，结算所有 open span |
| `_recover_run` | 崩溃恢复 | open span 结算为 `interrupted` |

工具事件继续走现有 SSE 路径；其余事件只作为内部 span 事件进入 runner，不写
`agent_run_events`（该表 `event_type` 有 CHECK 约束），因此 SSE 订阅端行为不变。

**执行器差异**：内置 runtime（`stream_local_agent`）能拿到全部 SDK 事件；远程 agent executor
（`AGENT_LOOP_URL`，`server/app/agent/loop.py`）的协议只有消息与工具事件，只能落
`agent` + `tool` span，根 span 标 `executor=remote`，评测时轨迹指标会降级。
`AGENT_EXECUTOR_URL`（`server/app/agent/executor.py`）只把 shell 工具放到沙箱容器执行，
事件仍由内置 runtime 发出，span 树保持完整。

## 3. Span 模型

`agent_run_spans`：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | bigint identity | 主键 |
| `run_id` | uuid FK | → `agent_runs.id`，级联删除 |
| `span_id` | text | `agent` / `llm:{event.seq}` / `tool:{tool_call_id}` / `memory:recall` / `memory:save` |
| `parent_span_id` | text | 默认 `agent` |
| `span_type` | text | `agent` / `llm` / `tool` / `memory` |
| `name` | text | `agent.turn`、`llm.turn.3`、工具名、`memory.recall`、`memory.save` |
| `status` | text | `running/completed/failed/aborted/interrupted` |
| `iteration` | int | ReAct 轮次，可空 |
| `started_at` / `ended_at` | timestamptz | 时间 |
| `duration_ms` | int | 结束时间减开始时间 |
| `input` / `output` | jsonb | 已截断/脱敏 |
| `attributes` | jsonb | model、provider、usage、finish_reason、stop_reason、truncated 标记等 |
| `error` | text | 失败原因 |

约束与索引：

- `UNIQUE (run_id, span_id)`，写入用 `ON CONFLICT ... DO UPDATE`，保证事件重复/重放幂等。
- `INDEX (run_id, started_at)` 供按运行回放。
- `CHECK` 约束限定 `span_type` / `status`。

树形结构固定为两层：`agent` → `llm` / `tool` / `memory`。不建嵌套 tool span。

`memory` span 记录研究记忆的两端：`memory:recall` 在第一次模型请求前写出（`input.question`、`output.hits`、`attributes` 里的 `floor` / `ttl_days` / 候选数与各跳过计数），`memory:save` 在 `done` 之后写出（`attributes.action` 为 `added`/`updated`/`skipped`/`failed`）。两者都是一次写出的终态 span，没有后续事件来关闭它们。

### 3.1 数据裁剪

- 三类 span 都落输入与输出：
  - `agent`：`input.message` / `output.content`；
  - `llm`：`input.messages`（默认落完整 prompt）/ `output.content` + `tool_calls` + `finish_reason`；
  - `tool`：`input.arguments` + `raw_arguments` / `output.content` + `is_error`。
- `AGENT_SPAN_PROMPT_MODE=full`（默认）落完整 messages；`summary` 只存消息条数、角色序列、
  最后一条消息的 hash，适合不允许落用户原文的部署。两种模式都会在 attributes 保留计数与 hash。
- 每个 `input` / `output` 字段默认上限 16 KiB（`AGENT_SPAN_MAX_BYTES`），超限截断并在
  值中给出 `{"truncated": true, "preview": ..., "original_bytes": ...}`。
- 工具入参/结果默认落库（`AGENT_SPAN_TOOL_CONTENT=on`），只做截断，不做内容改写。
- 不采集 API key、Authorization、base_url 凭据；运行时配置只取 model/provider 名称。
- 按 run 采样：`AGENT_SPAN_SAMPLE_RATE`（0~1，默认 1.0），被采样的 run 记全部 span，
  未采样 run 不写任何 span。

### 3.2 Usage

`model_response` 事件不含 token usage。usage 只在 `AgentCompleted.metadata` /
`RunResult.usage` 中，由 runner 在结束时写入根 span。需要 per-LLM 归因时按轮次均摊，并在
attributes 标 `usage_estimated: true`；有原始值时优先原始值。

## 4. 开关与保留

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_SPAN_CAPTURE` | `true` | 总开关；关闭后与现有行为完全一致 |
| `AGENT_SPAN_PROMPT_MODE` | `full` | `full` 落完整 messages / `summary` 只落计数与 hash |
| `AGENT_SPAN_MAX_BYTES` | `16384` | 单字段上限 |
| `AGENT_SPAN_SAMPLE_RATE` | `1.0` | run 级采样率 |
| `AGENT_SPAN_TOOL_CONTENT` | `true` | 是否落工具入参/结果 |
| `AGENT_SPAN_RETENTION_DAYS` | `30` | span 保留天数；不能超过 `AGENT_RUN_RETENTION_DAYS` |

保留任务复用 `server/scripts/prune_agent_runs.py`（cron/systemd 调用），新增
`repo.prune_spans(cutoff)`；先删 span，再按现有顺序删事件/run。span 通过外键级联随
`agent_runs` 删除，因此调大 span 保留期时必须同时调大 run 保留期。

采样按 `run_id` 的 SHA-256 决定，同一 run 的结果可复现；未采样的 run 无法评测。

## 5. 回放与 deepeval 适配

评测代码放在 `tests/agent_eval/`。`evaluation/` 是 benchmark submodule，不作为运行时依赖；
`deepeval/` 是仓库内源码，评测环境从它构建安装，不走 PyPI。

- 依赖：deepeval 源码在 `./deepeval`（版本 4.3.3），由根 `pyproject.toml` 的
  `[tool.hatch.envs.eval]` 以 path 依赖安装，不进入 core `dependencies`，也不从 PyPI 拉取。
  deepeval 要求 pydantic ≥ 2.11，而 server 锁 2.10.4，**必须在独立 venv/容器里跑评测**
  （`hatch run eval:test`），不要与 server 依赖装在同一环境。
- `spans_from_db(run_id)`：读取 span 行，按 `parent_span_id` 还原树。
- `to_llm_test_case(run, golden=None)`：构造公开的 `LLMTestCase`：
  `input`=用户消息、`actual_output`=最终回答、`tools_called`=工具 span、
  `expected_output`/`expected_tools` 来自 golden、`metadata` 带 `run_id`/`eval_case_id`。
- `to_trace_dict(spans)`：生成与 deepeval `trace_manager.create_nested_spans_dict()` 同形的
  嵌套 dict，仅在适配器内赋值 `LLMTestCase._trace_dict`。deepeval 4.3.3 没有公开的 trace
  导入 API，这是唯一私有接触点，隔离在单文件。
- **契约测试**：用 deepeval 公共 `trace()` / `@observe` 生成一条等价 trace，断言我们生成
  的 dict 与其 `_trace_dict` 结构一致。升级 deepeval 时先让该测试失败，再调整适配器。

### 5.1 指标矩阵

| 指标 | 依赖 | 无 golden 可用 |
| --- | --- | --- |
| `AnswerRelevancy` / `GEval` | `LLMTestCase` | 是 |
| `Faithfulness` | 需要 `retrieval_context`；默认不映射工具结果，故默认不可用 | 否 |
| `ToolCorrectness` | `tools_called` + `expected_tools` | 否 |
| `ArgumentCorrectness` | 工具入参（需要 `AGENT_SPAN_TOOL_CONTENT=on`） | 是 |
| `TaskCompletion` | `input` / `actual_output` / `tools_called`（trace 可选） | 是 |
| `StepEfficiency` / `PlanAdherence` / `PlanQuality` | `_trace_dict`；`summary` prompt 模式下计划类指标可能偏弱 | 是 |
| `AgentLoopDetection` | `_trace_dict`（确定性，无 LLM） | 是 |

历史 run 没有参考输出。评测时用 `agent_runs.request.metadata.eval_case_id`（或
`client_run_id`）关联 golden；关联不上的 run 只跑无参考指标。参考数据由评测脚本的
goldens 文件提供，不回写数据库。

### 5.2 运行方式

离线 CLI（`tests/agent_eval/run.py` 现在只是 `server/app/eval/cli.py` 的薄壳）：

```bash
python tests/agent_eval/run.py --run-id <uuid> --goldens tests/agent_eval/goldens.example.json
```

输出每条 run 的指标分数、原因与失败 span 定位（`llm.turn.N` / `tool:<name>`）。不接
Confident AI，不上传任何数据；judge 模型用本地配置的 OpenAI/DeepSeek。

在线评测走独立 `bokaclaw-eval` 容器 + `/evals` API + Dashboard `/dashboard/eval`
页面，golden 存在 `eval_goldens`、结果持久化到 `eval_runs`；设计见
[`eval-console-design.md`](eval-console-design.md)。

## 6. 验证

1. 容器内 `alembic upgrade head` / `alembic downgrade -1`。
2. 本地 split stack 调一轮带工具的 `/agent`，确认 `agent_run_spans` 形成
   `agent → llm/tool` 树，状态与终态一致。
3. 取消、重启恢复、远程 executor 三种场景各跑一次，确认 open span 被结算。
4. `AGENT_SPAN_CAPTURE=0` 时确认无新增行、现有测试全绿。
5. 用一条真实 run 跑适配器，确认公开指标与轨迹指标都能出分。

## 7. 后续扩展

- Dashboard 的 run 轨迹视图与在线评测已落地（`/dashboard/trace`、`/dashboard/eval`）。
- 线上按采样率长期积累失败 run，人工挑选后加入 goldens；golden 管理界面待做。
- 如需对接 OpenTelemetry，只需在采集处增加 exporter，表结构不变。
