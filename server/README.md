# BokaClaw Self-Hosted Server

BokaClaw ships a FastAPI server plus a local Next.js dashboard. Durable agent memory lives in
workspace Markdown files; Postgres holds a rebuildable retrieval index plus sessions, runs,
traces, and evaluation data. The server is secure by default, supports dashboard login and API
keys, and exposes OpenAPI docs at `/docs`.

## Quick start

### Prerequisites

```bash
cd server
cp .env.example .env
# Edit .env — at minimum set POSTGRES_PASSWORD, JWT_SECRET, and a model API key
```

### Agent-first

Run one command; the terminal prints the admin email, password, and first API key.

```bash
cd server
make bootstrap
```

This starts the stack, waits for the API and dashboard, creates the first admin, and generates the
first API key.

> The generated credentials print once in the `=== Ready ===` block. Save the password and API key
> before closing the terminal — the API key cannot be recovered afterwards.

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
cd server && make down      # stop the stack
cd server && make clean     # stop and delete volumes, including memory workspaces
```

### Browser-first

```bash
cd server
make up
```

Then open `http://localhost:3000` and complete the setup wizard.

## Memory

### Workspace Markdown is authoritative

Each dashboard user gets a workspace under `AGENT_MEMORY_ROOT/users/<user_id>/`:

```
USER.md                # identity and durable preferences, always injected
MEMORY.md              # curated long-term facts
memory/YYYY-MM-DD.md   # episodic captures and research conclusions
```

An entry is a Markdown bullet plus an indented `<!-- memory: {...} -->` metadata comment. The files
are human-readable and diffable; `trust` is written by code and never parsed from Markdown.

### Postgres is a derived index

Alembic migration `014` creates:

| Table | Purpose |
|---|---|
| `memory_index_entries` | One row per memory bullet; hybrid search fields and the embedding |
| `memory_index_files` | File hash/mtime state used for incremental reconciliation |
| `memory_history` | ADD/UPDATE/DELETE audit trail, in the legacy row shape |
| `memory_embedding_cache` | `(model, dims, content_hash) -> embedding` |
| `memory_index_state` | Per-user dirty flag, last reconcile, re-embed flag |

The index can be dropped and rebuilt at any time:

```bash
docker exec bokaclaw-server python -m app.memory.cli reconcile --all
docker exec bokaclaw-server python -m app.memory.cli reindex --user <user_id>
```

### Writes

- After a completed turn, the server appends one filtered episode to `memory/YYYY-MM-DD.md`. This
  is deterministic — no extraction model is called. Noise and secrets are filtered/redacted, and
  turns that already used a memory write tool, came from a subagent, or were triggered by cron are
  skipped.
- Explicit `save_memory` writes a curated fact to `MEMORY.md` (or `USER.md` for identity). Research
  conclusions are written to the day file under `## Research`.
- The generic `Write`/`Edit` tools cannot modify memory files. Only the memory tools can.

### Reads

Before each turn the server builds a small always-on profile (`USER.md` plus curated
identity/configuration/rule entries) and runs a hybrid query search. Results are injected as a
user-role `<recalled-memories>` message, never into the system prompt, and are labeled as data.
`trust="untrusted"` content is stored and searchable but never auto-injected.

Relevance combines semantic similarity, keyword score, importance, and a recency half-life (90 days
for episodes, 365 for curated facts; identity/configuration/rules do not decay). The query layer is
capped at `AGENT_MEMORY_RECALL_TOKENS` / `AGENT_MEMORY_RECALL_MAX`, and the whole injection block is
capped at 3000 characters.

### Agent memory tools

`POST /agent` exposes five tools, all scoped to the signed-in user's UUID:

| Tool | Effect |
|---|---|
| `search_memories` | Hybrid search over the user's entries |
| `get_memory` | Read one entry by id |
| `save_memory` | Write a curated fact (`kind=identity` goes to `USER.md`) |
| `update_memory` | Change text, kind, or importance |
| `delete_memory` | Remove one entry |

Set `AGENT_MEMORY_REQUIRE_APPROVAL=true` to confirm `update_memory` and `delete_memory` through the
human-in-the-loop `ask_user` channel.

