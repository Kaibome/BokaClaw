# claw-code 异常处理机制

本文整理当前代码中五类异常的检测、恢复与失败表现：

1. 上下文压缩失败
2. 模型调用失败
3. 工具调用失败
4. 子 Agent 调用失败
5. ReAct 循环超时

> 依据：`rust/crates/runtime`、`rust/crates/api`、`rust/crates/tools`、`rust/crates/rusty-claude-cli` 的当前实现。本文描述行为，不展开代码。
>
> 相关：[`exception-handling.md`](exception-handling.md)（BokaClaw 实现）、[`agent-loop-error-handling.md`](agent-loop-error-handling.md)（Hermes 实现）、[`exception-handling-comparison.md`](exception-handling-comparison.md)（三者对比）

---

## 总览

| 异常类型 | 检测方式 | 默认处理 | 是否中断当前轮 |
|---|---|---|---|
| 上下文压缩失败 | 无可压缩消息 / 压缩后仍超限 / 压缩后健康检查失败 | 跳过、渐进压缩重试、最终报错 | 视情况 |
| 模型调用失败 | 请求前预检、HTTP 状态、流式错误、重试耗尽 | 预检拦截、指数退避重试、上下文超限恢复 | 不可恢复时中断 |
| 工具调用失败 | 参数、权限、hook、执行错误 | 转成 `is_error` 工具结果交还模型 | 否 |
| 子 Agent 调用失败 | 创建失败、线程失败、子运行时失败 | 更新 manifest 为 failed，父级自行轮询 | 否 |
| ReAct 循环超时 | 迭代次数上限 | 主循环默认无上限，子 Agent 32 轮 | 是 |

核心差异：

- **工具失败是软失败**：模型能看到错误并自行修正。
- **模型失败是硬失败**：当前轮直接结束。
- **子 Agent 失败是异步失败**：父级不会收到通知，只能读取文件。
- **ReAct 循环没有墙钟超时**：只有可选的迭代上限。

---

## 1. 上下文压缩失败

### 1.1 触发场景与处理

| 场景 | 处理 | 结果 |
|---|---|---|
| 手动 `/compact` 时低于阈值 | 不改动会话，提示「Compaction skipped」 | 非错误 |
| 自动压缩没有可删除消息 | 返回空操作，不报告错误 | 非错误 |
| 压缩后请求仍超上下文 | 最多 4 轮渐进压缩，保留最近消息数 4 → 2 → 1 → 0 | 成功则继续；失败则返回错误 |
| 已经没有可压缩消息 | 打印「No further compaction possible」并停止重试 | 暴露原始错误 |
| 压缩后健康探针失败 | 在下一轮开始前报错，建议新开会话 | 当前轮失败 |
| 压缩结果写入会话文件失败 | 错误向上传播 | 当前轮失败 |

### 1.2 具体机制

- **压缩算法本身不会失败**：摘要由本地规则生成，不调用模型；Trident 的 Supersede / Collapse / Cluster 三个阶段都是尽力而为的转换，没有错误返回路径。
- **“压缩失败”主要表现为压不干净**：压缩后仍超出上下文窗口，此时 CLI 会按 4 → 2 → 1 → 0 的保留策略反复压缩并重试原请求。
- **健康探针**：如果会话已有压缩记录，每次新一轮开始前会用一次无副作用的 `glob_search` 探测工具执行器是否正常；失败则拒绝继续，提示会话可能不一致。
- **没有回滚**：压缩成功后内存会话立即替换；如果后续写盘失败，磁盘上仍是压缩前版本。

### 1.3 实现位置

- `rust/crates/runtime/src/compact.rs`
- `rust/crates/runtime/src/trident.rs`
- `rust/crates/runtime/src/conversation.rs`（自动压缩与健康探针）
- `rust/crates/rusty-claude-cli/src/main.rs`（上下文超限渐进恢复）

---

## 2. 模型调用失败

### 2.1 请求前预检

| 检查 | 失败行为 |
|---|---|
| 凭证缺失 | 直接失败，不重试，提示环境变量或检查 base URL |
| 上下文窗口预检 | 本地估算 token；Anthropic 额外尝试 count_tokens；超限直接返回上下文错误 |
| 请求体大小预检 | 超过 provider 限制直接拒绝 |
| OAuth 过期 | 有 refresh token 则自动刷新，否则失败 |

以上均属于不可重试错误。

### 2.2 可重试错误

- HTTP 状态：408、409、429、500、502、503、504、529
- 网络层：连接失败、超时、请求错误
- 特定 400：网关抖动类响应，例如无可解析响应体、连接重置、管道断开、空响应

重试策略：

