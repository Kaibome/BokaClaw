# Harness RSI 闭环设计

> 状态：设计中（2026-11-08）
>
> 适用范围：`server/`、`agent-loop/`、`dashboard/`、`server/app/eval/`
>
> 目标读者：Agent 执行层、评测、后端、Dashboard 开发者
>
> 前置阅读：[`agent-span-evaluation.md`](./agent-span-evaluation.md)、[`eval-console-design.md`](./eval-console-design.md)

## 1. 背景与目标

### 1.1 问题

Harness（模型权重之外决定 agent 怎么干活的部分）的改进目前是人工的、不可度量的：

- 改 prompt、工具描述、阈值，靠直觉判断"好像好一点"；
- 没有基线，也没有回滚路径；
- span 和 eval 已经有数据，但没有东西把它们接成"观测 → 改动 → 验证 → 上线"的回路。

本文设计一条**可自动执行的 Harness RSI（Recursive Self-Improvement）闭环**：让系统自己发现 harness 的失败模式、提出改动、用可重放的评测验证、自动接受或拒绝，并对线上退化自动回滚。

### 1.2 现状：可复用的资产

| 资产 | 位置 | 现状 |
| --- | --- | --- |
| Span 采集 | `server/app/agent/spans.py`、`agent_run_spans` | 逐事件 upsert，覆盖 agent / llm / tool / memory |
| 运行事件日志 | `agent_run_events` | 可回放的事件流，含 tool call/result |
| Trace 查询 | `server/app/routers/traces.py`、`/dashboard/trace` | 只读、admin-only、手动刷新 |
| Golden | `eval_goldens`、`server/app/eval/goldens.py` | input、expected_output、expected_tools、context、快照 hash |
| 评测队列 | `eval_runs`、`server/app/eval/worker.py` | 数据库队列 + `FOR UPDATE SKIP LOCKED` + lease，独立 `bokaclaw-eval` 容器 |
| 指标 | `server/app/eval/metrics.py` | `AgentLoopDetectionMetric`（确定性）+ judge 指标 |
| Replay | `dashboard/.../eval/page.tsx:499-540` | **仅前端三步拼接**：读 golden → `POST /agent` → 等终态 → `POST /evals` |
| Harness 旋钮 | `server/app/agent/runtime.py` 的 `AGENT_*` | 配置级，可改但未版本化 |

三个关键缺口：

1. **没有 harness 版本号**：无法回答"这个 run / 这次 eval 是哪个配置跑的"；
2. **replay 不是可脚本化的能力**：只存在于 Dashboard 按钮里，CLI/定时任务无法复用；
3. **没有 gate**：没有人或程序根据预注册阈值自动接受/拒绝一次改动。

### 1.3 目标

1. 每次 run 与 eval 都绑定 `harness_version`。
2. 提供可脚本化的 `replay`：给定 golden 与候选 harness，生成新的 run 并完成评测。
3. 从 span 自动生成失败分桶与归因建议。
4. 用预注册阈值和统计规则自动判定 accept / reject，并留下可审计记录。
5. 配置级改动自动应用、灰度、回滚；代码级改动自动生成 PR，人工只保留安全 gate。
6. Dashboard 展示迭代历史、指标对比与决策原因。

### 1.4 非目标

- 训练模型权重或做 RLHF。
- 让系统自动合并代码、自动修改权限/安全相关逻辑。
- 修改 `POST /agent` 的 SSE 契约或 `agent_run_spans` 的语义。
- 多副本调度、跨集群评测队列。
- 自动生成 golden（golden 的维护仍需人工确认）。

### 1.5 术语

| 术语 | 含义 |
| --- | --- |
| Harness | system prompt、工具集与描述、工具策略、上下文/压缩、重试与守卫阈值、子 Agent 编排、模型/执行器路由 |
| Harness Version | `git SHA + 配置快照 hash + prompt hash + 工具 spec hash + 子 Agent spec hash` 的清单 |
| Baseline | 当前线上 harness version |
| Candidate | 一次迭代提出的候选 harness version |
| Iteration | 一次完整的"归因 → 改动 → 验证 → 决策" |
| Gate | 依据预注册阈值做 accept / reject 的判定器 |
| Holdout | 不参与提议、只用于最终 gate 的评测子集 |

## 2. 闭环模型

```
① 观测 ──→ ② 归因 ──→ ③ 提出改动 ──→ ④ 离线验证 ──→ ⑤ 灰度上线
  ↑                                                    │
  └──────────── ⑥ 在线监控 / 自动回滚 ─────────────────┘
```

