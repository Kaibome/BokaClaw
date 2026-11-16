# 自托管服务端（`server/`）

BokaClaw 的 FastAPI REST 服务端。仅支持 Docker；不存在非 Docker 的本地运行方式。

## 命令

```bash
# 生产镜像
make build        # docker build -t bokaclaw-server -f Dockerfile .. (repo-root context)
make run_local    # docker run -p 8000:8000 with .env

# 开发环境（FastAPI + PostgreSQL/pgvector）
docker-compose up
```

| 服务 | 端口 |
|---------|------|
| BokaClaw API | 8888 |
| PostgreSQL (pgvector) | 8432 |
| Dashboard | 3000 |

## 记忆后端

- **Markdown 是权威来源。** 每个用户在 `AGENT_MEMORY_ROOT/users/<user_id>/` 下有一个工作区：
  `USER.md`、`MEMORY.md` 和 `memory/YYYY-MM-DD.md`。只有记忆工具可以写这些文件。
- **Postgres 是派生索引。** `memory_index_*` / `memory_history` 表由 Alembic 迁移 `014` 创建；
  可用 `python -m app.memory.cli reconcile --all` 丢弃并从文件重建。
- **`MEMORY_BACKEND`** 默认为 `markdown`。旧的 `mem0` 取值已不再支持。
- **回忆（Recall）** 是每轮一次的 profile + 查询检索；**捕获（Capture）** 在一轮对话完成后追加
  一条经过过滤的 episode，且不会调用 LLM。整合（Consolidation）属于 P2 功能。
- `AGENT_MEMORY_ROOT` 不得位于 `AGENT_WORKSPACE_ROOT` 内部，也永远不会挂载进 executor 容器。

## 约定

- **框架：** FastAPI + uvicorn，开发时自动重载。
- **存储：** PostgreSQL，启用 `vector` 和 `pg_trgm` 扩展。
- **热重载：** 开发版 Dockerfile 会挂载 `server/`，因此修改后无需重新构建即可生效。
- 本地开发使用 Docker Compose。不要新增“直接用 uvicorn 运行”的路径。
- `POST /agent` 在进程内运行 [`../agent-loop/`](../agent-loop/AGENTS.md)（`bokaclaw-agent`）。
  所有镜像都会安装它，因此生产构建上下文是仓库根目录；其 lint/测试规则参见
  [`../agent-loop/AGENTS.md`](../agent-loop/AGENTS.md)。
- 开发者工具位于 `app/agent/dev_tools.py`。任何文件系统工具都必须通过 `_resolve()`（工作区限定）
  解析路径；绝不能直接用模型输入的路径读写文件。Shell 工具始终受 `AGENT_TOOLS_ALLOW_SHELL`
  控制。
- 当设置了 `AGENT_EXECUTOR_URL` 时，Shell 命令会委托给 `app/agent/executor_app.py`；该服务运行在
  独立容器中，根文件系统只读、以非 root 用户运行、无外部网络，且只以读写方式挂载工作区。新的
  Shell 入口应继续走 `execute_command` / `execute_monitor`，以继承这套隔离；并且绝不把服务端的
  环境变量传给子进程。

绝不要提交 `.env`。compose 服务的凭据只能以占位符形式放在 `.env.example` 中。
