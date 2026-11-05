# DeepEval 框架结构与评测原理

> 状态：参考文档（2026-11-05）
>
> 代码基线：deepeval `4.3.3`（仓库内 `deepeval/` 为源码参考副本，运行时依赖见
> `server/eval-requirements.txt`）
>
> 适用范围：`server/app/eval/`、`tests/agent_eval/`、评测相关的设计与排障
>
> 目标读者：后端、Agent 执行层、评测开发者

## 1. 一句话结论

DeepEval 是一个以 **LLM-as-judge** 为主、确定性规则为辅的评测框架。它的基本范式是：

```
测试用例（Test Case）+ 指标（Metric）+ 阈值（Threshold）
        → 判定（judge 模型或确定性规则）
        → 0~1 的分数
        → success = score >= threshold
```

`assert_test()` 在指标失败时抛 `AssertionError`，可以直接作为 pytest/CI 门禁；
`evaluate()` 只返回结果，适合离线批量评测。

## 2. 核心范式

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌────────────────┐
│ LLMTestCase  │──▶│ Metric       │──▶│ Executor     │──▶│ Judge / 规则   │
│ 输入/输出/   │   │ threshold    │   │ 并发/缓存/   │   │ score 0~1      │
│ 上下文/工具  │   │ 必填参数     │   │ 超时/错误    │   │ reason         │
└──────────────┘   └──────────────┘   └──────────────┘   └───────┬────────┘
                                                                 ▼
                                                     ┌──────────────────────┐
                                                     │ MetricData           │
                                                     │ success = score      │
                                                     │         >= threshold │
                                                     └──────────────────────┘
