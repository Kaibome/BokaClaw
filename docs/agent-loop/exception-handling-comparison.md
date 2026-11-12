# BokaClaw、claw-code 与 Hermes 异常处理对比

> 状态：对比分析（对照当前文档版本）
>
> 对比对象：
> - [`exception-handling.md`](exception-handling.md)：BokaClaw
> - [`exception-handling-claw-code.md`](exception-handling-claw-code.md)：claw-code
> - [`agent-loop-error-handling.md`](agent-loop-error-handling.md)：Hermes
>
> 目标读者：需要判断三套 runtime 差异、做架构选型或迁移的开发者

本文比较三套实现处理五类异常的方式：上下文压缩失败、模型调用失败、工具调用失败、子 Agent 调用失败、ReAct 循环超时。

## 0. 对比前提

三篇源文档的边界不同：

- **BokaClaw**：Python SDK + FastAPI server + Postgres，异常处理覆盖 SDK 和 durable run 两层。
- **claw-code**：Rust CLI + 本地 runtime，异常处理主要围绕一次 CLI 轮次和进程生命周期。
- **Hermes**：Python `agent/conversation_loop.py` + `turn_*.py` 阶段模块，异常处理围绕一次 ReAct 回合的结构化恢复、响应校验和收尾。

因此本文比较的是“异常处理取向”，不是简单的“谁更健壮”。

## 1. 总体结论

- **BokaClaw 偏“失败后的系统状态”**：异常会落成 run 状态、错误码、事件和 span，部分输出会持久化，支持取消、超时和重启恢复。
- **claw-code 偏“单次请求的高韧性”**：模型调用重试更激进，请求前预检更完整，上下文超限会自动压缩并重试。
- **Hermes 偏“分层恢复与结构化解释”**：先做错误分类，再做一次性修复、重试、fallback、自动恢复阶梯和响应校验；每一层都有上限，最终一定收敛成带 `turn_exit_reason`、`failure_reason` 和可重试性标记的结果。
- 三者共同点：**工具失败基本都是软失败**，错误会交还给模型；但工具超时、熔断和长任务回收的完整度差异很大。

一句话概括：

```text
BokaClaw：这次跑不完也要把状态、部分结果和失败原因留下来。
claw-code：尽量把这次请求救回来，救不回来就退出。
Hermes：先分类、再分层修复、最后给出可解释的终止结果。
```

## 2. 核心对比

