# TenantIQ threat model

A living document. Scope: the multi-tenant RAG data path — upload → ingest → retrieve → generate →
answer. It records the assets we protect, who we defend against, the trust boundaries, the concrete
threats, and what mitigates each (with honest limits). It is written to be falsifiable: most
mitigations map to a test named in the last column.

## Assets

| Asset | Why it matters |
|-------|----------------|
| Tenant document content & chunks | The customer's private data; the core confidentiality asset. |
| Cross-tenant isolation | One tenant reading another's data is the catastrophic failure (CLAUDE.md: "isolation is sacred"). |
| The system prompt & grounding contract | If overridden, answers stop being grounded/cited and can leak or fabricate. |
| PII inside documents | Regulated/sensitive; every store it lands in widens exposure. |
| The shared ingestion worker | A saturated worker is denial-of-service for every tenant. |
| Per-tenant usage & cost data | Spend reveals a tenant's query volume and behaviour — competitively sensitive even though it contains no document text. |
| Service internals (DSNs, hostnames, keys) | Leaked internals aid a further attack. |

## Trust boundaries

1. **Browser → API.** Every request is Bearer-authenticated; the token's issuer selects the tenant
   (ADR-0002). Everything past the auth seam runs inside one tenant's context.
2. **Uploaded file → pipeline.** An uploaded document is **untrusted input** — possibly malformed,
   oversized, or adversarial.
3. **Retrieved chunk → LLM prompt.** Retrieved document text is **untrusted content** placed next to
   our instructions. This is the prompt-injection boundary.
4. **LLM / embedder → service.** External model backends; treated as fallible (may time out, may
   return malformed structured output).

## Adversaries

- **Malicious/curious tenant** — tries to read another tenant's data, or to steer the model via a
  crafted document (prompt injection), or to exhaust shared capacity.
- **Malicious document author** — a third party whose document a tenant ingests; carries injection
  payloads or resource bombs.
- **Passive data-exposure risk** — PII sitting in more stores than necessary, surfacing in answers or
  logs.

## Threats & mitigations

