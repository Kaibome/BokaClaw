# 面向 AI 编程助手的 Mem0 技能

Mem0 为 Claude Code、Codex、Cursor、OpenCode、OpenClaw 以及任何支持
[skills 标准](https://github.com/anthropics/skills)的助手提供结构化的技能定义。
技能会教助手如何使用 Mem0——既可以把 SDK 知识加载进上下文，也可以按需执行一套
端到端工作流。

## 两类技能

### 参考类技能 —— 常驻加载

安装一次后加载进上下文，让助手写出正确的 Mem0 代码。日常开发用这类技能。

| 技能 | 覆盖范围 | 安装 |
|-------|---------|---------|
| [`mem0`](./mem0/) | Python + TypeScript SDK（Platform + OSS）、框架集成 | `npx skills add https://github.com/mem0ai/mem0 --skill mem0` |
| [`mem0-cli`](./mem0-cli/) | 终端工作流（`mem0` CLI，Node 和 Python 两个版本） | `npx skills add https://github.com/mem0ai/mem0 --skill mem0-cli` |
| [`mem0-vercel-ai-sdk`](./mem0-vercel-ai-sdk/) | `@mem0/vercel-ai-provider` 和 `createMem0` | `npx skills add https://github.com/mem0ai/mem0 --skill mem0-vercel-ai-sdk` |

### 流水线技能 —— 按需运行

通过斜杠命令调用，执行特定的端到端工作流。这类技能会做实际工作：创建分支、编写
测试、运行代码。

| 技能 | 触发方式 | 安装 |
|-------|---------|---------|
| [`mem0-integrate`](./mem0-integrate/) | `/mem0-integrate` —— 通过 TDD 把 Mem0 接入现有仓库 | `npx skills add https://github.com/mem0ai/mem0 --skill mem0-integrate` |
| [`mem0-test-integration`](./mem0-test-integration/) | `/mem0-test-integration` —— 验证 `/mem0-integrate` 的产出 | `npx skills add https://github.com/mem0ai/mem0 --skill mem0-test-integration` |
| [`mem0-oss-to-platform`](./mem0-oss-to-platform/) | `/mem0-oss-to-platform` —— 把项目从 Mem0 OSS 迁移到托管版 Platform SDK | `npx skills add https://github.com/mem0ai/mem0 --skill mem0-oss-to-platform` |

`mem0-integrate` 和 `mem0-test-integration` 设计为在同一工作区按顺序运行：

```
/mem0-integrate          →  mem0-integrate/<slug> 分支 + .mem0-integration/ 产物
/mem0-test-integration   →  评分卡（编译 + 运行时验证，真实 API 冒烟测试）
```

## 如何选择技能

- **在新项目或现有项目里编写 Mem0 代码？** → `mem0`
- **使用终端 CLI？** → `mem0-cli`
- **使用 `@ai-sdk/*` 构建应用？** → `mem0-vercel-ai-sdk`
- **想让助手帮你把 Mem0 接入现有仓库？** → `mem0-integrate`，然后 `mem0-test-integration`
- **已经在用 Mem0 OSS，想迁移到托管版 Platform？** → `mem0-oss-to-platform`

## 相关链接

- [Vibecoding with Mem0](https://docs.mem0.ai/vibecoding) —— 官方入口页
- [Claude Code 集成](https://docs.mem0.ai/integrations/claude-code)
- [Mem0 Platform 控制台](https://app.mem0.ai)
- [Mem0 文档](https://docs.mem0.ai)

## 许可证

Apache-2.0