| 维度 | BokaClaw | claw-code | Hermes |
|---|---|---|---|
| 运行形态 | Python SDK + FastAPI + Postgres，后台 run manager | Rust CLI + 本地 runtime，单进程 REPL | Python ReAct 主循环 + turn 阶段模块 |
| 错误生命周期 | 异常 → run 状态/错误码 → SSE/HTTP → 持久化 | 异常 → 当前轮失败 → stderr/JSON/非零退出 | 异常不逃逸 → 结构化失败回合 + 退出原因/可重试性 |
| 失败轮持久化 | 有部分输出或工具轨迹就写 `failed` assistant 消息 | **失败轮次不写盘**，只保留启动时创建的会话文件 | 收尾步骤独立守护，已流文本保留；落库失败告警并重试 |
| 错误分类 | `agent_*` 错误码 + HTTP 状态码 | failure class + 可重试性 + 结构化 JSON | 最细：鉴权/计费/限流/服务端/请求形状/策略/provider 特例/未知 |
| 模型重试 | OpenAI SDK 默认 **2 次**，单次请求 60s | **8 次重试/9 次尝试** | 默认 **3 次**，另有一次性修复链、fallback、自动恢复阶梯 |
| 退避策略 | 依赖 SDK 默认 | 指数退避 1–128s，带抖动，429 优先 `Retry-After` | 约 2–60s 抖动；`Retry-After` 优先；限流用自适应退避 |
| 请求前预检 | 基本没有上下文窗口/请求体大小预检 | 凭证、上下文窗口、请求体大小、OAuth 均有预检 | 预检压缩、压缩锁/冷却、消息修复、Ollama 上下文地板、hook 否决 |
| 模型 fallback | 当前没有跨模型 fallback 链 | 子 Agent 有跨模型 fallback；主 CLI 没有 | 有 fallback 链，按错误类型决定切换时机 |
| 自动恢复阶梯 | 无 | 无 | 瞬时故障最多 5 个周期：15/30/60/60/60 秒 |
| 上下文超限恢复 | 没有“压缩后重试原请求”的恢复链 | 最多 4 轮渐进压缩，保留 4→2→1→0，再重建 runtime 重试 | 预检压缩 + 溢出绕过冷却；压缩超时会退款本次调用并结束回合 |
| 压缩实现 | **调用压缩模型**生成滚动摘要，失败降级截断 | **本地规则摘要**，不调用模型；Trident 三阶段转换 | 摘要压缩 + 确定性兜底摘要 |
| 压缩失败表现 | fail-open，主 run 继续；`fallback=true` | 压不干净则报错；压缩后健康探针失败则当前轮失败 | 兜底摘要或直接中止；冷却 60/300/900s；每回合最多 3 次 |
| 压缩持久化/回滚 | 只改模型视图，不写回历史 | 压缩后替换内存会话并写盘；写盘失败会报错，无回滚 | 压缩是允许改写历史的操作；有锁/冷却，强调缓存安全 |
| 工具失败 | 统一转 `is_error` tool result，run 继续 | 统一转 `is_error` tool result，主循环继续 | 统一转 tool error；未知工具连续 3 次才终止为 partial |
| 工具异常隔离 | Python 异常在 registry 统一捕获 | 主循环不隔离 panic；子 Agent 线程有 `catch_unwind` | handler 异常转 tool error；错误文本清洗后回填 |
| 工具超时 | 各工具自行控制：Bash 120s、MCP 30s、Skill 120s、Subagent 120s | 只有 Bash 在调用方显式传 `timeout` 时生效；其他工具无统一超时 | 并发批/单次调用各有截止时间；超时中断工作线程；交互式工具豁免 |
| 子 Agent 模式 | **同步**：父 Agent 等待子结果，拿到结构化 `SubagentResult` | **异步**：`Agent` 工具立即返回 running，父级轮询文件/manifest | 文档未覆盖子 Agent（本页范围为主循环与 turn 模块） |
| 子 Agent 失败感知 | 父 Agent 直接收到 `failed/timeout/cancelled` tool result | 父级**不会收到通知**，只能读输出文件或 manifest | 文档未覆盖 |
| 子 Agent 超时 | 默认 120s，定时器设置子 cancel_event | **无墙钟超时、无 watchdog**，卡住会一直 running | 文档未覆盖 |
| 子 Agent 轮数 | 有 `max_turns`，父级还有调用次数/token 预算 | 固定最多 32 轮 | 文档未覆盖 |
| ReAct 墙钟/存活 | 有：后台 1800s、legacy 300s，`agent_timeout` | **没有**墙钟超时 | 回合级 liveness watchdog：600s 无活动强制中止 |
| ReAct 迭代上限 | 有；当前 150，SDK 默认 10 | 主 CLI 默认无限；子 Agent 32 轮 | 有迭代上限，接近上限提前收口（文档未给默认值） |
| 无进展/重复检测 | `ProgressGuard`，可 nudge/stop | 文档明确“没有自动熔断” | 检测流式/结束时长片段重复；空响应有固定恢复阶梯 |
| 取消/中断 | `cancelled` / `interrupted` 是正式 run 状态，补写 pending tool result | 中断是正常返回，`TurnSummary.interrupted=true`，持久化会话 | 等待可被中断；剩余工具补 cancelled 结果 |
| 可观测性 | `agent_run_events`、run 状态、trace/span、HTTP 错误码 | telemetry、manifest、输出文件、stderr/JSON | `turn_exit_reason`、`failure_reason`、可重试性、telemetry |
| 权限/护栏 | MCP/Skill 审批，文件围栏 + executor 沙箱 | 独立 permission 层 + PreToolUse/PostToolUse hook | 审批/护栏拒绝转 tool result；硬护栏受控中止 |
| 失败分类 | `agent_upstream_error`、`agent_timeout`、`agent_no_progress` 等 | failure class + 可重试性分类 | failure class + 可重试性 + 恢复建议 |

## 3. 六个本质区别

### 3.1 错误是“进程错误”“run 状态”还是“结构化回合结果”

**BokaClaw**：

- 异常被转换成 `failed` / `cancelled` / `interrupted` 的 run 状态；
- 写入 `error_code`、`error_detail`；
- 前端通过 SSE 订阅；
- 重启后还能做恢复。

**claw-code**：

- 一次失败最终表现为当前轮失败、错误输出、非零退出码；
- 失败轮次不写盘；
- 进程退出后没有可恢复的 run。