- 默认最多重试 8 次，即最多 9 次尝试
- 指数退避，起始 1 秒，上限 128 秒，带随机抖动
- 429 优先使用服务端 `Retry-After`
- 每次请求的开始、成功、失败都会记录 telemetry
- 耗尽后返回 `api failed after N attempts`，并保留最后一次错误

### 2.3 不可重试错误

401/403、真实 400、413/422、JSON 解析失败、SSE 帧非法、上下文超限、请求体超限、缺少凭证、OAuth 不可刷新。

### 2.4 流式阶段

- 建立连接前的失败按上述重试规则处理。
- 流建立后中途断开或解码失败，**默认不重试**。
- 工具执行完成后，模型 10 秒内没有返回首个事件，会重新发送一次请求作为“继续”提示，仅一次。
- 用户中断会丢弃请求流，按 `interrupted` 处理，不算失败。

### 2.5 上下文超限恢复

文本交互路径会识别以下错误并自动压缩后重试：

- 上下文窗口类错误
- 「assistant stream produced no content」
- 流解码失败、无法解析输入等被判定为上下文问题的错误

恢复过程：

1. 若错误中包含服务端真实上下文窗口大小，把自动压缩阈值调整为窗口的 70%
2. 最多 4 轮压缩
3. 每轮先执行 Trident，再执行摘要压缩
4. 每轮保留最近消息数依次为 4、2、1、0
5. 压缩后重建 runtime，重试原请求

JSON 单次提示路径没有这套自动恢复逻辑，错误会直接进入结构化错误输出。

### 2.6 最终失败表现

- 标记该轮失败，返回错误。
- 文本模式打印「Request failed」。
- REPL 中错误会向上传播，最终以非零退出码结束进程；失败轮次不会写盘，但进程启动时创建的会话文件仍存在。
- JSON 模式输出结构化错误，包含 failure class、错误信息和修复提示。
- 子 Agent 路径有跨模型 fallback；主 CLI 当前没有接入 fallback 链。

### 2.7 实现位置

- `rust/crates/api/src/error.rs`
- `rust/crates/api/src/http_client.rs`
- `rust/crates/api/src/providers/anthropic.rs`
- `rust/crates/api/src/providers/openai_compat.rs`
- `rust/crates/rusty-claude-cli/src/main.rs`
- `rust/crates/tools/src/lib.rs`（子 Agent fallback 链）

---

## 3. 工具调用失败

### 3.1 软失败机制

以下错误统一转换成 `is_error=true` 的工具结果，交还给模型：

- 未知工具
- 参数 JSON 非法
- 参数校验失败（缺失字段、类型错误、枚举非法、路径越界、大小超限等）
- 权限拒绝
- PreToolUse / PostToolUse hook 拒绝或失败
- 工具执行返回错误
- 子 Agent 工具白名单拒绝

主循环不会因工具失败而退出。模型会看到错误信息，可以换工具或修正参数后重试。

### 3.2 特殊情况

| 情况 | 行为 |
|---|---|
| 权限拒绝 | 同样作为工具错误返回，不弹第二次确认 |
| 用户中断 | 未执行的工具会收到合成的「已中断」结果，保证 tool_use / tool_result 配对完整 |
| bash 超时 | 仅当调用方显式传入 `timeout` 时生效；其他工具没有统一执行超时 |
| 工具 panic | 主循环没有按工具隔离 panic，异常会向上展开；子 Agent 线程有 `catch_unwind` 保护 |
| 会话写入失败 | 少数会把工具错误升级为整轮失败的情况 |

### 3.3 没有自动重试

工具层本身不重试。是否重试由模型根据错误结果自行决定。

风险：如果模型持续调用同一个失败工具，主循环没有熔断机制，只能依赖迭代上限或用户中断。

### 3.4 实现位置

- `rust/crates/tools/src/lib.rs`
- `rust/crates/runtime/src/conversation.rs`
- `rust/crates/runtime/src/permissions.rs`
- `rust/crates/runtime/src/permission_enforcer.rs`
- `rust/crates/rusty-claude-cli/src/main.rs`

---

## 4. 子 Agent 调用失败

### 4.1 创建阶段失败

| 失败 | 处理 |
|---|---|
| `description` 为空 | `Agent` 工具直接返回错误 |
| `prompt` 为空 | `Agent` 工具直接返回错误 |
| 后台线程创建失败 | manifest 标记为 failed，工具返回错误 |
| 构建子 runtime 失败 | 子线程内标记 failed |

父级收到的是普通工具错误，可以继续对话。

### 4.2 子运行时失败

- 子 Agent 使用独立的 Session 和独立 runtime。
- 最多执行 32 轮。
- 模型调用失败、工具循环失败、达到迭代上限，均会把 manifest 更新为 `failed`。
- 失败信息会写入输出文件，并记录错误、阻塞分类和 lane event。
- 子线程 panic 会被 `catch_unwind` 捕获，标记为 `sub-agent thread panicked`。
- 子 Agent 有跨模型 fallback 链；所有模型都失败后才落成 failed。

