# `server/.env`, volumes and configuration

`server/.env` is gitignored and excluded from Docker build contexts. The start
script sources it with `set -a; . server/.env; set +a` and also passes it to
containers with `--env-file`. Keep it shell-compatible: `KEY=value` lines, no
`export`, no inline comments after values, no spaces around `=`.

## Template

```bash
OPENAI_API_KEY=

# Postgres
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=postgres
POSTGRES_USER=postgres
POSTGRES_PASSWORD=            # required; generate with: openssl rand -hex 24
POSTGRES_COLLECTION_NAME=memories

# Authentication
ADMIN_API_KEY=                # generate with: openssl rand -hex 32
JWT_SECRET=                   # generate with: openssl rand -base64 48
AUTH_DISABLED=false
DASHBOARD_URL=http://localhost:3000
APP_DB_NAME=mem0_app

# Workspace-Markdown memory (authoritative) + rebuildable Postgres index
MEMORY_BACKEND=markdown
AGENT_MEMORY_ROOT=/app/memory
AGENT_MEMORY_RECALL_TOKENS=1500
AGENT_MEMORY_RECALL_MAX=15
AGENT_MEMORY_RECALL_THRESHOLD=0.4
AGENT_MEMORY_CAPTURE=true
AGENT_MEMORY_RECONCILE_ON_START=true
AGENT_MEMORY_RECONCILE_INTERVAL_SECONDS=300

# Local embedder
MEMORY_DEFAULT_LLM_MODEL=gpt-5-mini
MEMORY_DEFAULT_EMBEDDER_PROVIDER=fastembed
MEMORY_DEFAULT_EMBEDDER_MODEL=BAAI/bge-small-zh-v1.5
HF_ENDPOINT=https://hf-mirror.com
HF_HUB_DISABLE_XET=1
# start-stack.sh passes this to the container; the default /tmp path is lost on recreate.
FASTEMBED_CACHE_PATH=/app/history/fastembed

BOKACLAW_TELEMETRY=false
REQUEST_LOG_RETENTION_DAYS=30

# Agent
AGENT_LOOP_MOCK=true          # true = canned streamed reply, no model key needed
AGENT_LOOP_URL=               # optional external executor; empty = in-process runtime
AGENT_LOOP_CONNECT_TIMEOUT_SECONDS=5
AGENT_LOOP_READ_TIMEOUT_SECONDS=120
AGENT_LOOP_MAX_SECONDS=300
AGENT_LOOP_HISTORY_MESSAGES=40
AGENT_MAX_ITERATIONS=10
AGENT_DEV_TOOLS=true
AGENT_OFFICE_TOOLS=true      # PptxBuild / PptxAddSlide / PptxUpdateSlide / PptxDeleteSlide
AGENT_MCP_ENABLED=false      # remote MCP tools (Streamable HTTP)
# AGENT_MCP_CONFIG_FILE=/app/config/mcp.json
AGENT_SKILLS_ENABLED=false   # local SKILL.md instruction packs
AGENT_SKILLS_ROOT=/app/skills          # bind-mounted read-only from SKILLS_HOST
AGENT_SKILLS_ALLOW_SCRIPTS=false       # SkillRun; executes scripts in the executor only
# AGENT_SKILLS_USER_ROOT=/app/skills-users
# AGENT_SKILLS_WORKSPACE_DIR=skills
AGENT_WORKSPACE_ROOT=/app/workspace   # bind-mounted from WORKSPACE_HOST
AGENT_TOOLS_ALLOW_SHELL=false
AGENT_EXECUTOR_URL=http://bokaclaw-executor:8099   # appended by start-stack.sh
AGENT_EXECUTOR_TOKEN=<random>                      # appended by start-stack.sh
```

`start-stack.sh` creates `WORKSPACE_HOST` (default
`/Users/pankaibo/BokaClaw/workspace`) and mounts it at `AGENT_WORKSPACE_ROOT`.
Agent `Write` / `Edit` / `NotebookEdit` cannot escape that directory.

Agent Skills are read from `SKILLS_HOST` (default
`/Users/pankaibo/BokaClaw/skills`), mounted **read-only** at
`AGENT_SKILLS_ROOT`. Put one directory per skill, each with a `SKILL.md`.
Enable them with `AGENT_SKILLS_ENABLED=true`. Per-user skills can be placed
under a separate `AGENT_SKILLS_USER_ROOT`; workspace skills are off by default
because the agent can write inside the workspace. With
`AGENT_SKILLS_ALLOW_SCRIPTS=true`, `SkillRun` sends a bounded copy of the skill
to `bokaclaw-executor` and runs one `scripts/` entry point after approval; the
skill directory itself is never mounted into the executor.

