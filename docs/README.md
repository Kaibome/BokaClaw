# BokaClaw Documentation

Mintlify documentation for BokaClaw: the workspace-Markdown memory format and operations, the
FastAPI server, the `bokaclaw-agent` runtime, the dashboard, and the evaluation console.

## Development

```bash
mintlify dev     # from docs/
make docs        # from the repository root
```

Authoring rules, navigation requirements, and the `llms.txt` coverage check are documented in
[`AGENTS.md`](AGENTS.md).

## Structure

| Path | Contents |
|------|----------|
| `memory/` | Workspace memory overview, Markdown format, operations, migration |
| `server/` | Server execution and human-in-the-loop design |
| `agent-loop/` | ReAct runtime, tools, subagents, context compression |
| `dashboard/` | Console behavior and i18n |
| `eval/` | Evaluation architecture and the eval console |
| `building-problem-record/` | Incident and problem records |

`docs.json` is the navigation tree and `llms.txt` is the agent-readable index.
