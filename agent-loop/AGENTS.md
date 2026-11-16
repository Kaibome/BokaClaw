# `agent-loop/` — bokaclaw-agent

独立的 Python SDK：一个极简 ReAct 运行时，支持工具执行和 Postgres 会话持久化。

## 命令

```bash
cd agent-loop
hatch env create
hatch run lint          # ruff check src tests examples
hatch run format        # ruff format src tests examples
hatch run format-check
hatch run test          # pytest
hatch build             # 生成 wheel + sdist
```

## 约定

- Python >= 3.10，所有模型和配置使用 Pydantic v2。
- Ruff 行宽 **120**；import 使用 isort 排序（`profile = black`，first-party 为
  `bokaclaw_agent`）。
- pytest 配合 `pytest-asyncio`；显式使用 `@pytest.mark.asyncio` 标记（strict 模式）。
- 单元测试不得访问网络：使用 `tests/conftest.py` 中的 `FakeProvider` 和
  `InMemorySessionStore`。
- Postgres 单元测试注入 fake pool；真实数据库测试由 `BOKACLAW_AGENT_TEST_DSN` 控制启用，并标记为
  `integration`。
- 保持 `psycopg` 为惰性导入，这样在没有 `postgres` extra 时 `import bokaclaw_agent` 仍可用。
- 绝不要在此修改 `.github/workflows/`；CI/CD 注册需要维护者批准。
- 公开 API 变更必须在同一次改动中更新 `README.md`。

## 目录结构

| 路径 | 用途 |
|------|---------|
| `src/bokaclaw_agent/core/` | `config`、`types`、`exceptions`、共享辅助函数 |
| `src/bokaclaw_agent/agent/runtime.py` | ReAct 循环、失败处理、持久化编排 |
| `src/bokaclaw_agent/agent/context.py` | 内存上下文、wire-format 辅助函数、截断 |
| `src/bokaclaw_agent/agent/compression.py` | Token 预算压缩与滚动摘要 |
| `src/bokaclaw_agent/tools/` | `@tool`、JSON Schema 推导、分发 |
| `src/bokaclaw_agent/providers/` | `ModelProvider` 协议 + OpenAI 兼容适配器 |
| `src/bokaclaw_agent/sessions/` | Store 协议、内存存储、Postgres 存储 |
| `tests/` | 单元测试；不访问网络 |
