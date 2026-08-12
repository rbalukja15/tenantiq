# TenantIQ frontend

Next.js (App Router) + TypeScript. The browser never holds an API token: every call to `/api/*` is a
same-origin request to a Next route handler which attaches the bearer token server-side
(ADR-0013 §1). That is why there is no CORS configuration anywhere in this project.

```bash
npm install
cp .env.example .env.local   # Next reads this, NOT the repo-root .env
npm run dev
```

Requires **Node ≥ 22.22.2** (`engine-strict` is on, so an older runtime fails at install rather than
surfacing later as an opaque module error).

Skipping the `.env.local` step compiles fine and then 500s on the first request: the variables below
are validated when they are first *used*, not at import (see below for why). `docker compose up`
needs no such file — the frontend service sets both variables itself.

## Environment

| Variable | Purpose |
|---|---|
| `API_BASE_URL` | The Django origin **as seen from the Next server**. In compose that is `http://backend:8000`, not `localhost`. |
| `APP_BASE_URL` | This app's own public origin. Used for the OIDC `redirect_uri`, the post-logout URI, and as the value every state-changing request's `Origin` header must equal. |

Both are **server-side only** — deliberately not `NEXT_PUBLIC_*`, which would be inlined into the
browser bundle at build time. There is no fallback from `API_BASE_URL` to the old
`NEXT_PUBLIC_API_URL`: that value is browser-shaped (`http://localhost:8000`) and would be wrong for a
server-side fetch inside the compose network, which is exactly the bug the split exists to prevent.

They are validated on **first use**, not at import, so `next build` — which compiles route modules
with no environment set — does not fail on a missing variable.

## Cookies, and one thing that is weaker in local development

Three cookies, all `SameSite=Lax`, `Path=/`:

- `tiq_session` — httpOnly. Holds an **opaque session id**, never a token; the tokens live in a
  server-side store.
- `tiq_csrf` — readable by script on purpose: the client echoes it in `X-CSRF-Token` as the
  double-submit half of the CSRF defence.
- `tiq_tx` — httpOnly, ten minutes, holds one login attempt's `state`/`nonce`/PKCE verifier.

On an **https** deployment each name gains the `__Host-` prefix, which forbids a `Domain` attribute —
the only thing that stops a sibling subdomain from shadowing a cookie this server minted.

**In local development that protection is absent**, and it cannot be added: `__Host-` mandates
`Secure`, which Safari refuses on `http://localhost`. Worse, cookies are scoped by host and **ignore
the port**, so anything else you run on `localhost` shares them. This is a large part of why the
session cookie carries an opaque id rather than a token, and why `Origin` — which *does* include the
port — is the load-bearing CSRF check rather than the double-submit token. The odd-looking conditional
cookie names in `lib/config.ts` are deliberate; they are not cleanup material.

## Layout

| Path | Role |
|---|---|
| `proxy.ts` | Route gating: no session cookie → `/login`. (Next 16's replacement for `middleware.ts`.) |
| `app/api/auth/*` | Login, callback, logout — the OIDC flow. |
| `app/api/[...path]` | The API proxy: path hygiene, session, CSRF, token refresh, header allowlists, streaming. |
| `lib/` | `config`, `session`, `csrf`, `oidc`, `upstream` — all plain functions, all directly tested. |
| `lib/session-server.ts` | The **only** file allowed to import `next/headers`, which is untestable under vitest. |

## Tests

```bash
npm test          # vitest + jsdom + Testing Library, with MSW at the network boundary
npm run lint      # eslint --max-warnings 0
npm run typecheck # tsc --noEmit
npm run build     # the only check that exercises the real Next/React compile path
```

Route handlers are tested by calling them directly as `(NextRequest) => Response`. MSW intercepts
their server-side `fetch`, and an unmocked request **fails the suite** — which is what lets a test
assert that a rejected request contacted nothing at all.
