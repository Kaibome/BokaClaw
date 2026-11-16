# 文档（`docs/`）

BokaClaw 的 Mintlify 站点。

## 命令

```bash
make docs          # 从仓库根目录运行
cd docs && mintlify dev
```

## 结构

| 路径 | 内容 |
|------|------|
| `memory/` | 工作区 Markdown 记忆格式、操作与迁移 |
| `server/` | 服务端执行与 human-in-the-loop 设计 |
| `agent-loop/` | ReAct 运行时、工具、子 Agent、上下文压缩 |
| `dashboard/` | 控制台行为与 i18n |
| `eval/` | 评测架构与评测控制台 |
| `building-problem-record/` | 事故与问题记录 |
| `docs.json` | 导航树 |
| `llms.txt` | 供 Agent 阅读文档的索引 |

## 新增页面

每个新的 `.mdx` 页面都必须包含三部分，否则 CI 会失败：

1. 页面本身，放在正确的分区下。
2. `docs.json` 中的导航条目。
3. `llms.txt` 中的一行，带 `[Both]` 范围标签，并以 `Use when ...` 开头的描述。

`docs-llms-txt-check.yml` 会在每个修改 `docs/**/*.mdx` 的 PR 上运行；当 `llms.txt` 不同步时阻止
合并。修复方法：

```bash
python scripts/check-llms-txt-coverage.py --write
```

把每个生成的占位描述替换为真实的 `Use when ...` 描述，移到正确的分区，并在 triage 标题为空后
删除它。

## 约定

- Frontmatter 需要 `title`、`description`，通常还需要 `icon`。
- 可使用 Mintlify 组件（`<Note>`、`<Card>`、`<Tabs>`、`<CodeGroup>`）；优先于原生 HTML。
- 代码示例必须可运行。如果示例调用了公开 API，必须与真实签名一致。
- 任何公开 REST 或 SDK 签名的变更，都必须在同一个 PR 中更新此处对应页面。