With `AGENT_EXECUTOR_URL` set, `Bash` / `PowerShell` / `Monitor` run in
`bokaclaw-executor`, which mounts the same workspace read-write but has a
read-only root filesystem (so `/app` is readable and immutable), runs as
UID 1000 with all capabilities dropped, and is attached only to the internal
`bokaclaw-exec` network. Commands get a sanitised environment, so neither the
executor token nor the server's API keys are visible. Clear the URL to fall
back to running shell commands inside the server process (no sandbox).

MCP servers can be declared inline with `AGENT_MCP_SERVERS_JSON`, or in a JSON
file mounted read-only:

```bash
-v /path/to/mcp.json:/app/config/mcp.json:ro
AGENT_MCP_CONFIG_FILE=/app/config/mcp.json
```

MCP runs inside the server process, so it uses the server's egress and header
secrets. Do not mount `AGENT_MEMORY_ROOT` or provider credentials into an MCP
server; keep MCP server credentials in the server `.env` and interpolate them
with `${ENV_NAME}`.

When `POSTGRES_HOST` is set for a container run, it is overridden with
`-e POSTGRES_HOST=bokaclaw-postgres`; the value in the file is only a default
for compose-style setups.

## Manual container commands

`start-stack.sh` automates all of this. Use these commands to isolate a failing
step; run them from the repository root.

```bash
docker network create bokaclaw-net          # ignore "already exists"
docker network create --internal bokaclaw-exec   # server <-> executor only

docker run -d --name bokaclaw-postgres --network bokaclaw-net --restart unless-stopped \
  --env-file server/.env -p 8432:5432 \
  -v mem0-local-pg:/var/lib/postgresql/data \
  pgvector/pgvector:pg17 postgres -c listen_addresses='*'

# one-off migration (the image does NOT migrate on start)
docker run --rm --network bokaclaw-net --env-file server/.env \
  -e POSTGRES_HOST=bokaclaw-postgres -e POSTGRES_PORT=5432 \
  -v mem0-local-history:/app/history \
  bokaclaw-server:test alembic upgrade head

docker run -d --name bokaclaw-executor --network bokaclaw-exec --restart unless-stopped \
  --read-only --tmpfs /tmp:rw,size=256m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 256 --memory 1g --cpus 2 --user 1000:1000 \
  -e AGENT_DEV_TOOLS=true -e AGENT_TOOLS_ALLOW_SHELL=true \
  -e AGENT_WORKSPACE_ROOT=/app/workspace \
  -e AGENT_EXECUTOR_TOKEN="$AGENT_EXECUTOR_TOKEN" -e PYTHONDONTWRITEBYTECODE=1 \
  -v /Users/pankaibo/BokaClaw/workspace:/app/workspace \
  bokaclaw-server:test uvicorn app.agent.executor_app:app --host 0.0.0.0 --port 8099 --no-access-log

docker run -d --name bokaclaw-server --network bokaclaw-net --restart unless-stopped \
  --env-file server/.env -e POSTGRES_HOST=bokaclaw-postgres -e POSTGRES_PORT=5432 \
  -e AGENT_EXECUTOR_URL=http://bokaclaw-executor:8099 \
  -e AGENT_EXECUTOR_TOKEN="$AGENT_EXECUTOR_TOKEN" \
  -e FASTEMBED_CACHE_PATH=/app/history/fastembed \
  --add-host host.docker.internal:host-gateway \
  -p 8000:8000 -v mem0-local-history:/app/history \
  -v /Users/pankaibo/BokaClaw/memory:/app/memory \
  -v /Users/pankaibo/BokaClaw/workspace:/app/workspace \
  bokaclaw-server:test uvicorn app.main:app --host 0.0.0.0 --port 8000

# the server needs both bridges to reach the executor
docker network connect bokaclaw-exec bokaclaw-server

docker run -d --name bokaclaw-dashboard --network bokaclaw-net --restart unless-stopped \
  -e NEXT_PUBLIC_API_URL=http://localhost:8000 \
  -e API_INTERNAL_URL=http://bokaclaw-server:8000 \
  -e NEXT_PUBLIC_INSTANCE_NAME=BokaClaw \
  -p 3000:3000 bokaclaw-dashboard:test
```

