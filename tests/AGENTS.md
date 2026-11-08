# Product tests (`tests/`)

Cross-package tests for the BokaClaw server and agent. They import `app.*` from `server/` by
prepending that directory to `sys.path`, so they run from the repository root without installing
the server as a package.

Package-local tests live next to their code:

| Path | Covers |
|------|--------|
| `server/tests/` | Memory backend, workspace grammar/index/service, eval worker, goldens |
| `agent-loop/tests/` | `bokaclaw-agent` runtime, tools, subagents, Postgres stores |
| `dashboard/` | Biome/TypeScript checks and `pnpm build` |

## Commands

```bash
# From the repository root
python -m pytest tests
python -m pytest tests/test_agent_runtime.py::test_memory_tools_are_scoped_to_the_user

# Server package tests
cd server && python -m pytest tests
```

Docker-only packages: if the host has no Python environment, run the suite inside the server image
with the repository mounted (the image does not bundle pytest):

```bash
docker run --rm -v "$PWD/server:/app" -v "$PWD/tests:/tests" -w /app \
  bokaclaw-server:test sh -lc 'pip install -q pytest pytest-mock pytest-asyncio && python -m pytest /app/tests /tests'
```

## Conventions

- Files are named `test_<module>.py`; ruff line length **120**.
- Tests must not require network, a live database, or an embedding model. Patch
  `app.main.get_memory_instance` (or the relevant service) with a fake.
- Module-level configuration (`app.core.auth`) is cached at import time. When a test reloads
  `app.main` with a different environment, refresh those globals in the same fixture.
- Mock external SDKs, never the code under test. A test that asserts the implementation back at
  itself is worse than no test.
- Bug fixes need a regression test that fails without the fix. Write it first and watch it fail.
