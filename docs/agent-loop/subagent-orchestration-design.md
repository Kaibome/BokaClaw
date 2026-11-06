# BokaClaw 子 Agent 调度能力设计

> **本文是初版调研，已被 [`subagent-orchestration-design-v2.md`](subagent-orchestration-design-v2.md) 取代。**
> v2 结合 [`subagent-call-walkthrough.md`](subagent-call-walkthrough.md) 中一次真实调用的解剖，
> 把任务包、结构化结果、工具级只读、核验与 resume 协议化。后续以 v2 为准。
>
> 状态：调研 + 方案设计（未实现）
>
> 日期：2026-11-06
>
> 适用范围：`agent-loop/`（`bokaclaw-agent`）、`server/`、`dashboard/`
>
> 目标读者：Agent 执行层、服务端、Dashboard 开发者

---

## 1. 结论

**不自己写一套 A2A，也不把 LangGraph / CrewAI / OpenAI Agents SDK / Microsoft Agent Framework 整体引入。**

推荐在现有 `bokaclaw-agent` 的 ReAct 循环上加一层很薄的 **「子 Agent 即工具」（Agent-as-Tool）** 原语：

```text
父 Agent（主循环，拥有最终答案）
  └── research_agent(task, context, expected_output)   ← 普通 Tool
        └── 子 AgentRuntime（全新上下文 + 受限工具 + 可选更便宜的模型）
              └── 返回一段压缩后的结论 / 结构化 JSON
```

理由是三句话：

1. **A2A 解决的是跨组织、跨进程、跨技术栈的 Agent 互操作**（Agent Card、任务生命周期、鉴权、SSE/推送），不是进程内调度。用它做内部子 Agent 属于杀鸡用牛刀，业界共识也是「A2A 是决策阶梯的最后一级」。
2. **开源框架都自带 Agent 主循环、状态机、持久化和 tracing**，与 BokaClaw 现有的「SDK 只管一轮 ReAct、server 拥有 run 生命周期」设计正面冲突。引入等于把已经跑通的执行层重写一遍，还多一个重依赖。
3. **这个能力本身很小**：一个子 Agent 工具 + 上下文隔离 + 工具白名单 + 深度/并发/超时/预算限制 + 结果压缩 + 嵌套 span，约 600–900 行代码，可以完全复用现有测试与可观测性设施。

分阶段落地：

| 阶段 | 内容 | 是否需要 DB 迁移 |
| --- | --- | --- |
| P1（MVP） | 串行子 Agent、工具白名单、结果压缩、usage 合并、嵌套 span | **不需要** |
| P2 | 并行委派、子 run 一等公民（可回放、可单独取消） | 需要 |
| P3 | 远程 Agent / 第三方 Agent | 需要，此时才考虑 A2A 适配器 |

---

## 2. 业界现状

### 2.1 先把概念拆开

「多 Agent」经常把四种完全不同的拓扑混在一起：

| 模式 | 控制权 | 上下文 | 典型实现 | 适合什么 |
| --- | --- | --- | --- | --- |
| **上下文隔离子 Agent**（subagent / worker） | 父 Agent 始终拥有最终答案 | 子 Agent 全新上下文，只回传摘要 | Claude Code Task、Anthropic orchestrator-worker | 读得多、写得少；隔离噪音；并行调研 |
| **Agent-as-Tool** | 父 Agent 调用一个「会思考的工具」，仍由父合成答案 | 子 Agent 可有独立上下文 | OpenAI `agent.as_tool()`、LangChain subagents pattern | 父需要汇总多个专家结果 |
| **Handoff** | 控制权交给专家，专家直接面向用户 | 保留对话历史，切换指令 | OpenAI handoffs、Swarm、MAF Handoff | 路由本身是工作流；专家应直接回答 |
| **图 / 工作流编排** | 由确定性代码控制边走 | 由状态图决定 | LangGraph、MAF Workflow、CrewAI Flows | 步骤可枚举、需要审计和重试 |
| **A2A（Agent2Agent）** | 两个独立部署的 Agent 对等协作 | 通过任务/artifact 交换 | Google A2A、Azure AI Foundry、Bedrock AgentCore | 跨团队、跨厂商、跨信任边界 |

BokaClaw 需要的是第一、二类的组合：**父 Agent 主循环不变，子 Agent 作为工具被调用，结果回到父上下文**。

### 2.2 各家的具体做法

**Anthropic / Claude Code**

- 主 Agent 分解任务，派生 3–5 个并行子 Agent；每个子 Agent 有独立上下文窗口，只把结论回传。官方披露在内部 research eval 上比单 Agent Opus 4 高约 **90.2%**，代价是约 **15 倍 token**；并观察到 BrowseComp 约 **80% 的方差可由 token 用量解释**——换句话说，收益主要来自「更大的有效上下文」，不是来自「多 Agent 这个形式本身」。
- 已知瓶颈：主 Agent 目前**同步等待**子 Agent，异步执行是官方明确说尚未完成的下一步。
- Claude Code 的 subagent 机制：`AgentDefinition` 提供 `description`（决定何时委派）、`prompt`（自己的系统提示）、`tools`（工具白名单）；子 Agent 不继承父对话历史、父工具结果和父系统提示，只拿到 Task 的 prompt 字符串。默认禁止无限递归，较新版本支持有限嵌套（默认最大深度 3）和并发上限；支持 `outputFormat` 结构化输出与 session resume。
- Claude Agent SDK 把同一套能力暴露为 `agents` 参数 + `Task` 工具，是「Agent-as-Tool」最直接的参考实现。

**OpenAI Agents SDK**

