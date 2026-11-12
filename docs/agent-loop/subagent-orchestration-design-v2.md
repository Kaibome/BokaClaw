# BokaClaw 子 Agent 调度能力设计 v2

> 状态：调研 + 方案设计；SDK P1、server P1 与 Dashboard P1 已实现（2026-11-06）
>
> 日期：2026-11-06
>
> 适用范围：`agent-loop/`（`bokaclaw-agent`）、`server/`、`dashboard/`
>
> 上一版：[`subagent-orchestration-design.md`](subagent-orchestration-design.md)（保留为初版调研）
>
> 本版依据：业界调研 + [`subagent-call-walkthrough.md`](subagent-call-walkthrough.md) 中一次**真实子 Agent 调用**的完整解剖

---

## 1. 结论

**不自己写 A2A，也不整体引入 LangGraph / CrewAI / OpenAI Agents SDK / Microsoft Agent Framework。**

在现有 `bokaclaw-agent` 上增加一层**「子 Agent 调用协议」**，而不是一个多 Agent 框架：

```text
父 Agent（主循环，拥有最终答案）
  └── explore(task) / research(task)      ← 普通 Tool
        ├── 子 Agent 独立上下文（系统提示 + 工具白名单）
        ├── 只回传结构化结果（facts / inferences / unknowns / sources）
        └── 父 Agent 抽查核验后再综合
```

v2 与初版最大的区别：**把子 Agent 调用的输入和输出都协议化**。

- 输入不是一句自由文本 `task`，而是 `goal / scope / background / questions / output_format / constraints` 六段式任务包；由 SDK 渲染成固定结构的 prompt。
- 输出不是一段散文，而是带证据的结构化信封：`facts[]`（每条带 evidence）、`inferences[]`、`unknowns[]`、`sources[]`、`usage`、`subagent_id`。
- 工具权限不是提示词里的「请不要修改」，而是**工具级白名单**（`read_only` 策略下根本没有 Write/Edit/Bash）。
- 父 Agent 不是「收到就信」，而是有一个显式的**核验步骤**：抽查高影响事实，区分观察到的事实与推断。

这四点是那次真实调用里最有价值的经验，也是 v1 设计没有充分表达的。

分阶段：

| 阶段 | 内容 | DB 迁移 |
| --- | --- | --- |
| P1（MVP） | 同步子 Agent、六段式任务包、结构化结果、工具策略、in-run resume、核验提示、嵌套 span、usage 分桶 | **不需要** |
| P2 | 并行、后台运行、子 run 一等公民、跨 run resume、实时嵌套视图、可选 verifier | 需要 |
| P3 | 远程 Agent / 第三方 Agent（此时才考虑 A2A） | 需要 |

### 实现状态（2026-11-06，SDK P1）

已在 `agent-loop/` 落地，`tests/test_subagent.py` 覆盖：

- `SubagentSpec` / `SubagentTask` / `SubagentResult` / `SubagentOptions` / `SubagentTool`；
- 六段式任务包渲染、独立 `ContextManager`、结构化结果信封；
- `read_only` / `research` / `code` / `custom` 四种工具策略（在子 `ToolRegistry` 层过滤）；
- 同 run 内 resume（`subagent_id` + 子 transcript）、调用次数与 token 预算（所有子 Agent 工具共享）、超时、父级取消传播；
- 子 usage 合并进父 `RunResult.usage`，并记录到 `ToolResult.usage` / `subagent_finished`；
- 委派台账 + `verification_policy` 的父级核验提示（`spot_check` / `strict` 只改提示，不另派 verifier）。

与设计的差异，按 P2 处理：

- `run_in_background`、并行、嵌套委派均被拒绝（`max_depth=2` 只保留深度上限，子 Runtime 不注册子 Agent 工具）；
- `strict` 暂不派 verifier 子 Agent；
- 跨 run resume 未实现；子 run 不写 DB；
- dashboard 嵌套视图属于下一阶段。

### 实现状态（2026-11-06，server P1）

已在 `server/` 落地，零 DB 迁移：

- `app/agent/subagents.py` 注册 `explore` / `plan` / `research_agent` 三个 spec，并实现 scope 校验：工作区外的绝对路径默认拒绝，`AGENT_SUBAGENT_ALLOW_ABSOLUTE_PATHS` 可放开；非 http/https URL 一律拒绝；`plan` 与 `explore` 同为只读，但返回 `steps` / `files` / `tradeoffs` / `risks` 的结构化方案；
- `LocalAgentConfig` 增加 10 个 `AGENT_SUBAGENT_*` 配置项，默认关闭，关闭时行为与之前完全一致；开启时把委派指引追加到系统提示；
- `_bridge_event()` 映射三种 SDK 事件：`subagent_started` / `subagent_event` / `subagent_finished`；
- `SpanRecorder` 生成 `tool:<parent_call>` → `subagent:<id>` → `sublm:*` / `subtool:*` 的嵌套 span，`subagent_finished` 收尾并回填 turns / usage / isolated_chars / result_chars；
- `RunUsageMeter.SOURCES` 增加 `subagents`；即使 `AGENT_SPAN_CAPTURE=false`，子 Agent 的 usage 仍然上报；
- 取消沿用父 run 的 `cancel_event`；SSE 与 `agent_run_events` 契约不变，委派仍表现为一次普通工具调用。

未做（按设计留给 P2）：`AGENT_SUBAGENT_MAX_CONCURRENT`（并行尚未实现）、子 run 一等公民、dashboard 嵌套实时视图。

### 实现状态（2026-11-06，Dashboard P1）

已在 `dashboard/` 落地：

- `components/agent/subagent-trace.tsx`：按 `SubagentResult` 信封形状识别委派，渲染 REPORT / FACTS 表格 / INFERENCES / UNKNOWNS / SOURCES 与 `turns · isolated_chars · result_chars · tokens`；运行中或截断的结果回退为原始 JSON；
- `components/agent/tool-trace.tsx`：解析成功时分发到 `SubagentTraceRow`，普通工具行为不变；
- `components/agent/message-bubble.tsx`：usage tooltip 按来源显示 `Agent loop / Compression / Memory LLM / Memory embedding / Subagents` 的 token 总量；
- `server/app/routers/traces.py` 增加 `roots_only`，Dashboard 的 run 列表传 `true`，避免 `subagent:<id>` 这类嵌套 `agent` span 被当成独立 run（见 P-21）。
- Trace 页支持多级 span 下钻：打开 Child spans 时一次取回整个 run 的 span，详情 Dialog 以路径栈驱动，可连续点开 `tool → subagent → sublms`，Back 出栈并命中详情缓存（见 P-22）。实时嵌套视图仍属 P2。

---

## 2. 从真实调用解剖中学到的 10 件事

`subagent-call-walkthrough.md` 记录了一次 Explore 子 Agent 的完整调用。把它逐阶段映射到设计约束：

