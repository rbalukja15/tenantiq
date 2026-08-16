# Evaluation

The differentiator: TenantIQ's retrieval and answers are _measured_, not assumed.

```bash
make eval          # runs inside the compose stack, against the real embedder
```

## Retrieval quality (#21)

### What is measured

A curated corpus of seven documents and eighteen questions lives in
[`backend/app/eval/dataset/`](../backend/app/eval/dataset). The harness ingests the corpus into a
throwaway tenant **through the real ingestion pipeline** — the same extraction, PII redaction,
chunking and embedding the product runs — then puts every question through **the real retrieval
path**. Measuring a reimplementation of either would report on a parallel universe.

### Ground truth is a phrase, not a chunk id

Each question names one or more **marker phrases**: verbatim strings that must appear in a chunk for
it to count as relevant. A retrieved chunk is relevant if it contains any of them, compared with
whitespace collapsed.

This is deliberate. Chunk ids and indices are an artefact of the current chunking configuration —
change `TENANTIQ_CHUNK_TARGET_TOKENS`, re-ingest, or swap the extractor, and every id in the corpus
moves. A dataset keyed on ids would keep producing numbers and quietly stop measuring anything. A
marker phrase also has the property that a human can check it by reading the source document, which
an id does not.

For the same reason, **how many relevant chunks exist per question is derived, never asserted**: the
harness counts them by scanning the ingested corpus at run time. And a marker that survives dataset
validation but matches no _ingested_ chunk aborts the run instead of scoring zero — PII redaction
rewriting a phrase mid-pipeline would otherwise look exactly like a retrieval failure.

### How to read the numbers

| Metric        | Question it answers                                                                                                                   |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `hit@k`       | Did anything useful come back in the top k? The prompt is built from the top k, so a miss means the answer cannot be grounded at all. |
| `recall@k`    | Of the chunks that should have come back, how many did?                                                                               |
| `precision@k` | Of what came back, how much was relevant?                                                                                             |
| `MRR`         | How far down the list was the first relevant chunk?                                                                                   |

**`precision@k` has a ceiling of `relevant / k`.** Most questions here have exactly one relevant
chunk, so `precision@5` cannot exceed 0.20 however perfect retrieval is. A low value in that column
is usually arithmetic, not a defect — read `hit@k` and `MRR` for whether the right passage was found.

### Results

**Every result is stamped with the embedder that produced it**, because a retrieval number without
one is not a result. CI runs a deterministic hashing embedder that encodes lexical overlap and no
semantics; numbers from it exercise the harness and are never a baseline. The harness prints a loud
warning when that is what ran.

|           |                                           |
| --------- | ----------------------------------------- |
| Run       | 14 August 2026                            |
| Embedder  | `nomic-embed-text` via Ollama, 768-dim    |
| Chunking  | 800 target / 100 overlap tokens           |
| Corpus    | 7 documents, 14 chunks, 30,936 characters |
| Questions | 18 in-corpus, 3 out-of-corpus             |

| Metric    | @1   | @3       | @5       |
| --------- | ---- | -------- | -------- |
| hit       | 0.56 | **1.00** | **1.00** |
| precision | 0.56 | 0.35     | 0.22     |
| recall    | 0.50 | 0.94     | 0.97     |

**MRR 0.75.**

The shape of that is the finding: **the right passage is always within the top 3, but it is only the
single best match a little over half the time.** Since the prompt is assembled from the top
`TENANTIQ_RETRIEVAL_TOP_K` (default 5) sources, answers are groundable for every question in this
set — but the model is frequently handed the correct passage as source [2] or [3] rather than [1],
with near-miss passages above it. That is an argument for keeping `k` at 5 rather than trimming it,
and a concrete target for a reranking step later.

### Choosing the similarity floor

Retrieval always returns its `k` nearest neighbours however far away they are, so
`TENANTIQ_RETRIEVAL_MIN_SIMILARITY` is the only thing that lets the product refuse. The dataset
includes three questions the corpus cannot answer, purely to measure **separation**:

|                                   |            |
| --------------------------------- | ---------- |
| Worst in-corpus top similarity    | 0.558      |
| Best out-of-corpus top similarity | 0.403      |
| Gap                               | **+0.155** |

A floor anywhere in `(0.403, 0.558)` refuses every unanswerable question while still answering every
answerable one. **The shipped default is `0.0`, which refuses nothing** — every question gets sources
whether or not the corpus has an answer, and the refusal state the UI builds (ADR-0016) is
unreachable in a default deployment. On this corpus a value around `0.45`–`0.50` is defensible.

That number is corpus- and embedder-specific and should be re-derived, not copied, after #53 settles
the production embedding provider. The gap being positive at all is the useful part: it means a
threshold _can_ separate the two, which is not guaranteed.

Note that the floor does not affect the metrics above — they are measured on raw nearest-neighbour
results, because a threshold hiding a relevant chunk would show up as a retrieval failure rather than
as the tuning choice it is.

### Limitations, stated plainly

- **The corpus is small.** 14 chunks against `k=5` means a single query returns over a third of the
  corpus, so `hit@3` and `hit@5` saturate at 1.00 and cannot discriminate further. `hit@1` and `MRR`
  carry the signal at this size. Growing the dataset is a data change — drop a `.txt` into
  `dataset/corpus/` and add questions to `questions.json`; the harness needs no edit.
- **One embedder, one model, one language.** Nothing here says how `nomic-embed-text` compares to a
  hosted provider. Re-run after #53.
- **Relevance is binary.** A chunk either contains a marker or does not; there is no graded
  relevance, so a chunk that is nearly right scores exactly as badly as one that is unrelated.
- **These are retrieval numbers only.** Whether the answer built from those chunks is _faithful_ to
  them is a different question, and is #22's.

## Answer faithfulness (M5, #22)

- Method: LLM-as-judge scoring whether each answer is grounded in its cited context.
- Flags: hallucinated or uncited claims.
- Results: _not yet measured._