- 明确区分 **handoffs** 与 **agents-as-tools**：handoff 把控制权交给专家，由专家回答用户；agents-as-tools 让 manager 保持控制权，把专家当作有界能力调用，最后由 manager 合成。
- 官方建议：**不要过早拆分**。先一个 Agent，只有当「能力隔离、策略隔离、提示词清晰度、trace 可读性」有实质收益时才加专家。需要重试/验证/再生成的循环时用 agents-as-tools，因为 handoff 之后专家无法把控制权交回编排者。

**LangGraph / LangChain**

- `langgraph-supervisor` 已不再积极维护，官方迁移方向是 **subagents pattern**：主 `create_agent` 把每个专家包成 `@tool`，工具内部调用 `subagent.invoke(...)`。这与我们的方案一致。
- 社区总结的硬约束：必须加循环守卫；层级深度建议不超过 2–3 层；状态要精简，只把蒸馏后的摘要往上传递；worker 用便宜模型，顶层用最强模型。
- 另一条重要经验：Anthropic 自己的研究系统刻意选择了**扁平 supervisor**，而不是多层树。

**CrewAI**

- `Crew` 的 hierarchical process 在实践中被批评较多：manager 往往不能选择性委派，任务实际接近串行执行；delegation ping-pong / 无限循环是头号故障；每次委派前多一次 manager LLM 调用，成本接近翻倍。生产上更推荐用确定性的 `Flow` 包住小型 `Crew`。
- 这提示我们：**不要做「经理 Agent 自由对话式派活」**，而要做「父 Agent 通过有 schema 的工具调用派活」。后者可测试、可限流、可审计。

**Microsoft Agent Framework**

- AutoGen 于 2025-10 进入维护模式，新能力进入 MAF（AutoGen + Semantic Kernel 合并）。MAF 的编排原语是 Sequential / Concurrent / Handoff / GroupChat / Magentic，Handoff 必须配 `WithMaxHandoffs()` 之类的终止条件。
- MAF 的 Workflow 更接近 LangGraph；它同样把「Agent 会话状态」和「工作流状态」分开管理。这与 BokaClaw 的 server-owned run 生命周期是同一类问题，但 MAF 是另一个运行时。

**Google A2A**

- 2025-04 发布，2025-06 捐赠给 Linux Foundation。核心是 Agent Card（能力发现）、task 生命周期、artifact、流式/推送、企业级鉴权。
- A2A 与 MCP 互补：MCP 是 Agent → 工具/资源，A2A 是 Agent → Agent。A2A 的价值在**跨信任边界**：对方是另一个团队部署、另一套技术栈、需要正式任务契约和身份。
- 2026 年的实践共识是：**内部紧耦合子 Agent 用本地 subagent；A2A 放在决策阶梯最后一级**。把 A2A 用在单进程内部，会额外引入网络、身份、发现、兼容性、分布式状态五类成本，却换不到任何收益。

### 2.3 失败模式：多 Agent 大多死于协作，不是模型

MAST（*Why Do Multi-Agent LLM Systems Fail?*, arXiv:2503.13657）基于 7 个框架、1600+ 条失败轨迹的结论：

- **79% 的失败属于规格与协作问题**，不是模型能力或基础设施问题。
- 高频模式：步骤重复（15.7%）、推理与行动不一致（13.2%）、不知道何时终止（12.4%）、丢失对话历史、信息隐藏/截断、过早终止、验证缺失。
- 对设计的直接含义：**必须有明确的终止契约、结果 schema、深度/调用次数上限、循环守卫，以及一个能判断「子 Agent 结果够不够」的父级**。

### 2.4 什么时候不该上子 Agent

- 一个 prompt 就能稳定完成，且每一步都需要同一份上下文。
- 任务强顺序依赖，拆开后只能串行等待，隔离带来的收益小于协调成本。
- 对延迟/成本敏感，且没有并行的独立子任务。
- 子任务需要父对话里的大量隐含上下文，而这些上下文无法用一段任务说明表达清楚。

BokaClaw 已经有上下文压缩、fact ledger、no-progress guard、research memory。这些手段能缓解「上下文太长」，但不能解决「多个独立调研方向需要并行 + 每个方向都读很多资料」。子 Agent 的增量价值主要在后两点。

---

## 3. BokaClaw 现状与约束

### 3.1 已有能力

| 层 | 现状 | 位置 |
| --- | --- | --- |
| SDK 主循环 | 同步 ReAct，`run()` / `stream()` / `arun()`；工具**串行**执行 | `agent-loop/src/bokaclaw_agent/agent/runtime.py:244` |
| 工具 | `@tool` 从类型注解与 docstring 生成 schema；`ToolRegistry` 按名分发 | `agent-loop/src/bokaclaw_agent/tools/registry.py:151` |
| 上下文 | system + 历史 + 本轮；压缩、fact ledger、step notice、progress guard | `agent-loop/src/bokaclaw_agent/agent/context.py`、`compression.py` |
| 服务端执行 | 每个 run 新建 Runtime；`AgentRunManager` 拥有排队/领取/取消/恢复 | `server/app/agent/runner.py:418` |
| 事件 | run 内单调 `seq`；`agent_run_events` 持久化并可重放 | `server/app/models.py:194` |
| Trace | `agent_run_spans` 已支持 `parent_span_id` 和 `span_type="agent"`；`AGENT_SPAN_CAPTURE` 默认 `true` | `server/app/models.py:218`、`server/app/agent/spans.py:551`、`server/.env.example:167` |
| 评测 | span 树 → deepeval；`agent` 节点已有 `agent_handoffs` 字段（当前为空） | `server/app/eval/adapter.py:110` |

### 3.2 关键约束

