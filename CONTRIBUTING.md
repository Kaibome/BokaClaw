# Contributing to BokaClaw

Thanks for taking the time to contribute. By participating you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- Open an issue for anything beyond a trivial fix so the approach can be agreed first.
- Search existing issues before opening a new one.
- A bug report needs a reproducible case, the version, and the real output or traceback.
- Link the issue from the pull request (`Closes #<number>`).

## Making a change

1. Fork and branch from `main` (`feature/...`, `fix/...`).
2. Run the checks for **every package you touched**:

   ```bash
   # Server
   cd server && pytest tests && ruff check app tests

   # Agent runtime
   cd agent-loop && hatch run test && hatch run lint

   # Dashboard
   cd dashboard && pnpm install && pnpm build

   # Docs
   python scripts/check-llms-txt-coverage.py
   ```

3. Add or update tests. A bug fix needs a regression test that fails without the fix.
4. Update the matching `docs/` page for any public REST or SDK change. Every new
   `.mdx` page needs a `docs/docs.json` entry and a `docs/llms.txt` line.
5. Commit with [Conventional Commits](https://www.conventionalcommits.org/).
6. Fill in the pull request template and describe how the code was written and
   how you verified it.

## Memory changes

Workspace Markdown is the source of truth. Do not add a second writable store or
let the generic file tools edit memory files. If you change the Markdown grammar
or index schema, update `docs/memory/` and add a migration; the index must remain
rebuildable from the files.

## Do not

- Commit `.env` files, API keys, or credentials.
- Modify `.github/workflows/` without maintainer approval.
- Submit work you did not run.
- Fabricate traces, benchmarks, or test results.

BokaClaw's early versions were built on Mem0; the runtime no longer depends on it.
The original Apache-2.0 license and attribution remain in `LICENSE`.