**Hermes**：

- 设计原则是“异常不逃逸”；
- 每层异常都被转成错误响应、tool 结果或失败回合；
- 最终一定带 `turn_exit_reason`、`failure_reason` 和可重试性；
- CLI、网关、TUI/Desktop 各自渲染下一步建议。

三者的关注点分别是：BokaClaw 的**状态一致性**、claw-code 的**进程退出语义**、Hermes 的**可解释恢复**。

### 3.2 模型层的韧性：Hermes 最完整，claw-code 次之，BokaClaw 最简洁

**Hermes** 的恢复栈最长：

1. 请求前预检；
2. 错误分类；
3. 一次性修复链（凭证池、计费、推理配置、图片降级、OAuth beta 等）；
4. 通用重试（默认 3 次，抖动退避）；
5. fallback 切换；
6. 自动恢复阶梯（5 个周期）；
7. 响应校验与空响应/截断恢复。

**claw-code** 侧重：

- 8 次重试、指数退避、`Retry-After`；
- 凭证、上下文窗口、请求体大小、OAuth 预检；
- 上下文超限时最多 4 轮渐进压缩后重试；
- 流式阶段 10 秒无首包重发一次；
- 主 CLI 没有 fallback，子 Agent 有跨模型 fallback。

**BokaClaw** 最简洁：

- 主要依赖 OpenAI SDK 的 2 次重试；
- 没有上下文超限恢复链；
- 没有请求体预检；
- 没有模型 fallback 链；
- 优势在失败后 run 状态清楚、部分输出不丢、后续可恢复。

Hermes 的独特之处是**响应校验层**：非法/空响应、截断续写（最多 4 次）、空内容固定恢复阶梯、重复退化检测、只有推理无回答、内容过滤中止等，都有独立的恢复路径。

### 3.3 压缩哲学：三种不同取舍

**BokaClaw**：

- 压缩依赖模型摘要；
- 失败 fail-open，直接退化为截断；
- 主 run 继续；
- 代价是压缩失败原因不可观测，且同一批消息不会自动重试。

**claw-code**：

- 压缩是本地确定性的，不调模型；
- 失败主要是“压不干净”；
- 用 4 轮渐进压缩 + 健康探针 + 重试原请求解决；
- 代价是压缩后可能写盘失败且没有回滚。

**Hermes**：

- 摘要失败默认提交确定性兜底摘要，可配置为直接中止；
- 压缩超时会退款本次 API 调用并结束回合，不发送必然超限的请求；
- 有压缩锁/冷却（60/300/900s）、无效压缩阻断、每回合最多 3 次；
- provider 实证的上下文溢出可以绕过冷却一次。

一个是“**压不下去也要继续跑**”（BokaClaw），一个是“**保证最终能压下去**”（claw-code），一个是“**压缩本身也要有预算、冷却和退款语义**”（Hermes）。

### 3.4 工具失败：三者都软失败，但 Hermes 的截止时间最完整

**共同点**：

- 未知工具、参数错误、handler 异常都转成 tool result；
- 主循环不会因为单个工具失败直接退出；
- 模型可以看到错误并自行纠正。

**差异**：

- **BokaClaw**：registry 统一捕获 Python 异常；Bash/MCP/Skill/Subagent 有各自的超时；run 超时不强杀工具线程。
- **claw-code**：主循环不隔离 panic；子 Agent 线程有 `catch_unwind`；只有 Bash 在显式传 `timeout` 时才有超时；没有自动熔断。
- **Hermes**：并发批/单次调用各有截止时间，超时会中断工作线程；交互式工具豁免；未知工具连续 3 次才终止为 partial；参数 JSON 非法重试 3 次后注入恢复用 tool 结果；工具结果过大时截断并溢出到文件。

Hermes 还额外处理了“执行前落库失败不执行工具”和“执行后落库失败不基于内存结果继续推理”，这是 BokaClaw 和 claw-code 文档中没有展开的边界。

### 3.5 子 Agent：BokaClaw 同步可控，claw-code 异步自治，Hermes 文档未覆盖

**BokaClaw**：

- 子 Agent 是父 Agent 的一个同步工具调用；
- 父 Agent 等待结果，拿到 `completed/partial/failed/timeout` 的结构化 envelope；
- 失败被隔离，父 Agent 可以继续决策；
- 有超时、轮数、调用次数、token 预算。

**claw-code**：

