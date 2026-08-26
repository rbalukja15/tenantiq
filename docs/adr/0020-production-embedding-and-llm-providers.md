# ADR-0020 — Voyage for embeddings, and one dimension everywhere

- **Status:** Accepted
- **Date:** 2026-08-24

## Context

#53 is the last unknown between here and a live deployment: dev embeds with a local Ollama model and
tests use a hashing stand-in, so nothing in the repo said what production embeds with. ADR-0004 flagged
why that cannot be deferred forever — **the embedding provider is a dimension decision**, the pgvector
column and its HNSW index are a fixed width, and changing it after a corpus exists means re-embedding
everything.

Anthropic has no embeddings API, so "use Anthropic" answers only half the question. The two halves —
what embeds, and what generates — turned out to have different answers and different cost shapes.

## Decision

**Voyage AI (`voyage-4`) for production embeddings.** Retrieval-specialist models, and the free grant
is 200 million tokens against a corpus measured in thousands — so for this project's scale the
embedding bill is zero and would stay zero at a scale it will not reach. The client is standard-library
`urllib`, like `OllamaEmbedder`: the official SDK would be a dependency for one JSON body to one
endpoint, and the HTTP boundary is already the seam the tests stub.

**1024 dimensions, in every environment, including tests.** This is the consequential part.
`voyage-4` emits 256/512/1024/2048 and **cannot emit 768**, so the previous local model
(`nomic-embed-text`, 768) could not stay. The alternative — a 768 dev and a 1024 prod — is worse than
it sounds: `Chunk.embedding` is `VectorField(dimensions=settings.TENANTIQ_EMBEDDING_DIM)`, so the width
is serialized into a migration. Two widths means the migration history describes a schema neither
environment has, and `makemigrations --check` in CI becomes a check on which `.env` the runner had.

So the local model moves to `mxbai-embed-large`, which emits 1024. One width, one migration history,
one number in every config file.

**Migration 0014 discards every existing vector.** A 768-dim vector cannot be widened into a
meaningful 1024-dim one, so the honest operation is to drop them; `manage.py backfill_embeddings`
refills exactly the rows it nulls, because it only touches `embedding IS NULL`. Documents keep their
text, chunks and offsets. Until the backfill runs retrieval returns nothing, rather than returning
wrong things.

The migration clears the data with **DDL rather than an UPDATE**, and that is not a style choice.
`app_chunk` has FORCE'd row-level security (ADR-0002) and a migration runs with no
`app.current_tenant` GUC, so `UPDATE app_chunk SET embedding = NULL` matches **zero rows** and
succeeds silently — leaving every old vector in place for the `ALTER` to choke on. RLS governs DML,
not DDL. This was verified against a real database with a real 768-dim row, not reasoned about: the
`UPDATE` reported `UPDATE 0`, and `ALTER TABLE ... TYPE vector USING NULL` cleared it.

**Generation stays unset by default, and that is the cost control.** `TENANTIQ_LLM_FACTORY` already
falls back to Ollama when `ANTHROPIC_API_KEY` is absent. Embeddings are a one-off per document;
generation is per query and unbounded by anything except traffic. So the deployment ships with the
embedding provider configured and the answering provider **not**, which means a misconfigured deploy
fails loudly rather than billing an account nobody meant to use. Setting the key is a deliberate act,
and the per-tenant quotas from #49 are what cap it afterwards.

**The default embedder is still Ollama, not Voyage.** A fresh clone must cost nothing and need no
account, so production opts in by setting `TENANTIQ_EMBEDDER_FACTORY` and `VOYAGE_API_KEY` together.
Spending money is never the path of least resistance.

**`TENANTIQ_VOYAGE_MODEL`, not a repurposed `TENANTIQ_EMBEDDING_MODEL`.** The latter already means
"the Ollama model" in every existing `.env`, and silently changing what a documented variable means is
worse than an inconsistent name — a knob that quietly does something else is the same defect class as
the compose forwarding gap. The asymmetry with `TENANTIQ_LLM_MODEL` / `TENANTIQ_LLM_OLLAMA_MODEL` is
the price, and it is the cheaper one.

**Three request fields are stated rather than defaulted**, because each default is wrong here:
`input_type` (`document` when embedding a corpus, `query` when embedding a question — Voyage prepends
a different retrieval prompt, and the wrong one degrades ranking *without erroring*);
`output_dimension` (so a future change to the model's default cannot start returning vectors the
column will not take); and `truncation: false` (the default embeds a *prefix* of an over-long input,
which for a citation-grounded product is the quiet kind of wrong).

## Consequences

**The forced model change was an upgrade, and that is measured.** Re-running #21's harness on
`mxbai-embed-large`:

| Metric   | `nomic-embed-text` (768) | `mxbai-embed-large` (1024) |
| -------- | ------------------------ | -------------------------- |
| hit@1    | 0.56                     | **0.83**                   |
| recall@3 | 0.94                     | **1.00**                   |
| MRR      | 0.75                     | **0.91**                   |

The previous evaluation's headline finding — the right passage is in the top 3 but rarely at rank 1 —
is largely gone. **The only reason that is a fact rather than a hope is that the numbers were already
being measured.** #22's harness had already caught a real defect (the context window in #90); this is
the other use, where a forced change gets accepted on evidence instead of on the assumption that a
bigger model is probably fine.

Note what did *not* improve: the separation gap between answerable and unanswerable questions
narrowed (+0.155 → +0.126). Better ranking and better refusal are different properties.

**These are not the production numbers.** They were measured on the local model that shares Voyage's
width, not on Voyage. Matching widths does not mean matching geometry, so `make eval` must be re-run
with a `VOYAGE_API_KEY` before anything here is quoted as production, and #88's similarity floor must
be derived from *that* run. Every report is stamped with the embedder that produced it precisely so
the two cannot be confused.

**Everyone with an existing corpus must re-embed**, including the local demo stack. The upgrade path
is `migrate` then `backfill_embeddings`, and the dimension guard from #46 makes a half-done upgrade
loud: pointing `TENANTIQ_EMBEDDING_MODEL` back at a 768 model fails ingestion with a message naming
both numbers rather than storing a vector the column rejects. That guard fired for real during this
work, when a stale `.env` still pinned `nomic-embed-text`.

**Voyage's free grant is one-time, not recurring.** It is enormous relative to this corpus, but a
deployment that re-embeds continuously would eventually exhaust it and start billing at
$0.06/M tokens. Nothing in the code watches that; the cost accounting in #17 covers generation only.

**Still open.** Generation has no provider decision beyond "Anthropic if a key is set" — model choice
and a spend ceiling are a judgement call the deployment makes, not the repo. The embedding cost is not
metered the way the LLM's is.