| # | 观察 | 设计要求 |
| --- | --- | --- |
| 1 | **是否委派本身就是一次决策**：目录有 15 个文件、需要通读、结论小，才派子 Agent；「查价格」这种一次闭合的任务不派 | 父提示词要有明确的委派/不委派判据；每次 run 有调用次数上限 |
| 2 | **prompt 是五段式**：角色约束、目标+绝对路径、背景（显式声明「你看不到对话历史」）、编号问题、返回格式 | 输入协议化；SDK 负责渲染，而不是让父模型每次自由发挥 |
| 3 | **子 Agent 只有自己的系统提示 + 这一条用户消息**，没有父历史 | 子 Runtime 全新 `ContextManager`；父历史、父工具结果一律不传 |
| 4 | **只读是工具级硬约束**：Explore 的工具列表里没有 Write/Edit | 工具策略（policy）在 `ToolRegistry` 层过滤；提示词只做补充说明 |
| 5 | **调用参数不只是 prompt**：还有 `subagent_type`、`description`、`max_turns`、`run_in_background`、`resume`、`name` | 任务包要包含 label、类型、步数预算、超时、是否后台、是否续接 |
| 6 | **输出末尾有 Agent ID**，可用于后续 resume | 每次调用返回 `subagent_id`；同一父 run 内支持续接；跨 run 续接需要持久化（P2） |
| 7 | **中间步骤全部留在子上下文**，只有最终消息回到父级 | 子 Agent 用 `run()` 而不是把子 delta 转发给父；父上下文只增加一条 tool result |
| 8 | **子 Agent 的报告是「自称完成」，不是「已核实」**：父级用三次 Bash 抽查后才采信 | 结果信封区分 facts/inferences/unknowns，并带 evidence；父提示词要求抽查 |
| 9 | **综合有原则**：转述关键结论不粘贴原文、保留增量发现、区分事实与推断 | 父提示词给出综合规则；结果里保留 evidence 供父级压缩转述 |
| 10 | **成本是不对称的**：约 600 字 prompt + 800 字结论，换掉 15 次工具输出（上万 token）的隔离 | 记录 `isolated_chars` / `noise_ratio`；只在噪声比足够高时委派 |

这 10 条里，第 2、4、8 条是 v1 最欠缺的：v1 只说了「传 task/context」，没有规定**怎么传**、**怎么保证只读**、**怎么让父级验证**。v2 把这三件事变成 SDK 的默认行为。

---

## 3. 业界现状（保留结论）

### 3.1 五种拓扑

| 模式 | 控制权 | 上下文 | 代表 | BokaClaw 是否采用 |
| --- | --- | --- | --- | --- |
| 上下文隔离子 Agent | 父拥有最终答案 | 子全新上下文，只回摘要 | Claude Code Task、Anthropic orchestrator-worker | **采用** |
| Agent-as-Tool | 父调用专家并合成 | 子可独立上下文 | OpenAI `agent.as_tool()`、LangChain subagents | **采用**（同一实现） |
| Handoff | 交给专家，专家直接回答用户 | 保留历史、切换指令 | OpenAI handoffs、Swarm、MAF | 不采用 |
| 图/工作流 | 确定性代码控制 | 状态图 | LangGraph、MAF Workflow、CrewAI Flows | 不采用（未来可作为外层编排） |
| A2A | 独立部署的 Agent 对等协作 | task/artifact | Google A2A、Azure Foundry、Bedrock AgentCore | 仅跨边界时采用 |

### 3.2 关键事实

- Anthropic 的 orchestrator-worker 在内部 research eval 上比单 Agent 高约 **90.2%**，token 约 **15 倍**；约 **80% 的方差可由 token 用量解释**。主 Agent 同步等待子 Agent 是官方承认的瓶颈。
- Claude Code 的 subagent：独立上下文、工具白名单、不继承父历史、较新版本默认最大嵌套深度 3、支持并行与结构化输出。
- OpenAI 明确建议**不要过早拆分**；需要重试/验证循环时用 agents-as-tools，而不是 handoff。
- LangGraph 的 `langgraph-supervisor` 已不再积极维护，官方迁移方向是「主 Agent 把专家包成 `@tool`」。
- CrewAI 的 hierarchical process 在生产中被批评：manager 往往不能选择性委派、实际接近串行、delegation ping-pong、成本接近翻倍。
- MAST（1600+ 条失败轨迹）：**79% 的多 Agent 失败是规格与协作问题**，不是模型问题；高频模式是步骤重复、推理行动不一致、不知道何时终止。
- A2A 是跨组织协议（Agent Card、任务生命周期、鉴权、SSE/推送），与 MCP 互补；在单进程内部用它只会增加网络、身份、发现、兼容性、分布式状态五类成本。

---

## 4. BokaClaw 现状与约束

### 4.1 已有能力

| 层 | 现状 | 位置 |
| --- | --- | --- |
| SDK 主循环 | 同步 ReAct，`run()` / `stream()` / `arun()`；工具**串行** | `agent-loop/src/bokaclaw_agent/agent/runtime.py:244` |
| 工具 | `@tool` 从类型注解与 docstring 生成 schema；`ToolRegistry` 按名分发 | `agent-loop/src/bokaclaw_agent/tools/registry.py:151` |
| 上下文 | system + 历史 + 本轮；压缩、fact ledger、step notice、progress guard | `agent-loop/src/bokaclaw_agent/agent/context.py`、`compression.py` |
| 服务端执行 | 每个 run 新建 Runtime；`AgentRunManager` 拥有排队/领取/取消/恢复 | `server/app/agent/runner.py:418` |
| 事件 | run 内单调 `seq`；`agent_run_events` 持久化并可重放 | `server/app/models.py:194` |
| Trace | `agent_run_spans` 支持 `parent_span_id` 和 `span_type="agent"`；`AGENT_SPAN_CAPTURE` 默认 true | `server/app/models.py:218`、`server/app/agent/spans.py:551` |
| 评测 | span 树 → deepeval；`agent` 节点已有 `agent_handoffs` 字段（当前为空） | `server/app/eval/adapter.py:110` |

### 4.2 关键约束

1. **SDK 不拥有 run 生命周期**：子 Agent 不能自己建 `agent_runs`；它只能发事件，由 server 决定怎么记录。
2. **工具串行**：P1 直接复用串行路径；并行需要改 `_execute()` 的工具执行段（`runtime.py:378-436`）。
3. **`agent_runs` 有「一个会话同时只能有一个活跃 run」的部分唯一索引**（`server/app/models.py:133-138`）。子 run 直接塞进 `agent_runs` 必须先改这个索引。
4. **`agent_run_events.event_type` 有 CHECK 约束**（`server/app/models.py:198-202`）。P1 不新增持久化事件类型。
5. **`agent_run_spans` 已支持嵌套**，是 P1 最划算的可观测性通道，零迁移。
6. **usage 来源是固定元组**（`server/app/agent/usage.py:19`）。子 Agent 的 token 必须单独成桶。
7. **服务端在 daemon 线程里跑 SDK**，整轮有 `max_seconds` 墙钟上限（默认 300s），进程级 run 并发默认 2。P1 子 Agent 不额外占用 run 并发，但消耗父轮墙钟；P2 子 run 会竞争信号量。
8. **`agent_id` 只是透传元数据**，不选择提示词/模型/工具。子 Agent 路由由工具 `description` 决定，spec 静态注册。
9. **开发工具受 `AGENT_WORKSPACE_ROOT` 约束**（`server/app/agent/dev_tools.py` 的 `_resolve()`）。子 Agent 的 `scope` 路径必须走同一套校验，不能因为「子 Agent 是只读的」就绕过工作区边界。
10. **WebFetch 已有 SSRF 防护**（拒绝私有/回环地址）。子 Agent 复用该工具，不新开网络入口。