1. **SDK 不拥有 run 生命周期**。子 Agent 不能自己建 `agent_runs`；它只能发事件，由 server 决定怎么记录。
2. **工具是串行的**。P1 直接复用串行路径最简单；并行需要改 `_execute()` 的工具执行段（`runtime.py:378-436`）。
3. **`agent_runs` 有「一个会话同时只能有一个活跃 run」的部分唯一索引**（`server/app/models.py:133-138`）。如果把子 run 直接塞进 `agent_runs`，必须先把这个索引改成只约束根 run。
4. **`agent_run_events.event_type` 有 CHECK 约束**（`server/app/models.py:198-202`）。每加一种持久化事件类型都要迁移；P1 应尽量不加。
5. **`agent_run_spans` 已经支持嵌套**，这是 P1 最划算的可观测性通道，零迁移。
6. **usage 来源是固定元组** `("agent_loop", "compression", "memory_llm", "memory_embedding")`（`server/app/agent/usage.py:19`）。子 Agent 的 token 必须单独成桶，否则无法回答「这次多花了多少」。
7. **服务端在 daemon 线程里跑 SDK**（`server/app/agent/runtime.py:805` 的 `stream_local_agent`），整轮有 `max_seconds` 墙钟上限（默认 300s，`AGENT_LOOP_MAX_SECONDS`），进程级 run 并发默认 2（`AGENT_RUN_CONCURRENCY`）。P1 的子 Agent 在父 worker 线程内同步执行，不额外占用 run 并发，但会消耗父轮的墙钟预算；P2 若把子 Agent 做成独立 run，会与父 run 竞争这个信号量。
8. **`agent_id` 目前只是透传的元数据**，不选择提示词/模型/工具（`server/app/agent/runtime.py:688-751` 只构造一个 `AgentConfig`）。要做「按名字选择子 Agent」，需要新增一个 spec 注册表，而不是复用 `agent_id`。

---

## 4. 方案选型

### 4.1 为什么不自己写 A2A

| 维度 | A2A 需要 | BokaClaw 内部子 Agent 需要 |
| --- | --- | --- |
| 发现 | Agent Card、注册中心 | 编译期/配置期已知 |
| 传输 | HTTP + JSON-RPC + SSE | 同进程函数调用 |
| 身份 | OAuth/mTLS、卡片签名 | 继承当前用户与 run |
| 任务状态 | submitted/working/completed/failed… | 父工具调用成功/失败 |
| artifact | 跨 Agent 文件/数据契约 | 一个字符串或 JSON |
| 故障模型 | 网络分区、重试幂等、取消竞态 | 异常 + 超时 + cancel_event |

A2A 的每一项都是真实成本，而内部场景一项都用不上。**如果未来要把 BokaClaw 的 Agent 暴露给外部系统，或调用第三方 Agent，再在 `server/` 边界加一个 A2A adapter**；内部编排继续走本地接口，两者互不影响。

### 4.2 为什么不直接用开源框架

| 方案 | 能带来什么 | 与 BokaClaw 的冲突 | 结论 |
| --- | --- | --- | --- |
| **LangGraph** | 图编排、checkpoint、supervisor、并行 | 会取代现有 ReAct 主循环与状态模型；checkpointer 与 server 的 run/事件表重复；依赖重 | 不采用。只借鉴「subagents-as-tools + 循环守卫 + 深度上限」 |
| **OpenAI Agents SDK** | handoff、agents-as-tools、tracing | 自带 Agent 循环、session、provider 抽象；handoff 语义与 server-owned run 冲突；会形成第二套执行栈 | 不采用。借鉴「manager 拥有最终答案、不要过度拆分」 |
| **CrewAI** | role/crew 抽象、Flow | hierarchical 委派在生产中被证明不稳定；角色提示词与现有工具/权限模型不匹配 | 不采用 |
| **Microsoft Agent Framework** | Workflow、Handoff、GroupChat、OTel | 新框架、in-process only、生态绑定 Azure；同样自带会话状态 | 不采用 |
| **PydanticAI** | 类型化 Agent、依赖注入 | 仍是一套新的循环与状态；与 `bokaclaw-agent` 功能重叠 | 不采用 |
| **自建薄层** | 完全复用现有循环、工具、事件、span、usage、评测 | 需要自己实现深度/并发/超时/递归保护 | **采用** |

判断标准不是「框架好不好」，而是「引入后 BokaClaw 还剩多少自己的代码」。现有 SDK 已经把 ReAct、流式、压缩、持久化、取消、事件做完并有测试；再引入一个框架，等于把最有价值的部分替换掉。

### 4.3 借鉴哪些设计，不抄哪些代码

借鉴：

- Claude Code / Agent SDK 的 `AgentDefinition`：`description` 决定路由，`prompt` 独立，`tools` 白名单，子 Agent 不继承父历史。
- OpenAI 的 agents-as-tools：父保持控制权，子结果回到父，由父合成最终答案。
- Anthropic 的 orchestrator-worker：扁平结构、每个子 Agent 一个明确子任务、只回传蒸馏结果、大产物落文件/存储而不是塞回上下文。
- LangGraph 社区的硬约束：深度 ≤ 2–3、循环/调用次数守卫、状态精简、模型分层。

不抄：

- 不做 handoff（子 Agent 直接面向用户）。
- 不做自由对话式 manager/group chat。
- 不做无限递归。
- 不做跨进程 A2A。

---

## 5. 目标设计

### 5.1 总体结构

