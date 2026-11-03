# Self-hosted server (`server/`)

FastAPI REST server wrapping the Python SDK. Docker only; there is no local non-Docker path.

## Commands

```bash
# Production image
make build        # docker build -t mem0-api-server -f Dockerfile .. (repo-root context)
make run_local    # docker run -p 8000:8000 with .env

# Development stack (FastAPI + PostgreSQL/pgvector + Neo4j)
docker-compose up
```

| Service | Port |
|---------|------|
| mem0 API | 8888 |
| PostgreSQL (pgvector) | 8432 |
| Neo4j HTTP | 8474 |
| Neo4j Bolt | 8687 |

## Conventions

- **Framework:** FastAPI on uvicorn, auto-reload in dev.
- **Stores:** PostgreSQL with the pgvector extension, Neo4j 5.x with the APOC plugin.
- **Hot reload:** the dev Dockerfile mounts both `server/` and `mem0/`, so SDK edits take effect without a rebuild.
- Use Docker Compose for local work. Do not add a "run it with uvicorn directly" path.
- The server imports the Python SDK from the repo, so its conventions apply to any SDK code you touch: see [`../mem0/AGENTS.md`](../mem0/AGENTS.md).
- `POST /agent` runs [`../agent-loop/`](../agent-loop/AGENTS.md) (`bokaclaw-agent`) in-process. Every image installs it, so the production build context is the repository root; see [`../agent-loop/AGENTS.md`](../agent-loop/AGENTS.md) for its lint/test rules.
- Developer tools live in `agent_dev_tools.py`. Any filesystem tool must resolve paths through `_resolve()` (workspace confinement); never read or write a path directly from model input. Shell tools stay behind `AGENT_TOOLS_ALLOW_SHELL`.

Never commit `.env`. Credentials for the compose services belong in `.env.example` as placeholders only.
