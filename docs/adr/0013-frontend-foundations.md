# ADR-0013 — Frontend foundations: tenant discovery, token storage, data fetching, streaming client

- **Status:** Accepted
- **Date:** 2026-07-27

## Context

M4 builds the actual product surface: an app shell with auth (#18), a streaming chat UI with
citations (#19), and document management (#20). #52 exists because #18 could not start — four
foundational questions were unanswered, and the scaffold's toolchain could not test UI at all.

The unanswered questions:

- **Tenant discovery (chicken-and-egg).** Per-tenant OIDC config lives only in the database
  (`Tenant.oidc_issuer` / `oidc_client_id`, ADR-0002) and `GET /api/me` requires a token. A browser
  arriving at the login page therefore has no way to learn *which* Keycloak realm and client to
  authenticate against — it needs that config *before* it can obtain a token.
- **Token storage, and therefore CORS.** A browser on `:3000` sending an `Authorization` header to
  `:8000` is a cross-origin request needing CORS. But the real decision underneath is *where the
  token lives*, because that determines whether cross-origin calls happen at all.
- **Data fetching.** Server Components, a client-side cache library, or plain `fetch` in effects.
- **Streaming client.** Native `EventSource` cannot send an `Authorization` header, which is why
  ADR-0009 already specified `fetch` + `ReadableStream`; the frontend needs a concrete SSE parser.

And the toolchain: React 18.3 pinned against Next 15.5 (the App Router pairs with React 19), ESLint 8
(end-of-life) on a legacy `.eslintrc`, vitest two majors behind, and — most importantly — **no
component-test infrastructure at all** (no DOM, no Testing Library, no API mocking), despite every
#18–#20 acceptance criterion being UI behaviour.

## Decision

### 1. Token storage: a BFF proxy with httpOnly cookies

The browser **never holds an access token**. Next.js route handlers under `/api/*` act as a
Backend-for-Frontend: they hold an **opaque session id** in an httpOnly, Secure, SameSite=Lax cookie
the page's JavaScript cannot read; the tokens themselves never leave the server. The id resolves to a
server-side session record, and the route handler attaches the bearer token to the upstream Django
call.

*(Corrected during #18. The original wording put the token itself in the cookie. Browsers cap a
cookie at roughly 4 KB and **silently drop** an oversized `Set-Cookie` — no error, no warning — so a
realm whose access token carries a few roles and groups, plus a refresh token and an ID token, would
produce a login that succeeds server-side followed by an invisible redirect loop: on that tenant only,
in production only. An opaque id is a fixed ~36 bytes whatever the IdP emits. The store is an
in-process `Map` pinned on `globalThis`; a frontend restart drops sessions and users re-login through
a still-valid IdP SSO session, which costs one invisible redirect. Swapping it for Redis is one file
behind an unchanged interface.)*

The BFF authenticates as a **public** OAuth client using Authorization Code + PKCE (S256). No
per-tenant client secret is stored: the only per-tenant configuration channel is the deliberately
public discovery endpoint (§2), so a secret would require a second, authenticated Next→Django
configuration channel with its own credential. This is the RFC 9700 posture for browser-delivered
flows; confidential clients remain the documented upgrade path.

Rejected alternatives:

- *SPA + token in `localStorage`* — any XSS exfiltrates a tenant-scoped API token. Unacceptable for a
  project whose defining invariant is tenant isolation.
- *SPA + token in memory* — better (no persistence for XSS to read), but the token still lives in a
  scriptable context, it is lost on refresh without silent renew, and it forces CORS on the API.

Consequences of the BFF choice, all deliberate:

- **No CORS on the API surface.** Every XHR/`fetch` the page makes is same-origin, so Django needs no
  `Access-Control-Allow-*` configuration and there is no preflight path to misconfigure — a classic
  way to accidentally widen an API. (The OIDC login *redirect* to Keycloak is a top-level navigation,
  not a CORS request, so it is unaffected.)
- The proxy must **stream** the SSE response (#48) through to the client rather than buffering it;
  Next route handlers can return the upstream `ReadableStream` directly, and must not sit behind
  anything that re-buffers it (the backend already sends `X-Accel-Buffering: no`).
- It adds a server-side hop to every call, and the proxy becomes a component that must itself be
  careful never to forward the raw token to the browser.
- **It introduces CSRF exposure, which the bearer-token design did not have.** This is the honest
  trade for taking tokens out of JavaScript: a bearer token must be *added* by script, so a
  cross-site request can never carry it, whereas a cookie is attached by the browser automatically.
  `SameSite=Lax` blocks the cross-site POST case in every currently supported browser, but it is one
  control, not a complete answer (it does not cover same-site subdomain attackers, and `Lax` is
  required rather than `Strict` because the OIDC provider redirects back with a top-level navigation
  that must arrive already authenticated). **#18 therefore rejects every state-changing request whose
  `Origin` header is absent or does not equal `APP_BASE_URL`**, and additionally ships a double-submit
  cookie + header token as defence in depth. Naming this here so it is not discovered late: choosing
  cookies means owning CSRF.

  *(Corrected during #18. The original wording made the double-submit token the control. It cannot
  be: a double-submit token proves only that the caller could **read** the cookie, and cookie write
  scope is same-**site**, not same-**origin** — a sibling subdomain, or in development any other port
  on `localhost` (cookies ignore ports), can plant both halves of the pair and pass. `Origin` includes
  scheme, host and port and cannot be forged by a cross-origin page, so it is the load-bearing check
  and runs first. This also applies to **starting** a login, which is why `/api/auth/login` is a POST:
  `SameSite=Lax` deliberately permits top-level GET navigations, so a GET login endpoint would let any
  site push a visitor into an attacker-chosen tenant — the victim authenticates against the attacker's
  realm and their next upload lands in the attacker's workspace. Tenant isolation holds perfectly at
  every layer and the data still crosses.)*

### 2. Tenant discovery: a public, minimal discovery endpoint

`GET /api/tenants/discovery?slug=<slug>` returns **only** `{ issuer, client_id }` for an *active*
tenant. Both values are public by definition in OIDC — the issuer is fetched unauthenticated at
`/.well-known/openid-configuration`, and a public client id is not a secret. It returns 404 for an
unknown or inactive tenant and exposes nothing else about the tenant.

Rejected alternatives:

- *Subdomain → realm mapping* (`acme.tenantiq.app`) — how large multi-tenant SaaS usually does it, and
  it needs no public endpoint; rejected for now because it requires wildcard DNS + TLS and makes local
  and compose development awkward. The discovery endpoint does not preclude adding it later.
- *Single-tenant build-time env config* — simplest, but it cannot demonstrate multi-tenancy, which is
  the point of the project.

Accepted costs: it is an **unauthenticated endpoint**, so it is a tenant-slug enumeration oracle (an
attacker can learn which slugs exist). It therefore returns a minimal payload, is rate-limited on its
own `discovery` scope keyed on a hash of the requested **slug** (#49, ADR-0011), and must never leak
tenant counts or a listing. Enumeration of *names* is judged acceptable; enumeration of *data* is not,
and remains impossible.

*(Corrected during #18 — twice, because the first replacement was also wrong. It cannot reuse the
`read` scope: the tenant-keyed throttles return a `None` cache key when there is no tenant, which DRF
treats as "do not throttle", so the project's only public endpoint would have been entirely unbounded.
Nor can it be keyed on the client IP: under the BFF (§1) the browser never calls this endpoint — the
Next server calls it on the browser's behalf — so Django sees one `REMOTE_ADDR` for every discovery
request, making an IP key a single global bucket in which one anonymous flood denies login to every
tenant at once. A slug key bounds the blast radius to the tenant actually under attack. An attacker
rotating slugs is not bounded by this at all; that is accepted, since slug enumeration is already
accepted above and this is one indexed lookup returning public values. Per-client rate limiting is not
expressible in Django behind a BFF and belongs at the edge if it is ever wanted.)*

*(Implementing this endpoint belongs to #18; #52 only fixes the decision.)*

### 3. Data fetching: Server Components by default, plain `fetch` on the client

Read-only, non-interactive views fetch on the **server** (React Server Components) through the proxy's
session, so no data-fetching library is shipped to the browser for them. Genuinely interactive,
client-side state (the chat stream, upload progress, polling document status) uses plain `fetch` in
client components with explicit loading/error states.

No client cache library (TanStack Query, SWR) is adopted **yet**: the surface is small, and adding one
now would be a dependency chosen before the problem it solves has appeared. It stays an open option
for #20's document-status polling if hand-rolled state proves fiddly.

### 4. Streaming client: `fetch` + `ReadableStream` with an explicit SSE parser

Native `EventSource` stays unusable, though **not** for ADR-0009's original reason: with the BFF the
browser sends no `Authorization` header at all — the session cookie would ride along automatically —
so the header objection is moot. What rules it out is that `EventSource` is **GET-only**, while
`/api/query` is a POST carrying the question in its body. The client will `POST /api/query` via the
proxy and parse the SSE frames from
the response body with a small, tested `event:`/`data:` parser that tolerates chunk boundaries
splitting a frame mid-way (a real behaviour of chunked transfer, and an easy source of dropped
tokens). The parser is ordinary testable code, not a framework feature.

### 5. Toolchain

React 19 + Next 16 (the App Router's supported pairing), TypeScript strict, ESLint 9 **flat config**
(`eslint-config-next@16` ships a native flat config array, so no `FlatCompat` bridge is needed), and a
real component-test harness: **vitest 4 + jsdom + Testing Library + MSW**.

Mocking happens at the **network boundary** (MSW) rather than by patching modules, so a component's
real fetch-and-parse code runs in tests — the part most likely to break. Unhandled requests are
configured to **error**, so a test cannot silently pass against a request nobody mocked.

Two rules the Next presets do *not* provide are added explicitly, because the project's conventions
depend on them: `@typescript-eslint/no-explicit-any` and `no-unused-vars` are set to **error**
(`next/typescript` registers the parser but ships an empty rule set, so "no `any`" was enforced by
nothing), and `lint` runs with **`--max-warnings 0`** — Next's accessibility rules are *warnings*, and
`eslint .` exits 0 on warnings, so without this they could never fail CI.

**Node 22 LTS** replaces Node 20 in CI and the frontend image: Node 20 reached end-of-life in April
2026, and the toolchain requires ≥ 22 regardless. The declared floor is the **real** one —
`>=22.22.2`, jsdom 30's requirement — with `engine-strict=true` in `.npmrc` so an unsupported runtime
fails at install with a legible message instead of surfacing later as an opaque `ERR_REQUIRE_ESM`
from a transitive dependency. CI runs **lint → typecheck → test → build**: the build is kept because
it is the only step that exercises the real Next/React compile path (a server/client boundary
violation passes both lint and `tsc --noEmit` and fails only there).

## Consequences

- **Easier.** #18 can start: every foundational decision is fixed, and UI behaviour is testable — a
  component can render, fetch through a mocked API, and handle its error state. (`TenantBadge` proved
  that here; #18 replaced it with `TenantHome` and a suite that covers the same ground properly.)
  The BFF removes CORS from the problem space entirely and keeps tokens out of reach of XSS.
- **Harder / accepted costs.** Every API call now has a server-side hop that must be written and kept
  in sync (including streaming pass-through). **Cookie auth means owning CSRF** — `SameSite=Lax`, an
  `Origin` equality check on every state-changing request, and a double-submit token behind it (#18):
  a class of bug a bearer-token SPA does not have. The discovery endpoint is a deliberate, minimal
  public surface and a slug-enumeration
  oracle. Choosing no client cache library means some hand-rolled loading/error state, revisited if it
  gets repetitive. Next 16 and React 19 are recent majors, so breaking-change churn is possible —
  mitigated by lint, typecheck, tests, **and the build** all running in CI (the build step was added
  with this change specifically so that claim is true).
- **Sequencing.** #18 implements the discovery endpoint, the proxy route handlers, and the session
  cookie; #19 builds the SSE parser and chat UI on top; #20 reuses the same fetch conventions. The
  frontend Dockerfile and CI both moved to Node 22 in this change so nothing else has to.
- **Deferred, and bounded.** Back-channel logout and token revocation are not implemented.
  `app/auth/verifier.py` validates signature, issuer, audience and expiry with no introspection call,
  so terminating a session or disabling a user at the IdP takes effect at the API only once the
  current access token expires (Keycloak's default is five minutes). Bounded, and named here rather
  than discovered later.