### 2.1 六个阶段

| 阶段 | 输入 | 产出 | 失败模式 |
| --- | --- | --- | --- |
| ① 观测 | spans、events、eval 结果、请求日志 | 失败分桶与基线指标 | 只看日志、没有分桶 |
| ② 归因 | 失败分桶、span 证据 | 假设 + 目标旋钮 | 一次改多个变量 |
| ③ 提出改动 | 假设、允许清单 | Candidate manifest + diff | 改动超出可测范围 |
| ④ 离线验证 | golden、候选 harness | 成对指标与置信区间 | 单次运行、无 CI |
| ⑤ 灰度上线 | 通过的 candidate | 按用户/比例切流 | 对照组不干净 |
| ⑥ 监控/回滚 | 线上指标 | accept 或 revert | 无自动回滚 |

### 2.2 闭环成立的五个条件

1. **信号独立**：评测不由 proposer 编写或修改；
2. **阈值预注册**：看结果之前固定 accept 条件；
3. **可重放**：固定 golden 快照、judge 模型与温度；
4. **可回滚**：harness manifest 版本化，回滚是切版本而不是手改；
5. **自动拒绝/回滚**：Gate 和监控必须能自主否决，而不是只出报告。

缺任意一条，系统只是"AI 辅助开发"，不是 RSI。

### 2.3 角色分离

| 角色 | 职责 | 建议实现 |
| --- | --- | --- |
| Runner | 按 harness 执行任务 | `server` + `agent-loop` |
| Proposer | 读失败证据，提出改动 | 外部 agent / 人工；第一版允许人工 |
| Evaluator | 固定 golden + 固定 judge 打分 | `bokaclaw-eval` worker |
| Gatekeeper | 按预注册规则判定 | `scripts/rsi/gate.py` |
| Human | 安全敏感改动、代码合并 | PR review |

Proposer 不得修改 golden、judge 配置或 gate 阈值。

## 3. 阶段设计

### 3.1 观测

数据源：

- `agent_run_spans`：`span_type ∈ {agent, llm, tool, memory}`，`status ∈ {running, completed, failed, aborted, interrupted}`；
- `agent_run_events`：`tool.call` / `tool.result` / `message.*`；
- `agent_runs`：`status`、`error_code`、`model_calls`、token usage；迭代数从 `llm` span 的 `iteration` 或 span 数量推断；
- `eval_runs`：`metrics`、`overall_success`、`golden_hash`；
- `request_logs`：请求级错误与延迟。

第一版用 SQL 规则做分桶，不引入新的存储：

| 桶 | 判定 |
| --- | --- |
| `loop` | 同一 run 内重复 `tool_call` 名称+参数 hash ≥ N 次 |
| `tool_error` | `tool` span `status=failed` 占比 ≥ 阈值 |
| `no_progress` | `agent_runs.error_code = agent_no_progress` |
| `timeout` | `agent_timeout` 或子 Agent `status=timeout` |
| `context_pressure` | `llm` span 带 `context_compressed` 且 run 失败 |
| `protocol` | `agent_protocol_error` / `agent_upstream_error` |

产出：`failure_snapshot`（JSON），包含样本 run_id、桶分布、每个桶的代表性 span_id。

### 3.2 归因

规则优先，LLM judge 只做补充：

| 证据 | 目标旋钮 |
| --- | --- |
| 同一工具连续失败 | 工具描述、`AGENT_TOOL_RESULT_MAX_CHARS` |
| `no_progress` 集中 | `AGENT_PROGRESS_NUDGE_AFTER`、`AGENT_PROGRESS_STOP_AFTER` |
| 压缩后质量下降 | `AGENT_CONTEXT_MAX_TOKENS`、`AGENT_COMPRESSION_MODEL` |
| 子 Agent 超时 | `AGENT_SUBAGENT_TIMEOUT_SECONDS`、`AGENT_SUBAGENT_MAX_TURNS` |
| 参数错误集中 | 工具 schema / 描述 |
| 简单任务成本高 | 模型路由（见 §4.2） |

每次迭代只允许改**一个变量**，否则指标变化无法归因。

### 3.3 提出改动

`HarnessChange` 的最小结构：

```json
{
  "base_harness_version": "hv_...",
  "target": "config|prompt|tool_spec|route",
  "knob": "AGENT_PROGRESS_NUDGE_AFTER",
  "from": 3,
  "to": 4,
  "hypothesis": "no_progress 桶中 40% 在 3 次无进展后被提前终止",
  "expected_effect": {"no_progress_rate": "-10%"},
  "guardrails": ["cost_per_run", "p95_duration"],
  "rollback": "restore previous harness_version"
}
```