| # | Threat | Mitigation | Limits | Proof |
|---|--------|-----------|--------|-------|
| T1 | Cross-tenant data read via any query path | Two layers: tenant-scoped manager (raises if no tenant) + **forced Postgres RLS** under a non-superuser role (ADR-0002). Retrieval, generation, and the query API all inherit it. | RLS depends on running as the app role; a superuser DB connection would bypass it. | `test_rls.py`, `test_tenant_isolation.py`, `test_rag.py::…cannot_ground_in_another_tenants_chunks` |
| T2 | Prompt injection: a document overrides the system prompt / exfiltrates the prompt / changes the model's role | **Structural** defense (ADR-0010): each source is wrapped in an unforgeable `[[UNTRUSTED SOURCE …]]` fence — content cannot forge the marker or smuggle chat-role/control tokens — and the system prompt frames fenced content as untrusted data to be ignored as instructions. | Not a formal guarantee: a sufficiently misaligned model could still be swayed. Defense-in-depth, not proof. | `test_guardrails.py`, `test_rag.py::…fences_each_source…` / `…cannot_forge_a_fence…`, `test_generation.py::test_document_injection_cannot_override_system_instructions` |
| T3 | PII persists in chunks / vector index / answers | `redact_pii` runs at ingest, before chunking (ADR-0010): email, phone, US SSN, Luhn-valid cards → typed placeholders, tolerant of the whitespace/newline splits PDF extraction introduces. Never reaches a stored chunk, the index, or an answer. Backfill (`reingest_documents`) targets every document with chunks. | "Obvious PII" only — misses names, addresses, novel formats, and PII broken *mid-token* by a hard wrap; can false-positive on phone-shaped triples. A mitigation, not a guarantee. If a source file is gone, stale chunks can't be re-redacted and retrieval doesn't gate on status — deleting the document is the full purge, and since #51 that is an actual endpoint which removes the raw file too (ADR-0015). Until it is deleted the unredacted upload stays on disk (tenant-scoped, non-public). | `test_guardrails.py`, `test_ingestion.py::test_ingestion_redacts_pii_before_storing_chunks` / `…split_across_a_page_join_newline`, `test_reingest.py` |
| T4 | Ingestion resource exhaustion (huge/complex/looping document monopolizes the shared worker) | Bounds (#47): Celery `soft_time_limit`/`time_limit`; PDF page-count and extracted-text-size caps in `parsing.py`; a soft-limit hit is a **permanent** failure (no retry amplification). | Bounds are generous defaults; tuning is per-deployment. | `test_parsing.py`, `test_ingestion.py`, `test_tasks.py` |
| T7 | API-edge capacity abuse: one tenant floods `/api/query` (unbounded LLM spend) or uploads/reads, degrading the shared service for others | **Per-tenant** throttling (#49, ADR-0011): sliding-window burst rates on separate query/upload/read scopes, plus fixed-window daily/monthly query **quotas** — all keyed on the tenant, so one tenant's exhaustion can never consume another's budget. 429 + `Retry-After`. Limits are configuration. | Multi-worker correctness depends on a shared cache (Redis); it fails *open* if the cache is unreachable. Quota counters are coarse (may admit one or two over the limit). Unauthenticated-request cost is an edge/WAF concern (DRF rejects anon before throttles run), deferred to #25. Precise token/$ accounting is #17. | `test_throttling.py`, `test_ratelimit_api.py::test_hammering_the_query_endpoint_throttles_the_tenant_but_not_another` |
| T5 | Service internals leak to a tenant via error messages | `_user_safe_message` (#47) maps every failure to a sanitized reason; the raw exception (DSNs, hostnames, paths) goes only to the server log. | — | `test_ingestion.py`, `test_documents_api.py::…error_is_sanitized…` |
| T6 | Fabricated answers / invented citations | Grounding contract (ADR-0007/0008): answer only from numbered sources; citations resolve to real retrieved chunk IDs, invented numbers are dropped. | The LLM can still misread a source; grounding constrains, doesn't verify semantics. | `test_generation.py`, `test_rag.py` |

| T8 | A tenant reads another tenant's usage/cost data (inferring its query volume and behaviour) | `UsageRecord` is a `TenantOwnedModel`, so reporting reads through the tenant-scoped manager, and migration `0012` gives the table the same **forced RLS** policy as documents/chunks (#17, ADR-0012). The `/api/usage` aggregate can only ever cover the caller's tenant. | Cost figures are *estimates* (chars-per-token heuristic), so they are a spend signal, not an invoice — see ADR-0012. Rows accumulate indefinitely (no retention policy yet). | `test_usage.py::test_summary_never_includes_another_tenants_spend`, `test_usage_api.py::test_usage_endpoint_never_reports_another_tenants_spend`, `test_rls.py::test_every_tenant_owned_table_has_forced_rls` |

| T9 | **Forced-tenant login (session fixation by workspace).** A third-party page starts a login for a tenant *it* controls; the victim authenticates against the attacker's realm and their next upload lands in the attacker's workspace. | `/api/auth/login` is a **POST** requiring `Origin == APP_BASE_URL` (#18, ADR-0013 §1). `SameSite=Lax` deliberately permits top-level GET navigations, so a GET login endpoint would be exploitable by a plain link; a cross-site form POST always carries a mismatched `Origin`. Note the data crosses even though tenant isolation holds perfectly at every layer — this is an *authentication* flaw, not an isolation one. | `Origin` is absent on some legacy clients; those are rejected rather than admitted. | `auth-login.test.ts::refuses a cross-site login attempt…` / `…no Origin header` |
| T10 | **CSRF on the BFF.** Cookie auth means the browser attaches credentials automatically, so any cross-site page could drive state-changing API calls. | `Origin` equality is checked **first and fails closed** on every non-GET, with a double-submit cookie+header token as defence in depth (#18). The token alone is insufficient: it proves only that the caller could *read* the cookie, and cookie write scope is same-**site** — a sibling subdomain, or in dev any other port on `localhost`, can plant both halves. | Relies on browsers sending `Origin` on state-changing requests, which all currently supported ones do. | `csrf.test.ts::rejects a cross-origin request even when both halves of the token match`, `proxy.test.ts` CSRF cases |
| T11 | **Token or session exfiltration through the proxy.** A crafted `/api/*` path redirects the upstream call to an attacker's host, which then receives a valid tenant bearer token; or upstream headers leak back into the app's origin. | Path segments are charset-allowlisted and dots-only segments rejected, and the upstream URL is built as an absolute `/api/...` path so the origin cannot move (#18). Request headers are an **allowlist** (never the browser's `Cookie`, a client `Authorization`, `Host`, or `X-Forwarded-*`); response headers are an allowlist too (never `Set-Cookie`, `Content-Length`, `WWW-Authenticate`). Reachable without any CSRF token, since a GET is a plain navigation that `Lax` permits — hence the check runs before any outbound fetch. | The header policy is proven as a pure function; the integration-level assertion does not discriminate in the vitest/MSW harness, which is stated in `lib/upstream.ts` rather than glossed. | `proxy.test.ts` path-hygiene cases (each asserts **nothing was fetched**), `upstream.test.ts` exact-allowlist assertions |
| T12 | **Cross-tenant bleed in the BFF session layer** — one browser's session resolving to another tenant's token, or a *cached* response serving tenant A's data to tenant B. | The session cookie holds an opaque id resolving to a per-session server-side record; the proxy and the RSC both attach that session's own token, and every upstream call is `cache: "no-store"`. Every proxied response is additionally stamped `Cache-Control: private, no-store` + `Vary: Cookie` — Django sets no caching headers on most responses, which would leave per-tenant data on a cookie-authenticated URL *heuristically cacheable* by any shared cache. Tokens are never rendered into HTML. | The store is in-process, so it does not survive a restart or span containers (accepted; Redis is a drop-in). | `proxy.test.ts::never serves one tenant's session with another tenant's token or data` / `::marks proxied tenant data uncacheable`, `TenantHome.test.tsx::renders each session's own tenant…` / `::opts the identity fetch out of the fetch cache`, `session.test.ts::keeps two sessions completely separate` |
| T13 | **A sign-out that does not sign out.** The user clicks Sign out, the UI agrees, and the session cookie stays in the browser — on a shared machine, for its full lifetime. | Cookies are expired with `clearCookie` (`lib/session.ts`), which re-sets them with their real flags plus a zero lifetime. `response.cookies.delete()` cannot be used: it emits no `Secure`, and RFC 6265bis requires a browser to **ignore** a `__Host-`-prefixed `Set-Cookie` that lacks it — so every deletion was a silent no-op on https, the only deployment where the prefix applies. Logout also deletes the server-side record, so the id is worthless regardless. | Tests pin the https behaviour explicitly, because the default test environment is http, where the bug is invisible. | `auth-logout.test.ts::emits clearing cookies the browser will actually accept`, `auth-callback.test.ts::clears the transaction cookie with Secure on https` |
| T14 | **Cross-tenant destruction.** The API's first destructive route (`DELETE /api/documents/<id>`, #51) means a mis-scoped lookup no longer leaks another tenant's data — it destroys it, irreversibly, along with the chunks and the raw file. | The same two layers as every read path (ADR-0002): the endpoint resolves through the tenant-scoped manager, so another tenant's id is a 404 before any delete is attempted, and forced RLS is underneath it. Deletion is deliberately permitted in any status, including PROCESSING, so a wedged document stays deletable (ADR-0015); the worker treats the vanished row as a normal stop. Since #20 the route is reachable from the UI, where a two-step per-row confirmation that names the document and states that its passages go too is the only thing between a misclick and irreversible loss (ADR-0017) — a UX control, not a security one, but the one that matters for the *owning* tenant. | No soft delete and no undo: a delete by the *owning* tenant is final. Orphaned storage is possible if the process dies between the row's commit and the file callback — logged, not reconciled. | `test_document_detail_api.py::test_delete_of_another_tenants_document_is_404_and_changes_nothing`, `…::test_delete_removes_the_chunks`, `…::test_the_file_is_destroyed_only_once_the_row_delete_commits`, `documents-screen.test.tsx::asks before deleting…` / `…keeps the row when the delete was refused` |

## Out of scope (tracked elsewhere)

- Exact provider token counts — #17 ships estimates; replacing them means surfacing provider usage through the streaming `LLMClient` (ADR-0012).
- Embedding (ingest) cost metering — `UsageRecord.kind` leaves room for it; ingest volume is already bounded by #47.
- Unauthenticated / per-IP edge throttling (#25) — a WAF concern. Note the tenant-discovery endpoint (#18) is the one route that *is* reachable unauthenticated; it is throttled per requested slug, because behind the BFF every such request arrives from the Next server and a per-IP bucket would be a single global one. An attacker rotating slugs is unbounded by that and remains an edge concern.
- Back-channel logout / token revocation — an IdP-side session termination takes effect at the API only when the current access token expires (ADR-0013 Consequences).
- Secrets management, network policy, and infra hardening (deployment concern, M6+).
- Semantic correctness of answers beyond grounding (evaluation suite, #21/M5).

## How to extend this document

When you add a data path or a control, add a row: name the threat, the mitigation, its honest limit,
and the test that proves it. A mitigation without a proof column is a claim, not a control.
