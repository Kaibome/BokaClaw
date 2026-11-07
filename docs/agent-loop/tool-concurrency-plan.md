# 工具并发调用改造方案

> 状态：已实现（P0 核心 + server 接入）；P2 子 Agent 并发未做
>
> 日期：2026-11-07
>
> 范围：`agent-loop/`（核心改动）、`server/app/agent/`（配置与工具标记）、`docs/agent-loop/tool-execution.md`（同步更新）
>
> 背景：当前一次模型响应里的多个 `tool_call` 严格串行执行（`runtime.py:449-512`）；子 Agent 也是同步阻塞。本文给出“只并行执行、按声明顺序回放状态”的最小改造方案。
>
> 实现状态（2026-11-07）：`Tool.effect` / `Tool.concurrency_safe`、`ToolRegistry.plan_batches` / `invoke_batch` / `invoke_many`、`AgentConfig.tool_concurrency`、runtime 的“按批执行 + 按批回放”、server 的 `AGENT_TOOL_CONCURRENCY` 与只读工具标记均已落地；新增 `agent-loop/tests/test_tool_concurrency.py`（13 个用例），agent-loop 全量 194 passed / 1 skipped。P2 子 Agent 并发仍未实现。

---

## 1. 目标与非目标

### 目标

1. 同一次模型响应里的多个工具调用可以并行执行，墙钟时间从“求和”降到“最慢那条”。
2. **协议顺序、run 级状态、无进展判定、usage 合并、事件契约全部保持不变**。
3. 默认关闭（`tool_concurrency=1`），行为与现在逐字节一致。
4. 只允许显式声明为并发安全的工具进入并行批次。

### 非目标（本阶段不做）

- 子 Agent 并发（放到 P2，单独设计）。
- 工具级超时、自动重试、幂等键、per-tool 熔断。
- 同一工具调用的结果缓存或去重。
- 把 runtime 从同步生成器改成 asyncio。

---

## 2. 核心设计：执行并行，状态串行

把一次响应里的工具处理拆成两个阶段：

```text
阶段 A：执行（可并行）
  calls[0..n) 按 concurrency_safe 切成若干连续批次
  安全批次 → 线程池并行；不安全调用 → 单独串行，并且是批次边界
  每个 index 只写入 results[index]，不碰上下文、不记台账、不发事件

阶段 B：回放（严格串行、按 index）
  对每个 (call, result) 依次：
    tool_results.append / usage 合并 / ledger.record / guard.record
    emit TOOL_RESULT / append tool_message / append new_messages
```

这样做的原因：协议顺序和所有 run 级状态都只依赖回放顺序，而回放顺序就是 `tool_calls` 的声明顺序；并行只影响哪条先算完，不影响结果落在哪个槽位。

### 2.1 批次划分规则

从 `index=0` 开始向后扫描：

- 工具存在、参数没有解析错误、且 `tool.concurrency_safe is True` → 加入当前批次；
- 遇到不满足条件的调用，或遇到 `concurrency_safe=False` 的工具 → 结束当前批次，该调用**单独串行执行**，并成为下一条批次边界；
- 批次长度 > 1 且 `max_workers > 1` 才真正并行。

这个规则保证：

- 写类工具（`Write` / `Edit` / `Bash`）之间仍然按声明顺序执行；
- 安全工具不会跨过写类工具“提前”执行，避免 `Write A → Read A` 被重排；
- 连续多个只读工具（如 3 个 `WebFetch`、`Read` + `Grep`）能并行。

### 2.2 并发只对 `tool_error_mode="return"` 生效

`error_mode="raise"` 时保持完全串行。原因：当前语义是“某个工具抛出后整轮立即中断、不补齐协议消息”，并行执行会让其他工具在异常前已经产生副作用，且难以复现原来的中断点。server 使用的是 `"return"`，不受影响。

### 2.3 取消语义

- 提交前检查一次 `cancel_event`；
- 批次内用 `concurrent.futures.wait(timeout=0.2)` 轮询；一旦置位：
  - `future.cancel()` 撤销**尚未开始**的调用；
  - 已开始的调用无法中断，允许它继续跑完，但结果不再等待；
