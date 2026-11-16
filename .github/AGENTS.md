# CI/CD 与仓库自动化（`.github/`）

> **未经维护者明确批准，不要修改任何 workflow。** 发布凭据与 workflow 文件名绑定，而 gate
> workflow 决定贡献是否被接受。在此提出任何改动前，先阅读本文件。

## CI：单一入口，多条流水线

`ci-gate.yml`（**CI Gate**）是唯一入口。它在每个 PR 上运行，检测哪些包发生了变更，仅以可复用
workflow（`workflow_call`）方式调用相关的包 workflow，最后由 `CI Gate` job 汇总结果：跳过的
流水线视为通过，失败或取消的视为失败。它是分支保护中**唯一需要设为 required** 的 CI 状态检查。

| Workflow | 文件 | 独立触发条件 | 执行内容 |
|---|---|---|---|
| CI Gate | `ci-gate.yml` | 所有 PR | 路由并汇总以下全部内容 |
| Server | `server-checks.yml` | push 到 main（`server/`、`agent-loop/`、`tests/`）、手动 | 在已应用 Alembic 的 PostgreSQL/pgvector 上运行 Ruff + pytest（`server/tests` + 根目录 `tests/`） |
| Agent Runtime | `agent-loop-checks.yml` | push 到 main（`agent-loop/`）、手动 | 在 Python 3.10 和 3.12 上运行 Ruff + pytest |
| Dashboard | `dashboard-checks.yml` | push 到 main（`dashboard/`）、手动 | pnpm install、Prettier、TypeScript、Next.js build |
| docs llms.txt | `docs-llms-txt-check.yml` | 手动 | `docs/llms.txt` 覆盖率 |
| GitHub Scripts | 内联于 `ci-gate.yml` | 无 | 用 `node` 运行每个 `.github/scripts/*.test.js` |

包 workflow 既有供 gate 使用的 `workflow_call`，也有自己的 `push` / `workflow_dispatch` 触发
条件，且**没有 `pull_request` 触发条件**。接入新包时：在 `changes` job 下添加过滤条件，添加一个
`uses:` 该包 workflow 的调用 job，并把该调用 job 列进 gate 的 `needs`。

`Server` job 会准备 `pgvector/pgvector:pg17`，创建 `mem0_app` 数据库，并在 pytest 前执行
`alembic upgrade head`。它设置测试所期望的非敏感环境变量（`POSTGRES_*`、`APP_DB_NAME`、
`JWT_SECRET`、`AUTH_DISABLED`、`ADMIN_API_KEY`、`PYTHONPATH=server`）。

`GitHub Scripts` 是 `ci-gate.yml` 内的普通 job，因为若用可复用 workflow 包住两次 `node` 调用，
文件本身会比测试还长。它由 `github_scripts` 过滤条件触发，覆盖 `.github/scripts/**`、
`VOUCHED.td`、`pr-gate.yml` 和 `vouch-check-pr.yml`。

## 分支保护

`Main Branch Rule` ruleset 强制仅 squash 合并、线性历史、禁止删除、禁止 force-push，并要求一次
批准评审。其 `required_status_checks` 规则应包含两项状态检查：`CI Gate`（本 workflow）和
`license/cla`（CLA Assistant）。修改该 ruleset 需要仓库管理员权限；`maintain` 不够。

## CD

这里有意不设发布路由或包发布 workflow。此前的 Mem0 SDK、CLI 和集成发布器已随相应包一并移除；
BokaClaw 的包目前不从本仓库发布。新增发布能力需要经维护者批准的 workflow 和 registry 的
trusted-publisher 配置。

## 贡献门禁

| Workflow | 文件 | 作用 |
|---|---|---|
| PR Gate | `pr-gate.yml` | 关闭未关联 `accepted` 标签 issue 的 PR；当该标签出现后重新打开。对成员、机器人、草稿和仅文档改动豁免。从不 checkout PR 代码。 |
| Vouch (check PR) | `vouch-check-pr.yml` | 关闭来自 `VOUCHED.td` 中已 denounce 作者的 PR。对仅未出现在名单中的作者，只评论一次，不阻止任何操作。 |
| Vouch (manage list) | `vouch-manage-by-issue.yml` | 维护者在任意 issue 上评论 `!vouch @user`、`!denounce @user` 或 `!unvouch @user` 来编辑信任名单。通过 GitHub App token 针对 `VOUCHED.td` 开 PR，由维护者合并。 |
| PR Labeler | `pr-labeler.yml` | 基于路径打标签，并传播所关联 issue 的标签 |
| Stale Bot | `stale.yml` | 标记陈旧 issue 和 PR |
| llms.txt Check | `docs-llms-txt-check.yml` | 当 `docs/llms.txt` 不同步时，阻止修改 `docs/**/*.mdx` 的 PR |

支持的标签：`server`、`agent-runtime`、`dashboard`、`memory`、`documentation`、`ci`。