```text
┌──────────────────────────────────────────────────────────────┐
│ server AgentRunManager（父 run）                              │
│  build_runtime()                                             │
│    ├── memory tools / dev tools                             │
│    └── SubagentTool("research_agent")  ← 普通 Tool           │
└──────────────────────────────┬───────────────────────────────┘
                               │ tool_call(arguments={task,...})
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ SubagentTool.invoke()                                        │
│  ├── 深度/调用次数/预算检查                                   │
│  ├── 新 cancel_event + 超时 watchdog                         │
│  ├── 工具白名单过滤（去掉子 Agent 工具，除非允许嵌套）         │
│  ├── 新 AgentRuntime（全新 ContextManager，无父历史）          │
│  │     └── run(task_packet, cancel_event=child_cancel)       │
│  ├── usage 合并到父 run（source="subagents"）                 │
│  ├── 嵌套 agent span + 子工具 span                            │
│  └── 结果裁剪 / JSON schema 校验 → ToolResult                 │
└──────────────────────────────┬───────────────────────────────┘
                               │ 只有最终结论回到父上下文
                               ▼
                    父 Agent 继续 ReAct / 合成答案
```

### 5.2 SDK 公共 API

新增 `agent-loop/src/bokaclaw_agent/agent/subagent.py`：

```python
from dataclasses import dataclass, field
from typing import Any

from ..core.config import ModelConfig


@dataclass(frozen=True)
class SubagentSpec:
    """一个可被父 Agent 调用的子 Agent 定义。"""

    name: str                          # 工具名，如 "research_agent"
    description: str                   # 给父模型看的「何时使用」
    system_prompt: str                 # 子 Agent 自己的系统提示
    tool_names: tuple[str, ...] | None = None   # None = 继承父工具（仍会去掉子 Agent 工具）
    model: ModelConfig | None = None   # None = 继承父模型
    max_iterations: int = 8
    timeout_seconds: float = 120.0
    max_result_chars: int = 8_000
    output_schema: dict[str, Any] | None = None  # 可选 JSON Schema
    allow_nested: bool = False         # 是否允许子 Agent 再派子 Agent
    metadata: dict[str, Any] = field(default_factory=dict)


class SubagentTool:
    """把一个 SubagentSpec 暴露成普通 Tool。"""

    def __init__(
        self,
        spec: SubagentSpec,
        *,
        parent_config: "AgentConfig",
        parent_tools: "ToolRegistry",
        depth: int = 0,
        max_depth: int = 2,
        emit: Callable[[AgentEvent], None] | None = None,
        provider_factory: Callable[[ModelConfig], ModelProvider] | None = None,
    ) -> None: ...

    def as_tool(self) -> "Tool": ...
```

对外暴露的调用参数（由 `as_tool()` 生成 JSON Schema）：

```json
{
  "task": "string，必填。要完成的子任务，写成自包含的说明。",
  "context": "string，可选。子 Agent 看不到父对话，必须把相关背景、约束、已有发现放在这里。",
  "expected_output": "string，可选。期望的输出格式/长度/重点。"
}
```

`AgentRuntime` 新增参数：

```python
AgentRuntime(
    config,
    tools=...,
    provider=...,
    store=...,
    on_event=...,
    subagents: Iterable[SubagentSpec] = (),   # 新增
    subagent_depth: int = 0,                  # 新增，内部递归用
    subagent_options: SubagentOptions | None = None,  # 新增
)
```

`SubagentOptions`：

```python
class SubagentOptions(BaseModel):
    enabled: bool = False
    max_depth: int = 2                # 0 = 禁用；2 = 父 -> 子 -> 孙
    max_concurrent: int = 3
    max_calls_per_run: int = 4
    max_result_chars: int = 8_000
    default_timeout_seconds: float = 120.0
    token_budget: int | None = None   # 整个父 run 的子 Agent token 上限
```

### 5.3 上下文契约（最重要的部分）

**父 → 子**：只有三样东西。

1. `task`：自包含的任务说明。
2. `context`：父认为必要的背景、约束、已证实/已排除的线索。
3. `expected_output`：输出契约。

不传父对话历史、不传父工具结果、不传父系统提示。这与 Claude Code 的行为一致，也是上下文隔离的意义所在。

**子 → 父**：只有最终结果，且必须压缩。

- 默认返回子 Agent 的最终文本，按 `max_result_chars` 截断（保留头尾，中间标注截断）。
- 配置了 `output_schema` 时，要求子 Agent 输出 JSON，父侧用 `jsonschema`/Pydantic 校验；校验失败返回结构化错误，父可以重试一次。
- 建议的研究类结果 schema：

```json
{
  "summary": "3-5 句结论",
  "findings": [
    {"claim": "...", "evidence": "...", "source": "https://..."}
  ],
  "sources": ["https://..."],
  "open_questions": ["..."],
  "confidence": "high | medium | low"
}
```

**大产物不走上下文**：子 Agent 抓到的长网页、生成的文件、完整代码，写到 workspace 或 mem0，结果里只放路径/引用。这是 Anthropic 说的「避免传话游戏」和「大 artifact 落外部存储」。

**结果大小上限是硬约束**：子 Agent 的价值是「读很多、回很少」。如果子结果又长又啰嗦，父上下文照样爆炸，等于没隔离。

### 5.4 执行、并发与限制

**P1：串行。**

- `SubagentTool.invoke()` 在父 ReAct 的工具执行段同步调用子 `runtime.run()`。
- 多个子 Agent 调用仍然按模型返回顺序串行执行。实现最简单，行为与现有工具完全一致。
- 这已经能拿到上下文隔离与专用工具/模型的主要收益。

**P2：并行。**

- `Tool` 增加 `parallel_safe: bool = False`；子 Agent 工具设为 `True`。
- `_execute()` 检测当前 assistant 消息里的工具调用：如果**全部**是 `parallel_safe`，用 `ThreadPoolExecutor(max_workers=max_concurrent)` 并发执行，并按原始顺序回填 `tool` 消息。
- 只要混入一个非并行安全工具，整批退回串行，保持现有语义。
- 并发执行要求 `_emit()` 的 `_event_seq` 加锁；每个子 Agent 使用独立 Runtime，不共享可变状态。

