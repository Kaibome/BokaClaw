# AGENTS.md

BokaClaw is a self-hosted AI agent workbench: a Next.js console, a FastAPI server, and the
`bokaclaw-agent` ReAct runtime. Durable agent memory lives in workspace Markdown files; Postgres
holds a rebuildable retrieval index plus sessions, runs, traces, and evaluation data.

This is a polyglot repository. Read the `AGENTS.md` nearest the files you edit before running
commands; each package has its own toolchain.

## Packages

| Path | What it is | Toolchain |
|---|---|---|
| `server/` | FastAPI API, agent executor, memory backend, tracing, eval worker | Docker only; `server/requirements.txt`; ruff 120 |
| `agent-loop/` | `bokaclaw-agent` Python SDK: ReAct loop, tools, subagents, context compression | hatch; ruff 120; pytest |
| `dashboard/` | Next.js 15 console | pnpm; Biome/TypeScript |
| `deepeval/` | Vendored DeepEval source used by `server/app/eval/` | see its own files |
| `docs/` | Mintlify documentation | see [`docs/AGENTS.md`](docs/AGENTS.md) |

## Memory architecture

- **Workspace Markdown is authoritative.** `AGENT_MEMORY_ROOT/users/<user_id>/` holds `USER.md`,
  `MEMORY.md`, and `memory/YYYY-MM-DD.md`.
- **Postgres is a derived index.** `memory_index_entries`, `memory_index_files`,
  `memory_history`, and `memory_index_state` are created by Alembic migration `014`. They can be
  dropped and rebuilt with `python -m app.memory.cli reconcile --all`.
- **Writes:** completed turns append one filtered episode; explicit `save_memory` writes curated
  facts. Only memory tools may modify memory files — never the generic `Write`/`Edit` tools.
- **Reads:** a small always-on profile plus a hybrid query search, injected as a user-role
  `<recalled-memories>` block, never into the system prompt.
- **Trust:** `owner` / `agent` / `tool` / `untrusted` / `system` is written by code, never parsed
  from Markdown. `untrusted` content is never auto-injected.
- Memory code lives in `server/app/memory/`; agent hooks live in `server/app/agent/runtime.py`,
  `runner.py`, and `turn_memory.py`.

## Commands

```bash
# Server
make server-build
make server-test                 # server/tests + root tests/
cd server && ruff check app tests

# Agent runtime
cd agent-loop && hatch run test
cd agent-loop && hatch run lint

# Dashboard
cd dashboard && pnpm install && pnpm build

# Local stack (Postgres + server + dashboard + executor)
bash .claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh --recreate

# Memory maintenance
docker exec bokaclaw-server python -m app.memory.cli reconcile --all
docker exec bokaclaw-server python scripts/migrate_mem0_to_workspace_memory.py --dry-run
```

## Do NOT

- Commit `.env` files, API keys, or credentials.
- Modify `.github/workflows/` without maintainer approval.
- Put `AGENT_MEMORY_ROOT` inside `AGENT_WORKSPACE_ROOT`, or mount it into the executor container.
- Add a new dependency to the server without updating `server/requirements.txt` and documenting it.
- Change a public REST contract without updating `docs/` and the dashboard consumer in the same change.
- Use npm or yarn in `dashboard/`; it is pnpm-only.
- Use `require()` in TypeScript; ES module imports only.

## Conventions

- Python: Pydantic v2, `snake_case.py`, ruff line length **120**, tests named `test_<module>.py`.
- TypeScript: strict mode, pnpm, Biome where the package configures it.
- Commits: Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`).
- Bug fixes include a regression test that fails without the fix. User-facing features update
  `docs/` and add `docs.json` + `docs/llms.txt` entries.

## Attribution

BokaClaw's early versions were built on Mem0. The runtime no longer depends on Mem0; the original
Apache-2.0 license and attribution remain in `LICENSE`.