第一版允许清单（配置级）：

| 旋钮 | 默认值 | 作用 |
| --- | --- | --- |
| `AGENT_MODEL` / `AGENT_SUBAGENT_MODEL` / `AGENT_COMPRESSION_MODEL` | 继承持久化 LLM 配置 | 模型路由 |
| `AGENT_MAX_ITERATIONS` | 见 `load_local_config` | 步数预算 |
| `AGENT_PROGRESS_NUDGE_AFTER` / `AGENT_PROGRESS_STOP_AFTER` | 3 / 5 | 无进展守卫 |
| `AGENT_CONTEXT_MAX_TOKENS` / `AGENT_CONTEXT_KEEP_RECENT_TOKENS` | 见配置 | 压缩 |
| `AGENT_TOOL_RESULT_MAX_CHARS` | 见配置 | 工具输出截断 |
| `AGENT_TOOL_CONCURRENCY` | 1 | 工具并行度 |
| `AGENT_SUBAGENT_*` | 见配置 | 子 Agent 预算/超时/深度 |
| `AGENT_RESEARCH_MEMORY*` | 开启 | 记忆召回 |

不允许自动修改：权限、沙箱、鉴权、网络访问、安全相关代码。

### 3.4 离线验证

现有能力：`eval_goldens` 提供输入与期望，`eval_runs` 在独立 worker 中跑 deepeval。现有 Dashboard 已实现 replay 的三步拼接，但没有可复用的服务端入口。

设计目标是把三步变成一个可脚本化流程：

```text
for case in eval_set:
    for arm in [baseline, candidate]:
        run = POST /agent(message=golden.input,
                          metadata={eval_case_id, rsi_iteration_id, harness_version})
        wait until terminal
        eval = POST /evals(run_id=run.id, eval_case_id, use_llm_judge)
```

要点：

- **成对比较**：同一 case 的两个 arm 必须用同一 golden 快照（`golden_hash`）；
- **重复运行**：每 arm 至少 3 次，judge 指标取中位数并报告分布；
- **统计**：bootstrap 置信区间；只有 `ci_low > 0` 且 `delta ≥ MIN_EFFECT` 才算改善；
- **Holdout**：提议阶段只看 train 子集，最终 gate 才跑 holdout；
- **成本上限**：每次迭代的 token/费用预算写进 iteration 记录，超预算直接中止。

### 3.5 灰度上线（控制面写库，数据面热加载）

**部署边界**：RSI 循环不控制 Docker，也不能控制。`bokaclaw-executor` 是只读根文件系统、无外网、无 Docker socket 的沙箱；server 容器同样没有 Docker socket。因此 `docker compose up -d`、重启容器、直接改 `.env` 都不能作为自动部署手段，也不应该给 agent 这种权限。

采用"控制面 / 数据面"分离：

- **控制面**：`scripts/rsi/loop.py` 只写 Postgres（`harness_versions` + `harness_rollouts`），不需要任何容器权限；
- **数据面**：server 在 run claim 时从 `harness_rollouts` 解析出生效的 `harness_version`，把配置随 payload 传给 executor；run 执行期间配置被冻结，不受后续切换影响；
- **回滚**：把 `harness_rollouts` 中 candidate 的比例改回 0，或把 baseline 设为唯一生效版本。已经在跑的 run 继续用它 claim 时的配置。

灰度按 `hash(user_id + rollout_id) % 100` 切流，per-version 在线指标直接从 `agent_runs.harness_version_id` 关联 span 计算。

**进程级配置不在此列**：`AGENT_RUN_*`、`AGENT_EXECUTOR_URL`、`AGENT_WORKSPACE_ROOT`、`AGENT_TOOLS_ALLOW_SHELL` 等只在进程启动时读取，且涉及安全边界，必须走外部 CI/人工重启，不进入 RSI 自动部署。

### 3.6 在线监控与自动回滚

守护指标（任一越界即回滚）：

| 指标 | 回滚条件 |
| --- | --- |
| 任务成功率 | 跌破 baseline 的 CI 下界 |
| p95 延迟 | 超过 baseline × 1.2 |
| 单次运行成本 | 超过 baseline × 1.2 |
| `tool` 失败率 | 超过 baseline + 5pp |
| `agent_upstream_error` | 显著上升 |
| 安全违规 | 任意一次 |