### Embeddings

Vectors are normalized and zero-padded to `halfvec(3072)`. Each row records `embedding_model` and
`embedding_dims`; search only uses vectors from the current model, and other rows stay keyword
searchable until a background re-embed completes. Changing the embedder through `/configure` marks
the index for re-embedding.

### Migration from Mem0

Existing Mem0 rows and the legacy SQLite history file can be imported once:

```bash
docker exec bokaclaw-server python scripts/migrate_mem0_to_workspace_memory.py --dry-run
docker exec bokaclaw-server python scripts/migrate_mem0_to_workspace_memory.py --verify
```

See [`docs/memory/migration.mdx`](../docs/memory/migration.mdx) for user mapping, backups, and
rollback.

## Security defaults

- Dashboard login uses JWTs.
- Programmatic access uses `X-API-Key`.
- Auth is enabled by default. `AUTH_DISABLED=true` exists for local development only.
- Memory workspaces are created `0o700` with `0o600` files, and `AGENT_MEMORY_ROOT` must not be
  inside `AGENT_WORKSPACE_ROOT` or mounted into the executor container.

## Forgotten password

```bash
cd server
make reset-admin-password EMAIL=admin@example.com PASSWORD='new-strong-password'
```

Anyone with shell access to the host already has full access to the database and secrets, so this
command does not expand the attack surface.

## Request log retention

`request_logs` is append-only. Prune it periodically:

```bash
cd server
make prune-logs                               # defaults to 30 days
make prune-logs REQUEST_LOG_RETENTION_DAYS=7
```

Wire the command into cron or a systemd timer in production. The `created_at` column uses a BRIN
index, so range deletes stay cheap on large tables.

## Local URLs

- Dashboard: `http://localhost:3000`
- API: `http://localhost:8888`
- OpenAPI docs: `http://localhost:8888/docs`

## Dashboard

- **Requests** — live audit log of API calls.
- **Memories** — browse, search, create, edit, and delete memory entries.
- **Agent** — chat with the agent runtime. Conversations and runs are stored in the app database.
- **Entities** — list every `user_id`, `agent_id`, and `run_id` with entry counts.
- **API Keys** — create, label, and revoke per-user keys.
- **Configuration** — runtime LLM and embedder override, persisted in the app database.
- **Settings** — account profile and password.

## Agent chat

`POST /agent` runs one agent turn for the signed-in user with the bundled
[`bokaclaw-agent`](../agent-loop/README.md) ReAct runtime, in-process. It reuses the configured LLM
(provider `openai` or `deepseek`); set `AGENT_MODEL` / `AGENT_API_KEY` / `AGENT_BASE_URL` to use
another OpenAI-compatible endpoint. Compression summaries can run on a separate, cheaper model via
`AGENT_COMPRESSION_MODEL`.

The Claude Code-style developer tools are registered by default: `Read`, `Write`, `Edit`,
`NotebookEdit`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, and the presentation tools `PptxBuild`,
`PptxUpdateSlide`, `PptxAddSlide`, and `PptxDeleteSlide`. `Bash`, `PowerShell`, and `Monitor` are
added only when `AGENT_TOOLS_ALLOW_SHELL=true`. `AGENT_OFFICE_TOOLS=false` removes the Pptx tools.

`WebFetch` caches every successful page for the duration of one run. Fetched page text is returned
inside `<<<BEGIN UNTRUSTED WEB CONTENT source=<url>>>>` … `<<<END UNTRUSTED WEB CONTENT>>>` markers.

File paths are resolved inside `AGENT_WORKSPACE_ROOT`, and `..` or symlink escapes are rejected.
Set `AGENT_EXECUTOR_URL` to run shell tools in the sandboxed executor container; leaving it empty
runs them inside the server process, where there is **no OS-level sandbox**.

Conversations are managed through `/conversations`. Set `AGENT_LOOP_URL` to delegate turns to an
external executor over HTTP instead.

```bash
curl -N -X POST http://localhost:8888/agent \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"message": "What did I save about the launch?", "stream": true}'
```