The server command overrides the image `CMD`, which includes `--reload`; that
is useful in development but wastes CPU in a long-running local stack.

## Two databases, one cluster

| Variable | Database | Used for |
|---|---|---|
| `POSTGRES_DB` | `postgres` | Legacy Mem0 vector collection (`memories`), kept for the one-time migration; not written by the runtime |
| `APP_DB_NAME` | `mem0_app` | Alembic tables: `users`, `api_keys`, `settings`, `request_logs`, `conversations`, `conversation_messages`, and the derived memory index (`memory_index_*`, `memory_history`) |

Both live in the same Postgres instance and container. `server/app/core/db.py` builds
the SQLAlchemy URL from `APP_DB_NAME`; the workspace memory backend records its
index in that same application database.

## Volumes

| Volume | Container path | Holds |
|---|---|---|
| `mem0-local-pg` | `/var/lib/postgresql/data` | The whole PG 17 cluster (both databases, `pgvector` extension, memories) |
| `mem0-local-history` | `/app/history` | Legacy `history.db` (migration input only) and `fastembed/`, the ONNX embedder cache |
| `/Users/pankaibo/BokaClaw/memory` (host bind, `MEMORY_HOST`) | `/app/memory` | Authoritative workspace Markdown: `users/<user_id>/{USER.md,MEMORY.md,memory/YYYY-MM-DD.md}` |
| `/Users/pankaibo/BokaClaw/workspace` (host bind, `WORKSPACE_HOST`) | `AGENT_WORKSPACE_ROOT` (`/app/workspace`) | Files written by the agent's `Write` / `Edit` / `NotebookEdit` tools; survives container re-creation |
| `/Users/pankaibo/BokaClaw/skills` (host bind, `SKILLS_HOST`, **read-only**) | `AGENT_SKILLS_ROOT` (`/app/skills`) | Global Agent Skills: one directory per skill with a `SKILL.md`; mounted into the server only, never into the executor |

Memory is bind-mounted so the files are directly visible on macOS. It is mounted
into the server only, never into the executor. The old `mem0-local-memory`
volume may still exist after the switch; keep it as a rollback copy until the
host directory has been verified, then remove it explicitly.

`docker rm` does not delete volumes. `docker volume rm mem0-local-pg` **does**,
and it wipes the derived memory index, users, and conversations. Deleting the
host `MEMORY_HOST` directory deletes the authoritative Markdown files. Never do
either without an explicit request.

## pgvector dimensions

The workspace index uses a fixed `halfvec(3072)` column. Vectors from smaller
models (fastembed's `BAAI/bge-small-zh-v1.5` is **512**, OpenAI
`text-embedding-3-small` is **1536**) are normalized and zero-padded, so inserts
never fail on width. Each row records `embedding_model`; search only ranks rows
embedded by the current model, and the rest stay keyword-searchable until
`python -m app.memory.cli reindex` re-embeds them. Models wider than 3072 are
rejected.

## Why the start script repairs `pg_hba.conf`

The volume was initialised by the all-in-one image with:

```
host    all    all    127.0.0.1/32    scram-sha-256
```

Inside the all-in-one container the API and Postgres shared the network
namespace, so this was enough. With separate containers, the server's source
address is the bridge IP and every connection fails with:

```
FATAL: no pg_hba.conf entry for host "172.18.0.3", user "postgres",
database "mem0_app", no encryption
```

The script appends

```
host all all 0.0.0.0/0 scram-sha-256
host all all ::/0 scram-sha-256
```

and runs `SELECT pg_reload_conf()`. This is idempotent and only affects the
local volume.

## Why the start script resets the Postgres password

The original password lived only in the deleted all-in-one container's
environment. The cluster's `local` socket still trusts the `postgres` OS user,
so the script connects through `docker exec -u postgres psql` and runs
`ALTER USER postgres PASSWORD '<value from .env>'`. After that, `.env` is the
single source of truth and the server container can authenticate over TCP.

## Admin account

`users` is not seeded by migrations. While it is empty, the dashboard setup
flow / `POST /auth/register` creates the first admin. Once a user exists,
registration returns 403. Use `scripts/reset_admin_password.py` inside the
server container (see the parent `SKILL.md`) instead of deleting rows.
