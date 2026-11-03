# `agent-loop/` — bokaclaw-agent

Standalone Python SDK: a minimal ReAct runtime with tool execution and Postgres session persistence.

## Commands

```bash
cd agent-loop
hatch env create
hatch run lint          # ruff check src tests examples
hatch run format        # ruff format src tests examples
hatch run format-check
hatch run test          # pytest
hatch build             # wheel + sdist
```

## Conventions

- Python >= 3.10, Pydantic v2 for all models and configs.
- Ruff line length **120**; imports sorted with isort (`profile = black`, first-party `bokaclaw_agent`).
- pytest with `pytest-asyncio`; use explicit `@pytest.mark.asyncio` markers (strict mode).
- No network in unit tests: use `FakeProvider` from `tests/conftest.py` and `InMemorySessionStore`.
- Postgres unit tests inject a fake pool; the real-database test is gated by `BOKACLAW_AGENT_TEST_DSN` and marked `integration`.
- Keep `psycopg` imports lazy so `import bokaclaw_agent` works without the `postgres` extra.
- Never change `.github/workflows/` here; CI/CD registration needs maintainer approval.
- Public API changes require updating `README.md` in the same change.

## Layout

| Path | Purpose |
|------|---------|
| `src/bokaclaw_agent/runtime.py` | ReAct loop, failure handling, persistence orchestration |
| `src/bokaclaw_agent/tools.py` | `@tool`, JSON Schema derivation, dispatch |
| `src/bokaclaw_agent/context.py` | In-memory context, wire-format helpers, truncation |
| `src/bokaclaw_agent/providers/` | `ModelProvider` protocol + OpenAI-compatible adapter |
| `src/bokaclaw_agent/sessions/` | Store protocol, in-memory store, Postgres store |
| `tests/` | Unit tests; no network |