---

## 5. 方案选型

### 5.1 为什么不自己写 A2A

| 维度 | A2A 需要 | BokaClaw 内部子 Agent 需要 |
| --- | --- | --- |
| 发现 | Agent Card、注册中心 | 编译期/配置期已知 |
| 传输 | HTTP + JSON-RPC + SSE | 同进程函数调用 |
| 身份 | OAuth/mTLS、卡片签名 | 继承当前用户与 run |
| 任务状态 | submitted/working/completed/failed… | 父工具调用成功/失败 |
| artifact | 跨 Agent 文件/数据契约 | 一个结构化 JSON |
| 故障模型 | 网络分区、重试幂等、取消竞态 | 异常 + 超时 + cancel_event |

A2A 的每一项都是真实成本，内部场景一项都用不上。**只有当子 Agent 是另一个团队部署、另一套技术栈、需要正式任务契约时，才在 `server/` 边界加 A2A adapter。**

### 5.2 为什么不直接用开源框架

| 方案 | 能带来什么 | 与 BokaClaw 的冲突 | 结论 |
| --- | --- | --- | --- |
| LangGraph | 图编排、checkpoint、supervisor、并行 | 取代现有 ReAct 主循环与状态模型；checkpointer 与 run/事件表重复 | 不采用，只借鉴模式 |
| OpenAI Agents SDK | handoff、agents-as-tools、tracing | 自带循环、session、provider 抽象；形成第二套执行栈 | 不采用 |
| CrewAI | role/crew、Flow | hierarchical 委派不稳定；提示词/权限模型不匹配 | 不采用 |
| Microsoft Agent Framework | Workflow、Handoff、GroupChat、OTel | 新框架、in-process only、生态绑定 | 不采用 |
| PydanticAI | 类型化 Agent、依赖注入 | 与 `bokaclaw-agent` 功能重叠 | 不采用 |
| **自建薄层** | 复用现有循环、工具、事件、span、usage、评测 | 需自己实现深度/并发/超时/递归保护 | **采用** |

判断标准不是「框架好不好」，而是「引入后 BokaClaw 还剩多少自己的代码」。现有 SDK 已经把 ReAct、流式、压缩、持久化、取消、事件做完并有测试；再引入一个框架，等于替换最有价值的部分。

### 5.3 这次设计更像「协议」，不是「框架」

真实调用解剖说明：子 Agent 的价值不来自复杂的编排算法，而来自四个契约：

1. **任务契约**：父必须把子任务写成自包含的任务包。
2. **上下文契约**：子只拿任务包 + 自己的系统提示，不拿父历史。
3. **权限契约**：子只拿到策略允许的工具，只读是硬约束。
4. **结果契约**：子只回传结构化结论 + 证据，不回传中间过程。

把这四个契约固化成 SDK 的默认行为，就是一个约 800–1200 行的薄层；不需要框架，也不需要协议栈。

---

## 6. 目标设计

### 6.1 总体结构

```text
┌──────────────────────────────────────────────────────────────┐
│ server AgentRunManager（父 run）                              │
│  build_runtime()                                             │
│    ├── memory tools / dev tools                             │
│    └── SubagentTool("explore") / SubagentTool("research")    │
└──────────────────────────────┬───────────────────────────────┘
                               │ tool_call(arguments=SubagentTask)
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ SubagentTool.invoke()                                        │
│  ├── 校验 scope（工作区 / URL）、深度、调用次数、token 预算    │
│  ├── 渲染六段式 prompt                                        │
│  ├── 按 tool_policy 过滤工具（只读 = 没有 Write/Edit/Bash）    │
│  ├── 新 AgentRuntime（全新 ContextManager，无父历史）          │
│  ├── run(prompt, cancel_event=child_cancel)                  │
│  ├── 结果规范化：facts / inferences / unknowns / sources      │
│  ├── usage / isolated_chars 合并到父 run                      │
│  ├── 嵌套 agent span + 子工具 span                            │
│  └── 返回结构化 ToolResult（含 subagent_id，可 resume）        │
└──────────────────────────────┬───────────────────────────────┘
                               │ 只有结构化结论回到父上下文
                               ▼
                    父 Agent 抽查 evidence → 综合最终答案
```

### 6.2 SDK 公共 API

新增 `agent-loop/src/bokaclaw_agent/agent/subagent.py`。

#### 6.2.1 `SubagentSpec`：类型级定义

```python
class SubagentSpec(BaseModel):
    name: str                             # 工具名，如 "explore" / "research_agent"
    description: str                      # 给父模型看的「何时用 / 何时不用」
    system_prompt: str                    # 子 Agent 自己的系统提示（角色 + 工具规范）
    tool_policy: Literal["read_only", "research", "code", "custom"] = "read_only"
    tool_names: tuple[str, ...] | None = None   # 仅 custom 使用
    model: ModelConfig | None = None      # None = 继承父模型
    default_max_turns: int = 8
    default_timeout_seconds: float = 120.0
    max_result_chars: int = 8_000
    output_schema: dict[str, Any] | None = None
    allow_nested: bool = False
    resumable: bool = True
```

#### 6.2.2 `SubagentTask`：调用级任务包

这是 v2 的核心。父模型不是只传一句 `task`，而是填六个字段：

```python
class SubagentTask(BaseModel):
    label: str                            # 短标签（对应 walkthrough 的 description），<= 60 字符，用于 trace/UI
    goal: str                             # 一句话目标
    scope: list[str] = []                 # 绝对路径 / URL / 数据源；服务端校验
    background: str = ""                  # 自包含背景；渲染时会加「你看不到对话历史」
    questions: list[str] = []             # 编号问题 = 完成标准
    output_format: str = ""               # 表格 / 要点 / JSON schema
    constraints: list[str] = []           # 只读、不要建议、只报事实、引用来源
    max_turns: int | None = None          # 覆盖 spec 默认值
    timeout_seconds: float | None = None
    run_in_background: bool = False       # P1 拒绝；P2 支持
    resume: str | None = None             # 上一次调用的 subagent_id
```

SDK 把它渲染成固定结构的 prompt（与 walkthrough 的五段式一一对应）：

```text
你是 {role}。{constraints}

目标：{goal}
范围：{scope}

背景（你看不到任何对话历史，以下是全部信息）：
{background}

请回答以下问题：
1. ...
2. ...

返回格式：
{output_format}

要求：
- 事实与推断分开。每条事实附证据（文件路径:行号 / URL / 命令输出节选）。
- 无法证实的内容写入 unknowns，不要编造。
- 不要给建议，只报告事实。
```

这样做的好处：