- 所有没有拿到结果的槽位填一条 `aborted` 的 `ToolResult`，内容与现有 `_abort_pending_tool_calls` 一致：`{"error": "run aborted: ..."}`；
- 回放阶段照常把真实结果和 aborted 结果按顺序写入消息；
- 回放结束后再调用 `check_cancelled()`，此时 `pending_calls` 为空，`_cancel_run` 只负责持久化、发 `RUN_END`、抛 `RunCancelled`。

与现状的差异要写进文档：串行时“取消”发生在工具返回之后，工具结果一定被记录；并行时在飞工具的结果可能被丢弃（副作用仍已发生）。

---

## 3. 代码改动清单

### 3.1 `agent-loop/src/bokaclaw_agent/tools/registry.py`

1. `Tool` 增加字段：

```python
concurrency_safe: bool = False
```

2. `Tool.from_callable(..., concurrency_safe: bool = False)` 透传；`@tool(..., concurrency_safe=True)` 支持装饰器写法。默认 `False`，保持保守。
3. 新增 `ToolRegistry.plan_batches(...)`、`invoke_batch(...)` 与 `invoke_many(...)`：

```python
def plan_batches(self, calls: list[ToolCall]) -> list[list[int]]: ...
def invoke_batch(self, calls, batch, *, error_mode="return", max_workers=1, cancel_event=None) -> list[ToolResult]: ...
def invoke_many(self, calls, *, error_mode="return", max_workers=1, cancel_event=None) -> list[ToolResult]: ...
```

职责：

- `plan_batches` 按 2.1 切批；
- `invoke_batch` 执行单批：单调用批次走现有 `self.invoke`，多调用批次提交线程池，结果按批次顺序返回；取消时补齐 aborted 结果；
- `invoke_many` 是面向调用方的便捷封装，按批执行并返回与 `calls` 等长、同序的结果；
- 三者都**不发事件、不改上下文**；runtime 自己决定何时发事件与回放。

4. 新增内部方法 `_safe_batch(calls, start)` 与 `_aborted_result(call, reason)`。
5. 线程池生命周期：每个 run 一个 executor（懒创建，`max_workers=config.tool_concurrency`），run 结束时 `shutdown(wait=False, cancel_futures=True)`。避免每个批次创建/销毁线程。

### 3.2 `agent-loop/src/bokaclaw_agent/core/config.py`

```python
tool_concurrency: int = Field(default=1, ge=1, le=8)
```

说明：上限 8 是防御性的，避免一次响应里几十个调用把上游或沙箱打满。实际生效值再取 `min(config值, 批次数)`。

### 3.3 `agent-loop/src/bokaclaw_agent/agent/runtime.py`

把 449-512 的执行循环替换为：

1. `calls = list(response.tool_calls)`；`pending_calls = calls`；
2. 计算批列表：`tool_concurrency <= 1` 或 `tool_error_mode != "return"` 时退化为“每次调用一批”（保持旧的串行事件交错），否则用 `plan_batches(calls)`；
3. 对每个批次：
   - 先发该批全部 `TOOL_CALL` 事件（`status="running"`）；
   - `batch_results = self.tools.invoke_batch(...)`；
   - 按批内 index 顺序回放：现有 469-508 的逻辑原样搬进循环体；
   - `pending_calls = calls[batch[-1] + 1:]`；
   - `check_cancelled(iteration)`；取消时 `_cancel_run` 负责补齐后续批次的 aborted 结果；
4. 所有批次结束后 `pending_calls = []`；
5. `if guard_stop: stop_reason = "no_progress"`（位置不变）。

其他改动：

- `_emit` 增加一个 `threading.Lock` 保护 `_event_seq` 自增与事件构造。本方案下回放阶段仍只在主线程发事件，但工具内部的子事件（如子 Agent、自定义工具）可能在 worker 线程发出，锁是必要的防御。
- `_execute` 增加 `try/finally`，在结束时关闭 executor。
- `_cancel_run` / `_abort_pending_tool_calls` 不需要改语义；并发路径已经把 `pending_calls` 置空，且所有槽位都有结果。

