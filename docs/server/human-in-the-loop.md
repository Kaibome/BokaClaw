# Human-in-the-loop：运行中向用户提问

> 状态：方案 + 实现（2026-11-08）
>
> 范围：`server/app/agent/`、`server/app/routers/agent.py`、`dashboard/`
>
> 相关：[`agent-execution-decoupling.md`](agent-execution-decoupling.md)、[`../agent-loop/tool-execution.md`](../agent-loop/tool-execution.md)

## 1. 目标

Agent 在一次 run 执行过程中，如果缺少只有用户才知道的信息（账号、偏好、业务口径、选择项），可以**暂停当前工具调用**，向用户提问；用户回答后，工具返回答案，run 继续。

典型场景：

- 需要用户确认目标范围（“要对比哪两个版本？”）；
- 需要用户提供工具查不到的私有信息（“部署在哪个 region？”）；
- 在多个可行方案之间让用户选择。

不是审批流、不是人工接管：run 保持 `running`，模型仍然拥有最终决定权。

## 2. 设计选择

| 选择 | 理由 |
|---|---|
| **阻塞式工具**，而不是“结束 run 再续跑” | 一次 run 对应一次用户请求，阻塞实现不需要引入 `waiting_input` 状态、新 run 的会话互斥和续跑协议 |
| **工具化**（`ask_user`） | 由模型决定何时需要信息；不需要修改 SDK 的 ReAct 循环 |
| **事件持久化**（`user.question` / `user.answer`） | 复用现有 `agent_run_events` + SSE：断线重连、`after_seq` 回放、Trace 都能直接工作 |
| **HTTP 回答** | 与取消一致：`POST /agent/runs/{run_id}/answers`，按 run 归属鉴权 |
| **仅后台 run** | 旧版请求绑定模式没有独立 writer/管理器，不注册该工具 |

## 3. 端到端流程

```text
模型返回 tool_call: ask_user(question, options, ...)
   │
   ▼
RunInteraction.ask（SDK worker 线程）
   ├─ 生成 question_id，登记 PendingQuestion
   ├─ 通过 run 的 RunEventWriter 写 user.question（落库 + 发布 SSE）
   ├─ 阻塞等待，每 0.2s 检查 cancel_event / 超时
   ▼
Dashboard 收到 user.question → 渲染问题卡片
   │  用户选择/输入 → POST /agent/runs/{run_id}/answers
   ▼
RunInteraction.answer（事件循环）
   ├─ 写 user.answer 事件
   ├─ 唤醒 PendingQuestion
   ▼
工具返回 {"answer": "..."} → 模型继续
```

超时或取消时，工具返回 `{"error": "timeout" | "cancelled"}`，模型按普通工具错误继续或收尾。

## 4. 工具契约

```python
ask_user(
    question: str,
    options: list[str] | None = None,
    allow_free_text: bool = True,
    timeout_seconds: int = 0,   # 0 = 使用 AGENT_USER_INPUT_TIMEOUT_SECONDS
) -> dict
```

返回：

```json
{"question_id": "q_...", "answer": "用户输入或选项"}
```

错误：

```json
{"error": "timeout"}
{"error": "cancelled"}
{"error": "A question is already waiting for the user."}
```

约束：

- 同一 run 同时只允许一个待回答问题；
- 每个问题默认 240 秒超时，范围 5–600 秒；实际生效值还会与 `AGENT_LOOP_MAX_SECONDS - 15` 取小，避免 run 级超时先于问题超时触发；
- 问题长度 ≤ 2,000 字符，选项 ≤ 8 个、每个 ≤ 200 字符，答案 ≤ 8,000 字符；
- `ask_user` 声明为写工具，永远是批次屏障，不能与其他工具并发；
- 子 Agent 使用 `read_only` 策略时拿不到该工具。

## 5. HTTP / SSE 契约

### 5.1 回答

```http
POST /agent/runs/{run_id}/answers
{
  "question_id": "q_...",
  "answer": "free text or option value"
}
```

响应：

```json
{"run_id": "...", "question_id": "q_...", "accepted": true}
```