回滚 = 把 `harness_rollouts` 的 candidate 比例置 0（或禁用该 rollout），并写入 `rsi_iterations.decision = rolled_back`；不需要重启容器，已在执行的 run 保持其 claim 时冻结的配置。

## 4. 变更面

### 4.1 配置级（第一阶段）

只改 `LocalAgentConfig` 暴露的 `AGENT_*`。好处：可回滚、无需构建镜像、可与 baseline 成对比较。风险：只能优化"参数"，不能改变循环结构。

### 4.2 路由

路由是成本最低、杠杆最高的一类变更，因为它通常是配置而不是代码。当前 harness 有四类：

| 路由 | 现状 | 位置 |
| --- | --- | --- |
| 执行器 | mock / local / remote，按配置静态选择 | `server/app/agent/loop.py::stream_agent` |
| 模型 | 主 / 子 Agent / 压缩三套，按角色静态配置 | `LocalAgentConfig`、`build_subagent_specs`、`_resolve_compression_provider` |
| 工具 | 按名字分发 + `tool_policy` 白名单 + 并发安全分批 | `ToolRegistry.invoke`、`SubagentTool._select_tools` |
| 子 Agent | 父模型依据 `SubagentSpec.description` 自行选择 | `agent-loop/.../subagent.py:85`、`SUBAGENT_GUIDANCE` |

可作为 RSI 目标的路由改动：

1. 简单任务走便宜模型、失败升级（需要新增分类器或规则）；
2. 压缩走独立小模型；
3. 主 provider 失败时切换备用 endpoint（当前只有同 provider 重试，没有跨 provider fallback）；
4. 调整子 Agent 的 `description` 与工具白名单，改变委派分布。

路由改动的评测必须**按难度/工具组合分层**：eval 集偏简单时，换便宜模型可能指标不掉、成本大降，但线上难任务会退化。

### 4.3 代码级（第二阶段）

包括 prompt 文本、工具描述、`runtime.py` 的循环逻辑、`ToolRegistry` 策略、子 Agent 实现。必须：

- 由 Proposer 生成 diff 并开 PR；
- 走同一套 replay + gate；
- 人工 review 安全边界；
- 合并后仍以 `harness_version` 灰度。

## 5. 数据模型与接口

### 5.1 Harness 版本

新增 `harness_versions`：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | UUID PK | |
| `manifest_hash` | String(64) unique | 规范化 JSON 的 SHA-256 |
| `git_sha` | String(40) | 镜像/提交 |
| `config` | JSONB | `AGENT_*` 快照，排除密钥 |
| `prompt_hash` / `tools_hash` / `subagents_hash` | String(64) | 子系统指纹 |
| `created_at` | timestamptz | |

`harness_rollouts`（P3 新增，控制面）：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | UUID PK | |
| `harness_version_id` | UUID FK | 指向 `harness_versions` |
| `percentage` | Integer | 0–100，按 `hash(user_id + rollout_id)` 切流 |
| `status` | String | `active` / `disabled` |
| `created_at` / `updated_at` | timestamptz | |

同一时刻只允许一条 `active` 的 candidate rollout；未命中 candidate 的流量使用 baseline。

`agent_runs` 增加 `harness_version_id`（nullable），run claim 时写入；`eval_runs` 通过 `run_id` 间接关联。

### 5.2 迭代记录

新增 `rsi_iterations`：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | UUID PK | |
| `base_harness_version_id` / `candidate_harness_version_id` | UUID | |
| `change` | JSONB | 见 §3.3 |
| `evidence` | JSONB | 失败分桶与样本 span |
| `metrics_before` / `metrics_after` | JSONB | 汇总指标 |
| `decision` | String | `accepted` / `rejected` / `rolled_back` |
| `decision_reason` | Text | gate 输出的可读原因 |
| `golden_set_hash` | String(64) | 防止中途换题 |
| `created_at` / `finished_at` | timestamptz | |

唯一约束：`(base_harness_version_id, candidate_harness_version_id, golden_set_hash)`，保证幂等重跑不会重复计费。

`rsi_iteration_runs` 记录每次 replay 的 run_id / eval_run_id 与 arm（baseline / candidate）。

### 5.3 Replay 与 Gate

首版不新增公开 REST API，避免把内部流程暴露成契约：

- `scripts/rsi/replay.py`：按 §3.4 调现有 `POST /agent` 与 `POST /evals`；
- `scripts/rsi/gate.py`：读 iteration、计算成对指标、输出 accept/reject；
- `scripts/rsi/loop.py`：夜间任务，串起观测 → 提议 → replay → gate → 决策。