`pr-gate.yml` 和 `vouch-check-pr.yml` 使用 `pull_request_target`，这是给 fork PR 打标签和关闭
所必需的。二者都不 checkout PR 代码，也都没有 `run:` 步骤，因此不存在 pwn-request 或脚本注入
面。请保持这一点：这些文件中未来任何 `run:` 步骤都绝不能插值 `github.event.*` 文本。

两个 workflow 都做了两重维护者豁免，真正起作用的是第二重。`author_association` 是按查看者视角
渲染的，而 webhook 载荷没有特权查看者：`MEMBER` 需要作者的 org 成员身份是**公开**的，
`COLLABORATOR` 需要**直接**的仓库邀请。因此守卫还会跳过 head 分支位于本仓库的 PR
（`head.repo.full_name == github.repository`）。两个条件都要保留。

`pr-gate.yml` 有两个 job，其 `if:` 条件被刻意设计为互斥。`gate` 负责关闭，且只在 `opened`、
`reopened` 和 `ready_for_review` 时运行。`reopen` 负责重新打开，且只在 `edited` 或
`issues: labeled` 且带 `accepted` 标签时运行。没有任何事件能同时触发关闭和重新打开，这是编辑
任一守卫时必须保留的性质。

`pr-gate.yml` 中的 denounce 判断防止两个门禁互相抵消：一个被 denounce 的作者，如果 PR 又缺少
accepted issue，会被两个 workflow 同时关闭；这样给关联 issue 打标签时，否则会重新打开一个 vouch
必须保持关闭的 PR。重新打开通过 `GITHUB_TOKEN` 执行，不会产生事件，因此 `vouch-check-pr.yml`
不会第二次触发。`.github/scripts/vouch-decision.test.js` 覆盖该判定表。

并发组同时以 `github.event.action`、`github.event_name` 和编号为 key。缺少 event name 时，
维护者在 `accepted` 之后立刻加上 `bug`，会中途取消重新打开。缺少 action 时，`opened` 和
`edited` 共用同一组，作者在打开后几秒内编辑模板，会取消即将对其执行门禁的那次运行。

`pr-gate.yml` 中的 `GATE_EFFECTIVE_FROM` 是 `created_at` 截止时间：`reopened` 和
`ready_for_review` 也会在旧 PR 上触发，该截止时间防止当时尚不存在的规则去关闭积压的 PR。请把它
设为实际的合并日期（UTC）。

gate 的仅文档豁免覆盖 `docs/` 以及四个根目录文件：`README.md`、`CONTRIBUTING.md`、
`CODE_OF_CONDUCT.md` 和 `SECURITY.md`。`AGENTS.md`、`CLAUDE.md` 和 `LLM.md` 仍受门禁约束，
因为它们是功能性指令。嵌套在其他任何位置的 Markdown（包括 `.github/`）仍受门禁约束。新增说明性
根目录文件时，需要把它加到 `rootDocs`。

`.github/scripts/pr-gate-docs-exemption.test.js` 从 `pr-gate.yml` 中抽出 `rootDocs` 和 `isDocs`
两行并求值，因此测试的是实际发布的规则。请保持 `rootDocs` 为带引号名称的 `Set`，`isDocs` 为
单个箭头表达式，否则测试会拒绝。

## Issue 表单与模板

`ISSUE_TEMPLATE/*.yml` 是 GitHub issue **表单**，不是 markdown 模板。只有表单支持
`required: true` 和可机器解析的字段 id。空白 issue 在 `config.yml` 中被禁用。

当前字段 id：

| 表单 | Id |
|------|-----|
| `bug_report.yml` | `component`、`description`、`verification`、`ai_assistance` |
| `feature_request.yml` | `component`、`description`、`ai_assistance` |
| `documentation_issue.yml` | `description`、`ai_assistance` |

`component` 的选项为 Server / REST API、Agent runtime、Dashboard、Memory、Documentation 和
Other。没有自动给 issue 打标签的 workflow；在 triage 时手动添加标签。

## 信任名单

`VOUCHED.td` 每行一个 GitHub 用户名，`#` 表示注释。Vouch 内置的豁免只有以 `[bot]` 结尾的账号
以及具有 `write` 或 `admin` 权限的仓库协作者。仅组织成员身份不算豁免，因此
`vouch-check-pr.yml` 会显式跳过 `OWNER`、`MEMBER` 和 `COLLABORATOR` 作者。

失败时默认放行（fail open）。如果 action 读取不到 `VOUCHED.td`，它会认为没有人被 denounce。

`vouch-manage-by-issue.yml` 以 `merge-immediately: "false"` 运行：`Main Branch Rule` ruleset
要求批准评审，因此机器人只开 PR，由维护者合并。它需要 `VOUCH_APP_ID` 和
`VOUCH_APP_PRIVATE_KEY` 仓库 secret；`vouch-check-pr.yml` 两者都不需要。
