# Documentation (`docs/`)

Mintlify site for BokaClaw.

## Commands

```bash
make docs          # from repo root
cd docs && mintlify dev
```

## Structure

| Path | Contents |
|------|----------|
| `memory/` | Workspace-Markdown memory format, operations, migration |
| `server/` | Server execution and human-in-the-loop design |
| `agent-loop/` | ReAct runtime, tools, subagents, context compression |
| `dashboard/` | Console behavior and i18n |
| `eval/` | Evaluation architecture and the eval console |
| `building-problem-record/` | Incident and problem records |
| `docs.json` | Navigation tree |
| `llms.txt` | Index for agents reading the docs |

## Adding a page

Every new `.mdx` page needs three things, or CI fails:

1. The page itself under the right section.
2. A navigation entry in `docs.json`.
3. A line in `llms.txt` with the `[Both]` scope tag and a description that starts with `Use when ...`.

`docs-llms-txt-check.yml` runs on every PR touching `docs/**/*.mdx` and blocks the merge when
`llms.txt` is out of sync. To fix:

```bash
python scripts/check-llms-txt-coverage.py --write
```

Replace each generated placeholder with a real `Use when ...` description, move it into the correct
section, and delete the triage heading once it is empty.

## Conventions

- Frontmatter needs `title`, `description`, and usually `icon`.
- Mintlify components (`<Note>`, `<Card>`, `<Tabs>`, `<CodeGroup>`) are available; prefer them over
  raw HTML.
- Code samples must be runnable. If a sample calls a public API, it has to match the real signature.
- Any change to a public REST or SDK signature has to update the matching page here in the same PR.