```

三个对象各司其职：

| 对象 | 作用 | 典型字段 |
| --- | --- | --- |
| 用例 | 定义“评什么” | `input`、`actual_output`、`expected_output`、`context`、`tools_called`、`_trace_dict` |
| 指标 | 定义“怎么判” | `measure()`、`threshold`、`_required_params`、`strict_mode` |
| 判定器 | 定义“谁来判” | judge LLM、嵌入模型、确定性规则、轨迹分析 |

## 3. 包结构

```
deepeval/
├── evaluate/                  入口与编排
│   ├── evaluate.py            evaluate() / assert_test()
│   ├── execute/               e2e / agentic / loop / trace_scope
│   ├── expectations.py        expectations 式断言
│   ├── console_report.py      终端 / HTML / Markdown 报告
│   └── local_store.py         本地结果落盘
│
├── test_case/                 评测输入模型
│   ├── LLMTestCase            input / actual_output / expected_output / context
│   ├── ConversationalTestCase 多轮对话
│   ├── ArenaTestCase          多模型对比
│   └── ToolCall / MCP 相关
│
├── dataset/                   Golden、EvaluationDataset
├── synthesizer/               从文档或上下文合成 golden
│
├── metrics/                   指标库（核心）
│   ├── base_metric.py         BaseMetric：measure()/a_measure() 契约
│   ├── indicator.py           指标级进度与并发 gather
│   ├── utils/                 初判、QAG verdict、schema 抽取、超时
│   ├── dag/                   DAGMetric：多节点有向无环图指标
│   ├── g_eval/                自定义 criteria + evaluation_steps
│   ├── faithfulness/          RAG 指标
│   ├── task_completion/       Agent 轨迹指标
│   └── bias/ toxicity/ pii_leakage/ ...  安全指标
│
├── models/                    Judge 模型抽象
│   ├── base_model.py          DeepEvalBaseLLM：generate()/a_generate()
│   ├── llms/                  OpenAI / Anthropic / DeepSeek / Gemini / Ollama
│   ├── retry_policy.py        重试、超时、外层 deadline
│   └── system_one/            实验性快速判定模式
│
├── test_run/                  一次评测运行的状态
│   ├── test_run.py            TestRunManager：聚合、缓存、落盘、上传
│   └── cache.py               .deepeval/.deepeval-cache.json 指标缓存
│
├── tracing/                   span / trace 采集（OTel / OpenInference）
├── integrations/ openai/ anthropic/ openai_agents/   框架埋点
├── optimizer/ red_teaming/ benchmarks/ simulator/    提示优化、红队、基准
└── cli/                       deepeval test run
```

可以按职责分成三层：

| 层 | 内容 | 说明 |
| --- | --- | --- |
| 输入层 | `test_case/`、`dataset/` | 用例与 golden 定义评测边界 |
| 指标层 | `metrics/` | `BaseMetric` 统一契约，具体指标负责拆解与判定 |
| 支撑层 | `models/`、`test_run/`、`tracing/` | judge、缓存、运行记录、span 采集 |

## 4. 运行时架构

```
┌──────────────────────────────────────────────────────────────────┐
│ 入口层   evaluate()  /  assert_test()  /  deepeval test run      │
└──────────────────────────────┬───────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ 编排层   evaluate/execute/*                                      │
│ 用例级并发 · 指标级并发 · 超时 · 缓存 · 跳过/失败策略            │
└──────────┬───────────────────┬────────────────────┬──────────────┘
           ▼                   ▼                    ▼
   ┌──────────────┐   ┌────────────────┐   ┌──────────────────┐
   │ 用例层        │   │ 指标层          │   │ Judge 层          │
   │ LLMTestCase  │──▶│ BaseMetric     │──▶│ DeepEvalBaseLLM  │
   │ Golden       │   │ measure()      │   │ generate()       │
   │ context/tool │   │ score + reason │   │ 结构化输出校验    │
   └──────┬───────┘   └───────┬────────┘   └──────────────────┘
          └───────────────────┴────────────────────┐
                                                   ▼
                                   ┌──────────────────────────────┐
                                   │ 结果层 TestResult / MetricData│
                                   │ score · threshold · success  │
                                   │ reason · model · cost · error│
                                   └──────────────┬───────────────┘
                                                  ▼
                                   ┌──────────────────────────────┐
                                   │ 报告 / 缓存 / 本地 JSON / 上传│
                                   └──────────────────────────────┘
```

关键源码位置：

| 能力 | 位置 |
| --- | --- |
| `evaluate()` 入口 | `deepeval/deepeval/evaluate/evaluate.py:213` |
| `assert_test()` | `deepeval/deepeval/evaluate/evaluate.py:85` |
| 异步批量执行 | `deepeval/deepeval/evaluate/execute/e2e.py:483` |
| 指标契约 | `deepeval/deepeval/metrics/base_metric.py:153` |
| 运行收尾与上报 | `deepeval/deepeval/test_run/test_run.py:1176` |

## 5. 评测工作原理

### 5.1 一次 `evaluate()` 的生命周期

1. **组装用例**：把被测系统的输入、输出、上下文、工具调用、执行轨迹填入
   `LLMTestCase`。
2. **声明指标与阈值**：每个指标实例带 `threshold`，并用 `_required_params`
   声明自己依赖哪些字段。
3. **参数校验**：缺字段时抛 `MissingTestCaseParamsError`。在
   `skip_on_missing_params=True` 下该指标被标记为跳过，而不是失败。
4. **执行 `measure()`**：渲染 prompt 模板，调用 judge 模型，用 Pydantic schema
   约束结构化输出；多步指标会串起“抽取 → 判定 → 打分 → 理由”的链。
5. **聚合单指标结果**：得到 `score`、`success`、`reason`、`evaluation_model`、
   `evaluation_cost`、`error` 等元数据。
6. **汇总与产出**：一个用例的指标合并为 `TestResult`，全部用例合并为一次
   test run；随后打印报告、写缓存、落本地 JSON，或在配置了 Confident AI 时上传。

### 5.2 指标契约

```python
metric.measure(test_case)   # 产出 0~1 的 score
metric.success = metric.score >= metric.threshold
```

- `strict_mode=True` 时阈值被强制为 1，且低于阈值的分数直接被压成 0，
  即“必须满分才算通过”。
- `MetricData.name` 来自指标的 `__name__` 属性，是人类可读名（如
  `"Task Completion"`），不是类名。
- `evaluate()` 返回 `EvaluationResult`，不抛断言异常；`assert_test()` 在任一
  指标失败时 `raise AssertionError`。

### 5.3 示例：Faithfulness 的判定链

`FaithfulnessMetric` 是典型的链式 judge，而不是把整段回答丢给模型问“好不好”：

```
retrieval_context ──▶ ① truths（抽取可核验事实）
                         │
actual_output     ──▶ ② claims（抽取回答中的断言）
                         │
                         ▼
                   ③ verdicts（逐条判定）
                     yes / no / borderline
                         │
                         ▼
                   ④ score = passing / total
                     默认 yes 与 borderline 都算通过；
                     penalize_ambiguous_claims=True 时 borderline 不算
                         │
                         ▼
                   ⑤ reason（从矛盾点生成理由）
```

对应实现：`deepeval/deepeval/metrics/faithfulness/faithfulness.py:113`。
公共的 QAG 打分逻辑在 `deepeval/deepeval/metrics/utils/qag.py:113`：

```
score = passed / total
strict_mode 且 score < threshold 时，score 置 0
```

同步与异步路径（`measure` / `a_measure`）逻辑一致；异步路径会把互相独立的步骤
用 `asyncio.gather` 并发执行。

### 5.4 批量编排语义

```
校验输入 / 用例类型
  → 重置 global TestRunManager
  → 构造用例级任务（Semaphore(max_concurrent=100) + throttle）
  → 每个用例内所有指标并发 measure（asyncio.gather）
      ├─ 查缓存：命中则复用旧 MetricData，成本记 0
      ├─ 未命中则执行 measure 并写入缓存
      ├─ 缺失参数 → skip
      └─ 异常 → 按 ErrorConfig 忽略或抛出
  → 汇总为 TestResult / MetricData
  → 终端报告、HTML/Markdown、本地 JSON
  → wrap_up_test_run：如配置 Confident AI 则上传
```

几个容易忽略的语义：

- **并发**：用例级受 `Semaphore` 与 `throttle` 控制，指标级用 `asyncio.gather`。
- **超时**：用例级有整体 gather 超时，单任务有 per-task 超时；超时任务会取消并标记。
- **缓存**：`.deepeval/.deepeval-cache.json`，键由“用例内容 + hyperparameters +
  指标配置（threshold/model/criteria 等）”组成。
- **跳过 ≠ 失败**：`skip_on_missing_params=True` 时缺字段的指标被跳过，deepeval
  4.3.3 甚至不会在结果里生成占位行。
- **失败策略**：`ignore_errors=True` 把异常记录到 `metric.error` 并视为失败；
  否则异常向上抛出。

## 6. 指标的四类范式

| 范式 | 代表指标 | 判定方式 | 成本 |
| --- | --- | --- | --- |
| 确定性指标 | `ExactMatchMetric`、`PatternMatchMetric`、`JsonCorrectnessMetric`、`AgentLoopDetectionMetric` | 代码规则、正则、JSON Schema、轨迹分析 | 低，可复现 |
| QAG / 链式 judge | `Faithfulness`、`AnswerRelevancy`、`ContextualRecall`、`Bias`、`Toxicity` | 拆成多项 yes/no 判定，按通过比例打分 | 中，多次 LLM 调用 |
| 自定义 criteria | `GEval`、`DAGMetric` | 用户给 criteria 或评价步骤，LLM 生成并执行评价链 | 中到高 |
| 轨迹指标 | `TaskCompletion`、`StepEfficiency`、`PlanAdherence`、`ToolCorrectness` | 读取 `_trace_dict`，评估任务完成度、步骤效率、计划一致性、工具选择 | 中，依赖 trace 质量 |

实践上应优先使用确定性指标：它们便宜、可复现，也不会把用户内容发给外部模型。

## 7. 自定义指标

DeepEval 官方支持自定义评测项，**不需要修改源码，也不需要注册**：实现一个符合
`BaseMetric` 契约的类，把实例放进 `evaluate(metrics=[...])` 或 `assert_test()`
即可。并发编排、缓存、报告和结果序列化都会像内置指标一样处理它。

### 7.1 三条扩展路径

| 方式 | 代码量 | 判定方式 | 适用场景 |
| --- | --- | --- | --- |
| `GEval` | 一个实例 | 自然语言 criteria + 评价步骤，LLM 判定 | 快速试一个新评价维度，不写类 |
| `DAGMetric` | 建一个判定图 | 多节点有向无环图，LLM 逐节点判定 | 评价有明显步骤链：抽取 → 比对 → 结论 |
| 继承 `BaseMetric` | 一个类 | 任意：确定性规则、LLM、或组合已有指标 | 规则可复现、团队自有指标、需要读 trace |

前两种只是预置好的自定义指标；真正要写代码的只有第三种。

### 7.2 指标契约

```python
from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase, SingleTurnParams


class MyMetric(BaseMetric):
    # 声明依赖哪些用例字段；缺字段时由 skip_on_missing_params 决定是否跳过
    _required_params = [SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT]

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold

    def measure(self, test_case: LLMTestCase) -> float:
        self.score = ...                       # 0~1，必须设置
        self.reason = "..."                    # 可选，落库与报告会读取
        self.success = self.is_successful()    # 基类实现：score >= threshold
        return self.score

    async def a_measure(self, test_case: LLMTestCase) -> float:
        return self.measure(test_case)         # 无异步逻辑时直接回退

    @property
    def __name__(self) -> str:
        return "My Metric"                     # 指标身份，落库与缓存都用它
```

必须遵守的只有几条：

- `measure()` 与 `a_measure()` 都要实现；`async_mode=False` 时仍建议保留异步回退。
- 必须设置 `self.score`；`self.success` 用基类的 `is_successful()`。
- 抛异常前先写 `self.error = str(exc)` 再 `raise`。只有这样才能被
  `ignore_errors=True` 记成失败，而不是打断整次评测。
- `__name__` 是指标身份，必须唯一（见 7.6）。
- 通过 `evaluate()` 调用时，`_required_params` 由 `prepare_measure()` 统一校验；
  直接调用 `measure()`（例如单元测试）时要自己先检查字段。

### 7.3 确定性指标示例

“实际调用的工具必须都在 golden 的 expected_tools 里”：

```python
class ToolNameAllowlistMetric(BaseMetric):
    _required_params = [
        SingleTurnParams.TOOLS_CALLED,
        SingleTurnParams.EXPECTED_TOOLS,
    ]

    def __init__(self, threshold: float = 1.0):
        self.threshold = threshold

    def measure(self, test_case: LLMTestCase) -> float:
        expected = {tool.name for tool in test_case.expected_tools or []}
        called = {tool.name for tool in test_case.tools_called or []}
        extra = sorted(called - expected)
        self.score = 0.0 if extra else 1.0
        self.reason = (
            f"Unexpected tools: {', '.join(extra)}"
            if extra
            else "Only expected tools were called."
        )
        self.success = self.is_successful()
        return self.score

    async def a_measure(self, test_case: LLMTestCase) -> float:
        return self.measure(test_case)

    @property
    def __name__(self) -> str:
        return "Tool Name Allowlist"
```

这类指标不调用外部模型、可复现、零成本，应与 `AgentLoopDetectionMetric`
一样常开。

### 7.4 LLM 判定的自定义指标

优先用 `GEval`，用自然语言描述一个新维度：

```python
from deepeval.metrics import GEval
from deepeval.test_case import SingleTurnParams

GEval(
    name="Answer Consistency",
    criteria="回答是否与执行轨迹中的工具输出一致，不得编造工具未返回的事实。",
    evaluation_params=[
        SingleTurnParams.INPUT,
        SingleTurnParams.ACTUAL_OUTPUT,
        SingleTurnParams.CONTEXT,
    ],
    threshold=0.5,
)
```

需要显式建模判定步骤时用 `DAGMetric`：

```python
from deepeval.metrics import DAGMetric, DeepAcyclicGraph
```

如果需要完全控制“抽取 → 判定 → 打分”的链路，可以在 `measure()` 里组合已有指标
（例如同时实例化 `AnswerRelevancyMetric` 与 `FaithfulnessMetric` 再合成分数），
或用 `deepeval.metrics.utils.initialize_model()` 取 judge 模型，用 Pydantic schema
约束其结构化输出。

### 7.5 接入方式

自定义指标写在 `server/app/eval/custom_metrics.py`，然后在
`server/app/eval/metrics.py:15` 的 `build_metrics()` 里追加：

```python
metrics: list[BaseMetric] = [AgentLoopDetectionMetric(threshold=1.0)]
metrics.append(ToolNameAllowlistMetric(threshold=1.0))   # 确定性，常开
if use_llm_judge:
    metrics += [ ... 现有 judge 指标 ... ]
    # GEval / 自定义 judge 指标放这里
```

`evaluator.py` 不需要改动：它只消费 `MetricData`，自定义指标产出的结构与内置
指标完全一致。

### 7.6 注意事项

1. **缓存键不包含自定义参数。** `test_run/cache.py:453` 的
   `create_metric_configuration()` 只取固定字段（`threshold`、
   `evaluation_model`、`strict_mode`、`include_reason`、`n`、`criteria`、
   `language`、`embeddings`、`evaluation_steps`、`evaluation_params`、
   `assessment_questions`）。自定义的 `allowlist`、`rubric` 等不会进 key。
   当前 `evaluate()` 的 `use_cache` 默认是 `False`，暂不受影响；**一旦开启缓存，
   改自定义参数可能命中旧分数**。规避：把参数写进 `criteria` / `evaluation_steps`，
   把参数编进 `__name__`，或关闭缓存。
2. **`__name__` 撞名会拿到别人的缓存。** 缓存命中时比对的是
   `cached.metric_data.name == metric.__name__`；两个自定义指标同名且配置字段相同
   会互相串。给每个指标一个独特的人类可读名。
3. **用 `SingleTurnParams`，不要用 `LLMTestCaseParams`。** 后者在 4.3.3 已弃用，
   访问时会发 `DeprecationWarning`。
4. **轨迹仍只能走私有字段。** 需要整棵 span 树时读 `test_case._trace_dict`
   （`adapter.py` 就是这么写入的），属于升级 deepeval 时必须跑契约测试的依赖。
5. **judge 类自定义指标必须放在 `use_llm_judge` 开关后面。** 它会外发 input、
   output 与工具参数，应沿用 `EVAL_JUDGE_MAX_CHARS` 截断和“默认关闭”策略。
6. **`measure()` 一定要 `return self.score`。** deepeval 同时使用返回值和
   `self.score`，两者不一致会造成难查的偏差。

## 8. BokaClaw 的落地方式

评测是一条异步、隔离、可回放的链路：

```
┌────────────┐  POST /evals   ┌────────────┐  INSERT queued  ┌──────────────────┐
│ Dashboard  │───────────────▶│ FastAPI    │────────────────▶│ Postgres         │
│ /dashboard │◀── GET 轮询 ───│ /evals     │                 │ eval_runs（队列）│
└────────────┘                └────────────┘                 └────────┬─────────┘
                                                                      │ claim / heartbeat
                                                                      ▼
                                                             ┌──────────────────┐
                                                             │ eval worker      │
                                                             │（独立镜像）      │
                                                             └────────┬─────────┘
                                                                      │ evaluate(case, metrics)
                                                                      ▼
                                                             ┌──────────────────┐
                                                             │ deepeval 4.3.3   │
                                                             └────────┬─────────┘
                                                                      │ TestResult
                                                                      ▼
                                                             结果写回 eval_runs
                                                             Dashboard 轮询展示
```

### 8.1 关键设计点

| 设计点 | 做法 | 原因 |
| --- | --- | --- |
| 依赖隔离 | deepeval 由仓库内 `./deepeval` 源码构建，只装进 `server/eval.Dockerfile` 的独立镜像 | server 固定 pydantic 2.10.4，deepeval 要求 pydantic ≥ 2.11.7；源码入库后评测与仓库代码同源 |
| span → 用例 | `server/app/eval/adapter.py` 把 `agent_run_spans` 还原为 `LLMTestCase` | 采集端不依赖 deepeval，评测端可回放、可替换 |
| 轨迹字段 | 整棵 span 树写入 `case._trace_dict` | 4.3.3 没有公开的 trace 导入 API，私有字段只允许 adapter 触碰 |
| 指标选择 | `server/app/eval/metrics.py` 统一构建指标列表 | 确定性指标常开，judge 指标按开关追加 |
| judge 开关 | `EVAL_JUDGE_ENABLED` + `EVAL_JUDGE_API_KEY` 双重开关 | 默认不把用户内容发给外部 judge |
| 结果解释 | `server/app/eval/evaluator.py` 补齐被跳过指标的占位行 | deepeval 4.3.3 不返回跳过项，前端需要解释分数缺口 |

### 8.2 指标选择

`server/app/eval/metrics.py:15` 的 `build_metrics()`：

- 始终启用 `AgentLoopDetectionMetric(threshold=1.0)`：纯确定性，不需要 judge。
- 仅当显式开启 judge 时追加：
  - `AnswerRelevancyMetric(threshold=0.5)`
  - `TaskCompletionMetric(threshold=0.5)`
  - `StepEfficiencyMetric(threshold=0.5)`
  - `ArgumentCorrectnessMetric(threshold=0.5)`
- `ToolCorrectnessMetric(threshold=0.5)` 额外要求该用例同时有 `expected_tools`
  和 `tools_called`。

### 8.3 评测执行

`server/app/eval/evaluator.py:43` 的 `evaluate_run()`：

1. 校验 golden 快照哈希，防止冻结数据损坏。
2. `adapter.require_deepeval()` 校验版本必须为 4.3.3。
3. `adapter.to_llm_test_case()` 把 span 树转成用例。
4. 开启 judge 时按 `EVAL_JUDGE_MAX_CHARS` 截断输入、输出、上下文和工具参数。
5. 调用 `evaluate()`，关闭进度条与结果打印，错误策略为
   `ignore_errors=True, skip_on_missing_params=True`。
6. 把 `MetricData` 转成落库行；对 deepeval 未返回的跳过指标补占位行。
7. 总体成功定义为：所有非跳过指标都满足 `success is not False` 且无 `error`。

### 8.4 文件索引

| 职责 | 文件 |
| --- | --- |
| 判分入口 | `server/app/eval/evaluator.py` |
| span → 用例 | `server/app/eval/adapter.py` |
| 指标选择 | `server/app/eval/metrics.py` |
| worker 生命周期 | `server/app/eval/worker.py` |
| 开关与 judge 环境 | `server/app/eval/config.py` |
| 运行时依赖 | `server/eval-requirements.txt` |

## 9. 关键注意事项

1. **分数是 judge 的判断，不是客观真值。** 换 judge 模型或改 prompt 版本，分数可能
   变化。它适合做相对回归和趋势，不适合当绝对精度。
2. **指标是否适用由 `_required_params` 决定。** 没有 retrieval context 时
   Faithfulness 无法出分；没有 trace 时轨迹指标会退化或跳过。
3. **确定性指标优先。** 能用规则判定的就不要调用 LLM：更便宜、可复现，也不涉及
   数据外发。
4. **缓存与阈值互相影响。** 阈值属于缓存键，改阈值会触发重算；但 judge 模型本身的
   行为不在缓存键里，换模型不一定失效，需要换缓存目录或禁用缓存。
5. **版本必须锁死。** 本项目依赖 4.3.3 的私有 `_trace_dict` 结构。升级 deepeval 前
   必须先跑 adapter 契约测试，确认轨迹字段仍然兼容。
6. **judge 会外发内容。** deepeval 的 LLM 指标会把 input、actual output、工具调用
   和 golden 的 expected output 发给 judge。默认关闭；开启后 UI 必须明示外发目标。

## 10. 参考位置

| 主题 | 位置 |
| --- | --- |
| span 采集与评测边界 | [`agent-span-evaluation.md`](./agent-span-evaluation.md) |
| 在线评测控制台 | [`eval-console-design.md`](./eval-console-design.md) |
| deepeval 源码参考 | `deepeval/` |
| 运行时依赖与版本 | `server/eval-requirements.txt`、`server/app/eval/adapter.py` |