Dashboard 只读展示 `rsi_iterations` 与指标对比（Phase 3）。

### 5.4 不走公开 API 的理由

- replay 是管理员级批处理，现有 `/agent` 与 `/evals` 已够用；
- 公开 `POST /rsi/...` 会把未稳定的判定规则变成外部契约；
- 脚本可以放在 CI 或 cron 里，权限沿用 admin key。

### 5.5 运行期配置解析

自动灰度需要让配置在**不重启进程**的情况下按 run 生效。解析顺序固定为：

1. `AgentRunManager._run_worker` 在 claim 之后调用 `resolve_active_harness(user_id)`：命中 active rollout 则用 candidate，否则用 baseline；
2. 把选中的 `harness_version_id` 写入 `agent_runs`（P0 已建立的冻结点）；
3. 从 `harness_versions.config` 还原 `LocalAgentConfig`，与 env 默认值合并，作为 `harness_config` 参数传给 executor；
4. `stream_local_agent` 优先使用传入的 `harness_config`，没有时回退 `load_local_config()`（兼容 P0 之前的 run 与 legacy 路径）；
5. `build_runtime` 只接收解析后的 config，不感知来源；
6. `AGENT_RUN_*`、executor URL、workspace、shell 开关等进程级/安全配置仍只从 env 读取，启动时固定。

这样"部署"退化成两次数据库写（新版本 + rollout 指针），"回滚"是一次指针更新；agent 的沙箱权限完全不变。

## 6. 指标与判定

### 6.1 指标矩阵

| 类型 | 指标 | 来源 | 用途 |
| --- | --- | --- | --- |
| 确定性 | Agent Loop Detection（=1.0 通过） | `AgentLoopDetectionMetric` | 一票否决 |
| 确定性 | Tool Correctness（有 expected_tools 时） | `ToolCorrectnessMetric` | 工具路由质量 |
| Judge | Task Completion、Answer Relevancy、Step Efficiency、Argument Correctness | deepeval（阈值 0.5） | 质量 |
| 效率 | 迭代数、tokens、耗时 | `agent_runs` / span | 成本与速度 |
| 守护 | 工具错误率、upstream 错误、安全违规 | spans / events | 自动回滚 |

不要只优化一个聚合分：用指标向量 + Pareto 判断。

### 6.2 统计规则

- 同一 golden、同一 judge 配置下成对比较；
- 每 arm `n ≥ 3`；报告均值、中位数、bootstrap 95% CI；
- `MIN_EFFECT` 在评测集上预先标定（建议至少 3pp 或 0.05 分）；
- judge 温度固定为 0（若 provider 支持）；judge 模型变更视为新的评测配置，不能与旧结果混比；
- holdout 只在 gate 阶段跑一次，避免被间接优化。

### 6.3 判定伪代码

```python
def decide(before, after, guardrails, holdout):
    primary = paired_delta(before.primary, after.primary)
    if primary.ci_low <= 0 or primary.mean < MIN_EFFECT:
        return "rejected", "primary metric not improved"
    if after.loop_detection_pass_rate < 1.0:
        return "rejected", "agent loop detected"
    for name, limit in guardrail_limits.items():
        if after[name] - before[name] > limit:
            return "rejected", f"guardrail {name} regressed"
    if paired_delta(before.holdout, holdout).mean < 0:
        return "rejected", "holdout regression"
    return "accepted", "all gates passed"
```

## 7. 分阶段实施

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **P0：可追溯** | `harness_versions` + `agent_runs.harness_version_id`；失败分桶 SQL；基线报告 | 任取一个 run 能回答"哪个 harness 版本、属于哪个失败桶" |
| **P1：可回放** | `scripts/rsi/replay.py`；golden 成对运行；结果写入 iteration | 同一 golden 在 baseline 上重复 3 次，确定性指标一致 |
| **P2：可判定** | `gate.py` + 预注册阈值；配置级 allowlist；人工触发一次完整迭代 | 注入一个明显劣化（如 `AGENT_MAX_ITERATIONS=1`），gate 必须 reject |
| **P3：自动闭环** | 夜间 `loop.py`；写 `harness_rollouts` 控制面；run claim 时热加载配置；自动回滚；Dashboard 迭代列表 | 连续 5 次迭代无需人工搬运数据；回滚演练通过 |
| **P4：代码级** | prompt/工具/循环改动以 PR 进入同一验证链 | 代码级候选也经过 replay + gate + 人工 review |

### 7.1 P0 可执行清单