- **父模型不需要每次手工构造长 prompt**，降低漏掉背景/格式的概率。
- **渲染是确定性的**，可测试、可回放、可评测。
- **任务包本身可以进 trace**，调试时能看到父到底给了子什么。

**实现注意：** `SubagentTool.as_tool()` 暴露给模型的是**扁平参数**（`label`、`goal`、`scope`、`background`、`questions`、`output_format`、`constraints`、`max_turns`、`timeout_seconds`、`resume`），而不是一个嵌套的 `SubagentTask` JSON Schema。多数模型 provider 对 `$ref` / `$defs` 的支持不一致；扁平参数更稳，也更容易在 trace 和 Dashboard 里逐字段展示。`SubagentTool` 内部再把参数组装成 `SubagentTask` 并做校验。

#### 6.2.3 `SubagentResult`：结构化结果信封

```python
class Fact(BaseModel):
    claim: str
    evidence: str                        # path:line / URL / 命令输出节选
    confidence: Literal["high", "medium", "low"] = "medium"


class SubagentResult(BaseModel):
    subagent_id: str
    subagent_type: str
    status: Literal["completed", "partial", "failed", "timeout", "cancelled"]
    report: str | None                   # 散文摘要，受 max_result_chars 限制
    data: dict | None                    # output_schema 校验后的 JSON
    facts: list[Fact] = []
    inferences: list[str] = []
    unknowns: list[str] = []
    sources: list[str] = []
    usage: dict[str, int] = {}
    turns: int = 0
    isolated_chars: int = 0              # 子工具输出总字符数（被隔离的噪声量）
    stop_reason: str | None = None
    duration_ms: int = 0
    resumable: bool = False
```

`ToolResult.content` 是 `SubagentResult.model_dump_json()` 的截断版。父上下文只增加这一条，不增加子 Agent 的任何中间消息。

**为什么要有 facts / inferences / unknowns：**

walkthrough 的第 8 条经验是「子 Agent 的报告是自称完成，不是已核实」。如果结果只有一段散文，父级只能整段相信或整段不信。拆成三层后：

- `facts` 带 evidence，父级可以低成本抽查（`Read path:line` / `WebFetch url`）。
- `inferences` 明确标注为推断，父级合成时不会当成事实转述。
- `unknowns` 暴露边界，父级可以决定补查或如实告诉用户。

#### 6.2.4 `SubagentTool`

**路由形态：一类型一工具，还是单一 Agent 工具？** P1 采用**一类型一工具**（`explore`、`research_agent` 各自是一个 `Tool`）：每个 spec 的 description 独立，父模型按工具名路由，额度/策略也按 spec 配置，trace 里一眼能看出派了哪种。walkthrough 里的单一 `Agent` 工具 + `subagent_type` 参数在类型很多、工具列表预算紧张时更省，但需要一个 enum 维护路由表。两者可以共存：如果类型超过 4–5 个，再加一个统一的 `delegate(subagent_type=...)` 入口，内部仍落到同一个 `SubagentTool`。

```python
class SubagentTool:
    def __init__(
        self,
        spec: SubagentSpec,
        *,
        parent_config: AgentConfig,
        parent_tools: ToolRegistry,
        depth: int = 0,
        max_depth: int = 2,
        emit: Callable[[AgentEvent], None] | None = None,
        provider_factory: Callable[[ModelConfig], ModelProvider] | None = None,
    ) -> None: ...

    def as_tool(self) -> Tool: ...
```

`AgentRuntime` 新增：

```python
AgentRuntime(
    config,
    tools=...,
    provider=...,
    store=...,
    on_event=...,
    subagents: Iterable[SubagentSpec] = (),
    subagent_depth: int = 0,
    subagent_options: SubagentOptions | None = None,
)
```

```python
class SubagentOptions(BaseModel):
    enabled: bool = False
    max_depth: int = 2
    max_concurrent: int = 3                # P2
    max_calls_per_run: int = 4
    max_result_chars: int = 8_000
    default_timeout_seconds: float = 120.0
    token_budget: int | None = None
    verification_policy: Literal["none", "spot_check", "strict"] = "spot_check"
```

### 6.3 工具策略：只读是硬约束

| 策略 | 允许的工具 | 用途 |
| --- | --- | --- |
| `read_only` | `Read`、`Glob`、`Grep`、`WebFetch`、`WebSearch`、`search_memories` | Explore / 研究 / 代码阅读 |
| `research` | `read_only` + `save_memory`（可选） | 需要沉淀结论的调研 |
| `code` | `read_only` + `Write`、`Edit`、`Bash`、`Monitor`（仅当父已启用） | 代码修改（P2 再开放） |
| `custom` | `tool_names` 显式列表 | 特殊场景 |

实现方式：`SubagentTool` 在构造子 `AgentRuntime` 时，只把策略允许的 `Tool` 实例放进子 `ToolRegistry`。**不在提示词里依赖「请不要修改」**。

**`mode` 不暴露为调用参数。** walkthrough 里的 `mode` 是调用级可选参数；在 BokaClaw 里，读写模式由 spec 的 `tool_policy` 静态决定，父模型不能通过参数提权。否则「只读」就退化成了提示词约束。

补充：

- `read_only` **不包含 `Bash`**。walkthrough 里的 `ls -la` 在 BokaClaw 用 `Glob`/`Read` 完成；需要 shell 的探索型任务用 `research` 或显式 `custom`，并继承父的 `AGENT_TOOLS_ALLOW_SHELL` 限制。
- 子 Agent 的 `user_id` 必须与父一致；`search_memories` / `save_memory` 的作用域不能扩大。
- `scope` 里的绝对路径必须通过 `_resolve()` 的工作区校验；URL 必须通过 WebFetch 的 SSRF 校验。**只读不等于可以越界读。**

### 6.4 结果规范化

子 Agent 的最终文本需要被转换成 `SubagentResult`。三种模式：

1. **`output_schema` 提供时**：要求子 Agent 输出 JSON，父侧用 Pydantic/JSON Schema 校验；校验失败时尝试一次「只输出 JSON」的修复请求，仍失败则返回 `status="partial"` + 原文。
2. **研究类 spec 内置 schema**：`summary / findings[{claim,evidence,source}] / sources / open_questions / confidence` 映射到 `report / facts / sources / unknowns`。
3. **无 schema 时**：`report` = 最终文本；`facts`/`inferences`/`unknowns` 为空。P1 至少内置第 2 种，避免父级拿到散文。

截断策略：`report` 按 `max_result_chars` 头尾截断；`facts` 最多保留 N 条（默认 20），按 confidence 排序；`sources` 去重并限制条数。截断信息写入 `SubagentResult` 的 `truncated` 字段（可选）。

### 6.5 委派决策与提示词

**父 Agent 的编排提示**（由 `build_runtime` 追加）：