- 子 Agent 是后台线程，`Agent` 工具立即返回 `running`；
- 父级不会收到失败通知，只能轮询文件/manifest；
- 没有墙钟超时和 watchdog，卡住后会一直 `running`；
- 但子 Agent 有跨模型 fallback。

**Hermes**：

- 当前 `agent-loop-error-handling.md` 的范围是主循环和 `turn_*` 阶段模块，**没有描述子 Agent 机制**；
- 因此本项不做三方对比。

BokaClaw 要“父子协作可控”，claw-code 要“后台任务尽快返回、不阻塞主 CLI”。

### 3.6 ReAct 终止：墙钟、存活检测与无限循环

**BokaClaw**：

- 有完整的终止矩阵：墙钟超时、最大迭代、无进展保护、用户取消、服务关闭、重启恢复；
- 失败会被分成 `agent_timeout`、`agent_upstream_error`、`agent_no_progress`、`agent_cancelled`、`agent_interrupted`。

**claw-code**：

- 主循环没有墙钟超时；
- 主 CLI 默认迭代无限；
- 只有子 Agent 有 32 轮上限；
- 主要靠用户中断。

**Hermes**：

- 有回合级 liveness watchdog：600s 没有任何活动则强制中止；
- 有迭代上限，接近上限时提前收口；
- 有重复退化检测和空响应恢复阶梯；
- 外层未预期异常每回合最多重试 8 次。

三者分别代表：**固定预算**（BokaClaw）、**用户主导**（claw-code）、**活动检测 + 分层恢复**（Hermes）。

## 4. 相对共同缺口

1. **工具失败熔断不完整**
   - BokaClaw 和 claw-code 都没有通用熔断；
   - Hermes 只对未知工具连续 3 次做 partial 终止，仍不是通用熔断。

2. **工具超时覆盖不一致**
   - BokaClaw 依赖各工具自己实现；
   - claw-code 只有 Bash 显式超时；
   - Hermes 的截止时间最完整，但文档未覆盖所有工具类型。

3. **长任务资源回收**
   - BokaClaw 的 run 超时不强杀工具线程；
   - claw-code 的子 Agent 无 watchdog；
   - Hermes 有 600s liveness watchdog，但子 Agent 机制未在文档中展开。

4. **失败后的持久化语义不同**
   - BokaClaw 会保存部分失败结果；
   - claw-code 失败轮次不写盘；
   - Hermes 强调收尾步骤独立守护和已流文本保留，但落库失败仍依赖告警与重试。

## 5. 总体评价

| 实现 | 最强项 | 主要短板 |
|---|---|---|
| BokaClaw | run 生命周期、部分结果持久化、SSE、trace/span、取消与恢复 | 模型重试弱、无 fallback、无上下文超限恢复、工具超时不强杀 |
| claw-code | 模型重试、请求前预检、上下文超限渐进压缩、子 Agent fallback | 失败轮不持久化、无墙钟超时、无 watchdog、主循环无熔断 |
| Hermes | 错误分类、一次性修复链、fallback、自动恢复阶梯、响应校验、liveness watchdog | 分层多、路径复杂；子 Agent 机制未在本文档范围内 |

可以互相借鉴的方向：

| 方向 | 建议 |
|---|---|
| BokaClaw 借鉴 Hermes | 错误分类、一次性修复链、fallback、响应校验、deadline/watchdog |
| BokaClaw 借鉴 claw-code | 指数退避重试、上下文超限渐进压缩 |
| claw-code 借鉴 BokaClaw | run 级状态、部分持久化、无进展保护、统一事件观测 |
| claw-code 借鉴 Hermes | 错误分类、空响应/截断恢复、liveness watchdog |
| Hermes 借鉴 BokaClaw | durable run 状态、SSE/事件回放、跨重启恢复 |
| 三者共同改进 | 统一工具超时、通用工具熔断、长任务资源回收 |

## 6. 相关文档

- [`exception-handling.md`](exception-handling.md)：BokaClaw 五类异常的当前实现
- [`exception-handling-claw-code.md`](exception-handling-claw-code.md)：claw-code 五类异常的当前实现
- [`agent-loop-error-handling.md`](agent-loop-error-handling.md)：Hermes ReAct 循环异常处理
- [`runtime-behavior.md`](runtime-behavior.md)：BokaClaw 循环终止与取消
- [`tool-execution.md`](tool-execution.md)：BokaClaw 工具执行与权限
