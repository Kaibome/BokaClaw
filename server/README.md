# BokaClaw Self-Hosted Server

Mem0 ships a self-hosted FastAPI server plus a local dashboard. It is secure by default, supports dashboard login and API keys, and exposes OpenAPI docs at `/docs`.

> **Upgrading?** The Postgres image changed from the archived `ankane/pgvector:v0.5.1`
> to the official `pgvector/pgvector:pg17`, and `POSTGRES_PASSWORD` is now a required
> env var. If you have an existing install, see
> [Migrating from ankane/pgvector to pgvector/pgvector](#migrating-from-ankanepgvector-to-pgvectorpgvector)
> before running `docker compose up`.

## Quick Start

### Prerequisites

Copy the example env file and set a Postgres password (required):

```bash
cd server
cp .env.example .env
# Edit .env — at minimum set POSTGRES_PASSWORD and OPENAI_API_KEY
```

### Agent-first

Run one command; the terminal prints the admin email, password, and first API key.

```bash
cd server
make bootstrap
```

This starts the stack, waits for the API and dashboard to be ready, creates the first admin, and generates the first API key.

> The generated credentials print once in the `=== Ready ===` block. Save the password and API key before closing the terminal — the API key cannot be recovered afterwards.

> `make bootstrap` skips the setup wizard, so the use-case → custom-instructions step doesn't run. To add custom instructions afterwards, `POST /configure` with `{"custom_instructions": "..."}`, or run the Browser-first flow on a fresh install.

You can override the generated credentials:

```bash
cd server
make bootstrap EMAIL=admin@company.com PASSWORD='strong-password' NAME='Admin'
```

For machine-readable output:

```bash
cd server
OUTPUT=json make seed
```

Teardown:

```bash
# Stop the stack
cd server && make down

# Wipe all data (including the Postgres volume)
cd server && make clean
```

### Browser-first

Start the stack and finish setup by walking through the wizard in your browser.

```bash
cd server
make up
```

Then open `http://localhost:3000` and complete the setup wizard.

## Security Defaults

- Dashboard login uses JWTs.
- Programmatic access uses `X-API-Key`.
- Auth is enabled by default.
- `AUTH_DISABLED=true` exists for local development only and should not be used in production.

## Forgotten password

Reset an admin password from the host while the stack is running:

```bash
cd server
make reset-admin-password EMAIL=admin@example.com PASSWORD='new-strong-password'
```

This is the supported recovery path. Anyone with shell access to the host already has full access to the database and secrets, so this command does not expand the attack surface.

## Request log retention

The `request_logs` table is append-only and grows with traffic (~864k rows/day at 10 req/s). Prune it periodically:

```bash
cd server
make prune-logs                               # defaults to 30 days
make prune-logs REQUEST_LOG_RETENTION_DAYS=7  # shorter window
```

Wire the command into cron or a systemd timer in production. The `created_at` column uses a BRIN index, so range deletes stay cheap even on large tables.

## Local URLs

- Dashboard: `http://localhost:3000`
- API: `http://localhost:8888`
- OpenAPI docs: `http://localhost:8888/docs`

## Dashboard

Once logged in, the dashboard exposes:

- **Requests** — live audit log of API calls (method, path, status, latency).
- **Memories** — browse memories, filter by user ID.
- **Agent** — chat with the agent runtime. Conversations and messages are stored in the app database.
- **Entities** — list every `user_id`, `agent_id`, and `run_id` that owns memories, with counts. Delete an entity to cascade-delete its memories.
- **API Keys** — create, label, and revoke per-user keys.
- **Configuration** — runtime LLM and embedder override. Changes persist to the app database and reapply on restart, layered over the values from your `.env`.
- **Settings** — account profile and password.

## Agent chat

`POST /agent` runs one agent turn for the signed-in user with the bundled
[`bokaclaw-agent`](../agent-loop/README.md) ReAct runtime, in-process. The
runtime reuses the LLM configured for mem0 (provider `openai` or `deepseek`),
so `OPENAI_API_KEY` or `DEEPSEEK_API_KEY` is enough; set `AGENT_MODEL` /
`AGENT_API_KEY` / `AGENT_BASE_URL` to use another OpenAI-compatible endpoint.
Two memory tools are exposed to the model, both scoped to the signed-in user's
UUID: `search_memories` and `save_memory`. The Claude Code-style developer
tools are also registered by default: `Read`, `Write`, `Edit`, `NotebookEdit`,
`Glob`, `Grep`, and `WebFetch`. `Bash`, `PowerShell`, and `Monitor` are added
only when `AGENT_TOOLS_ALLOW_SHELL=true`.

File paths are resolved inside `AGENT_WORKSPACE_ROOT`, and `..` or symlink
escapes are rejected. Shell tools are best run in the bundled executor
container. Set `AGENT_EXECUTOR_URL` (for the local stack:
`http://bokaclaw-executor:8099`) and `Bash`, `PowerShell`, and `Monitor` are
delegated to `server/app/agent/executor_app.py` there. That container mounts only the
workspace read-write at `AGENT_WORKSPACE_ROOT`, uses a read-only root
filesystem (so `/app`, the bundled source, is readable but immutable), runs as
UID 1000 with all capabilities dropped, and sits on an internal Docker network
with no route to the internet or the other containers. Commands get a
sanitised environment, so API keys and database credentials are not visible.

Leaving `AGENT_EXECUTOR_URL` empty keeps the old behaviour: commands run inside
the server process, where there is **no OS-level sandbox** and the shell can
reach `.env` and database credentials. `AGENT_DEV_TOOLS=false` removes the
whole developer tool set.

The server stores both sides of the conversation and is the only writer; the
browser never talks to the model directly. Conversations are managed through
`/conversations` (list, create, rename, delete, messages). Set `AGENT_LOOP_URL`
to delegate the turn to an external executor over HTTP instead.

```bash
curl -N -X POST http://localhost:8888/agent \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"message": "What did I save about the launch?", "stream": true}'
```

The stream emits `meta`, `message.delta`, `message.completed`, and `done`,
with `error` before `done` on failure. Omit `"stream": true` to get a single
JSON response instead. Requests are rate limited to 30/minute per IP.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_MODEL` | mem0's LLM model | Override the model used for agent turns. |
| `AGENT_API_KEY` | mem0's provider key | Override the API key. |
| `AGENT_BASE_URL` | provider default | OpenAI-compatible base URL (Qwen, vLLM, Ollama, OpenRouter, ...). |
| `AGENT_SYSTEM_PROMPT` | built-in prompt | Replace the system prompt. |
| `AGENT_MAX_ITERATIONS` | `10` | ReAct steps before the turn fails. |
| `AGENT_TEMPERATURE` | `0.2` | Sampling temperature. |
| `AGENT_MAX_TOKENS` | provider default | Max tokens per model call. |
| `AGENT_REQUEST_TIMEOUT_SECONDS` | `60` | Timeout for one model call. |
| `AGENT_MAX_RETRIES` | `2` | Model client retries. |
| `AGENT_CONTEXT_MAX_TOKENS` | `32000` | Token budget for the in-process runtime's model view; `0` / `off` disables compression. |
| `AGENT_CONTEXT_KEEP_RECENT_TOKENS` | half the budget | Recent tokens kept verbatim by compression. |
| `AGENT_CONTEXT_COMPRESSION` | `summary` | `summary` (model summary, truncation fallback) or `truncate` (drop only). |
| `AGENT_TOOL_RESULT_MAX_CHARS` | `16000` | Per-tool-result cap in the model view; full content is still stored. `0` / `off` disables it. |
| `AGENT_DEV_TOOLS` | `true` | Register the developer tool set. |
| `AGENT_WORKSPACE_ROOT` | `/app/workspace` | File tools cannot escape this directory. Created on first use. |
| `AGENT_TOOLS_ALLOW_SHELL` | `false` | Register `Bash`, `PowerShell`, and `Monitor`. |
| `AGENT_EXECUTOR_URL` | — | Base URL of the sandboxed executor; empty runs shell tools in-process. |
| `AGENT_EXECUTOR_TOKEN` | — | Bearer token sent to the executor. |
| `AGENT_EXECUTOR_TIMEOUT_SECONDS` | `120` | HTTP timeout for executor requests. |
| `AGENT_TOOL_TIMEOUT_SECONDS` | `120` | Maximum runtime for one command. |
| `AGENT_TOOL_MAX_OUTPUT_BYTES` | `200000` | Output cap per command or fetch. |
| `AGENT_TOOL_MAX_FILE_BYTES` | `2000000` | Largest file the file tools will read or write. |
| `AGENT_WEB_FETCH_TIMEOUT_SECONDS` | `30` | `WebFetch` request timeout. |
| `AGENT_WEB_FETCH_MAX_BYTES` | `1000000` | `WebFetch` download cap. |
| `AGENT_WEB_FETCH_ALLOW_PRIVATE` | `false` | Allow `WebFetch` to reach localhost / private networks. |
| `AGENT_LOOP_URL` | — | External executor base URL, without `/invoke`. Takes precedence over the in-process runtime. |
| `AGENT_LOOP_API_KEY` | — | Sent as `Authorization: Bearer ...` to the external executor. |
| `AGENT_LOOP_MOCK` | `false` | Stream a canned reply for local UI work. |
| `AGENT_LOOP_CONNECT_TIMEOUT_SECONDS` | `5` | Executor connect timeout. |
| `AGENT_LOOP_READ_TIMEOUT_SECONDS` | `120` | Executor read timeout. |
| `AGENT_LOOP_MAX_SECONDS` | `300` | Total wall clock for one turn. |
| `AGENT_LOOP_HISTORY_MESSAGES` | `40` | Prior turns forwarded as context. |

The `AGENT_CONTEXT_*` / `AGENT_TOOL_RESULT_*` variables apply only to the
in-process runtime. A remote executor set through `AGENT_LOOP_URL` uses its own
configuration.

If no model can be resolved, `POST /agent` returns 503 `agent_not_configured`;
set `AGENT_MODEL` (plus `AGENT_BASE_URL` for non-OpenAI providers). From inside
the compose network, an external executor running on the host is reachable at
`http://host.docker.internal:<port>`. The external executor contract is
documented in [`../agent-loop/README.md`](../agent-loop/README.md).

## Telemetry

Enabled by default, matching the Mem0 OSS library. Sends at most two events per install to the same anonymous PostHog project the library uses:

- `admin_registered` — fired when the first admin is created (wizard or direct API call). Properties: email domain, server version, install UUID.
- `onboarding_completed` — fired when the setup wizard reaches its final success state. Carries the same properties plus the freeform `use_case` the operator entered. API-only bootstraps never emit this event.

Set `MEM0_TELEMETRY=false` to opt out.

## Security headers

The dashboard sets the following response headers on every path (see `dashboard/next.config.mjs`):

- `X-Frame-Options: DENY`
- `Content-Security-Policy: frame-ancestors 'none'`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`

Together these prevent iframe embedding, sniffing of mislabelled MIME types, and cross-origin referrer leaks. Harden further behind your own reverse proxy if needed.

## Migrating from `ankane/pgvector` to `pgvector/pgvector`

The `ankane/pgvector` Docker image is archived and no longer maintained. This release
replaces it with the official `pgvector/pgvector:pg17` image (PostgreSQL 17, pgvector 0.8.0).

**What changed:**

| | Before | After |
|---|---|---|
| Docker image | `ankane/pgvector:v0.5.1` | `pgvector/pgvector:pg17` |
| PostgreSQL version | 15 | 17 |
| pgvector version | 0.5.1 | 0.8.0 |
| Credentials | Hardcoded `postgres`/`postgres` | Driven by `POSTGRES_USER` / `POSTGRES_PASSWORD` env vars |

### Fresh installs (no existing data)

No migration needed. Copy `.env.example` to `.env`, set `POSTGRES_PASSWORD`, and run:

```bash
cd server
make up
```

### Existing installs (preserving data)

PostgreSQL 17 cannot read data files written by PostgreSQL 15 directly.
You must export your data first, then import it into the new container.

**1. Export your data from the old container**

With the old stack still running:

```bash
cd server

# Dump all databases (mem0 memories + mem0_app auth/config data)
docker compose exec -T postgres pg_dumpall -U postgres > mem0_backup.sql
```

Verify the dump file is non-empty:

```bash
ls -lh mem0_backup.sql
```

**2. Stop the old stack and remove the old volume**

```bash
# Stop containers
docker compose down

# Remove the old Postgres data volume
docker compose down -v
```

> **Warning:** `docker compose down -v` deletes the `postgres_db` volume permanently.
> Only run this after you have verified your backup.

**3. Update your `.env`**

The Postgres credentials are no longer hardcoded in `docker-compose.yaml`.
Add them to your `.env` file (or verify they match your old setup):

```bash
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=postgres
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<your-password>    # required — compose will refuse to start without it
POSTGRES_COLLECTION_NAME=memories
```

If you previously relied on the hardcoded defaults (`postgres`/`postgres`), set
`POSTGRES_PASSWORD=postgres` to keep the same credentials.

**4. Start only Postgres**

Start **only** the Postgres container first — do not start the mem0 API yet.
The API runs `alembic upgrade head` on startup, which creates empty tables that
would conflict with the restore.

```bash
docker compose up -d postgres
```

Wait for the Postgres healthcheck to pass:

```bash
docker compose exec -T postgres pg_isready -q && echo "ready" || echo "not ready"
```

**5. Restore your data**

```bash
docker compose exec -T postgres psql -U postgres < mem0_backup.sql
```

You may see notices like `role "postgres" already exists` — these are harmless.

> **Important:** You must restore before starting the mem0 API container. The API
> runs database migrations on startup which create empty tables — restoring after
> that would fail with duplicate-key errors and lose your API keys and settings.

**6. Start the API**

Now start the mem0 API container. Alembic will detect the existing tables and
only apply any new migrations:

```bash
docker compose up -d mem0
```

**7. Verify**

```bash
# Check the API is healthy
make health

# Confirm your memories are present
curl -s http://localhost:8888/memories?user_id=<your-user-id> -H "X-API-Key: <your-api-key>"
```

### Rollback

If you need to revert, restore the old image tag in `docker-compose.yaml`:

```yaml
postgres:
    image: ankane/pgvector:v0.5.1
```

Then `docker compose down -v`, `docker compose up -d --build`, and restore from
`mem0_backup.sql` into the old container the same way.

## Reference

Additional product and API documentation lives at [docs.mem0.ai](https://docs.mem0.ai/open-source/overview).