```text
你可以把「中间过程噪声大、结论小」的独立子任务委派给子 Agent。
适合委派：
- 需要通读多个文件/网页，最终只要一份结论；
- 任务可以自包含地描述，不依赖当前对话的隐含上下文；
- 只读探索、调研、对照检查。
不适合委派：
- 一次工具调用就能回答的问题；
- 需要当前对话大量隐含上下文的改写/决策；
- 需要写文件、改代码、执行有副作用的命令（当前子 Agent 不提供）。
每次委派都要写清楚：目标、范围、背景、要回答的问题、输出格式。
子 Agent 看不到当前对话，背景必须显式写进任务包。
最多 4 次委派；能自己快速回答的不要委派。
子 Agent 返回后：
1. 先看 facts 的 evidence，至少抽查一条高影响事实；
2. 区分它的事实与推断，不要把推断当事实转述；
3. 把关键结论压缩进最终答案，不要粘贴子 Agent 原文。
```

**子 Agent 的系统提示**（`explore` 示例）：

```text
你是只读探索子 Agent。你只能读取，不能修改、创建或删除任何文件。
你只看到任务包，看不到父对话。
规则：
1. 先规划再行动；优先用 Glob/Grep 定位，再 Read 具体文件。
2. 不要重复读取同一文件；证据足够时停止。
3. 不要向用户提问，不要输出过程性思考，只输出最终结果。
4. 每条事实附证据：文件路径:行号，或 URL，或命令输出节选。
5. 推断单独列出，不要混进事实。
6. 无法证实的内容写入 unknowns。
7. 按任务包要求的格式输出，总长度不超过 6000 字符。
```

**委派台账（delegation ledger）**：在父 run 的模型视图里追加一个只读块，记录已派发的 `label / goal / status / 一句话结果`，防止父模型重复派同一个任务。与现有 fact ledger 一样，只改视图、不写会话。

### 6.6 核验

`verification_policy`：

| 值 | 行为 |
| --- | --- |
| `none` | 父直接采信；只建议用于低风险的信息收集 |
| `spot_check`（默认） | 父提示词要求抽查至少一条高影响 fact；抽查用的工具调用出现在 trace 里 |
| `strict` | 在 `spot_check` 基础上，额外派一个 `verifier` 子 Agent 对照 evidence 审查；成本更高，P2 再开放 |

核验不是「再信一个 Agent」，而是**父级用低成本工具直接验证 evidence**。walkthrough 里父级用三次 `ls`/`head` 抽查，就是 `spot_check` 的原型。

结果信封里的 `evidence` 字段必须可执行地验证：

- 文件事实 → `path:line` 或可 grep 的文本；
- 网页事实 → URL + 原文节选；
- 命令事实 → 命令 + 关键输出。

如果子 Agent 给不出 evidence，父级应把该条降级为 `inference` 或 `unknown`。

### 6.7 续接（resume）

walkthrough 的第 6 条：子 Agent 返回 Agent ID，之后可以 resume，历史不丢。

P1（同一父 run 内）：

- `SubagentTool` 在父 run 内维护 `dict[subagent_id, ChildSession]`；`ChildSession` 保存子 Agent 的 `new_messages`、spec、usage。
- 父模型用 `resume=subagent_id` 再次调用时，新建子 Runtime 并载入这些消息，继续对话。
- `subagent_id` 格式：`sub_<uuid4hex>`；写入 `SubagentResult`，也写入 span attributes。
- 父 run 结束后内存释放；`SubagentResult.resumable` 标记为 `false`（P1 不跨 run）。

P2（跨 run）：

- 子 Agent 变成独立 `agent_run` 或写入 `bokaclaw_agent_sessions`；`subagent_id` 持久化，`resume` 可在后续用户轮次使用。
- Dashboard 提供「继续这个子 Agent」入口（可选）。

### 6.8 执行、并发与限制

**P1：串行。**

- `SubagentTool.invoke()` 在父 ReAct 的工具执行段同步调用子 `runtime.run()`。
- 多个子 Agent 调用按模型返回顺序串行；实现最简单，行为与现有工具一致。
- 已能拿到上下文隔离、工具策略、结构化结果、核验、usage 归因的主要收益。

**P2：并行。**

- `Tool` 增加 `parallel_safe: bool = False`；子 Agent 工具设为 `True`。
- `_execute()` 检测当前 assistant 消息里的工具调用：如果**全部**是 `parallel_safe`，用 `ThreadPoolExecutor(max_workers=max_concurrent)` 并发执行，并按原始顺序回填 `tool` 消息。
- 混入非并行安全工具时整批退回串行。
- 并发执行要求 `_emit()` 的 `_event_seq` 加锁；每个子 Agent 独立 Runtime，不共享可变状态。

**P2：后台运行。**

- `run_in_background=true` 时，`SubagentTool` 启动子线程并立即返回 `subagent_id` + `status="running"`；父 Agent 可以继续其他工作。
- 子 Agent 完成时发出 `subagent_finished` 事件，并在父 run 的下一次模型请求里追加一条 runtime-context 通知（只改视图）。
- 父 run 结束前必须等待或取消所有后台子 Agent；否则标记为 `interrupted`。
- P1 收到 `run_in_background=true` 时返回明确错误：`background subagents are not enabled`。

**限制：**

| 限制 | 默认 | 行为 |
| --- | --- | --- |
| `max_depth` | 2 | 超过时子工具集不注册子 Agent 工具；父调用返回错误 |
| `max_calls_per_run` | 4 | 超过后返回 `{"error": "subagent call budget exhausted"}` |
| `timeout_seconds` | 120 | `threading.Timer` 设置子 `cancel_event`；子 Agent 在检查点停止 |
| `max_turns` | 8 | 子 Agent 自己的 ReAct 步数上限；达到后走「无工具收尾」 |
| `max_result_chars` | 8000 | 截断后回传 |
| `token_budget` | 可空 | 父 run 累计子 Agent token 超限后拒绝新委派 |
| 递归 | 默认禁止 | 子工具白名单里去掉所有 `SubagentTool` |
| 墙钟 | 父轮 `max_seconds` | 子超时必须小于父轮剩余时间，至少留一次父模型调用的余量 |

**取消语义：**

- 父 `cancel_event` 必须传播到所有子 Agent。子 Agent 的取消事件是组合对象：父事件 **或** 本地超时事件任一置位即取消。
- 父取消时，子 Agent 在下一个检查点停止，`SubagentTool` 返回 `status="cancelled"` 的 `SubagentResult`，父的取消流程继续。
- 子 Agent 失败/超时**不应**默认让父 run 失败。子 Agent 工具强制使用 `return` 错误语义（即使父配置 `tool_error_mode="raise"`），把结构化错误回给模型，让父决定重试、换方向或直接回答。
- P2 有子 run 后，父 run 取消要级联取消子 run；子 run 取消不反向影响父。

### 6.9 成本模型

walkthrough 的成本视角值得固化：

```text
委派收益 ≈ 被隔离的中间输出 token - (任务包 prompt + 子结果 + 子 Agent 的模型开销)
```

实现上记录三个量：

- `isolated_chars`：子 Agent 所有工具输出的字符数总和；
- `result_chars`：回传父上下文的字符数；
- `noise_ratio ≈ isolated_chars / max(result_chars, 1)`。

建议：