### 4.3 父级如何感知

- `Agent` 工具立即返回，状态为 `running`，不会等待子任务结束。
- 子 Agent 失败不会主动通知父级。
- 父级只能通过读取输出文件或 manifest 轮询状态。
- `TaskList` / `TaskGet` / `TaskOutput` 使用另一套任务注册表，**查不到子 Agent**。

### 4.4 限制

- 没有子 Agent 墙钟超时，也没有 watchdog。
- 如果子 Agent 卡住，manifest 会一直停留在 `running`。
- 进程退出后，后台线程和任务状态都会丢失。

### 4.5 实现位置

- `rust/crates/tools/src/lib.rs`（Agent 工具、子线程、manifest）
- `rust/crates/runtime/src/team_cron_registry.rs`
- `rust/crates/rusty-claude-cli/src/main.rs`

---

## 5. ReAct 循环超时

### 5.1 没有墙钟超时

主循环本身没有基于时间的超时。唯一的内建边界是迭代次数：

| 场景 | 上限 | 超限行为 |
|---|---|---|
| 主 CLI 主循环 | 默认无限 | 不会因轮数自动停止 |
| 子 Agent | 32 轮 | 返回「conversation loop exceeded the maximum number of iterations」，整轮失败 |

### 5.2 实际存在的时间边界

这些限制都在单次请求或单个工具层，而不是整个循环：

| 边界 | 默认值 |
|---|---|
| HTTP 连接超时 | 30 秒 |
| HTTP 请求超时 | 5 分钟，可通过环境变量调整 |
| 工具执行后首包停顿 | 10 秒，触发一次重试 |
| Provider 重试退避上限 | 128 秒 |

### 5.3 用户中断

- Esc 或 Ctrl+C 会设置中断信号。
- 中断是正常返回：`TurnSummary.interrupted = true`，不归类为错误。
- 工具执行中被中断时，会为所有待执行工具补一条「已中断」结果，再退出循环。
- 中断后会持久化会话。

### 5.4 风险

- 如果模型持续调用工具、工具持续失败，主循环没有自动熔断，可能一直运行到用户中断。
- 工具没有统一执行超时，阻塞型工具可能长时间占用循环。
- 子 Agent 没有墙钟超时，卡住后只能靠外部清理。

### 5.5 实现位置

- `rust/crates/runtime/src/conversation.rs`
- `rust/crates/rusty-claude-cli/src/main.rs`
- `rust/crates/api/src/http_client.rs`
- `rust/crates/tools/src/lib.rs`

---

## 6. 风险与改进建议

| 风险 | 建议 |
|---|---|
| 主 ReAct 循环无墙钟超时 | 增加可配置的单轮或单会话 deadline |
| 工具无统一超时与 panic 隔离 | 为工具执行增加超时包装和 `catch_unwind` |
| 子 Agent 无 watchdog | 增加心跳超时，超时后标记 failed 并清理 |
| 子 Agent 结果不可查询 | 把 Agent 任务接入统一任务注册表，或提供 AgentList / AgentWait |
| 压缩无回滚 | 写盘失败时保留旧会话或做原子替换 |
| 主 CLI 无 provider fallback | 复用子 Agent 的 fallback 链 |
| 失败轮次不持久化 | 明确产品语义；如需断点恢复，可在失败时也保存会话 |
| 工具失败无熔断 | 统计连续相同失败，达到阈值后终止本轮或提示用户 |

---

## 7. 关键实现文件

| 文件 | 职责 |
|---|---|
| `rust/crates/runtime/src/conversation.rs` | ReAct 主循环、中断、迭代上限、自动压缩、失败记录 |
| `rust/crates/runtime/src/compact.rs` | 摘要压缩、阈值判断、压缩摘要生成 |
| `rust/crates/runtime/src/trident.rs` | Supersede / Collapse / Cluster 三阶段压缩 |
| `rust/crates/runtime/src/permissions.rs` | 权限决策、规则匹配、审批 |
| `rust/crates/runtime/src/permission_enforcer.rs` | 工具执行层权限检查 |
| `rust/crates/api/src/error.rs` | 错误分类、可重试性、failure class |
| `rust/crates/api/src/http_client.rs` | 连接/请求超时配置 |
| `rust/crates/api/src/providers/anthropic.rs` | Anthropic 预检、重试、流式错误 |
| `rust/crates/api/src/providers/openai_compat.rs` | OpenAI 兼容预检、重试、流式错误 |
| `rust/crates/tools/src/lib.rs` | 工具分发、参数校验、Agent 子任务、fallback 链 |
| `rust/crates/rusty-claude-cli/src/main.rs` | 错误展示、上下文超限恢复、会话持久化、中断监听 |
