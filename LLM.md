# BokaClaw

BokaClaw is a self-hosted AI agent workbench. It combines:

- a Next.js 15 console for chat, traces, evaluation, memory inspection, and configuration;
- a FastAPI server for auth, sessions, agent execution, SSE streaming, tracing, and evaluation;
- `bokaclaw-agent`, an embeddable ReAct runtime with tools, subagents, context compression, and
  Postgres session persistence;
- workspace-Markdown memory with a rebuildable Postgres index.

## Memory

Each dashboard user gets a workspace under `AGENT_MEMORY_ROOT/users/<user_id>/`:

```
USER.md                # identity and durable preferences
MEMORY.md              # curated long-term facts
memory/YYYY-MM-DD.md   # episodic captures and research conclusions
```

Markdown is authoritative. `memory_index_entries` and friends in Postgres are derived and can be
rebuilt with `python -m app.memory.cli reconcile --all`. Completed turns append a filtered episode;
the model can also use `search_memories`, `save_memory`, `get_memory`, `update_memory`, and
`delete_memory`. See `docs/memory/` for the format and operations.

## Runtime

`bokaclaw-agent` runs an OpenAI-compatible ReAct loop with sync/async tools, cooperative
cancellation, token-budget compression, subagent delegation, and Postgres session stores. The server
runs it in-process (or delegates to `AGENT_LOOP_URL`) and remains the system of record for
conversations and runs.

## Repository map

| Path | Contents |
|---|---|
| `server/` | FastAPI API, agent executor, memory backend, tracing, eval worker |
| `agent-loop/` | `bokaclaw-agent` SDK |
| `dashboard/` | Next.js console |
| `docs/` | Architecture, memory, runtime, dashboard, and evaluation docs |
| `tests/` | Product tests |
| `deepeval/` | Vendored DeepEval source used by the eval worker |

BokaClaw's early versions were built on Mem0; the runtime no longer depends on it. The original
Apache-2.0 license and attribution remain in `LICENSE`.
