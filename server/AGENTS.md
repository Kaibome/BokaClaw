# Self-hosted server (`server/`)

FastAPI REST server for BokaClaw. Docker only; there is no local non-Docker path.

## Commands

```bash
# Production image
make build        # docker build -t bokaclaw-server -f Dockerfile .. (repo-root context)
make run_local    # docker run -p 8000:8000 with .env

# Development stack (FastAPI + PostgreSQL/pgvector)
docker-compose up
```

| Service | Port |
|---------|------|
| BokaClaw API | 8888 |
| PostgreSQL (pgvector) | 8432 |
| Dashboard | 3000 |

## Memory backend

- **Markdown is authoritative.** Each user has a workspace under `AGENT_MEMORY_ROOT/users/<user_id>/`:
  `USER.md`, `MEMORY.md`, and `memory/YYYY-MM-DD.md`. Only the memory tools may write these files.
- **Postgres is a derived index.** The `memory_index_*` / `memory_history` tables are created by
  Alembic migration `014`; they can be dropped and rebuilt from the files with
  `python -m app.memory.cli reconcile --all`.
- **`MEMORY_BACKEND`** defaults to `markdown`. The legacy `mem0` value is no longer supported.
- **Recall** is a per-turn profile + query search; **capture** appends one filtered episode after a
  completed turn and never calls an LLM. Consolidation is a P2 feature.
- `AGENT_MEMORY_ROOT` must not be inside `AGENT_WORKSPACE_ROOT`, and it is never mounted into the
  executor container.

## Conventions

- **Framework:** FastAPI on uvicorn, auto-reload in dev.
- **Stores:** PostgreSQL with the `vector` and `pg_trgm` extensions.
- **Hot reload:** the dev Dockerfile mounts `server/`, so edits take effect without a rebuild.
- Use Docker Compose for local work. Do not add a "run it with uvicorn directly" path.
- `POST /agent` runs [`../agent-loop/`](../agent-loop/AGENTS.md) (`bokaclaw-agent`) in-process. Every image installs it, so the production build context is the repository root; see [`../agent-loop/AGENTS.md`](../agent-loop/AGENTS.md) for its lint/test rules.
- Developer tools live in `app/agent/dev_tools.py`. Any filesystem tool must resolve paths through `_resolve()` (workspace confinement); never read or write a path directly from model input. Shell tools stay behind `AGENT_TOOLS_ALLOW_SHELL`.
- Shell commands are delegated to `app/agent/executor_app.py` when `AGENT_EXECUTOR_URL` is set; that service runs in a separate container with a read-only root filesystem, a non-root user, no external network, and only the workspace mounted read-write. Keep new shell entry points on `execute_command` / `execute_monitor` so they inherit that isolation, and never pass the server's environment to a child process.

Never commit `.env`. Credentials for the compose services belong in `.env.example` as placeholders only.
