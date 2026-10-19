# web — Vue 3 SPA (task list, SSE timeline, gate decisions)

Pure API consumer: no SSR, no state library. Vue 3 + TypeScript + Vite,
vue-router, hand-written CSS. Every request carries `X-User-Id` (src/api.ts);
SSE is consumed via fetch + ReadableStream (src/sse.ts) because the native
EventSource can neither send custom headers nor control reconnection.

Screens (design: repo LLD §15):

- `/tasks` — task list + submit (title + PRD, 64 KiB pre-checked client-side)
- `/tasks/:id` — SSE timeline, gate decision forms (approve/reject, feedback,
  evidence veto), artifact cards, cooperative cancel

Run: `docker compose up -d web` → http://localhost:3000 (nginx proxies /v1 to
api; `proxy_buffering off` keeps the event stream live).
Dev: `npm install && npm run dev` — vite proxies /v1 to localhost:8000.
