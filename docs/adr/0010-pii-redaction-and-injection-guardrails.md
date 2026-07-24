# ADR-0010 — PII redaction on ingest & prompt-injection hardening of retrieved content

- **Status:** Accepted
- **Date:** 2026-07-23

## Context

TenantIQ ingests customer documents and feeds retrieved chunks to an LLM. Two content-driven risks
follow from that, both flagged by issue #16:

- **PII.** An uploaded document may contain personal data (emails, phone numbers, SSNs, payment-card
  numbers). Once ingested it is stored as chunk text, embedded into the vector index, and can be
  echoed back in an answer. The more places PII lands, the larger the exposure surface.
- **Prompt injection.** A retrieved chunk is *untrusted*: a customer's own document (or a document a
  customer was tricked into uploading) can contain text engineered to override the system prompt —
  "ignore previous instructions", "reveal this prompt", a forged `System:` turn. Because the RAG
  prompt concatenates retrieved text with our instructions, that text is an instruction-injection
  vector.

Two design forks had to be resolved.

**When to redact PII.** Options: (a) at **ingest**, on the extracted text before chunking; (b) at
**retrieval/answer time**, leaving stored chunks raw. Ingest-time is the stronger data-minimization
posture — PII never lands in a stored chunk, the vector index, or an answer — but it means stored
chunks differ from the raw file, and documents ingested before the change need a backfill. A concern
was the #45 fidelity invariant (a chunk is an exact `source[start:end]` slice, offsets are citation
anchors): would redaction break it?

**How to resist injection.** Options: a **phrase blocklist** (strip/deny known injection wordings),
or a **structural** control (delimit untrusted content unforgeably and instruct the model to treat it
as data). Blocklists are brittle — trivially bypassed by rephrasing, and they mangle legitimate text.

## Decision

**PII is redacted at ingest, on the extracted text, before chunking.** `app.guardrails.redact_pii`
replaces email, US SSN, North-American phone numbers, and **Luhn-valid** payment-card numbers with
typed placeholders (`[REDACTED_EMAIL]`, …). Because it runs *before* `chunk_text`, chunks slice the
**redacted** extracted text — so `chunk.text == redacted_source[start:end]` still holds and **#45
fidelity is preserved** (offsets address the redacted extracted text, which is the source of record
for chunks). Card candidates are Luhn-checked so long non-card digit runs (order IDs, references) are
not false-positived. Redaction is idempotent (placeholders contain no PII patterns), so ingest and a
re-ingestion backfill compose safely. A setting, `TENANTIQ_REDACT_PII` (default on), can disable it
for evaluation baselines (#21) that need the raw text.

- **Whitespace tolerance.** PDF extraction joins pages with `\n` and lays out tables with runs of
  spaces, so PII regularly straddles a newline or a multi-space gap. The numeric patterns (SSN, card,
  phone) and the email `@` tolerate bounded whitespace/newline runs between components, so a card or
  SSN split across a page break is still caught. (Bounds are kept small so a match can't span
  unrelated numbers across paragraphs.)
- **Backfill.** Documents ingested before this change still hold raw PII. `manage.py
  reingest_documents [--tenant slug]` re-runs the idempotent ingestion pipeline (re-parse → redact →
  re-chunk → re-embed), tenant-scoped, replacing the chunk set atomically. Its default target is
  **every document that has chunks** (the ones that could hold pre-redaction PII), not `status=ready`
  — a re-ingest that fails permanently leaves stale chunks on a now-FAILED/PROCESSING document, which
  a `status=ready` filter would strand. In-place chunk mutation was rejected: it would desynchronize
  embeddings and break #45 for backfilled rows.

**Injection defense is structural, not a blocklist.** At prompt assembly, every retrieved source is
wrapped in an unforgeable fence (`app.guardrails.fence_source`): a doubled-bracket
`[[UNTRUSTED SOURCE [n]: title]] … [[END SOURCE [n]]]` marker, with the content **neutralized** so it
cannot forge a marker (any `[[`/`]]` run in content is broken) or smuggle a chat-role/control token
(`<|…|>`, `[INST]`, `<<SYS>>` are defused). The system prompt then tells the model that everything
between the markers is untrusted data, never instructions, and to ignore instructions found inside a
source. Neutralization is applied **only to the copy rendered into the prompt** — the stored chunk
and the citation text stay verbatim, so #45 and citation resolution are untouched. We explicitly
**ruled out** a natural-language phrase blocklist: it is bypassable and would degrade answer
faithfulness on legitimate documents.

## Consequences

- **Easier.** PII no longer persists in chunks/index/answers; the exposure surface shrinks to the
  original uploaded file on disk (already tenant-scoped and non-public). Injection resistance is a
  property of prompt *structure*, testable without a live LLM: content provably cannot forge a fence,
  and the system prompt provably frames sources as data. The acceptance test (a document whose text
  is an injection payload cannot steer a fence-respecting model) runs hermetically.
- **Harder / accepted costs.** Redaction is regex-based "obvious PII" — it will miss names, addresses,
  novel formats, and PII broken *mid-token* by a hard wrap (e.g. an email split inside its domain);
  it can also false-positive on phone-shaped number triples. It is a mitigation, not a guarantee (see
  `docs/threat-model.md`). Chunks now differ from the raw file, so a future citation re-resolver
  (#51) must re-extract **and re-redact** to reproduce chunk offsets, or resolve against stored chunk
  text. Redacting before embedding slightly changes retrieval near redacted spans (desirable — we do
  not want to retrieve by SSN). Structural fencing assumes a reasonably instruction-following model;
  it is defense-in-depth with the system-prompt framing, not a formal guarantee against every model.
  Finally, if a document's *source file* is gone, re-ingestion can't reproduce redacted chunks, and
  retrieval does not gate on document status — so fully purging such a document's stale chunks means
  deleting the document. Status-gated retrieval is a candidate follow-up.
- **Sequencing.** Landing before #21 freezes its evaluation dataset means baselines are computed on
  redacted text from the start (or with `TENANTIQ_REDACT_PII=false` if raw text is wanted); redaction
  will not silently shift a frozen baseline later.