**墙钟与并发预算：**

- 父轮有 `max_seconds`（服务端默认 300s）墙钟上限；子 Agent 的 `timeout_seconds` 必须小于父轮剩余时间，至少留出一次父模型调用的余量。串行委派两个 120s 子 Agent 会吃掉 240s，父轮可能来不及收尾。
- P1 的子 Agent 在父 worker 线程内执行，不消耗 `AGENT_RUN_CONCURRENCY`（默认 2），但会把父 run 的占用时间拉长。
- P2 若把子 Agent 做成独立 `agent_run`，必须为子 run 单独计数，避免它们和用户发起的 run 抢同一个信号量；父 run 应持有子 run 的配额，而不是全局配额。

**限制（P1 就要有）：**

| 限制 | 默认 | 行为 |
| --- | --- | --- |
| `max_depth` | 2 | 超过时子 Agent 的工具集里不注册子 Agent 工具；父调用返回错误 |
| `max_calls_per_run` | 4 | 超过后 `SubagentTool` 直接返回 `{"error": "subagent call budget exhausted"}` |
| `timeout_seconds` | 120 | `threading.Timer` 设置子 `cancel_event`；子 Agent 在检查点停止 |
| `max_iterations` | 8 | 子 Agent 自己的 ReAct 步数上限 |
| `max_result_chars` | 8000 | 截断后回传 |
| `token_budget` | 可空 | 父 run 累计子 Agent token 超限后拒绝新委派 |
| 递归 | 默认禁止 | 子 Agent 的工具白名单里去掉所有 `SubagentTool` |

**取消语义：**

- 父 `cancel_event` 必须传播到所有子 Agent。子 Agent 的取消事件是一个组合对象：父事件 **或** 本地超时事件任一置位即取消。
- 父取消时，子 Agent 在其下一个检查点停止，`SubagentTool` 返回 `is_error=True` 的 `ToolResult`，父的取消流程继续。
- 子 Agent 失败/超时**不应**默认让父 run 失败。子 Agent 工具强制使用 `return` 错误语义（即使父配置 `tool_error_mode="raise"`），把错误作为工具结果回给模型，让父决定重试、换方向或直接回答。
- P2 有子 run 后，父 run 取消要级联取消子 run；子 run 取消不反向影响父。

### 5.5 服务端集成（P1，零迁移）

**注册。** 新增 `server/app/agent/subagents.py`。注意 `agent_id` 目前只是透传字符串，不是配置选择器；子 Agent 的路由由父模型依据工具 `description` 决定，spec 本身在代码里静态注册。如果以后需要租户自定义子 Agent，再把 spec 放进数据库，而不是复用 `agent_id`。

```python
def build_subagent_tools(sdk, config, payload) -> list:
    if not config.subagents_enabled:
        return []
    specs = [
        sdk.SubagentSpec(
            name="research_agent",
            description=(
                "Delegate an independent web-research question. Use it when the "
                "question needs many searches/fetches and would flood the main context. "
                "Do not use it for a single known URL or a trivial lookup."
            ),
            system_prompt=RESEARCH_AGENT_PROMPT,
            tool_names=("WebSearch", "WebFetch", "Read", "Glob", "Grep", "search_memories"),
            model=config.subagent_model_config,   # 可选更便宜的模型
            max_iterations=config.subagent_max_iterations,
            timeout_seconds=config.subagent_timeout_seconds,
            max_result_chars=config.subagent_max_result_chars,
            output_schema=RESEARCH_RESULT_SCHEMA,
        ),
    ]
    return [sdk.SubagentTool(spec, ...) for spec in specs]
```

`build_runtime()`（`server/app/agent/runtime.py:688`）把返回值追加到工具列表：

```python
tools = [
    *_memory_tools(sdk, payload.get("user_id")),
    *build_dev_tools(sdk),
    *build_subagent_tools(sdk, config, payload),
]
```

**事件桥接。** SDK 新增三种事件，`_bridge_event()`（`server/app/agent/runtime.py:498`）映射：

| SDK 事件 | 服务端内部事件 | 去向 |
| --- | --- | --- |
| `subagent_started` | `AgentSubagentStarted` | span：新增 `agent` span，`parent_span_id` = 对应工具 span |
| `subagent_event` | `AgentSubagentEvent` | span：子工具/子 LLM span（可选，P1 可只记 start/end） |
| `subagent_finished` | `AgentSubagentFinished` | span 收尾；`AgentUsageUpdate(source="subagents")` |

**不要新增 `agent_run_events` 类型。** 父 run 对前端仍然只产生 `tool.call` / `tool.result`：委派就是一个名为 `research_agent` 的工具调用，`ToolTrace` 原样渲染。子 Agent 的内部过程进 `agent_run_spans`，Trace 页能看到嵌套树。这样 P1 完全不需要改 `models.py` 的 CHECK 约束。

**Span 结构（零迁移，复用 `parent_span_id`）：**

```text
agent (root)
├── llm.turn.1
├── tool:call_xxx (research_agent)
│   └── agent (subagent:research_agent)
│       ├── llm.turn.1
│       ├── tool:call_yyy (WebSearch)
│       ├── tool:call_zzz (WebFetch)
│       └── llm.turn.2
└── llm.turn.3
```

`SpanRecorder` 需要：