**P0 完成定义**：任意一个已执行的 run 都能回答"它由哪一版 harness 跑出"，并且能按失败类型聚合出一份基线报告。历史 run 允许为 `NULL`，语义是"未知 / 未执行"。

预估：2.5–3.5 人日。建议按下列顺序提交，每步保持可回滚。

#### P0-1 新增 harness manifest 模块

新文件 `server/app/agent/harness.py`：

```python
def canonical_json(value: Any) -> str: ...
def redact_config(config: LocalAgentConfig) -> dict: ...
def effective_system_prompt(config: LocalAgentConfig) -> str: ...
def build_manifest(sdk: Any, config: LocalAgentConfig) -> dict: ...
def manifest_hash(manifest: dict) -> str: ...
def load_manifest() -> tuple[dict, str]: ...   # 读 env + 组装 + 算 hash
```

要点：

- manifest 结构固定为：
  ```json
  {
    "schema": 1,
    "git_sha": "…|unknown",
    "config": { "model": "…", "max_iterations": 20, "…": "…" },
    "prompt_hash": "…",
    "subagents_hash": "…",
    "tools_hash": "…"
  }
  ```
- `git_sha`：优先读 `GIT_SHA` / `BUILD_SHA` 环境变量，缺失时写 `"unknown"`，不调用 `git`（容器里通常没有 `.git`）。
- `config`：从 `LocalAgentConfig` 显式挑选非敏感字段；**不要** `asdict()` 后整体落库。排除 `api_key`、`compression_api_key`，并把 `base_url` 里的 userinfo（`https://user:pass@host`）剥掉。
- `prompt_hash`：哈希的是 `build_runtime` 实际使用的 system prompt（会追加 `UNTRUSTED_DATA_RULE` 和 `SUBAGENT_GUIDANCE`），不是 `config.system_prompt` 原文。为防漂移，把 `server/app/agent/runtime.py:873-883` 的拼接逻辑抽成 `effective_system_prompt(config)`，`build_runtime` 和 `harness.py` 共用。
- `subagents_hash`：`build_subagent_specs(sdk, config)` 的稳定摘要，去掉 `ModelConfig.api_key`；`subagents_enabled=false` 时哈希 `{"enabled": false}`。
- `tools_hash`：用哑 user id（如 `"__harness__"`）构建 `_memory_tools(sdk, "__harness__")` + `build_dev_tools(sdk, load_dev_tool_config())`，只哈希每个工具的 `{name, description, parameters}`，按 name 排序。构建过程不执行任何工具。
- `manifest_hash`：`sha256(canonical_json(manifest))`，`sort_keys=True`、`separators=(",", ":")`、`ensure_ascii=False`。
- 不缓存也可以（单次构建是毫秒级）；如果 `_run_worker` 里成为热点，再按 `config_hash` 加进程内缓存。

#### P0-2 数据模型与迁移

`server/app/models.py` 新增：

```python
class HarnessVersion(Base):
    __tablename__ = "harness_versions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    manifest_hash: Mapped[str] = mapped_column(String(64), unique=True)
    git_sha: Mapped[str | None] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    tools_hash: Mapped[str | None] = mapped_column(String(64))
    subagents_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
```

`AgentRun` 新增：

```python
harness_version_id: Mapped[uuid.UUID | None] = mapped_column(
    ForeignKey("harness_versions.id", ondelete="SET NULL"), nullable=True
)
```

迁移 `server/alembic/versions/013_add_harness_versions.py`：

- `revision = "013"`，`down_revision = "012"`；
- `op.create_table("harness_versions", ...)`；
- `op.add_column("agent_runs", sa.Column("harness_version_id", sa.Uuid(), nullable=True))`；
- `op.create_foreign_key(..., ondelete="SET NULL")`；
- `op.create_index("ix_agent_runs_harness_version_id", "agent_runs", ["harness_version_id"])`；
- downgrade 反向删除。沿用 008/011 的 `sa.Uuid()` + JSONB 风格。

#### P0-3 仓储方法

`server/app/agent/repo.py` 新增：

```python
def get_or_create_harness_version(self, manifest: dict, manifest_hash: str) -> HarnessVersion
def set_run_harness_version(self, run_id: uuid.UUID, version_id: uuid.UUID) -> bool
def get_harness_version(self, run_id: uuid.UUID) -> HarnessVersion | None
```

实现要点：

- `get_or_create` 用 `INSERT ... ON CONFLICT (manifest_hash) DO NOTHING` 后 `SELECT`，避免并发重复插入；
- `set_run_harness_version` 用条件更新 `WHERE id = :run_id AND harness_version_id IS NULL`，返回 `rowcount > 0`，保证幂等；
- 失败只记日志，不影响 run。

