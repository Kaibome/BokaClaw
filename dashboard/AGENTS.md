# `dashboard/` — BokaClaw dashboard

Next.js 15 App Router front end for the self-hosted BokaClaw stack. It talks to `server/` over HTTP and never calls the model directly.

## Commands

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

pnpm only. Do not use npm or yarn, and do not regenerate `pnpm-lock.yaml` with another package manager.

## Conventions

- TypeScript strict mode; `@/*` maps to `src/*`. ES module `import` syntax only.
- Server Components by default. Add `"use client"` only for state, effects, browser APIs, or event handlers.
- All API calls go through `src/utils/api.ts` (`api`, `postStream`) and the endpoint constants in `src/utils/api-endpoints.ts`. Do not hardcode URLs.
- Auth tokens live in memory; refresh goes through the Next route `/api/auth/refresh`. Never persist JWTs in `localStorage`.
- SSE uses `postStream` + `SseParser` from `src/utils/sse.ts`. `EventSource` cannot send the `Authorization` header.
- Add request/response shapes to `src/types/api.ts` next to the existing types.
- Reuse `src/components/ui` (shadcn/Radix) and the existing semantic Tailwind tokens; use `typo-*` utilities and `font-fustat`. Do not add a new UI, icon, or chart library.
- Shared state uses Redux Toolkit under `src/store`; keep page-local state local.
- User-facing copy goes through `src/i18n` (`useI18n().t("namespace.key")` in client components, `getServerMessage` in server components). Add every new string to both `messages/en.ts` and `messages/zh.ts`; TypeScript enforces key parity. See `docs/dashboard/i18n.md`.
- Formatting is Prettier. ESLint runs during `next build` via `next/core-web-vitals`; do not add a second linter config.
- Keep the security headers and `output: "standalone"` in `next.config.mjs` intact.

## Layout

| Path                        | Purpose                                                                  |
| --------------------------- | ------------------------------------------------------------------------ |
| `src/app/(auth)/`           | Login and auth-adjacent routes                                           |
| `src/app/(root)/dashboard/` | Dashboard pages, including `agent/`                                      |
| `src/app/setup/`            | First-run setup wizard                                                   |
| `src/app/api/`              | Next route handlers (token refresh, health)                              |
| `src/components/ui/`        | shadcn/Radix primitives                                                  |
| `src/components/agent/`     | Agent chat UI: thread, composer, conversation list                       |
| `src/hooks/`                | `use-agent-chat`, auth, API query, debounce                              |
| `src/utils/`                | API client, endpoint constants, SSE parser, helpers                      |
| `src/i18n/`                 | Locale config, EN/ZH dictionaries, provider, formatters, server resolver |
| `src/components/i18n/`      | Language switcher                                                        |
| `src/store/`                | Redux Toolkit store and reducers                                         |
| `src/types/`                | Shared API types                                                         |
| `src/styles/globals.css`    | Tailwind layers and design tokens                                        |

## Notes

- The Agent page consumes `POST /agent` as SSE plus `/conversations*` from `server/`. Keep the contract in `docs/dashboard/agent-chat-design.md` in sync when it changes.
- No test runner is configured; verify changes with `pnpm typecheck` and `pnpm build`.
