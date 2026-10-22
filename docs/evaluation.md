# Evaluation

The differentiator: TenantIQ's retrieval and answers are _measured_, not assumed.

```bash
make eval                # retrieval metrics. Seconds, no LLM involved.
make eval-faithfulness   # also generates and judges an answer per question. Minutes.
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
| Run       | 24 August 2026                            |
| Embedder  | `mxbai-embed-large` via Ollama, 1024-dim  |
| Chunking  | 800 target / 100 overlap tokens           |
| Corpus    | 7 documents, 14 chunks, 30,936 characters |
| Questions | 18 in-corpus, 3 out-of-corpus             |

| Metric    | @1       | @3       | @5       |
| --------- | -------- | -------- | -------- |
| hit       | **0.83** | **1.00** | **1.00** |
| precision | 0.83     | 0.39     | 0.23     |
| recall    | 0.75     | **1.00** | **1.00** |

**MRR 0.91.**

**The right passage is now the single best match for five questions in six**, and every relevant
chunk is inside the top 3. The remaining misses are two questions where the correct passage lands at
rank 2 or 3 behind a near-neighbour, which is what keeps `k` at 5 rather than trimming it.

### What changed, and why the harness earned its keep

#53 moved production embeddings to Voyage, whose `voyage-4` emits 1024 dimensions and cannot emit
768 — so the local model changed with it, from `nomic-embed-text` to `mxbai-embed-large`. That was
forced by a dimension constraint, not chosen for quality. It turned out to be a substantial upgrade,
and **the only reason that is a fact rather than a hope is that the numbers were already being
measured**:

| Metric  | `nomic-embed-text` (768) | `mxbai-embed-large` (1024) |
| ------- | ------------------------ | -------------------------- |
| hit@1   | 0.56                     | **0.83**                   |
| recall@1| 0.50                     | **0.75**                   |
| recall@3| 0.94                     | **1.00**                   |
| MRR     | 0.75                     | **0.91**                   |

The previous entry's finding — _"the right passage is always in the top 3, but only the single best
match a little over half the time"_ — is what this fixed. A reranking step is a much weaker argument
now than it was a week ago.

> **These are still not the production numbers.** Production embeds with `voyage-4`; this was
> measured on the local model that happens to share its width. Re-run `make eval` with
> `TENANTIQ_EMBEDDER_FACTORY=app.embeddings.build_voyage_embedder` and a `VOYAGE_API_KEY` in `.env`
> to get them, after recreating the stack — `make eval` runs inside it, so the container only sees
> what compose forwarded when it started. Every result here is stamped with the embedder that
> produced it precisely so the two can never be confused.

### Choosing the similarity floor

Retrieval always returns its `k` nearest neighbours however far away they are, so
`TENANTIQ_RETRIEVAL_MIN_SIMILARITY` is the only thing that lets the product refuse. The dataset
includes three questions the corpus cannot answer, purely to measure **separation**:

|                                   |            |
| --------------------------------- | ---------- |
| Worst in-corpus top similarity    | 0.567      |
| Best out-of-corpus top similarity | 0.441      |
| Gap                               | **+0.126** |

A floor anywhere in `(0.441, 0.567)` refuses every unanswerable question while still answering every
answerable one. **The shipped default is still `0.0`, which refuses nothing** — every question gets
sources whether or not the corpus has an answer, and the refusal state the UI builds (ADR-0016) is
unreachable in a default deployment. That is #88, and on this corpus `0.50` sits inside the window.

Note the gap **narrowed** (+0.155 → +0.126) even as every ranking metric improved. The two are not
the same property: a better embedder pulls related text closer together, which helps ranking and
makes the answerable/unanswerable boundary tighter, not wider. A model that ranks better is not
automatically a model that refuses better.

The window is corpus- and embedder-specific and must be re-derived, not copied, once the eval runs
against `voyage-4` — the width matching does not make the geometry match. The gap being positive at
all is the useful part: it means a threshold _can_ separate the two, which is not guaranteed.

Note that the floor does not affect the metrics above — they are measured on raw nearest-neighbour
results, because a threshold hiding a relevant chunk would show up as a retrieval failure rather than
as the tuning choice it is.

### Limitations, stated plainly

- **The corpus is small.** 14 chunks against `k=5` means a single query returns over a third of the
  corpus, so `hit@3` and `hit@5` saturate at 1.00 and cannot discriminate further. `hit@1` and `MRR`
  carry the signal at this size. Growing the dataset is a data change — drop a `.txt` into
  `dataset/corpus/` and add questions to `questions.json`; the harness needs no edit.
- **One embedder, one model, one language.** These numbers are `mxbai-embed-large`'s. Production
  runs `voyage-4` (#53), which shares the width and not the geometry — re-run with a `VOYAGE_API_KEY`
  before quoting anything here as a production figure.
- **Relevance is binary.** A chunk either contains a marker or does not; there is no graded
  relevance, so a chunk that is nearly right scores exactly as badly as one that is unrelated.
- **These are retrieval numbers only.** Whether the answer built from those chunks is _faithful_ to
  them is a different question, and is #22's.

## Answer faithfulness (#22)

Retrieval finding the right passage is necessary and not sufficient. This measures the other half:
**is the answer actually supported by what it cites?**

### The split that makes the number comparable

The harness generates a real answer per question through `retrieve_context` and
**`stream_grounded_answer`** — the streaming path, which is what the SSE endpoint and therefore every
real user receives. That choice is load-bearing rather than incidental: the non-streaming path returns
structured `{answer, citations}`, so its citation list is _answer-level_ and the prose need not carry a
single `[n]` marker, which makes a per-claim grounding score inexpressible on it. Then:

1. **Splits it into claims mechanically.** Sentences, with their `[n]` markers attached
   (`app/eval/claims.py`). The judge rules on claims it did not choose. If the model that scores an
   answer also decides what the units of that answer are, the denominator moves between runs and a
   score can drift without anything about the answer changing.
2. **Checks three things without a model at all**, because they are the project's hardest rules and
   deserve deterministic ground:
   - **Invented citations** — a marker in the prose pointing at a source number that was never
     offered. `generation._resolve_citations` already drops these from the returned citation list, so
     the API never emits a dangling citation — but the _prose keeps the marker_, and an answer that
     reads "as set out in [7]" when there was no source 7 is precisely the failure this project
     claims is impossible. Nothing but this notices it.
   - **Uncited claims** — the grounding contract requires every claim to carry its source, so an
     uncited factual sentence is a violation regardless of whether evidence exists.
   - **Uncited figures** — "the LLM never computes numbers" is rule one. A sentence stating a figure
     with nothing behind it is the single most serious thing an answer here can contain. (Citation
     markers are stripped before looking for digits — `[1]` is a citation, not a claim about the
     number one.)
3. **Asks the judge only the semantic question**: is this sentence supported by the text of the
   sources _it cites_? The judge never sees a source the claim did not cite, so it cannot quietly
   credit a claim to evidence the answer never pointed at.

### Three numbers, and which one to read

|                   |                                           |
| ----------------- | ----------------------------------------- |
| **grounded**      | supported ÷ **all** claims — the headline |
| faithfulness      | supported ÷ **cited** claims              |
| citation coverage | cited ÷ all claims                        |

`grounded` is deliberately the harsher one: an uncited claim counts as ungrounded whether or not a
source exists that would have backed it. Reporting only `faithfulness` would let an answer that cites
one sentence in six score 1.00.

A verdict is a **tri-state**. A judge that cannot tell is a real outcome, and `unclear` is counted
separately rather than folded into either side — as is anything unparseable, a skipped claim, or a
word outside the enum. Defaulting a missing verdict either way would let a flaky judge move the
headline in whichever direction its failures happened to fall.

### What the harness refuses to pretend

- **A model call that fails is a gap in coverage, not a verdict.** Failures are excluded from every
  score and the count of measured answers is printed next to the numbers. The first real run proved
  why: a timeout in one question originally discarded every answer already collected.
- **A run judged by the stand-in is not a measurement.** The hermetic `FakeJudge` checks lexical
  containment, not comprehension. The report disowns its own numbers when that is what ran.
- **Self-assessment is called out.** When the judge and the generator resolve to the same model —
  the default locally, where both are Ollama — the report says so. `TENANTIQ_EVAL_JUDGE_MODEL` and
  `TENANTIQ_EVAL_JUDGE_OLLAMA_MODEL` exist to break the tie.

### Results

|                  |                                                                   |
| ---------------- | ----------------------------------------------------------------- |
| Run              | 26 August 2026                                                    |
| Retrieval        | `mxbai-embed-large`, 1024-dim (#53)                               |
| Generator        | `llama3.1` via Ollama                                             |
| Judge            | `llama3.1` via Ollama — **the same model** (see the caveat below) |
| Answers measured | **18 of 18**                                                      |
| Claims           | 39, of which 25 cited                                             |

|                        | 17 Aug (768-dim retrieval) | 26 Aug (1024-dim retrieval) |
| ---------------------- | -------------------------- | --------------------------- |
| **grounded**           | 0.36                       | **0.49**                    |
| faithfulness           | 0.72                       | **0.76**                    |
| citation coverage      | 0.50                       | **0.64**                    |
| unsupported claims     | 7                          | 6                           |
| unclear                | 0                          | 0                           |
| **uncited figures**    | **18**                     | **8**                       |
| **invented citations** | **0**                      | **0**                       |

Only retrieval changed between these runs — same generator, same judge, same questions. Better
sources produced answers that cite more (0.50 → 0.64) and assert bare figures less than half as often
(18 → 8). **Citation discipline is still the finding**: a third of claims carry no citation, and eight
sentences state a figure with nothing behind them, against a contract whose first rule is that the
LLM never computes numbers.

**Zero invented citations**, again. The enforcement that exists holds: the model cites real source
numbers or does not cite at all.

Note the denominator moved (50 → 39 claims), so the ratios are not measuring identical work. Fewer,
longer claims is itself a consequence of better sources — see the claim-splitting limitation below.

### The result that a score cannot show

The 17 August run answered the late-payment question by quoting the MSA: _"Overdue undisputed amounts
accrue interest at one percent (1%) per month."_ The 26 August run quotes **0.75%**, from Amendment
No. 2, which reduced the rate.

**The old answer was wrong, and its faithfulness score was fine.** It quoted its cited source
verbatim, so a judge asking "is this claim supported by the text it cites?" must say yes. The corpus
deliberately contains a superseding amendment, and only retrieval that surfaces the amendment over
the original clause can get the question right.

That is the ceiling on what this harness measures: **faithfulness is support-by-cited-source, not
correctness-in-context.** An answer can be perfectly faithful and still stale, whenever a corpus
contains documents that supersede each other — which is what a contract corpus mostly is. Catching it
needed a human reading two documents; no number here moved to signal it.

### Why 0.76 is a ceiling, not a measurement

The generator and the judge were both `llama3.1`. A model grading its own output is
known-optimistic, so read `faithfulness 0.76` and `grounded 0.49` as upper bounds. Note which
numbers this does and does not touch:

| Number                                                 | Judge-dependent?              |
| ------------------------------------------------------ | ----------------------------- |
| faithfulness, grounded                                 | **yes**                       |
| citation coverage, uncited figures, invented citations | no — computed without a model |

So the headline finding rests entirely on the model-free half and does not move whoever judges.

The defaults tie in **both** configurations — locally both resolve to `llama3.1`, and with an
Anthropic key both `TENANTIQ_LLM_MODEL` and `TENANTIQ_EVAL_JUDGE_MODEL` default to the same model —
so nobody gets an independent judge without configuring one. The report says so loudly on every such
run. Re-derive against an independent judge when #53 settles the provider, alongside the similarity
floor.

### Limitations

- **Claim splitting is naive.** Sentences, split on terminators. A numbered clause like `4.` reads as
  a boundary, so an answer that quotes a contract's numbering produces short fragments that are then
  counted and judged — one run produced a `Source [3]: 4.` "claim". That inflates the denominator by
  perhaps five to ten percent and adds noise to the support scores. A minimum word count would filter
  it; the trade-off against a sentence-segmentation dependency is in `app/eval/claims.py`.
- **Relevance to the question is not measured.** A claim can be perfectly supported by its source and
  still not answer what was asked.
- **Coverage is now complete** — 18 of 18, where the first run measured 17. The gap was #90's
  context-window bug, fixed since. A second infrastructure failure showed up on the way to this run
  and is worth knowing about: with a larger embedding model resident, `llama3.1` at
  `num_ctx=8192` exceeded the Ollama container's memory and every answer failed with a generic
  message over an HTTP 500. `OLLAMA_MAX_LOADED_MODELS: 1` fixes it. A run where every answer fails
  is easy to spot; the harness excluding failures from its scores is what stops a run where *some*
  do from quietly reporting a better number.
- **The judge is a measurement instrument with error.** Its verdicts and reasons are written to the
  `--json` output verbatim so a disagreement can be settled by reading them, not by trusting a score.