#### P0-4 在 run 开始时冻结

`server/app/agent/runner.py::_run_worker`，在 `claim` 成功、取到 `run` 之后、`spans.start` 之前插入：

```python
try:
    manifest, digest = await asyncio.to_thread(load_manifest)
    version = await asyncio.to_thread(self.repo.get_or_create_harness_version, manifest, digest)
    await asyncio.to_thread(self.repo.set_run_harness_version, run_id, version.id)
except Exception:
    logger.warning("Failed to record harness version for run %s", run_id, exc_info=True)
```

- 放在 `claim` 之后：queued 取消的 run 没有执行，保持 `NULL`；
- 放在 `spans.start` 之前：根 span 可以带上 `harness_version_id` 属性（可选）；
- 启动时 `startup()` 重新提交的 queued run 也走同一路径，无需额外处理；
- legacy 路径（`AGENT_RUN_BACKGROUND=false`）没有 `agent_runs` 行，P0 不覆盖，文档标注为已知限制。

#### P0-5 序列化与日志

- `server/app/agent/schemas.py::AgentRunOut` 增加 `harness_version_id: str | None = None`；
- `serialize_run()` 填该字段；
- `agent run started` 日志追加 `harness_version_id=%s`；
- 可选：`SpanRecorder.start()` 把 `harness_version_id` 写进根 span 的 `attributes`，方便按版本过滤 trace。

#### P0-6 失败分桶

纯函数放 `server/app/eval/failure_buckets.py`，CLI 放 `scripts/rsi/failure_buckets.py`（不新增公开 API）。

输入：时间窗口内的 `agent_runs`、`agent_run_events`（`tool.call`）、`agent_run_spans`（`tool` / `llm` / `agent`）。

分桶规则：

| 桶 | 判定 |
| --- | --- |
| `loop` | 同 run 内 `(tool name, canonical args)` 重复 ≥ N 次（默认 3，可用 `RSI_LOOP_REPEAT_THRESHOLD` 覆盖） |
| `tool_error` | 同 run 内 `tool` span `status=failed` 占比 ≥ 30% |
| `no_progress` | `agent_runs.error_code = agent_no_progress` |
| `timeout` | `agent_timeout`，或子 Agent span `attributes.subagent_status = timeout` |
| `context_pressure` | run 失败且任一 `llm` span 带 `context_compressed` |
| `protocol` | `agent_protocol_error` / `agent_upstream_error` |

输出 JSON：

```json
{
  "window": {"since": "…", "until": "…"},
  "totals": {"runs": 120, "failed": 18},
  "buckets": {
    "loop": {"runs": 5, "samples": ["run_id", "…"]},
    "tool_error": {"runs": 7, "samples": ["…"]}
  }
}
```

参数：`--since`、`--until`、`--limit`、`--out`。参数哈希需要在 Python 里算（SQL 表达力不够），按 run 分批读取，避免一次加载全表。

#### P0-7 测试

| 测试文件 | 覆盖 |
| --- | --- |
| `server/tests/test_harness_manifest.py` | 同配置 hash 稳定；改 `AGENT_MAX_ITERATIONS` 改变 hash；改 `AGENT_API_KEY` **不**改变 hash；敏感字段与 URL userinfo 被移除；manifest schema fixture 不漂移 |
| `server/tests/test_failure_buckets.py` | 用合成 row dict 覆盖六个桶与空窗口；重复参数归一化；子 Agent 超时识别 |
| `server/tests/test_harness_repo.py`（可选） | `get_or_create` 幂等、`set_run_harness_version` 只写一次；如无 DB fixture，先只测纯函数，repo 交给集成环境 |

#### P0-8 运维与验收

- `.env.example` 增加 `GIT_SHA=`（注释说明由构建注入，缺省 `unknown`）；
- 镜像构建加 `ARG GIT_SHA` / `ENV GIT_SHA=$GIT_SHA`，并在 compose/CI 传入；
- 迁移：`cd server && alembic upgrade head`（或本地 Docker 栈的迁移命令）；
- 更新本文状态与"NULL = 未知/未执行"语义；
- 历史 run 不回填，避免制造假数据。

验收：