The stream emits `meta`, `message.delta`, `message.completed`, and `done`, with `error` before
`done` on failure. Requests are rate limited to 30/minute per IP.

### Memory and agent variables

| Variable | Default | Purpose |
|---|---|---|
| `MEMORY_BACKEND` | `markdown` | Workspace-Markdown backend; `mem0` is no longer supported. |
| `AGENT_MEMORY_ROOT` | `/app/memory` | Authoritative memory workspace root. |
| `AGENT_MEMORY_RECALL` | `true` | Enable per-turn profile and query recall. |
| `AGENT_MEMORY_RECALL_TOKENS` | `1500` | Query-recall token budget. |
| `AGENT_MEMORY_RECALL_MAX` | `15` | Maximum query-recall entries. |
| `AGENT_MEMORY_RECALL_THRESHOLD` | `0.4` | Minimum semantic similarity. |
| `AGENT_MEMORY_CAPTURE` | `true` | Append one filtered episode after a completed turn. |
| `AGENT_MEMORY_EPISODIC_RETENTION_DAYS` | `180` | Episode retention used by `prune`. |
| `AGENT_MEMORY_RECONCILE_ON_START` | `true` | Reconcile dirty workspaces at startup. |
| `AGENT_MEMORY_RECONCILE_INTERVAL_SECONDS` | `300` | Background reconcile interval. |
| `AGENT_MEMORY_REQUIRE_APPROVAL` | `false` | Ask before `update_memory` / `delete_memory`. |
| `AGENT_MODEL` | configured LLM | Model used for agent turns. |
| `AGENT_API_KEY` | configured provider key | Override the API key. |
| `AGENT_BASE_URL` | provider default | OpenAI-compatible base URL. |
| `AGENT_SYSTEM_PROMPT` | built-in prompt | Replace the system prompt. |
| `AGENT_MAX_ITERATIONS` | `10` | ReAct steps before the turn stops calling tools. |
| `AGENT_FINAL_ANSWER_ON_MAX_ITERATIONS` | `true` | On the step limit, make one final tool-free request. |
| `AGENT_TEMPERATURE` | `0.2` | Sampling temperature. |
| `AGENT_MAX_TOKENS` | provider default | Max tokens per model call. |
| `AGENT_REQUEST_TIMEOUT_SECONDS` | `60` | Timeout for one model call. |
| `AGENT_MAX_RETRIES` | `2` | Model client retries. |
| `AGENT_CONTEXT_MAX_TOKENS` | `32000` | Model-view budget; `0` / `off` disables compression. |
| `AGENT_CONTEXT_WINDOW` | `AGENT_CONTEXT_MAX_TOKENS` | Context meter denominator (display only). |
| `AGENT_CONTEXT_COMPRESSION` | `summary` | `summary` or `truncate`. |
| `AGENT_COMPRESSION_MODEL` | `AGENT_MODEL` | Model used only for compression summaries. |
| `AGENT_TOOL_RESULT_MAX_CHARS` | `16000` | Per-tool-result cap in the model view. |
| `AGENT_TOOL_CONCURRENCY` | `1` | Parallel read-only tool calls (1 disables). |
| `AGENT_USER_INPUT_ENABLED` | `true` | Register `ask_user`. |
| `AGENT_USER_INPUT_TIMEOUT_SECONDS` | `240` | How long one question waits for an answer. |
| `AGENT_FACT_LEDGER` | `true` | Keep a run-scoped fact ledger in the model view. |
| `AGENT_STEP_BUDGET_NOTICE` | `true` | Append `Step X of N` near the step limit. |
| `AGENT_PROGRESS_GUARD` | `true` | Detect no-progress tool calls and force an answer. |
| `AGENT_RESEARCH_MEMORY` | `true` | Save and reuse web-research conclusions. |
| `AGENT_RESEARCH_MEMORY_TTL_DAYS` | `30` | Ignore saved conclusions older than this. |
| `AGENT_RESEARCH_MEMORY_MIN_SCORE` | `0.5` | Minimum similarity for a saved conclusion. |
| `AGENT_SUBAGENTS_ENABLED` | `false` | Register the `explore` and `research_agent` subagents. |
| `AGENT_SUBAGENT_TIMEOUT_SECONDS` | `120` | Wall-clock timeout for one child run. |
| `AGENT_SUBAGENT_MAX_TURNS` | `8` | ReAct step budget for one child run. |
| `AGENT_DEV_TOOLS` | `true` | Register the developer tool set. |
| `AGENT_OFFICE_TOOLS` | `true` | Register the Pptx tools. |
| `AGENT_WORKSPACE_ROOT` | `/app/workspace` | File tools cannot escape this directory. |
| `AGENT_TOOLS_ALLOW_SHELL` | `false` | Register `Bash`, `PowerShell`, and `Monitor`. |
| `AGENT_EXECUTOR_URL` | — | Base URL of the sandboxed executor. |
| `AGENT_EXECUTOR_TOKEN` | — | Bearer token sent to the executor. |
| `AGENT_TOOL_TIMEOUT_SECONDS` | `120` | Maximum runtime for one command. |
| `AGENT_WEB_FETCH_TIMEOUT_SECONDS` | `30` | `WebFetch` request timeout. |
| `AGENT_WEB_FETCH_ALLOW_PRIVATE` | `false` | Allow fetches to localhost / private networks. |
| `AGENT_LOOP_URL` | — | External executor base URL, without `/invoke`. |
| `AGENT_LOOP_MOCK` | `false` | Stream a canned reply for local UI work. |
| `AGENT_LOOP_HISTORY_MESSAGES` | `40` | Prior turns forwarded as context. |
| `AGENT_LOOP_HISTORY_TOOL_TRACE` | `true` | Replay persisted tool calls and results into history. |