- `noise_ratio < 3` 的任务不值得委派（父自己读更快/更便宜）；
- 父提示词里写入这个判据，但不要硬编码成运行时拒绝（模型有上下文判断）。
- Dashboard 在子 Agent 卡片上显示 `turns / isolated_chars / result_chars / tokens`，让用户直观看到「这次委派省了什么、花了什么」。

---

## 7. 服务端集成（P1，零迁移）

### 7.1 注册

新增 `server/app/agent/subagents.py`：

```python
def build_subagent_specs(sdk, config, payload) -> list:
    if not config.subagents_enabled:
        return []
    specs = [
        sdk.SubagentSpec(
            name="explore",
            description=(
                "Read-only exploration of workspace files. Use when a task needs "
                "reading many files but only a small conclusion is needed. "
                "Do not use for single-file reads or edits."
            ),
            system_prompt=EXPLORE_SYSTEM_PROMPT,
            tool_policy="read_only",
            default_max_turns=8,
            default_timeout_seconds=90,
        ),
        sdk.SubagentSpec(
            name="research_agent",
            description=(
                "Delegate an independent web-research question. Use when the "
                "question needs many searches/fetches and would flood the main "
                "context. Do not use for a single known URL or a trivial lookup."
            ),
            system_prompt=RESEARCH_SYSTEM_PROMPT,
            tool_policy="read_only",
            model=config.subagent_model_config,
            default_max_turns=config.subagent_max_turns,
            default_timeout_seconds=config.subagent_timeout_seconds,
            output_schema=RESEARCH_RESULT_SCHEMA,
        ),
    ]
    return specs
```

`build_runtime()`（`server/app/agent/runtime.py:688`）把 spec 交给 Runtime，由 Runtime 统一构造 `SubagentTool` 并注册到工具表（这样 `SubagentTool` 能拿到父 `ToolRegistry`、父 emitter 和 depth）：

```python
tools = [
    *_memory_tools(sdk, payload.get("user_id")),
    *build_dev_tools(sdk),
]

runtime = sdk.AgentRuntime(
    config,
    tools=tools,
    provider=provider,
    on_event=on_event,
    subagents=build_subagent_specs(sdk, config, payload),
    subagent_options=sdk.SubagentOptions(
        enabled=config.subagents_enabled,
        max_depth=config.subagent_max_depth,
        max_calls_per_run=config.subagent_max_calls_per_run,
        max_result_chars=config.subagent_max_result_chars,
        default_timeout_seconds=config.subagent_timeout_seconds,
        token_budget=config.subagent_token_budget,
        verification_policy=config.subagent_verification,
    ),
)
```

注意 `agent_id` 不是子 Agent 选择器；spec 静态注册，父模型通过工具 `description` 路由。

### 7.2 事件桥接

SDK 新增三种事件：

| SDK 事件 | 服务端内部事件 | 去向 |
| --- | --- | --- |
| `subagent_started` | `AgentSubagentStarted` | span：`agent` span，`parent_span_id` = 对应工具 span；payload 含 label、type、goal_chars |
| `subagent_event` | `AgentSubagentEvent` | span：子工具/子 LLM span（P1 只记 start/end，子工具可省略） |
| `subagent_finished` | `AgentSubagentFinished` | span 收尾；`AgentUsageUpdate(source="subagents")`；结果摘要 |

`_bridge_event()`（`server/app/agent/runtime.py:498`）映射。**P1 不新增 `agent_run_events` 类型**：父 run 对前端仍然只产生 `tool.call` / `tool.result`；委派显示为一个名为 `explore` / `plan` / `research_agent` 的工具调用。

### 7.3 Span 结构

复用 `parent_span_id`，零迁移：

```text
agent (root)
├── llm.turn.1
├── tool:call_xxx (explore)
│   └── agent (subagent:explore, subagent_id=sub_...)
│       ├── llm.turn.1
│       ├── tool:call_yyy (Glob)
│       ├── tool:call_zzz (Read)
│       └── llm.turn.2
└── llm.turn.3
```

`SpanRecorder` 需要：

- `self.subagent_spans: dict[str, dict]`，用 `subagent_id` 映射；
- 子工具 span id 加前缀，如 `subtool:<subagent_id>:<tool_call_id>`，避免与父工具 id 碰撞；
- 子 LLM span id 加前缀，如 `sublm:<subagent_id>:<seq>`；
- 子 span 的 `parent_span_id` 指向子 agent span；
- span `attributes` 至少包含：`subagent_type`、`depth`、`model`、`tool_names`、`goal_chars`、`result_chars`、`isolated_chars`、`turns`、`stop_reason`、`usage`。

如果 `AGENT_SPAN_CAPTURE=false`，P1 仍然有 `tool.call`/`tool.result` 的摘要，但没有嵌套树；这是现有开关的预期行为。

### 7.4 Usage

- `SOURCES` 增加 `"subagents"`（`server/app/agent/usage.py:19`）。
- `AgentUsageUpdate(source="subagents")` 在子 Agent 结束时上报；`calls` / `unknown_calls` 从子 `RunResult.usage` 推导。
- `done` payload、助手消息 `metadata.usage`、`GET /agent/runs/{id}` 自动带上 `sources.subagents`；Dashboard 类型加可选字段。
- 子 Agent 的 `isolated_chars` / `result_chars` 不进 usage 快照，放进 `SubagentResult` 和 span attributes；Dashboard 从 tool result 读取。

### 7.5 取消与恢复

- `AgentRunManager` 的 `cancel_event` 原样传给父 Runtime；`SubagentTool` 把它作为父侧取消源。
- 父 run 被服务重启恢复时，子 Agent 随父一起中断；P1 没有子 run，不需要单独恢复。
- 子 Agent 的 best-effort 持久化：P1 不写会话，只写 span；失败不影响父 run。
- P2 子 run 需要级联取消与恢复，见第 10 节。

### 7.6 配置

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `AGENT_SUBAGENTS_ENABLED` | `false` | 总开关；关闭时与现在完全一致 |
| `AGENT_SUBAGENT_MODEL` | 空 | 子 Agent 模型；空表示继承主模型 |
| `AGENT_SUBAGENT_MAX_DEPTH` | `2` | 最大嵌套深度 |
| `AGENT_SUBAGENT_MAX_CONCURRENT` | `3` | P2 并发上限 |
| `AGENT_SUBAGENT_MAX_CALLS_PER_RUN` | `4` | 每个父 run 的委派次数上限 |
| `AGENT_SUBAGENT_TIMEOUT_SECONDS` | `120` | 单个子 Agent 超时 |
| `AGENT_SUBAGENT_MAX_TURNS` | `8` | 子 Agent 最大 ReAct 步数 |
| `AGENT_SUBAGENT_MAX_RESULT_CHARS` | `8000` | 回传父上下文的最大字符数 |
| `AGENT_SUBAGENT_TOKEN_BUDGET` | 空 | 父 run 的子 Agent token 总预算 |
| `AGENT_SUBAGENT_VERIFICATION` | `spot_check` | `none` / `spot_check` / `strict` |
| `AGENT_SUBAGENT_ALLOW_ABSOLUTE_PATHS` | `false` | 是否允许 `scope` 使用工作区外的绝对路径 |

