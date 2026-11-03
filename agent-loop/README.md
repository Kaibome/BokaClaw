# bokaclaw-agent

A minimal **ReAct agent runtime** as a Python SDK:

- Sends messages and tool definitions to an OpenAI-compatible model.
- Executes registered tools from the model's `tool_calls`, feeds results back, and loops until a final answer.
- Keeps the conversation in memory during a run and persists it to Postgres (`sessions` + `messages`) when the run ends.
- Uses the same Postgres database as mem0 by default, with a `bokaclaw_agent_` table prefix so it never collides with `memories`.

## Install

```bash
pip install bokaclaw-agent          # in-memory sessions only
pip install "bokaclaw-agent[postgres]"   # adds psycopg3 + psycopg-pool
```

## Quick start

```python
import os

from bokaclaw_agent import (
    AgentConfig,
    AgentRuntime,
    ModelConfig,
    PostgresConfig,
    PostgresSessionStore,
    tool,
)


@tool
def get_weather(city: str) -> str:
    """Return the current weather for a city.

    Args:
        city: The city to look up.
    """
    return f"{city}: sunny, 25C"


runtime = AgentRuntime(
    AgentConfig(
        name="weather-agent",
        system_prompt="Use tools when they help. Answer concisely.",
        model=ModelConfig(
            model="deepseek-chat",
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com/v1",
        ),
    ),
    tools=[get_weather],
    store=PostgresSessionStore(PostgresConfig.from_env()),
)

result = runtime.run("What's the weather in Beijing?", session_id="user-42")
print(result.content)
runtime.close()
```

For OpenAI, omit `base_url` (or set `OPENAI_BASE_URL`). Any endpoint that speaks the OpenAI Chat Completions API works.

## Tools

`@tool` derives a JSON Schema from type hints and the docstring:

```python
from typing import Literal
from pydantic import BaseModel


class Address(BaseModel):
    city: str
    zip_code: str


@tool
def create_order(
    address: Address,
    priority: Literal["normal", "express"] = "normal",
    note: str | None = None,
) -> dict:
    """Create an order.

    Args:
        address: Delivery address.
        priority: Shipping priority.
        note: Optional delivery note.
    """
    return {"id": "ord_123", "priority": priority, "address": address.model_dump()}
```

Supported annotations include primitives, `Optional`, `Literal`, `Enum`, `list`, `dict`, and Pydantic models. Parameters are validated with a generated Pydantic model before the function runs.

Tool failures are returned to the model as `{"error": ...}` tool messages by default, so the agent can recover. Set `AgentConfig(tool_error_mode="raise")` to surface them instead. Unknown tools, invalid JSON arguments, and validation failures are handled the same way.

Async tools are supported:

```python
@tool
async def fetch_price(sku: str) -> float:
    """Fetch a price."""
    ...
```

Use `await runtime.arun(...)` when calling from an async framework. The synchronous `run()` also handles async tools when it is not already inside an event loop.

## Sessions and resume

Pass the same `session_id` to resume a conversation:

```python
runtime.run("My order number is ord_123.", session_id="user-42")
runtime.run("Where is it?", session_id="user-42")   # previous turns are loaded automatically
```

During a run the context lives in memory. At the end, one transaction:

1. upserts the session row (`bokaclaw_agent_sessions`),
2. assigns increasing `seq` values to the new messages,
3. inserts them into `bokaclaw_agent_messages`.

On success the session status is `completed`. If the model fails, a tool raises in `raise` mode, or `max_iterations` is reached, the partial context is still written with status `failed` (and an `error` text), then the original exception is raised. Unanswered tool calls get a synthetic `{"error": "run aborted: ..."}` tool message so the stored conversation remains valid for a later resume.

`AgentConfig.max_history_messages` limits how many recent messages are sent to the model on resume. It never changes what is persisted, and truncation only starts at a user turn or a completed assistant turn, so tool calls are never separated from their results.

Use `InMemorySessionStore` when you do not want a database:

```python
from bokaclaw_agent import InMemorySessionStore

runtime = AgentRuntime(config, provider=provider, tools=[get_weather], store=InMemorySessionStore())
```

## Postgres configuration

`PostgresConfig.from_env()` reads the same variables as the BokaClaw server:

| Variable | Default |
|---|---|
| `BOKACLAW_AGENT_POSTGRES_DSN` / `POSTGRES_DSN` | unset; when set it overrides the fields below |
| `POSTGRES_HOST` | `postgres` |
| `POSTGRES_PORT` | `5432` |
| `POSTGRES_DB` | `postgres` (the database that holds mem0's `memories` table) |
| `POSTGRES_USER` | `postgres` |
| `POSTGRES_PASSWORD` | `postgres` |

For local development against the server's Docker Compose Postgres, use the host port:

```bash
POSTGRES_HOST=localhost POSTGRES_PORT=8432 POSTGRES_DB=postgres \
POSTGRES_USER=postgres POSTGRES_PASSWORD=<from server/.env> \
python examples/quickstart.py
```

You can also pass `PostgresConfig(dsn="postgresql://user:pass@host:5432/postgres")`. The schema is created lazily with `CREATE TABLE IF NOT EXISTS`; set `auto_create_schema=False` if your database user cannot create tables. Tables are configurable via `table_prefix` (default `bokaclaw_agent`, must match `^[a-z_][a-z0-9_]*$`).

Schema:

```sql
bokaclaw_agent_sessions (
    id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, user_id TEXT, title TEXT,
    metadata JSONB, status TEXT, error TEXT, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ
)

bokaclaw_agent_messages (
    id BIGINT IDENTITY PRIMARY KEY,
    session_id TEXT REFERENCES bokaclaw_agent_sessions(id) ON DELETE CASCADE,
    seq BIGINT, role TEXT, content TEXT, tool_calls JSONB, tool_call_id TEXT,
    name TEXT, metadata JSONB, created_at TIMESTAMPTZ,
    UNIQUE (session_id, seq)
)
```

## Observability

```python
def on_event(event):
    print(event.type, event.iteration, event.session_id)

runtime = AgentRuntime(config, provider=provider, on_event=on_event)
```

Events: `run_started`, `model_request`, `model_response`, `tool_call`, `tool_result`, `run_ended`, `error`. Callback exceptions are logged and never interrupt a run.

## v1 limits

- No streaming output.
- No token counting or automatic summarisation; only message-count truncation on resume.
- Tool calls returned in one model turn are executed sequentially, in order.
- One active run per session is recommended. Concurrent writers are serialised by a row lock and `UNIQUE(session_id, seq)`, but turn ordering is not guaranteed.
- Text content only; multimodal `content` parts are not supported.

## Dashboard integration (HTTP contract)

The dashboard's Agent page never talks to this package directly. It calls
`POST /agent` on the BokaClaw server, and the server runs one turn in-process
through `server/agent_runtime.py` (the Docker images install this package from
`agent-loop/`). The server owns conversation persistence and passes history
explicitly, so the runtime is used without a `SessionStore`.

To run the runtime as a separate service instead, wrap it in an HTTP service
exposing `POST /invoke` and set `AGENT_LOOP_URL` on the server to that
service's base URL (no `/invoke` suffix). The contract below is only used on
that optional remote path.

### Request

```http
POST {AGENT_LOOP_URL}/invoke
Accept: text/event-stream
Content-Type: application/json
Authorization: Bearer {AGENT_LOOP_API_KEY}   # only when that env var is set
X-Request-ID: <server request id>
```

```json
{
  "conversation_id": "9f0f2f2e-4f1b-4f1b-9a37-2c1d6a4e6b11",
  "message_id": "0f6d5d3e-6a2e-4a7c-8f4f-6b2b8c4c5d10",
  "message": "What did I decide about the launch date?",
  "history": [
    { "role": "user", "content": "We are launching on the 12th." },
    { "role": "assistant", "content": "Noted." }
  ],
  "user_id": "3d1c2b4a-1f2e-4a3b-9c8d-7e6f5a4b3c2d",
  "agent_id": null,
  "run_id": null,
  "metadata": {}
}
```

| Field | Meaning |
|---|---|
| `conversation_id` | Dashboard conversation UUID. A natural `session_id` for this runtime. |
| `message_id` | UUID of the user message the server already stored. |
| `message` | The current user turn. It is **not** included in `history`. |
| `history` | Recent completed turns, oldest first, already truncated by the server (40 messages / ~24k characters by default). |
| `user_id` | Dashboard account UUID. Use it as the mem0 memory scope so the Agent page and Memories page agree. |
| `agent_id`, `run_id`, `metadata` | Passed through from the browser; may be null or empty. |

### Response

Return `Content-Type: text/event-stream` with at least one `message.delta`:

```
event: message.delta
data: {"delta": "The launch date is the 12th."}

event: message.completed
data: {"usage": {"input_tokens": 120, "output_tokens": 18}, "finish_reason": "stop"}

event: done
data: {}
```

- `message.delta` — `{"delta": "<text>"}`, may repeat. The server accumulates deltas in order and stores the concatenation.
- `message.completed` — optional. Only `usage`, `finish_reason`, and `run_id` are persisted.
- `error` — `{"detail": "<message>"}`; the browser receives it as `agent_upstream_error`.
- `done` — optional; ends the turn.
- `: ping` comment lines are ignored, so they work as heartbeats.

A non-SSE JSON body of `{"content": "...", "finish_reason": "stop"}` is also
accepted and treated as a single-shot reply. Since this runtime does not
stream yet, a first wrapper version can call `runtime.run(...)` and emit one
`message.delta` with `result.content`; the Agent page renders it without
typewriter progress.

A non-2xx response is surfaced to the browser as `agent_upstream_error` with
the first 500 bytes of the body. Transport failures and timeouts become
`agent_unreachable` / `agent_timeout` on the server side, so a wrapper only
has to return sane status codes.

### Cancellation

When the browser stops generating, the server cancels the upstream request
and stores whatever text already streamed with `status: "cancelled"`. A
wrapper that runs to completion regardless is still safe; the reply is simply
discarded.

### Server configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_LOOP_URL` | — | Executor base URL. Empty returns 503 `agent_not_configured`. |
| `AGENT_LOOP_API_KEY` | — | Sent as `Authorization: Bearer ...` to the executor. |
| `AGENT_LOOP_MOCK` | `false` | Stream a canned reply without calling the executor. |
| `AGENT_LOOP_CONNECT_TIMEOUT_SECONDS` | `5` | Connect timeout. |
| `AGENT_LOOP_READ_TIMEOUT_SECONDS` | `120` | Read timeout. |
| `AGENT_LOOP_MAX_SECONDS` | `300` | Total wall clock for one turn. |
| `AGENT_LOOP_HISTORY_MESSAGES` | `40` | Prior turns forwarded as context. |

From inside the compose network, a wrapper running on the host is reachable
at `http://host.docker.internal:<port>`.

## Development

```bash
cd agent-loop
hatch env create
hatch run lint
hatch run format-check
hatch run test

# integration test against a real Postgres
BOKACLAW_AGENT_TEST_DSN="postgresql://postgres:<password>@localhost:8432/postgres" \
  hatch run test -m integration
```

CI/CD workflow registration is intentionally not included: `.github/workflows/` changes require maintainer approval. Before publishing to PyPI, add a `<package>-checks.yml` / `<package>-cd.yml` pair and register the path filter and release tag prefix.