### 3.4 `server/app/agent/runtime.py` + `dev_tools.py`

1. `LocalAgentConfig` 增加 `tool_concurrency`，读取 `AGENT_TOOL_CONCURRENCY`，默认 1；构造 `AgentConfig` 时传入。
2. `build_dev_tools` 标记并发安全：

| 工具 | `concurrency_safe` |
|---|---|
| `Read` / `Glob` / `Grep` / `WebFetch` / `WebSearch` / `ViewImage` | `True` |
| `Write` / `Edit` / `NotebookEdit` / `Bash` / `PowerShell` / `Monitor` | `False` |
| `search_memories` | `True`（只读） |
| `save_memory` | `False`（写 mem0，可能产生重复） |
| office 工具 | 默认 `False`，逐个评估后开启 |
| `explore` / `research_agent` | `False`（P2 再开启） |

3. 日志：run 开始时记录 `tool_concurrency` 与每个批次大小，便于对照耗时。

### 3.5 `agent-loop/README.md` 与文档

- README 增加 `tool_concurrency` 与 `concurrency_safe` 的公开 API 说明（公共 API 变更要求同步 README）。
- `docs/agent-loop/tool-execution.md` §5 改为引用本方案。

---

## 4. 语义契约（改造后必须成立）

| 契约 | 验证方式 |
|---|---|
| `new_messages` 中 tool 消息数量 == `tool_calls` 数量，且顺序一致 | 断言索引与 id 一一对应 |
| `RunResult.tool_results` 顺序 == 声明顺序 | 同上 |
| `ToolLedger` / `ProgressGuard` 看到的结果顺序不变 | 用确定性工具构造 no-progress 场景 |
| usage 每条结果只合并一次 | 对比串行基线的 token 总数 |
| 不安全工具之间严格按声明顺序执行 | 记录每个工具的进入/退出时间 |
| 取消后每个声明的调用仍有对应 tool 消息 | 取消测试断言消息数 |
| `tool_error_mode="raise"` 行为不变 | 强制串行路径的回归测试 |
| 默认 `tool_concurrency=1` 时事件序列与改造前完全一致 | 事件快照对比 |

---

## 5. 并发安全模型

“不安全工具是批次边界”只解决**本 run、本响应内**的调用顺序，不等于工具执行安全。下面把它拆成可验证的保证、明确的不保证、以及分层的加固措施。

### 5.1 三条硬保证

| 编号 | 保证 | 实现要求 |
|---|---|---|
| **G1 全序** | 同一响应内，不安全工具与所有其他调用之间存在确定的先后关系 | 不安全调用单独成批；它之前的批次必须全部完成，它之后的批次必须等它完成 |
| **G2 批次屏障** | 第 i 批的所有 future 都结束后，才提交第 i+1 批 | 实现里必须 `join` 整批，不能“谁先完成谁进下一批” |
| **G3 回放顺序** | 消息、`tool_results`、ledger、guard、usage 的顺序等于声明顺序 | 执行结果先写回 `results[index]`，回放阶段单线程按 index 处理 |

G1 的前提是“安全工具真的没有副作用”。这是整个模型最脆弱的一环，靠声明和评审保证，不能靠边界保证。

### 5.2 批次边界**不**保证的六类情况

1. **外部写者**：同 workspace 的其他 run、executor 里并发的 shell、运维手工修改。批次屏障只约束当前 run 的当前响应，跨 run 没有任何互斥。
2. **先前的后台副作用**：`Monitor` 启动的进程、上一条 `Bash` 里的 `&`。它们在工具调用返回后继续运行，不在任何批次的 join 范围内。
3. **误标的工具**：把写操作标记成 `concurrency_safe=True`，边界就形同虚设。
4. **参数决定的安全性**：同一个工具，读不同文件安全、读同一文件也安全；但“读 A + 写 A”不安全，而后者往往由两个不同工具承担，布尔标记表达不了。
5. **取消后的在飞调用**：结果被丢弃，但副作用已经发生；边界无法回滚。
6. **工具内部的共享可变状态**：per-run cache、全局单例、连接池。它们是线程安全的（或不是），与调用顺序无关。