- 维护 `self.subagent_spans: dict[str, dict]`，用 `subagent_id` 映射；
- 子工具 span id 加前缀（如 `subtool:<subagent_id>:<tool_call_id>`），避免与父工具 id 碰撞；
- `parent_span_id` 指向子 Agent 的 agent span，而不是根 `"agent"`。

**Usage。**

- `SOURCES` 增加 `"subagents"`（`server/app/agent/usage.py:19`）。
- `AgentUsageUpdate(source="subagents")` 在子 Agent 结束时上报，`calls`/`unknown_calls` 从子 `RunResult.usage` 推导。
- `done` payload、助手消息 `metadata.usage`、`GET /agent/runs/{id}` 自动带上 `sources.subagents`；Dashboard 的类型加一个可选字段即可。
- 如果做了 P2 子 run，子 run 自己的 `agent_runs.usage` 也应记录，便于按子 Agent 归因。

**取消与恢复。**

- `AgentRunManager` 的 `cancel_event` 原样传给父 Runtime；`SubagentTool` 把它作为父侧取消源。
- 父 run 被服务重启恢复（`_recover_run`）时，子 Agent 随父一起中断；P1 没有子 run，不需要单独恢复。
- 子 Agent 的 best-effort 持久化：P1 不写会话，只写 span；失败不影响父 run。

**配置。**

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `AGENT_SUBAGENTS_ENABLED` | `false` | 总开关；关闭时与现在完全一致 |
| `AGENT_SUBAGENT_MODEL` | 空 | 子 Agent 模型；空表示继承主模型 |
| `AGENT_SUBAGENT_MAX_DEPTH` | `2` | 最大嵌套深度 |
| `AGENT_SUBAGENT_MAX_CONCURRENT` | `3` | P2 并发上限 |
| `AGENT_SUBAGENT_MAX_CALLS_PER_RUN` | `4` | 每个父 run 的委派次数上限 |
| `AGENT_SUBAGENT_TIMEOUT_SECONDS` | `120` | 单个子 Agent 超时 |
| `AGENT_SUBAGENT_MAX_ITERATIONS` | `8` | 子 Agent 最大 ReAct 步数 |
| `AGENT_SUBAGENT_MAX_RESULT_CHARS` | `8000` | 回传父上下文的最大字符数 |
| `AGENT_SUBAGENT_TOKEN_BUDGET` | 空 | 父 run 的子 Agent token 总预算 |

### 5.6 Dashboard

**P1：不改前端也能用。**

- 委派显示为一次普通工具调用（`research_agent`），`ToolTraceRow` 已能展示参数、输出、耗时、状态。
- `tool.result` 的 `content` 是压缩后的 JSON/摘要，展开即可看到。
- Trace 页的 span 树自动出现嵌套 `agent` 节点。
- 可选的小改动：在 `tool-trace.tsx` 里对 `entry.name` 命中子 Agent 名称的条目显示一个 `Bot`/`Users` 图标和「Subagent」标签，并把输出按 JSON 美化。

**P2：嵌套实时视图。**

- 父 run 发出 `tool.call` 后，子 run 作为独立 `agent_run` 存在；Dashboard 可以用 `GET /agent/runs/{child_id}/events?after_seq=N` 订阅子 run 的事件（与现有重连机制相同）。
- `ToolTraceRow` 展开后渲染子 Agent 的 `ToolTrace` + 文本增量。
- 需要新增 `subagent.started` / `subagent.completed` 持久化事件（此时才改 `event_type` CHECK），或者前端从 `tool.call` 的 metadata 里拿 `child_run_id` 直接订阅。

### 5.7 提示词设计

**父 Agent 的编排提示**（追加到 `config.system_prompt` 后面）：

```text
你可以把独立的、需要大量检索的子任务委派给 research_agent。
委派前确认：
- 子任务可以自包含地描述，不依赖当前对话的隐含上下文；
- 预期收益大于一次额外模型调用的成本；
- 最多 4 次委派，能自己快速回答的不要委派。
调用时把任务目标、必要背景、输出格式写清楚。子 Agent 看不到当前对话。
子 Agent 返回后，由你判断证据是否充分、是否需要补充检索，并合成最终答案。
```

**子 Agent 的系统提示**（`RESEARCH_AGENT_PROMPT`）：

```text
你是一个研究子 Agent。你只看到任务说明，看不到父对话。
规则：
1. 先规划再检索；优先官方/一手来源。
2. 不要重复抓取同一 URL；已有证据足够时停止。
3. 不要向用户提问，不要输出过程性思考，只输出最终结果。
4. 结论必须有来源；无法证实的写进 open_questions。
5. 按给定 JSON schema 输出；总长度不超过 6000 字符。
```

**模型分层**：父用最强模型，子用便宜/快模型（如 `AGENT_SUBAGENT_MODEL`）。Anthropic 的做法是 lead 用 Opus、worker 用 Sonnet；BokaClaw 可以让父保持 `AGENT_MODEL`，子用更便宜的模型，并用 `AGENT_SUBAGENT_MAX_ITERATIONS` 控制成本。

**路由质量**：`description` 是父模型决定是否委派的唯一依据，必须写清楚「什么时候用 / 什么时候不用」。Claude Code 的经验是 description 缺失或含糊会导致子 Agent 永不触发或过度触发。

### 5.8 可观测性与评测

**Trace**

- 子 Agent 是一个 `span_type="agent"` 的子 span，`attributes` 至少包含：`subagent_name`、`depth`、`model`、`tool_names`、`task_chars`、`result_chars`、`stop_reason`、`usage`。
- 子 LLM span 的 `parent_span_id` 指向子 agent span；子工具 span 同理。
- `server/app/eval/adapter.py` 的 `agent` 节点已经有 `agent_handoffs` 字段；P2 可以填入子 Agent 名称，评测侧无需改 schema。

