# CI/CD and repository automation (`.github/`)

> **Do not modify any workflow without explicit approval from a maintainer.** Publishing
> credentials are bound to workflow filenames, and the gate workflows decide whether
> contributions are accepted. Read this file before proposing any change here.

## CI: one gate, many pipelines

`ci-gate.yml` (**CI Gate**) is the single entry point. It runs on every PR, detects which
packages changed, calls only the relevant package workflows as reusable workflows
(`workflow_call`), and its final `CI Gate` job aggregates the results: skipped pipelines
pass, failed or cancelled ones fail. It is the **only CI status check that needs to be
required** in branch protection.

| Workflow | File | Standalone triggers | Runs |
|---|---|---|---|
| CI Gate | `ci-gate.yml` | All PRs | Routes to and aggregates everything below |
| Server | `server-checks.yml` | Push to main (`server/`, `agent-loop/`, `tests/`), manual | Ruff + pytest (`server/tests` + root `tests/`) against PostgreSQL/pgvector with Alembic applied |
| Agent Runtime | `agent-loop-checks.yml` | Push to main (`agent-loop/`), manual | Ruff + pytest on Python 3.10 and 3.12 |
| Dashboard | `dashboard-checks.yml` | Push to main (`dashboard/`), manual | pnpm install, Prettier, TypeScript, Next.js build |
| docs llms.txt | `docs-llms-txt-check.yml` | Manual | `docs/llms.txt` coverage |
| GitHub Scripts | inline in `ci-gate.yml` | none | `node` over every `.github/scripts/*.test.js` |

Package workflows have `workflow_call` for the gate plus their own `push` /
`workflow_dispatch` triggers, and **no `pull_request` trigger**. To wire in a new package:
add a filter under the `changes` job, add a call job that `uses:` the package workflow,
and list the call job in the gate's `needs`.

The `Server` job provisions `pgvector/pgvector:pg17`, creates the `mem0_app` database, and
runs `alembic upgrade head` before pytest. It sets the non-secret environment the tests
expect (`POSTGRES_*`, `APP_DB_NAME`, `JWT_SECRET`, `AUTH_DISABLED`, `ADMIN_API_KEY`,
`PYTHONPATH=server`).

`GitHub Scripts` is a plain job inside `ci-gate.yml` because a reusable workflow wrapping
two `node` invocations would be more file than test. It runs on the `github_scripts` filter,
which covers `.github/scripts/**`, `VOUCHED.td`, `pr-gate.yml`, and `vouch-check-pr.yml`.

## Branch protection

The `Main Branch Rule` ruleset enforces squash-only merges, linear history, no deletion,
no force-push, and one approving review. Two status checks belong in its
`required_status_checks` rule: `CI Gate` (this workflow) and `license/cla` (CLA Assistant).
Editing the ruleset requires repo admin; `maintain` is not enough.

## CD

There is intentionally no release router or package publishing workflow. The previous
Mem0 SDK, CLI, and integration publishers were removed with those packages; BokaClaw
packages are not published from this repository yet. Adding publishing requires a
maintainer-approved workflow and registry trusted-publisher configuration.

## Contribution gates

| Workflow | File | Purpose |
|---|---|---|
| PR Gate | `pr-gate.yml` | Closes PRs that do not link an issue labeled `accepted`, and reopens them when that label arrives. Exempts members, bots, drafts, and docs-only changes. Never checks out PR code. |
| Vouch (check PR) | `vouch-check-pr.yml` | Closes PRs from authors denounced in `VOUCHED.td`. Comments once on PRs from authors merely absent from it, and blocks nothing in that case. |
| Vouch (manage list) | `vouch-manage-by-issue.yml` | Maintainers edit the trust list by commenting `!vouch @user`, `!denounce @user`, or `!unvouch @user` on any issue. Opens a PR against `VOUCHED.td` through a GitHub App token, for a maintainer to merge. |
| PR Labeler | `pr-labeler.yml` | Path-based labels plus propagating labels from linked issues |
| Stale Bot | `stale.yml` | Marks stale issues and PRs |
| llms.txt Check | `docs-llms-txt-check.yml` | Blocks PRs touching `docs/**/*.mdx` when `docs/llms.txt` is out of sync |