The full list, including subagent and span-capture settings, is in
[`.env.example`](.env.example).

### Reusable research memory

After a completed turn whose tool trace contains a successful `WebSearch` or `WebFetch`, the server
stores the final answer, deduplicated source URLs, the question, and the capture time as one
workspace entry (`kind="research_conclusion"`). An identical question updates that entry instead of
appending a duplicate.

Before a turn, the in-process runtime looks up an identical question by its stable key, then
searches by similarity for paraphrases. Matches older than `AGENT_RESEARCH_MEMORY_TTL_DAYS` or below
`AGENT_RESEARCH_MEMORY_MIN_SCORE` are dropped; the rest are added as a user-role message immediately
before the current turn. The block is view-only, never part of the system prompt, and memory
failures never fail the turn.

### Prompt-injection posture

Tool output is untrusted input. The server applies three mitigations:

- The base system prompt defines an instruction hierarchy: only the system prompt and the user's
  messages carry instructions. Tool output is data.
- `WebFetch` returns page text inside explicit untrusted-content markers and neutralizes anything
  that could forge the closing marker.
- Recalled memory and reused research conclusions are injected as a user-role message, labeled as
  reference data, and never into the system prompt. `trust="untrusted"` entries are never
  auto-injected.

These are mitigations, not a guarantee. There is no approval step before `Write`, `Edit`, or
`Bash`: the executor container limits what a hijacked model can reach, but not which of its granted
tools it may use. Keep credentials out of the agent workspace.

If no model can be resolved, `POST /agent` returns 503 `agent_not_configured`.

## Telemetry

Anonymous telemetry sends at most two events per install:

- `admin_registered` — when the first admin is created.
- `onboarding_completed` — when the setup wizard reaches its final success state.

Properties are limited to the email domain, server version, install UUID, and the optional
freeform use case. Set `BOKACLAW_TELEMETRY=false` to opt out.

## Security headers

The dashboard sets `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`,
`X-Content-Type-Options: nosniff`, and `Referrer-Policy: strict-origin-when-cross-origin` on every
path (see `dashboard/next.config.mjs`).

## Reference

Product documentation is in [`docs/`](../docs/README.md), including the memory format, operations,
and migration guides.

BokaClaw's early versions were built on Mem0. The runtime no longer depends on it; the original
Apache-2.0 license and attribution remain in `LICENSE`.