**指标**

| 指标 | 用途 |
| --- | --- |
| 委派次数 / run | 判断是否过度委派 |
| 子 Agent token 占比 | 成本归因 |
| 子 Agent 成功率 / 超时率 | 稳定性 |
| 父 run 总 token、总时延 | 与单 Agent 基线对比 |
| 最终答案质量（现有 eval） | 判断收益是否真实 |
| 来源覆盖率 | 研究类任务的质量代理指标 |

**评测方法**

- 建一个 10–20 条的研究型 golden set（问题 + 期望结论 + 关键来源）。
- 跑三组：单 Agent、父 + 1 个子 Agent、父 + 并行 2–3 个子 Agent。
- 只有「质量提升 ≥ 阈值」且「成本/时延在可接受范围」时才默认开启。
- 不要用「子 Agent 自己说完成了」作为成功标准；用父答案 + 来源 + 人工/LLM judge。

### 5.9 安全边界

- **工具白名单**：子 Agent 只能拿到 spec 列出的工具。研究子 Agent 默认只读（`WebSearch`/`WebFetch`/`Read`/`Glob`/`Grep`/`search_memories`），不能 `Write`/`Edit`/`Bash`。
- **权限继承**：子 Agent 的 `user_id` 必须与父一致，memory 工具作用域不能扩大；不要给子 Agent 新的凭据。
- **不可信输入**：子 Agent 从网页抓到的内容，以及它返回的结果，都是不可信数据；父模型不能因为子结果里的指令而调用高权限工具。结果包装要明确标为「子 Agent 输出，不是用户指令」。
- **递归与预算**：深度、调用次数、token 预算三重限制；默认禁止子 Agent 再派子 Agent。
- **写操作**：P1 不提供可写的子 Agent。需要写代码/文件时，先只在父 Agent 里做，或为 `code_agent` 单独设计审批与沙箱。
- **审计**：每次委派记录 task、子 Agent 名称、模型、工具、usage、stop_reason；不记录完整子对话（避免把网页内容写进父 trace），需要时由子 run/span 承载。

### 5.10 兼容与回滚

- 总开关 `AGENT_SUBAGENTS_ENABLED=false` 时，`build_runtime()` 不注册任何子 Agent 工具，行为与现在完全一致。
- P1 不改表结构，不改 `agent_run_events` 的 CHECK，不改现有 SSE 事件；只新增 span 类型实例和 usage 来源。
- 如果评测显示收益不足，关掉开关即可，不需要回滚数据。
- SDK 的 `subagents` 参数默认空，独立使用者不受影响。

---

## 6. 实施计划

### Phase 0：基线与开关（0.5–1 天）

- 选 10–20 条研究型任务，记录当前单 Agent 的 token、时延、答案质量。
- 加 `AGENT_SUBAGENTS_ENABLED`（默认 false）和配置读取。
- 验收：开关关闭时所有现有测试通过，行为无变化。

### Phase 1：SDK 子 Agent 原语（3–5 天）

文件：

- `agent-loop/src/bokaclaw_agent/agent/subagent.py`（新增）：`SubagentSpec`、`SubagentTool`、结果裁剪/校验。
- `agent-loop/src/bokaclaw_agent/core/types.py`：`EventType` 增加 `subagent_started` / `subagent_event` / `subagent_finished`；`ToolResult` 增加可选 `usage`、`metadata`。
- `agent-loop/src/bokaclaw_agent/core/config.py`：`AgentConfig` 增加子 Agent 相关配置（或单独的 `SubagentOptions`）。
- `agent-loop/src/bokaclaw_agent/agent/runtime.py`：注册子 Agent 工具；把父 emitter 注入 `SubagentTool`；工具执行后合并 `ToolResult.usage`；传递 `subagent_depth`。
- `agent-loop/src/bokaclaw_agent/__init__.py`：导出新类型。
- `agent-loop/README.md`：公共 API 变更说明。
- `agent-loop/tests/test_subagent.py`（新增）：见下。

测试：

- 子 Agent 拿到全新上下文（不包含父历史）。
- 工具白名单生效；未列出的工具不可见。
- 深度/调用次数/超时/token 预算超限时返回错误而不是崩溃。
- 父取消传播到子 Agent；子 Agent 失败不默认让父失败。
- 结果截断、JSON schema 校验失败、usage 合并。
- 事件顺序与 `subagent_*` payload。
- 串行执行时多个子 Agent 的结果按顺序回填。

验收：`cd agent-loop && hatch run test && hatch run lint`。

### Phase 2：服务端接入（2–3 天）

文件：

- `server/app/agent/subagents.py`（新增）：specs、prompt、schema、工厂。
- `server/app/agent/runtime.py`：`build_runtime()` 注册工具；`_bridge_event()` 映射 `subagent_*`。
- `server/app/agent/events.py`：`AgentSubagentStarted` / `AgentSubagentFinished` 等 dataclass。
- `server/app/agent/spans.py`：子 agent span、子工具 span 的 parent 关系、id 前缀。
- `server/app/agent/usage.py`：`SOURCES` 增加 `"subagents"`。
- `server/app/agent/store.py` / 配置：环境变量读取。
- `dashboard/src/types/api.ts`：usage sources 加可选 `subagents`。

验收：

- 一次真实研究任务产生嵌套 span：根 agent → research_agent 工具 → 子 agent → WebSearch/WebFetch。
- `done` 的 `usage.sources.subagents` 非零，且父 `agent_loop` 与子 token 分开。
- 取消父 run 后子 Agent 在检查点停止，父 run 状态为 `cancelled`。
- 关闭开关时，事件、span、usage 与改造前一致。

