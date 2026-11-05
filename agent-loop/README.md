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

## Streaming

`stream()` is the incremental counterpart to `run()`. It yields assistant text
deltas as the provider produces them and returns the same `RunResult` when the
generator is exhausted:

```python
turn = runtime.stream("Explain pgvector in one paragraph.", session_id="user-42")
try:
    while True:
        print(next(turn), end="", flush=True)
except StopIteration as done:
    result = done.value
```

Tool-call rounds are executed inside the generator; only the final answer is
streamed. Providers that do not implement `stream_complete` fall back to a
single delta containing `result.content`. Closing the generator before
exhaustion abandons the turn without persisting it.

## Cancellation

`run()`, `stream()`, and `arun()` accept an optional `cancel_event`
(`threading.Event`). Cancellation is cooperative: the runtime checks the event
at safe boundaries and never interrupts a blocking model or tool call, nor the
streaming chunk loop (a partially assembled assistant message would leave
dangling tool calls).

Checkpoints:

1. before each ReAct iteration,
2. after the model response returns and before its tool calls execute,
3. before each tool call,
4. after each tool call and before the next iteration.

```python
import threading

from bokaclaw_agent import RunCancelled

cancel = threading.Event()
turn = runtime.stream("Explain pgvector.", session_id="user-42", cancel_event=cancel)

cancel.set()   # from another thread

try:
    while True:
        print(next(turn), end="", flush=True)
except StopIteration as done:
    result = done.value
except RunCancelled as exc:
    print(exc.session_id, exc.iteration)
    print(exc.result.stop_reason)   # "cancelled"
```

On cancellation the runtime:

1. fills every pending tool call with an aborted `tool` message,
2. persists the session with status `cancelled` and a `RunCancelled: ...` error,
3. emits `run_ended` with `{"stop_reason": "cancelled"}`,
4. raises `RunCancelled`, whose `result` holds the partial `RunResult`.

Closing a `stream()` generator early is not cancellation: it abandons the turn
without persisting anything.

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

A run stopped through `cancel_event` is written with status `cancelled` after its pending tool calls are filled with aborted results. Existing readers should tolerate the new status in addition to `active`, `completed`, and `failed`.

`AgentConfig.max_history_messages` limits how many recent messages are sent to the model on resume. It never changes what is persisted, and truncation only starts at a user turn or a completed assistant turn, so tool calls are never separated from their results.

Use `InMemorySessionStore` when you do not want a database:

```python
from bokaclaw_agent import InMemorySessionStore

runtime = AgentRuntime(config, provider=provider, tools=[get_weather], store=InMemorySessionStore())
```

## Context compression, fact ledger, and loop guards

Long tool-using runs can outgrow the model window one iteration at a time.
Optional settings bound the model view and keep the agent from looping; all are
off by default in the SDK:

| `AgentConfig` field | Default | Meaning |
|---|---|---|
| `context_max_tokens` | `None` (off) | Approximate token budget for one model request. When exceeded, older messages are folded into a rolling summary or dropped. |
| `context_keep_recent_tokens` | `None` | Tokens kept verbatim; defaults to half the budget. Must stay at least 256 tokens below `context_max_tokens` when both are set. |
| `context_compression` | `"summary"` | `"summary"` folds old messages with one model call; `"truncate"` only drops them. |
| `compression_model` | `None` | Separate `ModelConfig` used only for summaries. Defaults to the agent's own provider; set it to a cheaper model or endpoint. |
| `tool_result_max_chars` | `None` (off) | Maximum characters of one tool result in the model view. Minimum 256. |
| `fact_ledger` | `False` | Keep a compact, deduplicated ledger of this run's tool calls and findings in the model view, appended after compression. |
| `fact_ledger_max_chars` | `2500` | Maximum characters of the fact ledger. Minimum 256. |
| `step_budget_notice` | `False` | Append `Step X of N` (with a wrap-up directive near the end) to each request view. |
| `progress_guard` | `False` | Count tool calls that returned no new evidence; nudge after `progress_nudge_after`, then force a final answer after `progress_stop_after`. |
| `progress_nudge_after` | `3` | Consecutive no-progress calls before the nudge. Minimum 2. |
| `progress_stop_after` | `5` | Consecutive no-progress calls before the forced final answer. Must be at least `progress_nudge_after`. |

```python
config = AgentConfig(
    context_max_tokens=32_000,
    tool_result_max_chars=16_000,
    fact_ledger=True,
    step_budget_notice=True,
    progress_guard=True,
    compression_model=ModelConfig(model="deepseek-chat"),  # cheaper summary calls
)
```

**A separate model for summaries.** Compression is the one model call whose
input is a transcript and whose output is a short summary, so it does not need
the model driving the ReAct loop. Set `compression_model` to a `ModelConfig`
and every rolling summary goes to that provider instead; the main loop keeps
`model`. The provider is built lazily on the first compression, so a run that
never compress never opens the extra client. A provider error, an empty reply,
or an unusable summary still falls back to truncation, exactly as before, and
the tokens are reported in the same `context.compressed` usage stats.
Alternatively, inject one directly with
`AgentRuntime(..., compression_provider=my_provider)`.