### 5.3 加固措施

#### P0（随第一版一起做）

- **用能力声明代替布尔**：`effect: "read" | "write" | "external"`，只有 `read` 默认允许并发；`concurrency_safe` 保留为兼容别名，内部按 `effect` 判定。这样“读”和“写”的语义在类型层面就分开了，评审时也更容易发现误标。
- **默认拒绝**：未声明 `effect` 的工具一律视为 `write`。
- **工具契约清单**（写进 README）：
  1. 不修改任何共享状态；
  2. 不依赖与其他调用的先后顺序；
  3. 可重入，内部缓存必须自己加锁；
  4. 不发起“调用返回后仍在运行”的后台任务。
- **测试证明顺序**：用 `threading.Barrier` 和进入/退出时间戳验证 G1/G2，而不是只断言结果正确。

#### P1（并发规模上来后）

- **资源键分区**：给工具增加 `resource_keys(args) -> frozenset[str]`（文件规范化路径、URL、mem0 的 `user_id` 等）。同一批次内如果两个调用共享资源键，就在该点切批；读-读可以共享，读写、写-写必须串行。这是对“参数决定安全性”的通用解，比给每个工具写布尔值更可靠。
- **写文件的原子性与锁**：`Write`/`Edit` 改为“临时文件 + `os.replace`”，可选加 path 级 `flock`；`Read` 在批次结束后可对比 mtime/hash，检测是否被外部修改（只检测，不阻止）。
- **Monitor 生命周期绑定 run**：run 结束时停掉该 run 启动的 monitor。当前 `_MONITORS` 是全局字典 + atexit（`dev_tools.py:729-741`），跨 run 不可见。
- **cache 并发化**：`WebFetch` / `WebSearch` 的 per-run cache 是普通 dict（`dev_tools.py:1113-1118`），并发下会重复抓取同一 URL（不会损坏）。加锁并做 in-flight 去重即可。
- **跨 run 隔离**：每个 run 独立 workspace，或 workspace 级锁。否则并发的两个会话仍然可能互相覆盖文件——这是当前串行工具也存在、但与本次改造无关的既有问题。

#### P2（可观测与兜底）

- **工具级超时**：一个 hang 住的工具会阻塞整批 join；`max_seconds` 只能停 run，杀不掉线程。需要 per-call timeout 或在 executor 侧强制终止。
- **TOCTOU 检测**：记录读到的文件版本（mtime + size + 可选 hash），批次结束后标记“读取期间被修改”，让模型知道证据可能过期。

### 5.4 当前工具的判定表

| 工具 | 建议 `effect` | 主要风险 |
|---|---|---|
| `Read` / `Glob` / `Grep` | `read` | 外部写者导致 TOCTOU；文件很大时并发读有内存峰值 |
| `WebFetch` / `WebSearch` | `external`（只读） | per-run cache 无锁，重复抓取；外部内容可变 |
| `search_memories` | `external`（只读） | mem0 可能被并发写入，读到瞬时状态 |
| `save_memory` | `write` | 重复写入；必须边界 |
| `Write` / `Edit` / `NotebookEdit` | `write` | 同路径并发覆盖；需要原子写 |
| `Bash` / `PowerShell` | `write` | 任意副作用；必须边界 |
| `Monitor` | `write`（含后台） | 启动的进程在调用结束后继续运行，边界管不住 |
| office 工具 | `write` | 文件写；必须边界 |
| `explore` / `research_agent` | `write`（P1） | 子 Agent 会读文件、可能长时间运行；P2 单独设计 |

### 5.5 判定流程

对每个准备标记为可并发的工具，按顺序回答：

1. 有副作用吗？→ 有 → 串行边界。
2. 会读写共享可变状态吗？→ 会 → 串行边界，或由工具自己加锁。
3. 同一批里可能和其他调用共享资源键吗？→ 是 → 切批或串行。
4. 是外部系统且只读吗？→ 是 → 可并发，但要接受最终一致性。
5. 以上都没有 → 可并发。