---

## 8. Dashboard

### P1：不改前端也能用，但建议做一个小卡片

- 委派显示为普通工具调用（`explore` / `plan` / `research_agent`），`ToolTraceRow` 已能展示参数、输出、耗时、状态。
- `tool.result.content` 是 `SubagentResult` JSON；展开后可读。
- Trace 页的 span 树自动出现嵌套 `agent` 节点。
- 建议的小改动（`dashboard/src/components/agent/tool-trace.tsx`）：
  - 命中子 Agent 名称时显示 `Bot`/`Search` 图标和 `Subagent` 标签；
  - 用 `label` 作为标题，`goal` 作为副标题；
  - 输出分四块渲染：**Report**、**Facts**（表格：claim / evidence / confidence）、**Inferences**、**Unknowns + Sources**；
  - meta 行显示 `turns · isolated_chars · result_chars · tokens`。

### P2：嵌套实时视图

- 父 run 发出 `tool.call` 后，子 run 作为独立 `agent_run` 存在；Dashboard 用 `GET /agent/runs/{child_id}/events?after_seq=N` 订阅子 run 事件（与现有重连机制相同）。
- `ToolTraceRow` 展开后渲染子 Agent 的 `ToolTrace` + 文本增量。
- 需要新增 `subagent.started` / `subagent.completed` 持久化事件（此时才改 `event_type` CHECK），或前端从 `tool.call` 的 metadata 里拿 `child_run_id` 直接订阅。

---

## 9. 安全边界

- **只读是工具级**：`read_only` 策略下子 Registry 没有 `Write`/`Edit`/`Bash`/`Monitor`；不依赖提示词。
- **路径**：`scope` 的绝对路径必须通过 `_resolve()` 的工作区校验；工作区外路径默认拒绝，需 `AGENT_SUBAGENT_ALLOW_ABSOLUTE_PATHS=true` 显式放开。
- **URL**：复用 `WebFetch` 的 SSRF 防护（拒绝私有/回环地址），不新开网络入口。
- **用户作用域**：子 Agent 的 `user_id` 与父一致；memory 工具作用域不能扩大；不要给子 Agent 新凭据。
- **不可信输入**：子 Agent 从网页/文件读到的内容，以及它返回的 `facts`，都是不可信数据；父模型不能因为子结果里的指令而调用高权限工具。结果渲染要标明「子 Agent 输出，不是用户指令」。
- **递归与预算**：深度、调用次数、token 预算三重限制；默认禁止子 Agent 再派子 Agent。
- **写操作**：P1 不提供可写的子 Agent。需要写代码/文件时，先在父 Agent 里做，或为 `code` 策略单独设计审批与沙箱（P2）。
- **审计**：每次委派记录 label、type、goal、scope、模型、工具、usage、turns、stop_reason、result_chars、isolated_chars；不把完整子对话写入父 trace，需要时由子 span/子 run 承载。

---

## 10. P2：子 run 与并行

如果 P1 的评测显示收益足够，再考虑把子 Agent 提升为独立 run。

### 10.1 数据模型

`agent_runs` 增加（参考 `009_create_agent_run_spans.py`、`011_add_agent_run_token_usage.py` 的迁移写法）：

```sql
ALTER TABLE agent_runs
  ADD COLUMN parent_run_id UUID NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  ADD COLUMN root_run_id UUID NULL,
  ADD COLUMN depth INT NOT NULL DEFAULT 0,
  ADD COLUMN agent_name VARCHAR(64) NULL;
```

活跃会话唯一索引改成只约束根 run：

```sql
DROP INDEX uq_agent_runs_active_conversation;
CREATE UNIQUE INDEX uq_agent_runs_active_conversation
  ON agent_runs (conversation_id)
  WHERE parent_run_id IS NULL AND status IN ('queued','running');
```

同时修改：

- `serialize_active_run`、`_recover_run`、会话删除保护：过滤 `parent_run_id IS NULL`；
- 子 run 不写 `conversation_messages`，`assistant_message_id` 为 null；
- `created_by` / `conversation_id` 继承父 run；
- `agent_run_events` 复用现有类型；子 run 有自己的 `seq`；
- 父 run 取消时级联设置子 run 的 cancel；
- 父 run 重启恢复时，子 run 标记 `interrupted`，不单独续跑（除非实现子 run 恢复）。

### 10.2 并发预算

- 子 run 不应和用户发起的 run 抢全局 `AGENT_RUN_CONCURRENCY`。给父 run 一个子配额（如 3），在父 run 内用 `asyncio.Semaphore` 控制。
- 子 run 的墙钟计入父 run 的 `max_seconds`；父 run 超时先取消子 run，再收尾。
- 并行子 Agent 的结果仍按原始 tool call 顺序回填父上下文。

### 10.3 跨 run resume

- 子 run 的 `subagent_id` 持久化在 `agent_runs`/`agent_run_events` 的 metadata 中。
- 父 run 结束后，用户可以在后续轮次 resume 某个子 Agent；需要把子 run 的最终上下文（或 `conversation_messages` 中的子会话）加载回新的子 Runtime。
- 如果不做跨 run，`SubagentResult.resumable=false`，UI 不显示「继续」入口。

---

## 11. 评测

### 11.1 指标

| 指标 | 用途 |
| --- | --- |
| 委派次数 / run | 判断是否过度委派 |
| 子 Agent token 占比 | 成本归因 |
| 子 Agent 成功率 / 超时率 | 稳定性 |
| 父 run 总 token、总时延 | 与单 Agent 基线对比 |
| 最终答案质量（现有 eval） | 判断收益是否真实 |
| 来源覆盖率 | 研究类任务的质量代理指标 |
| `noise_ratio` | 判断这次委派是否划算 |
| `facts` 被父级引用的比例 | 判断子结果是否真的有用 |
| 抽查/核验次数 | 判断父级是否在验证而非盲信 |

### 11.2 方法

- 建 10–20 条 golden set：问题、期望结论、关键来源/文件。
- 跑三组：单 Agent、父 + 1 个子 Agent、父 + 并行 2–3 个子 Agent。
- 只有「质量提升 ≥ 阈值」且「成本/时延可接受」时才默认开启。
- 不以「子 Agent 自述完成」为成功标准；以父答案 + 来源 + judge 为准。
- 专门测一条：子 Agent 的中间工具输出**不得**出现在父 run 的 `model_request` 消息里（上下文隔离回归测试）。

---

## 12. 实施计划

### Phase 0：基线与开关（0.5–1 天）

- 选 10–20 条研究/探索型任务，记录单 Agent 的 token、时延、答案质量。
- 加 `AGENT_SUBAGENTS_ENABLED`（默认 false）和配置读取。
- 验收：开关关闭时所有现有测试通过，行为无变化。

### Phase 1：SDK 子 Agent 原语（4–6 天）

文件：

