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
| T3 | PII persists in chunks / vector index / answers | `redact_pii` runs at ingest, before chunking (ADR-0010): email, phone, US SSN, Luhn-valid cards → typed placeholders, tolerant of the whitespace/newline splits PDF extraction introduces. Never reaches a stored chunk, the index, or an answer. Backfill (`reingest_documents`) targets every document with chunks. | "Obvious PII" only — misses names, addresses, novel formats, and PII broken *mid-token* by a hard wrap; can false-positive on phone-shaped triples. A mitigation, not a guarantee. If a source file is gone, stale chunks can't be re-redacted and retrieval doesn't gate on status — deleting the document is the full purge. Raw file remains on disk (tenant-scoped, non-public). | `test_guardrails.py`, `test_ingestion.py::test_ingestion_redacts_pii_before_storing_chunks` / `…split_across_a_page_join_newline`, `test_reingest.py` |
| T4 | Ingestion resource exhaustion (huge/complex/looping document monopolizes the shared worker) | Bounds (#47): Celery `soft_time_limit`/`time_limit`; PDF page-count and extracted-text-size caps in `parsing.py`; a soft-limit hit is a **permanent** failure (no retry amplification). | Bounds are generous defaults; tuning is per-deployment. | `test_parsing.py`, `test_ingestion.py`, `test_tasks.py` |
| T7 | API-edge capacity abuse: one tenant floods `/api/query` (unbounded LLM spend) or uploads/reads, degrading the shared service for others | **Per-tenant** throttling (#49, ADR-0011): sliding-window burst rates on separate query/upload/read scopes, plus fixed-window daily/monthly query **quotas** — all keyed on the tenant, so one tenant's exhaustion can never consume another's budget. 429 + `Retry-After`. Limits are configuration. | Multi-worker correctness depends on a shared cache (Redis); it fails *open* if the cache is unreachable. Quota counters are coarse (may admit one or two over the limit). Unauthenticated-request cost is an edge/WAF concern (DRF rejects anon before throttles run), deferred to #25. Precise token/$ accounting is #17. | `test_throttling.py`, `test_ratelimit_api.py::test_hammering_the_query_endpoint_throttles_the_tenant_but_not_another` |
| T5 | Service internals leak to a tenant via error messages | `_user_safe_message` (#47) maps every failure to a sanitized reason; the raw exception (DSNs, hostnames, paths) goes only to the server log. | — | `test_ingestion.py`, `test_documents_api.py::…error_is_sanitized…` |
| T6 | Fabricated answers / invented citations | Grounding contract (ADR-0007/0008): answer only from numbered sources; citations resolve to real retrieved chunk IDs, invented numbers are dropped. | The LLM can still misread a source; grounding constrains, doesn't verify semantics. | `test_generation.py`, `test_rag.py` |

## Out of scope (tracked elsewhere)

- Per-tenant cost/token accounting (#17) — precise per-token/$ metering on top of T7's request-count quotas.
- Unauthenticated / per-IP edge throttling (#25) — a WAF concern; DRF rejects anon requests before app-layer throttles run.
- Secrets management, network policy, and infra hardening (deployment concern, M6+).
- Semantic correctness of answers beyond grounding (evaluation suite, #21/M5).

## How to extend this document

When you add a data path or a control, add a row: name the threat, the mitigation, its honest limit,
and the test that proves it. A mitigation without a proof column is a claim, not a control.
