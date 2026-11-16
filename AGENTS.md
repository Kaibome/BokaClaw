# AGENTS.md

BokaClaw 是一个自托管的 AI Agent 工作台，由 Next.js 控制台、FastAPI 服务端和 `bokaclaw-agent`
ReAct 运行时组成。持久的 Agent 记忆存放在工作区的 Markdown 文件中；Postgres 保存可重建的检索
索引，以及会话、运行、trace 和评测数据。

这是一个多语言混编的仓库。运行命令前，先阅读离你要编辑的文件最近的 `AGENTS.md`；每个包都有自己
的工具链。

## 包

| 路径 | 内容 | 工具链 |
|---|---|---|
| `server/` | FastAPI API、Agent 执行器、记忆后端、链路追踪、评测 worker | 仅 Docker；`server/requirements.txt`；ruff 120 |
| `agent-loop/` | `bokaclaw-agent` Python SDK：ReAct 循环、工具、子 Agent、上下文压缩 | hatch；ruff 120；pytest |
| `dashboard/` | Next.js 15 控制台 | pnpm；Biome/TypeScript |
| `deepeval/` | 供 `server/app/eval/` 使用的内嵌 DeepEval 源码 | 参见其自身文件 |
| `docs/` | Mintlify 文档 | 参见 [`docs/AGENTS.md`](docs/AGENTS.md) |

## 记忆架构

- **工作区 Markdown 是权威来源。** `AGENT_MEMORY_ROOT/users/<user_id>/` 下存放 `USER.md`、
  `MEMORY.md` 和 `memory/YYYY-MM-DD.md`。
- **Postgres 是派生索引。** `memory_index_entries`、`memory_index_files`、`memory_history` 和
  `memory_index_state` 由 Alembic 迁移 `014` 创建。可用
  `python -m app.memory.cli reconcile --all` 删除并重建。
- **写入：** 每完成一轮对话，追加一条经过过滤的 episode；显式调用 `save_memory` 则写入经过整理的
  事实。只有记忆工具可以修改记忆文件 —— 绝不能使用通用的 `Write`/`Edit` 工具。
- **读取：** 一小段常驻 profile，加上一次混合查询检索，以 user 角色的 `<recalled-memories>` 块
  注入，绝不写入系统提示词。
- **信任级别：** `owner` / `agent` / `tool` / `untrusted` / `system` 由代码写入，绝不从
  Markdown 中解析。`untrusted` 内容永远不会被自动注入。
- 记忆代码位于 `server/app/memory/`；Agent 钩子位于 `server/app/agent/runtime.py`、`runner.py`
  和 `turn_memory.py`。

## 命令

```bash
# 服务端
make server-build
make server-test                 # server/tests + 根目录 tests/
cd server && ruff check app tests

# Agent 运行时
cd agent-loop && hatch run test
cd agent-loop && hatch run lint

# 控制台
cd dashboard && pnpm install && pnpm build

# 本地整套环境（Postgres + server + dashboard + executor）
bash .claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh --recreate

# 记忆维护
docker exec bokaclaw-server python -m app.memory.cli reconcile --all
docker exec bokaclaw-server python scripts/migrate_mem0_to_workspace_memory.py --dry-run
```

## 禁止事项

- 提交 `.env` 文件、API key 或凭据。
- 未经维护者批准，修改 `.github/workflows/`。
- 将 `AGENT_MEMORY_ROOT` 放在 `AGENT_WORKSPACE_ROOT` 内部，或把它挂载进 executor 容器。
- 在未更新 `server/requirements.txt` 并补充文档的情况下，为服务端新增依赖。
- 在未同步更新 `docs/` 和 dashboard 调用方的情况下，变更公开的 REST 契约。
- 在 `dashboard/` 中使用 npm 或 yarn；它只允许 pnpm。
- 在 TypeScript 中使用 `require()`；只允许 ES 模块导入。

## 约定

- Python：Pydantic v2，文件名 `snake_case.py`，ruff 行宽 **120**，测试命名为 `test_<module>.py`。
- TypeScript：strict 模式，pnpm，在包配置了 Biome 的地方使用 Biome。
- 提交：Conventional Commits（`feat:`、`fix:`、`docs:`、`refactor:`、`test:`）。
- Bug 修复需附带一个在未修复时会失败的回归测试。面向用户的功能要更新 `docs/`，并在 `docs.json`
  和 `docs/llms.txt` 中添加条目。

## 归属声明

BokaClaw 的早期版本基于 Mem0 构建。运行时已不再依赖 Mem0；原始的 Apache-2.0 许可证与归属声明
保留在 `LICENSE` 中。