**Only the model view changes.** Compression and truncation run when the request
is built, after history is loaded and before `MODEL_REQUEST` is emitted.
`new_messages`, `RunResult.tool_results`, and everything written to a session
store keep the full original content, so a later run can still work from it.

- **Summary mode** keeps the system prompt, summarizes everything before the
  recent window, and appends the summary to the system message. The summary
  rolls forward: the next compression folds only newly dropped messages into
  the previous summary. Summarization is best effort — a provider error,
  empty reply, or too little content falls back to deterministic truncation,
  and the run continues.
- **Truncate mode** drops old messages at a safe boundary (never starting with
  an orphaned `tool` message). The most recent user turn is always pinned, so a
  single-user tool loop keeps its instruction.
- **Hard fit**: if the view is still over budget (for example one oversized
  message), the largest non-system messages get head/tail truncation with a
  `\n... [truncated] ...\n` marker, matching the server's tool-trace convention.
- Tool results longer than `tool_result_max_chars` are capped the same way. The
  `TOOL_RESULT` event carries `truncated` and `original_chars` alongside the
  view content, so observers can tell the difference.

**Fact ledger.** Even with compression, a long run can forget what it already
found and search for it again. With `fact_ledger=True` the runtime records one
compact line per distinct tool call (arguments, status, first results or a
content excerpt, and a repeat count) and appends it to the request *after*
compression, so early findings survive summarization. It is capped at
`fact_ledger_max_chars`; the oldest entries are dropped first. Like the summary,
it exists only in the request view.

**Step budget notice.** With `step_budget_notice=True` each request gets a
`Step X of N` line; the last two steps switch to a wrap-up instruction (answer
now, or make at most one final call). This is view-only as well.

**No-progress guard.** With `progress_guard=True` the runtime fingerprints the
*evidence* each tool result returns — the URL set of a search, the content of a
fetch or read, a command's stdout — and counts consecutive calls that add
nothing new. Errors, empty results, cache hits, repeated searches, and
different URLs that return an identical body all count as no progress. After
`progress_nudge_after`
such calls the request view gains a "change course or answer" note; after
`progress_stop_after` the loop stops and makes one tool-free request for a final
answer, exactly like the step limit. The successful path returns
`stop_reason="no_progress"`; if that request returns no text the run raises
`NoProgressError`. Both notices are view-only.

All blocks are appended as one trailing `user` message prefixed with
`[Runtime context, not a new user request]`, after compression and before
`MODEL_REQUEST`, so the emitted event shows exactly what the model received.