```sql
-- 1. 新 run 绑定了版本
select r.id, r.status, h.manifest_hash, h.git_sha
from agent_runs r
join harness_versions h on h.id = r.harness_version_id
order by r.created_at desc
limit 1;

-- 2. 同配置的两个 run 复用同一版本
select harness_version_id, count(*)
from agent_runs
where harness_version_id is not null
group by harness_version_id;

-- 3. 改一个非敏感旋钮后，新 run 指向新版本
--    改 AGENT_MAX_ITERATIONS 后重启 server，再跑一次 run
```

```bash
python scripts/rsi/failure_buckets.py --since 2026-11-01 --until 2026-11-08
cd server && pytest tests/test_harness_manifest.py tests/test_failure_buckets.py
```

#### P0 的关键风险

| 风险 | 缓解 |
| --- | --- |
| 容器里没有 `GIT_SHA`，所有构建塌缩成同一版本 | 构建时注入，缺失时显式记录 `unknown` |
| `build_runtime` 与 `harness.py` 对 prompt 的拼接不一致 | 抽出 `effective_system_prompt()` 单一来源 |
| Pydantic dump 把 `api_key` 带进 manifest | 显式字段 allowlist，不用整体 `asdict` |
| 工具 schema 顺序不稳定导致 hash 漂移 | 按工具名排序后序列化 |
| manifest 构建失败拖垮 run | 与 span 采集一致：try/except + warning |
| 每 run 构建一次有开销 | 先测量；必要时按 `config_hash` 缓存 |

## 8. 风险与缓解

| 风险 | 表现 | 缓解 |
| --- | --- | --- |
| Goodhart | eval 分数涨、真实体验不涨 | 轮换 holdout、保留 sealed 集、线上指标兜底 |
| 评测过拟合 | 改动专门讨好 judge | judge 与 proposer 隔离、确定性指标优先、多 judge |
| 归因错误 | 一次改多个变量 | 单变量 diff、小步、可回滚 |
| 非确定性 | 同配置两次结果差异大 | 成对重跑、CI、最小效应量 |
| 自证循环 | 模型自己改、自己评、自己批 | 三角色分离 + 人类安全 gate |
| 成本失控 | replay × judge 费用超预算 | 每次迭代预算上限、超限中止 |
| 安全边界 | 自我修改权限/文件/网络 | allowlist、executor 沙箱、`require_admin`、审计 |
| 版本漂移 | 配置与 manifest 不一致 | manifest 在 run 创建时冻结，禁止原地修改 |

## 9. 验证

1. **单元测试**：manifest 规范化与 hash 稳定；gate 的 accept/reject 分支用固定 fixture 覆盖。
2. **回放确定性**：同一 golden + baseline 重复 3 次，`AgentLoopDetection` 等确定性指标一致。
3. **注入回归**：故意把 `AGENT_MAX_ITERATIONS` 设为 1，gate 必须 reject 并记录原因。
4. **回滚演练**：模拟守护指标越界，断言自动切回 baseline 版本。
5. **幂等**：同一 `(base, candidate, golden_set_hash)` 重复触发不重复计费。
6. **无泄漏**：proposer 不可读 holdout；eval 配置变更留审计记录。

## 10. 未决问题

- Proposer 由谁承担：人工、外部 agent，还是 Claude Code 的 PR 流程？
- 灰度机制：已确定用 DB rollout 表 + claim 时热加载；是否需要按租户/部门维度而不是全局百分比？
- 进程级配置（`AGENT_RUN_*`、executor URL）的变更由谁执行：CI 流水线还是人工重启？
- 预算：每次迭代的金标规模、重复次数、judge 调用上限。
- Golden 维护：如何防止评测集与线上分布漂移？是否需要定期补充失败样本？
- 多副本：`AgentRunManager` 是单副本假设，灰度期间的对照组切流需要额外约束。
- judge 独立性：是否要求 judge 使用不同 provider，避免与主模型同源偏差。

## 11. 参考

| 主题 | 文件 |
| --- | --- |
| Span 采集与 deepeval 适配 | [`agent-span-evaluation.md`](./agent-span-evaluation.md) |
| Eval 控制台与队列 | [`eval-console-design.md`](./eval-console-design.md) |
| Span 采集实现 | `server/app/agent/spans.py` |
| Eval worker | `server/app/eval/worker.py` |
| Eval 指标 | `server/app/eval/metrics.py` |
| Trace 读取 | `server/app/routers/traces.py` |
| Run 生命周期 | `server/app/agent/runner.py` |
| Agent 配置 | `server/app/agent/runtime.py`、`agent-loop/src/bokaclaw_agent/core/config.py` |
| Replay 现状 | `dashboard/src/app/(root)/dashboard/eval/page.tsx:499-540` |