Supported labels: `server`, `agent-runtime`, `dashboard`, `memory`, `documentation`, `ci`.

`pr-gate.yml` and `vouch-check-pr.yml` use `pull_request_target`, which is required to label
and close fork PRs. Neither checks out PR code and neither has a `run:` step, so there is no
pwn-request or script-injection surface. Keep it that way: any future `run:` step in these
files must never interpolate `github.event.*` text.

Both workflows exempt maintainers twice, and the second guard is the one that holds.
`author_association` is rendered for the viewer, and a webhook payload has no privileged
viewer: `MEMBER` needs the author's org membership to be **public**, `COLLABORATOR` needs a
**direct** repository invite. So the guard also skips any PR whose head branch lives in this
repository (`head.repo.full_name == github.repository`). Keep both arms.

`pr-gate.yml` carries two jobs whose `if:` conditions are deliberately disjoint. `gate`
closes, and only ever runs on `opened`, `reopened`, and `ready_for_review`. `reopen`
reopens, and only ever runs on `edited` or on `issues: labeled` with the `accepted` label.
Nothing can both close and reopen on the same event, which is the property to preserve when
editing either guard.

The denounce test in `pr-gate.yml` keeps the two gates from cancelling each other out: a
denounced author whose PR also lacked an accepted issue was closed by both workflows, so
labeling the linked issue would otherwise reopen a PR that vouch must keep closed. Reopening
runs through `GITHUB_TOKEN`, which raises no events, so `vouch-check-pr.yml` never fires a
second time. `.github/scripts/vouch-decision.test.js` covers the decision table.

The concurrency group is keyed on `github.event.action` as well as `github.event_name` and
the number. Without the event name, a maintainer applying `bug` right after `accepted`
cancels the reopen mid-flight. Without the action, `opened` and `edited` share a group, and
an author who edits the template in the seconds after opening cancels the run that was about
to gate them.

`GATE_EFFECTIVE_FROM` in `pr-gate.yml` is a `created_at` cutoff: `reopened` and
`ready_for_review` fire on old PRs too, and the cutoff keeps rules that did not exist then
from closing the backlog. Set it to the actual merge date in UTC.

The gate's docs-only exemption covers `docs/` plus four root files: `README.md`,
`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and `SECURITY.md`. `AGENTS.md`, `CLAUDE.md`, and
`LLM.md` stay gated because they are functional instructions. Markdown nested anywhere else
(including `.github/`) stays gated. Adding a prose root file means adding it to `rootDocs`.

`.github/scripts/pr-gate-docs-exemption.test.js` pulls the `rootDocs` and `isDocs` lines out
of `pr-gate.yml` and evaluates them, so it exercises the shipped rule. Keep `rootDocs` a
`Set` of quoted names and `isDocs` a single arrow expression, or the test will reject it.

## Issue forms and templates

`ISSUE_TEMPLATE/*.yml` are GitHub issue **forms**, not markdown templates. Only forms
support `required: true` and machine-parseable field ids. Blank issues are disabled in
`config.yml`.

Current field ids:

| Form | Ids |
|------|-----|
| `bug_report.yml` | `component`, `description`, `verification`, `ai_assistance` |
| `feature_request.yml` | `component`, `description`, `ai_assistance` |
| `documentation_issue.yml` | `description`, `ai_assistance` |

The `component` options are Server / REST API, Agent runtime, Dashboard, Memory,
Documentation, and Other. There is no automatic issue-labeler workflow; add labels during
triage.

## Trust list

`VOUCHED.td` is one GitHub username per line, `#` for comments. Vouch's only built-in
exemptions are accounts ending in `[bot]` and repo collaborators with `write` or `admin`.
Organization membership alone is not one, so `vouch-check-pr.yml` skips `OWNER`, `MEMBER`,
and `COLLABORATOR` authors explicitly.

Failure is open by design. If the action cannot read `VOUCHED.td`, it treats nobody as
denounced.

`vouch-manage-by-issue.yml` runs with `merge-immediately: "false"`: the `Main Branch Rule`
ruleset requires an approving review, so the bot opens the PR and a maintainer merges it.
It needs the `VOUCH_APP_ID` and `VOUCH_APP_PRIVATE_KEY` repository secrets;
`vouch-check-pr.yml` needs neither.