只要有一项回答不确定，默认串行。

---

## 6. 测试计划

### 6.1 单元测试（`agent-loop/tests/test_tool_concurrency.py`）

1. **批次划分**：`[safe, safe, unsafe, safe]` → 批次 `[0,1]`、`[2]`、`[3]`。
2. **未知工具 / 参数错误**：不进入并行批次，仍返回错误结果。
3. **顺序**：三个工具随机 sleep，结果列表顺序与传入顺序一致。
4. **真正并行**：两个工具用 `threading.Barrier(2, timeout=2)` 汇合；串行会超时，并行通过。
5. **取消**：工具 A 置位 `cancel_event`；断言 A 有真实结果、B 为 aborted、消息数 == 2。
6. **`error_mode="raise"`**：即使包含安全工具也不并行，异常在声明位置抛出。
7. **usage**：每个结果带不同 usage，合并后的总数正确。

### 6.2 集成测试

- `FakeProvider` 返回一条带 3 个 `tool_calls` 的响应；断言 `new_messages` 顺序、`tool_results`、事件 `tool.call`/`tool.result` 的 id 配对。
- 取消场景：在批次执行中置位 `cancel_event`，断言最终 `RunResult.stop_reason == "cancelled"` 且协议完整。
- 回归：`tool_concurrency=1` 跑现有全部测试，事件序列与改造前一致。

### 6.3 server 测试

- `AGENT_TOOL_CONCURRENCY=4` 时 `AgentConfig.tool_concurrency == 4`；
- `build_dev_tools` 返回的 `Tool.concurrency_safe` 与 3.4 表格一致；
- 一次 3 个 `WebFetch` 的 run 中，`tool.call` 事件时间戳出现重叠。

---

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 工具实现并非线程安全 | 默认 `False`；只读白名单逐个标记；文档写明 `concurrency_safe=True` 的契约（可重入、无共享可变状态） |
| 并行写文件 | 写类工具不进入批次，且是批次边界 |
| 上游限流 / 429 | `max_workers` 默认 1、上限 8；后续可加按 provider 的并发信号量 |
| 取消后副作用已发生 | 文档写明；不对已开始工具做回滚 |
| 事件乱序 | 回放阶段单线程发事件；`_emit` 加锁；`tool_call_id` 保证可配对 |
| `asyncio.run` 每线程一个事件循环 | 可接受；后续若要共享 loop，需单独设计 async executor |
| 线程池泄漏 | 每个 run 一个 executor，`finally` 中 `shutdown(wait=False, cancel_futures=True)` |

---

## 8. 分阶段实施

### P0：核心能力（约 1–2 天）

- `Tool.concurrency_safe` + `ToolRegistry.invoke_many`；
- `AgentConfig.tool_concurrency`；
- runtime 改为“执行 + 回放”两阶段；
- 单元测试与集成测试；
- 默认关闭，现有测试全绿。

### P1：server 接入与观测（约 1 天）

- `AGENT_TOOL_CONCURRENCY` 配置；
- 标记只读工具；
- run 日志与 span 增加批次信息；
- 真实 run 对比：3 个 WebFetch 的墙钟时间从 `t1+t2+t3` 降到 `max(t1,t2,t3)`。

### P2：子 Agent 并发（约 2–3 天，独立设计）

- `SubagentOptions.max_concurrent`；
- `DelegationLedger.record` 加锁；
- `_task_validator` 移出 `state.lock`；
- 共享父取消 watcher，去掉每子 Agent 一个轮询线程；
- 父 provider 的并发/限流策略；
- 子 Agent 工具标记为 `concurrency_safe=True` 的前置条件。

---

## 9. 验收标准

1. `tool_concurrency=1` 时行为与改造前完全一致（测试 + 事件快照）。
2. `tool_concurrency=4` 时，3 个只读工具的实际执行时间出现重叠，墙钟时间接近最慢一条。
3. 任意取消点下，`tool_calls` 数量 == tool 消息数量，顺序一致。
4. 现有 `agent-loop` 测试与 server agent 测试全部通过，ruff 通过。
5. 文档与 README 同步更新。
