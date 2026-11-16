# `dashboard/` — BokaClaw 控制台

面向自托管 BokaClaw 整套服务的 Next.js 15 App Router 前端。它通过 HTTP 与 `server/` 通信，从不
直接调用模型。

## 命令

```bash
cd dashboard
pnpm install
pnpm dev          # http://localhost:3000
pnpm build
pnpm start
pnpm lint         # prettier --check .
pnpm format       # prettier --write .
pnpm typecheck    # tsc --noEmit
```

只允许 pnpm。不要使用 npm 或 yarn，也不要用其他包管理器重新生成 `pnpm-lock.yaml`。

## 约定

- TypeScript strict 模式；`@/*` 映射到 `src/*`。只使用 ES 模块 `import` 语法。
- 默认使用 Server Components。仅在需要状态、副作用、浏览器 API 或事件处理时才添加
  `"use client"`。
- 所有 API 调用都通过 `src/utils/api.ts`（`api`、`postStream`）以及 `src/utils/api-endpoints.ts`
  中的端点常量完成。不要硬编码 URL。
- 认证 token 保存在内存中；刷新通过 Next 路由 `/api/auth/refresh` 完成。绝不要把 JWT 持久化到
  `localStorage`。
- SSE 使用 `src/utils/sse.ts` 中的 `postStream` + `SseParser`。`EventSource` 无法发送
  `Authorization` 头。
- 在 `src/types/api.ts` 中现有类型旁添加新的请求/响应类型。
- 复用 `src/components/ui`（shadcn/Radix）和现有的语义化 Tailwind token；使用 `typo-*` 工具类和
  `font-fustat`。不要新增 UI、图标或图表库。
- 共享状态使用 `src/store` 下的 Redux Toolkit；页面局部状态保持局部。
- 面向用户的文案统一走 `src/i18n`（客户端组件用 `useI18n().t("namespace.key")`，服务端组件用
  `getServerMessage`）。每个新字符串都要同时加到 `messages/en.ts` 和 `messages/zh.ts`；
  TypeScript 会强制 key 对齐。参见 `docs/dashboard/i18n.md`。
- 格式化使用 Prettier。ESLint 在 `next build` 期间通过 `next/core-web-vitals` 运行；不要添加
  第二套 linter 配置。
- 保持 `next.config.mjs` 中的安全响应头和 `output: "standalone"` 不变。

## 目录结构

| 路径                        | 用途                                                                     |
| --------------------------- | ------------------------------------------------------------------------ |
| `src/app/(auth)/`           | 登录及认证相关路由                                                       |
| `src/app/(root)/dashboard/` | Dashboard 页面，包括 `agent/`                                            |
| `src/app/setup/`            | 首次运行设置向导                                                         |
| `src/app/api/`              | Next 路由处理器（token 刷新、健康检查）                                  |
| `src/components/ui/`        | shadcn/Radix 基础组件                                                    |
| `src/components/agent/`     | Agent 聊天 UI：消息线程、输入框、会话列表                                |
| `src/hooks/`                | `use-agent-chat`、认证、API 查询、防抖                                   |
| `src/utils/`                | API 客户端、端点常量、SSE 解析器、辅助函数                               |
| `src/i18n/`                 | 语言配置、EN/ZH 字典、Provider、格式化、服务端解析器                     |
| `src/components/i18n/`      | 语言切换器                                                               |
| `src/store/`                | Redux Toolkit store 与 reducer                                           |
| `src/types/`                | 共享 API 类型                                                            |
| `src/styles/globals.css`    | Tailwind 层与设计 token                                                  |

## 说明

- Agent 页面以 SSE 方式消费 `POST /agent`，并使用 `server/` 的 `/conversations*`。契约变更时，同步
  更新 `docs/dashboard/agent-chat-design.md`。
- 未配置测试运行器；用 `pnpm typecheck` 和 `pnpm build` 验证改动。
