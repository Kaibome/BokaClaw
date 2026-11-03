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

# Local embedder (required when reusing a 512-dim pgvector volume)
MEM0_DEFAULT_LLM_MODEL=gpt-5-mini
MEM0_DEFAULT_EMBEDDER_PROVIDER=fastembed
MEM0_DEFAULT_EMBEDDER_MODEL=BAAI/bge-small-zh-v1.5
MEM0_DEFAULT_EMBEDDING_DIMS=512
HF_ENDPOINT=https://hf-mirror.com
HF_HUB_DISABLE_XET=1

MEM0_TELEMETRY=false
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
AGENT_WORKSPACE_ROOT=/app/workspace
AGENT_TOOLS_ALLOW_SHELL=false
```

When `POSTGRES_HOST` is set for a container run, it is overridden with
`-e POSTGRES_HOST=bokaclaw-postgres`; the value in the file is only a default
for compose-style setups.

## Two databases, one cluster

| Variable | Database | Used for |
|---|---|---|
| `POSTGRES_DB` | `postgres` | mem0's vector collection (`memories`) and the `vector` extension |
| `APP_DB_NAME` | `mem0_app` | Alembic tables: `users`, `api_keys`, `settings`, `request_logs`, `conversations`, `conversation_messages` |

Both live in the same Postgres instance and container. `server/db.py` builds
the SQLAlchemy URL from `APP_DB_NAME`; `server/main.py` builds the mem0 config
from `POSTGRES_DB`.

## Volumes

| Volume | Container path | Holds |
|---|---|---|
| `mem0-local-pg` | `/var/lib/postgresql/data` | The whole PG 17 cluster (both databases, `pgvector` extension, memories) |
| `mem0-local-history` | `/app/history` | `history.db`, the mem0 message history SQLite file |

`docker rm` does not delete volumes. `docker volume rm mem0-local-pg` **does**,
and it wipes memories, users and conversations. Never run it without an
explicit request.

## pgvector dimensions

pgvector stores the vector width on the column when the collection is created.
`BAAI/bge-small-zh-v1.5` (fastembed) is **512**; OpenAI
`text-embedding-3-small` defaults to **1536**. Mixing them against an existing
collection fails at insert/search time. Changing the embedder means dropping
and rebuilding the collection, not just restarting the server.

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