错误：

| 状态 | code | 含义 |
|---|---|---|
| 404 | `agent_run_not_found` | run 不存在或不属于当前用户 |
| 409 | `agent_run_not_running` | run 已结束 |
| 404 | `agent_question_not_found` | question_id 未在等待 |
| 409 | `agent_question_answered` | 已回答或已过期 |

### 5.2 事件

`user.question`：

```json
{
  "run_id": "...", "conversation_id": "...", "seq": 12,
  "message_id": "assistant-message-id",
  "question_id": "q_...",
  "question": "Which environment?",
  "options": ["staging", "production"],
  "allow_free_text": true,
  "expires_at": "2026-11-08T10:05:00+00:00",
  "timeout_seconds": 240
}
```

`user.answer`：

```json
{
  "run_id": "...", "conversation_id": "...", "seq": 13,
  "message_id": "assistant-message-id",
  "question_id": "q_...",
  "answer": "staging",
  "answered_at": "2026-11-08T10:00:12+00:00"
}
```

事件写入顺序保证：`user.question` 落库并发布之后，`ask_user` 才开始等待；`user.answer` 落库之后才唤醒工具。断线重连时按 `after_seq` 回放，Dashboard 能从事件历史恢复待回答问题。

## 6. 服务端结构

| 组件 | 职责 |
|---|---|
| `app/agent/interactions.py` `RunInteraction` | 每个 run 一个；登记待回答问题、写事件、跨线程唤醒、超时与关闭 |
| `app/agent/user_input.py` | `ask_user` 工具与系统提示指引 |
| `AgentRunManager` | 创建/销毁 `RunInteraction`；`answer_question()` 供路由调用；取消时关闭 |
| `RunEventWriter` | 沿用现有 seq 分配、落库、发布 |
| `POST /agent/runs/{run_id}/answers` | 鉴权、状态校验、转发给 manager |
| `AgentRunEvent` CHECK + 迁移 013 | 允许 `user.question` / `user.answer` |

## 7. 边界与已知限制

- **服务重启**：等待中的问题不会恢复，run 被标记为 `interrupted`；用户需要重新发起。
- **占用并发槽位**：等待期间 run 仍是 `running`，会占用 `AGENT_RUN_CONCURRENCY` 的一个位置。默认超时 240 秒是保护；问题超时始终低于 run 级超时（默认 300 秒），超时后模型会收到 `{"error": "timeout"}` 并继续收尾。
- **不支持“先结束再续跑”**：如果以后需要跨重启恢复，需要引入 `waiting_input` 状态和“用户回答后新建 run”的协议。
- **旧版模式**：`AGENT_RUN_BACKGROUND=false` 时不注册 `ask_user`。
- **子 Agent**：默认 `read_only` 策略不包含 `ask_user`；不要让子 Agent 使用 `tool_policy="code"` 后在无人值守场景提问。
- **答案内容**：用户输入会进入模型上下文，按普通用户消息对待，不提升权限。

## 8. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_USER_INPUT_ENABLED` | `true` | 是否注册 `ask_user` 工具；关闭后模型看不到该工具 |
| `AGENT_USER_INPUT_TIMEOUT_SECONDS` | `240` | 单个问题的等待上限，范围 5–600；实际值还会低于 run 级超时 15 秒 |

## 9. 实现文件

- `server/app/agent/interactions.py`
- `server/app/agent/user_input.py`
- `server/app/agent/runtime.py`
- `server/app/agent/runner.py`
- `server/app/routers/agent.py`
- `server/app/models.py`
- `server/alembic/versions/013_add_user_input_events.py`
- `dashboard/src/hooks/use-agent-chat.ts`
- `dashboard/src/components/agent/user-question.tsx`
- `dashboard/src/components/agent/message-bubble.tsx`
- `dashboard/src/components/agent/message-thread.tsx`
- `dashboard/src/app/(root)/dashboard/agent/page.tsx`
- `dashboard/src/types/api.ts`、`dashboard/src/utils/api-endpoints.ts`、`dashboard/src/i18n/messages/{en,zh}.ts`