### Phase 3：Dashboard（2–3 天）

- `tool-trace.tsx`：子 Agent 工具用不同图标/标签，JSON 输出美化，显示子 Agent 的 usage 与 stop_reason。
- 可选：`subagent-trace.tsx`，展开后按嵌套层级渲染子工具。
- `message-bubble.tsx`：在助手消息的 usage tooltip 中显示「Subagents: X tokens」。
- `pnpm typecheck && pnpm build`。

### Phase 4：P2（按需）

- `Tool.parallel_safe` + 并行工具执行 + `_emit` 锁。
- `agent_runs` 子 run：新增迁移（参考 `009_create_agent_run_spans.py`、`011_add_agent_run_token_usage.py` 的写法），加 `parent_run_id`、`root_run_id`、`depth`、`agent_name`；把 `uq_agent_runs_active_conversation` 改成只约束 `parent_run_id IS NULL AND status IN ('queued','running')`；`serialize_active_run`、`_recover_run`、会话删除保护都过滤子 run。
- 子 run 事件回放与 Dashboard 实时嵌套视图。
- 级联取消与子 run 恢复。

### Phase 5：外部 Agent（按需）

- 在 `server/` 边界增加 A2A adapter，或 MCP client。
- 内部仍然使用 `SubagentTool` 接口；远程 Agent 实现同一个 `SubagentSpec` 协议。
- 只有在出现「跨团队/跨厂商/跨信任边界」的真实需求时再做。

---

## 7. 风险与缓解

| 风险 | 表现 | 缓解 |
| --- | --- | --- |
| 成本失控 | 15x token 是业界常见量级 | 默认关闭；调用次数/token 预算/模型分层；先做 A/B |
| 过度委派 | 父把简单问题也丢给子 Agent | 写清 description 的「不要用」条件；调用次数上限；评测监控 |
| 协作失败 | 子结果偏离任务、重复步骤、不终止 | 任务 packet 模板；子 Agent 步数上限；输出 schema；父级验证 |
| 结果太啰嗦 | 子 Agent 回传长文，父上下文照样爆炸 | `max_result_chars` 硬截断；结构化 schema；大产物落文件 |
| 递归爆炸 | 子 Agent 再派子 Agent，指数增长 | 默认禁止；`max_depth`；子工具白名单移除委派工具 |
| 取消不彻底 | 父停了，子还在跑 | 父 cancel_event 传播；子 Agent 超时 watchdog；P2 级联取消 |
| 调试困难 | 答案错了不知道是父还是子 | 嵌套 span + 每子 Agent usage/stop_reason；保留 task 与 result |
| 安全边界扩大 | 子 Agent 拿到写权限或跨用户记忆 | 只读工具白名单；user_id 继承；结果标记为不可信 |
| 评测失真 | 只看子 Agent 自述「完成」 | 用父答案 + 来源 + judge；对比单 Agent 基线 |

**明确不做（P1）：**

- handoff（子 Agent 直接面向用户）
- 自由对话式 manager / group chat
- 无限递归
- 可写的研究子 Agent
- 跨进程/跨组织的 A2A
- 子 Agent 的长期记忆（复用父 run 的 research memory 即可）

---

## 8. 一页决策表

| 问题 | 结论 |
| --- | --- |
| 自建 A2A？ | **否**。A2A 是跨信任边界协议；内部子 Agent 用同进程工具调用。 |
| 直接用 LangGraph / CrewAI / OpenAI Agents SDK / MAF？ | **否**。它们自带循环与状态，会和现有 SDK + server 生命周期重复；只借鉴模式。 |
| 怎么做？ | 在 `bokaclaw-agent` 增加 `SubagentSpec` + `SubagentTool`，子 Agent 作为普通工具；server 用 span + usage 记录；P2 再引入子 run。 |
| 第一版做多少？ | 串行、只读研究子 Agent、深度 2、每 run 最多 4 次、120s 超时、8k 结果、零 DB 迁移。 |
| 什么时候再考虑 A2A？ | 需要调用外部团队/厂商的 Agent，或把 BokaClaw Agent 暴露给外部时；在 `server/` 边界加 adapter。 |

---

## 9. 参考资料

- Anthropic, *How we built our multi-agent research system*：<https://www.anthropic.com/research/multi-agent-research-system>
- Claude Code, *Create custom subagents*：<https://code.claude.com/docs/en/sub-agents>
- Claude Agent SDK, *Subagents*：<https://code.claude.com/docs/en/agent-sdk/subagents>
- OpenAI, *Orchestration and handoffs*：<https://developers.openai.com/api/docs/guides/agents/orchestration>
- OpenAI Agents SDK, *Agent orchestration*：<https://github.com/openai/openai-agents-python/blob/main/docs/multi_agent.md>
- LangChain, *Migrate from langgraph-supervisor*：<https://docs.langchain.com/oss/python/migrate/langgraph-supervisor>
- Microsoft, *Introducing Microsoft Agent Framework*：<https://devblogs.microsoft.com/foundry/introducing-microsoft-agent-framework-the-open-source-engine-for-agentic-ai-apps/>
- Google, *Announcing the Agent2Agent Protocol (A2A)*：<https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/>
- Cemri et al., *Why Do Multi-Agent LLM Systems Fail?*（MAST）：<https://arxiv.org/abs/2503.13657>
- *Why CrewAI's Manager-Worker Architecture Fails*：<https://towardsdatascience.com/why-crewais-manager-worker-architecture-fails-and-how-to-fix-it/>
- *2026 Comparative Analysis: Agent Delegation and Handoff Infrastructure*：<https://appliedtechnologyindex.com/research/2026-comparative-analysis-agent-delegation-handoff-infrastructure/>
