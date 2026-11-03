# ADR-0021 — The similarity floor: measured per embedder, and run by the tests

- **Status:** Accepted
- **Date:** 2026-10-22

## Context

ADR-0007 made a similarity floor the product's only way to refuse: retrieval drops any candidate
whose cosine similarity is below `TENANTIQ_RETRIEVAL_MIN_SIMILARITY`, and a question with nothing
left gets the "no relevant context" answer. It shipped the floor at `0.0` as the conservative choice,
to be calibrated once the evaluation harness existed.

The harness (#21) showed that `0.0` is not conservative but inert. Text embeddings score effectively
always above it, so every candidate survives, `has_context` is true for any tenant with one chunk,
and everything built on the refusal is unreachable in a default deployment: #15's `refused`, #19's
wire flag and ADR-0016's NoEvidence state. A question the corpus knows nothing about gets its five
nearest passages anyway, and the model is asked to answer from them. The suite never noticed (#88):
every refusal test built its empty context by hand, and none ran retrieval at the shipped value.

What decides the value:

- **The window is measured, not reasoned.** On `mxbai-embed-large` the evaluation puts every
  answerable question's best match at 0.567 or above and every unanswerable one's at 0.441 or below
  (docs/evaluation.md). Any floor in between separates the two on that dataset.
- **The window belongs to one embedder and one corpus.** Cosine scales differ between models, so a
  value measured on one can refuse answerable questions on another. Since #53 there are two:
  `mxbai-embed-large` is the default (Ollama in `make dev` and the k8s base), `voyage-4` is
  production (ADR-0020). They share a width, not a geometry, and `voyage-4` has not been measured.
- **The suite cannot hold either model.** It embeds with the hashing stand-in, whose similarities
  count shared words. Its own window on the evaluation dataset is negative (−0.33: "What is the
  capital of Peru?" scores 0.47, because "is", "the" and "of" dominate both its vector and every
  chunk's), so it can calibrate nothing.

## Decision

**The default floor is `0.50`, measured on the default embedder. Every other embedder gets its own
value from its own `make eval` run, and production keeps `0.0` until `voyage-4` has one.**

- **`0.50` sits inside `mxbai-embed-large`'s window `(0.441, 0.567)`**, 0.059 above the best
  unanswerable question and 0.067 below the worst answerable one. The midpoint is 0.504. A round
  number about equally far from both edges is the honest precision for a dataset of 21 questions,
  three of them unanswerable. It is the settings default, the k8s base value, and what compose falls
  back to when `.env` leaves it unset (compose now forwards it).
- **Production does not inherit it.** The prod overlay runs `voyage-4` and keeps `0.0`, with the
  reason written next to it, until a run against Voyage measures its window. Copying `0.50` across
  would be a guess that looks like a decision, and its failure mode, refusing questions the corpus
  answers, is worse for a live demo than the status quo. Setting the measured value is a one-line
  change to the overlay.
- **The tests run the shipped value.** One test pins the default strictly inside the recorded
  window, so a floor at or below the best unanswerable score (`0.0` included), or at or above the
  worst answerable one, fails the suite. An
  ingestion-backed test and an HTTP test show that a question with no supporting passage is refused
  at the shipped value, through configuration, without the model being asked. Tests of what happens
  *after* retrieval (streaming, usage accounting, tenant isolation, the faithfulness pass) take a
  `no_similarity_floor` fixture, so they keep testing what they claim to test instead of the refusal.
- **`make eval` exits 1 when the gap is not positive.** At that point no floor can work, which is a
  failure of the embedder or the refusal signal, not a number to scroll past.

### Rejected alternatives

- **A table of floors keyed by embedder model, chosen at runtime.** It is the right shape once there
  are many embedders. With two, it is configuration pretending to be code, and because the value is
  also corpus-specific, a table would invite trusting a number on a corpus it was never measured on.
  Each environment sets the variable instead, where its operator can see it.
- **A relative threshold** (a fraction of the top hit's score, or a gap between ranks). It still
  answers when the best match is poor, which is exactly the case refusal exists for, and it would
  need an evaluation of its own.
- **Calibrating on the hashing embedder**, so the suite's numbers mean something. Lexical overlap
  cannot separate this dataset at all, so there is nothing to calibrate.
- **Keeping `0.0` and documenting it.** That is the bug.

## Consequences

- **Easier.** With the default embedder, a question the documents cannot answer gets the refusal,
  and with it the NoEvidence state, instead of an answer built from unrelated passages. An operator
  finds the trade-off in `.env.example` and docs/deployment.md rather than in this file. A
  regression to a floor that refuses nothing fails the suite instead of shipping.
- **Harder / accepted costs.**
  - *The window is small.* One embedder, a 14-chunk corpus and three unanswerable questions. A tenant
    whose documents are much shorter or longer than the evaluation's will see different scores.
    Re-derive with `make eval` on representative documents; per-tenant floors are not built.
  - *Production refuses nothing* until `voyage-4` is measured. It is not deployed yet (#25).
  - *The pinned tests no longer run the default.* That is deliberate, and the fixture's name says so
    wherever it applies.
  - *The faithfulness numbers in docs/evaluation.md were measured at `0.0`.* Every in-corpus question
    still clears `0.50`, but any of its passages scoring under it no longer reaches the prompt, so
    the next `--faithfulness` run is not directly comparable with the last.
  - *A host run on the hashing embedder now exits 1*, because its gap is negative. That is accurate:
    no floor works for that embedder.
- Supersedes ADR-0007's "default floor is conservative (0.0)".