When the server runs the agent in-process, compression, both loop controls, and
the no-progress guard are on by default at 32,000 tokens, 16,000-character tool
results, a 2,500-character ledger, and the step notice; see
[Server configuration](#server-configuration) to change or disable them.

## Step limit

`AgentConfig.max_iterations` caps the number of ReAct steps. When the budget
runs out while the model is still calling tools, the runtime makes **one final
request with tools disabled** and a short instruction to answer from what it
already gathered; set
`AgentConfig(final_answer_on_max_iterations=False)` to fail the turn instead.

The successful path returns a normal `RunResult` with
`stop_reason="max_iterations"` (the step limit is informational, not an
error), persists the turn as `completed`, and streams the answer in
`stream()` mode. The step-limit instruction is part of that one request view
only; it is never written to the session. If the final request returns no
text, the run raises `MaxIterationsExceeded` exactly as before.

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

Events: `run_started`, `model_request`, `model_response`, `tool_call`, `tool_result`, `context_compressed`, `run_ended`, `error`. Callback exceptions are logged and never interrupt a run.

`context_compressed` is emitted when a request was rewritten **or** when a summarization model call happened, immediately before the matching `model_request`. Its `data` is `{before_tokens, after_tokens, dropped_messages, summarized, fallback}`, plus `{usage, model_calls, usage_unknown_calls}` when a summary call ran. The `seq` rule below applies to it like every other event.

`model_response` always carries `usage` with `prompt_tokens` / `completion_tokens` / `total_tokens` for that one call. An empty object means the endpoint did not report usage — consumers must count it as an unknown call rather than zero tokens. Streaming requests ask for usage via `stream_options.include_usage` and retry once without it if an endpoint rejects the parameter.

Every `AgentEvent` carries a `seq` that starts at `1` for each run and increases monotonically (`run_started` is 1; `run_ended` or `error` is last). Sequence numbers are allocated by this SDK and are independent of any server-side event sequence.

## v1 limits

- `stream()` forwards text from the final answer; tool-call rounds produce no text.
- No tokenizer dependency: the context budget uses a conservative character-based estimate over messages only (tool schemas are deducted separately).
- Context compression, the fact ledger, the step notice, and the no-progress
  guard are opt-in in the SDK (off by default); `max_history_messages` still
  provides message-count truncation on resume.
- Tool calls returned in one model turn are executed sequentially, in order.
- One active run per session is recommended. Concurrent writers are serialised by a row lock and `UNIQUE(session_id, seq)`, but turn ordering is not guaranteed.
- Text content only; multimodal `content` parts are not supported.
- Cancellation is cooperative: it takes effect at the next checkpoint, not in the middle of a model or tool call.

## Dashboard integration (HTTP contract)

The dashboard's Agent page never talks to this package directly. It calls
`POST /agent` on the BokaClaw server, and the server runs one turn in-process
through `server/app/agent/runtime.py` (the Docker images install this package from
`agent-loop/`). The server owns conversation persistence and passes history
explicitly, so the runtime is used without a `SessionStore`. By default the
server executes each turn as a background `agent_run` and the HTTP response is
only an event subscription; disconnecting it does not cancel the turn.

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
accepted and treated as a single-shot reply. A wrapper can call
`runtime.stream(...)` and forward each yielded delta as a `message.delta`;
`runtime.run(...)` remains available for one-shot replies.

A non-2xx response is surfaced to the browser as `agent_upstream_error` with
the first 500 bytes of the body. Transport failures and timeouts become
`agent_unreachable` / `agent_timeout` on the server side, so a wrapper only
has to return sane status codes.

### Cancellation

With the default `AGENT_RUN_BACKGROUND=true`, the server executes each turn in a
background `AgentRunManager` task. Disconnecting the SSE stream only removes a
subscription; it does not cancel the run. Stop calls
`POST /agent/runs/{id}/cancel`, the server sets the run's `cancel_event`, and the
runtime stops at its next checkpoint. Whatever text already streamed is stored
with `status: "cancelled"` and the terminal run event reports
`agent_cancelled`. A wrapper that ignores cancellation is still safe; the reply
is simply discarded.

Set `AGENT_RUN_BACKGROUND=false` to restore the legacy request-bound path, where
closing the request cancels the upstream turn.

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
| `AGENT_FINAL_ANSWER_ON_MAX_ITERATIONS` | `true` | In-process runtime: answer with one tool-free request at the step limit instead of failing. |
| `AGENT_CONTEXT_MAX_TOKENS` | `32000` | In-process runtime token budget; `0` / `off` disables compression. |
| `AGENT_CONTEXT_KEEP_RECENT_TOKENS` | — | Recent tokens kept verbatim; empty uses half the budget. |
| `AGENT_CONTEXT_COMPRESSION` | `summary` | `summary` or `truncate`; anything else falls back to `summary`. |
| `AGENT_COMPRESSION_MODEL` | — | In-process runtime: model for context summaries; empty reuses `AGENT_MODEL`. |
| `AGENT_COMPRESSION_API_KEY` | `AGENT_API_KEY` | API key for the compression endpoint. |
| `AGENT_COMPRESSION_BASE_URL` | `AGENT_BASE_URL` | OpenAI-compatible base URL for the compression endpoint. |
| `AGENT_TOOL_RESULT_MAX_CHARS` | `16000` | In-process runtime tool-result cap; `0` / `off` disables it. |
| `AGENT_FACT_LEDGER` | `true` | In-process runtime: keep a run-scoped fact ledger in the request view so early findings survive compression. |
| `AGENT_FACT_LEDGER_MAX_CHARS` | `2500` | Fact ledger cap; values below 256 are raised to 256. |
| `AGENT_STEP_BUDGET_NOTICE` | `true` | In-process runtime: append `Step X of N` and a wrap-up directive near the step limit. |
| `AGENT_PROGRESS_GUARD` | `true` | In-process runtime: detect tool calls that return no new evidence and stop the loop when the model keeps repeating itself. |
| `AGENT_PROGRESS_NUDGE_AFTER` | `3` | Consecutive no-progress calls before the model is nudged; values below 2 are raised to 2. |
| `AGENT_PROGRESS_STOP_AFTER` | `5` | Consecutive no-progress calls before a final answer is forced; raised to the nudge threshold when lower. |
| `AGENT_RESEARCH_MEMORY` | `true` | Save conclusions from completed web-research runs to mem0; the in-process runtime also offers fresh similar ones on later turns. |
| `AGENT_RESEARCH_MEMORY_TTL_DAYS` | `30` | Saved conclusions older than this are not offered; values below 1 are raised to 1. |
| `AGENT_RESEARCH_MEMORY_MIN_SCORE` | `0.5` | Minimum similarity for a saved conclusion to be offered; clamped to 0–1. |
| `AGENT_CONTEXT_DEBUG` | off | Log the resolved context settings once per turn and each `context_compressed` event at INFO. Diagnostics only; the event is never forwarded to clients. |

The `AGENT_CONTEXT_*` / `AGENT_TOOL_RESULT_*` variables apply only when the
server runs this package in-process; they are not part of the remote `/invoke`
contract. `AGENT_RESEARCH_MEMORY` retrieval is also in-process only; saving
runs after any completed turn whose trace contains web tool events.

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