- `agent-loop/src/bokaclaw_agent/agent/subagent.py`（新增）：`SubagentSpec`、`SubagentTask`、`SubagentResult`、`SubagentTool`、prompt 渲染、工具策略、结果规范化、in-run resume。
- `agent-loop/src/bokaclaw_agent/core/types.py`：`EventType` 增加 `subagent_started` / `subagent_event` / `subagent_finished`；`ToolResult` 增加可选 `usage`、`metadata`。
- `agent-loop/src/bokaclaw_agent/core/config.py`：`SubagentOptions`；`AgentConfig` 增加子 Agent 相关字段或引用。
- `agent-loop/src/bokaclaw_agent/agent/runtime.py`：注册子 Agent 工具；注入父 emitter；工具执行后合并 `ToolResult.usage`；传递 `subagent_depth`；追加 delegation ledger。
- `agent-loop/src/bokaclaw_agent/__init__.py`：导出新类型。
- `agent-loop/README.md`：公共 API 变更说明。
- `agent-loop/tests/test_subagent.py`（新增）。

测试：

- 子 Agent 拿到全新上下文，父历史/父工具结果不在子 `model_request` 里。
- 工具策略生效：`read_only` 子 Registry 没有 Write/Edit/Bash。
- `SubagentTask` 渲染出的 prompt 包含五段式结构。
- 结果规范化：facts/inferences/unknowns/sources 正确映射；schema 校验失败降级为 partial。
- 深度/调用次数/超时/token 预算超限时返回结构化错误而不是崩溃。
- 父取消传播到子 Agent；子失败不默认让父失败。
- 结果截断、usage 合并、`isolated_chars` 统计。
- `subagent_id` 在同一父 run 内可 resume；父 run 结束后 `resumable=false`。
- 事件顺序与 `subagent_*` payload。

验收：`cd agent-loop && hatch run test && hatch run lint`。

### Phase 2：服务端接入（3–4 天）

文件：

- `server/app/agent/subagents.py`（新增）：specs、prompts、schema、工厂。
- `server/app/agent/runtime.py`：`build_runtime()` 注册工具；`_bridge_event()` 映射 `subagent_*`；配置读取。
- `server/app/agent/events.py`：`AgentSubagentStarted` / `AgentSubagentFinished` 等 dataclass。
- `server/app/agent/spans.py`：子 agent span、子工具/子 LLM span 的 parent 关系、id 前缀。
- `server/app/agent/usage.py`：`SOURCES` 增加 `"subagents"`。
- `dashboard/src/types/api.ts`：usage sources 加可选 `subagents`；tool result 的 `SubagentResult` 类型。

验收：

- 一次真实探索任务产生嵌套 span：根 agent → explore 工具 → 子 agent → Glob/Read。
- `done` 的 `usage.sources.subagents` 非零，父 `agent_loop` 与子 token 分开。
- 取消父 run 后子 Agent 在检查点停止，父 run 状态为 `cancelled`。
- 关闭开关时，事件、span、usage 与改造前一致。
- 子 Agent 的中间工具输出不出现在父 `model_request` 里。

### Phase 3：Dashboard（2–3 天）

- `tool-trace.tsx`：子 Agent 卡片（label/goal/type/turns/isolated_chars/tokens）。
- Facts 表格 + Inferences + Unknowns + Sources。
- `message-bubble.tsx`：usage tooltip 显示「Subagents: X tokens」。
- `pnpm typecheck && pnpm build`。

### Phase 4：P2（按需）

- `Tool.parallel_safe` + 并行工具执行 + `_emit` 锁。
- 子 run 数据模型、级联取消、跨 run resume。
- 后台运行与完成通知。
- 实时嵌套视图。
- 可选 `verifier`。

### Phase 5：外部 Agent（按需）

- 在 `server/` 边界增加 A2A adapter 或 MCP client。
- 内部仍使用 `SubagentTool` 接口；远程 Agent 实现同一个 spec 协议。
- 只在出现「跨团队/跨厂商/跨信任边界」的真实需求时再做。

---

## 13. 风险与缓解

| 风险 | 表现 | 缓解 |
| --- | --- | --- |
| 成本失控 | 15x token 是业界常见量级 | 默认关闭；调用次数/token 预算/模型分层；先做 A/B |
| 过度委派 | 父把简单问题也丢给子 Agent | description 写清「不要用」条件；调用次数上限；提示词写入 `noise_ratio` 判据 |
| 任务包写不好 | 子 Agent 缺背景，返回跑偏 | 六段式模板 + 渲染器；父提示词要求显式背景；测试 prompt 结构 |
| 协作失败 | 子结果偏离任务、重复步骤、不终止 | 步数上限；输出 schema；父级核验 |
| 结果太啰嗦 | 子 Agent 回传长文，父上下文照样爆炸 | `max_result_chars` 硬截断；结构化 schema；大产物落文件 |
| 盲信子结果 | 父把推断当事实转述 | facts/inferences/unknowns 分离；evidence 字段；`spot_check` 策略 |
| 递归爆炸 | 子 Agent 再派子 Agent | 默认禁止；`max_depth`；子工具白名单移除委派工具 |
| 取消不彻底 | 父停了，子还在跑 | 父 cancel_event 传播；子超时 watchdog；P2 级联取消 |
| 越界读取 | 子 Agent 读到工作区外文件 | `scope` 走 `_resolve()`；默认拒绝工作区外绝对路径 |
| 调试困难 | 答案错了不知道是父还是子 | 嵌套 span + 每子 Agent usage/stop_reason；保留 task 与 result |
| 评测失真 | 只看子 Agent 自述「完成」 | 父答案 + 来源 + judge；对比单 Agent 基线 |

**明确不做（P1）：**

- handoff（子 Agent 直接面向用户）
- 自由对话式 manager / group chat
- 无限递归
- 可写的子 Agent
- 跨进程/跨组织的 A2A
- 子 Agent 的长期记忆（复用父 run 的 research memory 即可）
- 跨 run resume（P2）

---

## 14. 一页决策表

| 问题 | 结论 |
| --- | --- |
| 自建 A2A？ | **否**。A2A 是跨信任边界协议；内部子 Agent 用同进程工具调用。 |
| 直接用 LangGraph / CrewAI / OpenAI Agents SDK / MAF？ | **否**。它们自带循环与状态，会和现有 SDK + server 生命周期重复；只借鉴模式。 |
| 怎么做？ | 在 `bokaclaw-agent` 增加 `SubagentSpec` + `SubagentTask` + `SubagentResult` + `SubagentTool`；子 Agent 作为普通工具；server 用 span + usage 记录。 |
| 第一版做多少？ | 同步、只读 explore/research 两类、六段式任务包、结构化结果、工具策略、in-run resume、spot_check 核验、深度 2、每 run 最多 4 次、120s 超时、8k 结果、零 DB 迁移。 |
| 什么时候再考虑 A2A？ | 需要调用外部团队/厂商的 Agent，或把 BokaClaw Agent 暴露给外部时；在 `server/` 边界加 adapter。 |
| 什么时候再考虑并行/子 run？ | P1 评测证明质量收益 ≥ 阈值，且需要更低时延或跨 run 续接时。 |

---

## 15. 参考资料

- 内部：[一次子 Agent 调用的完整解剖](subagent-call-walkthrough.md)
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
