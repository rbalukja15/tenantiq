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
| Run              | 17 August 2026                                                    |
| Generator        | `llama3.1` via Ollama                                             |
| Judge            | `llama3.1` via Ollama — **the same model** (see the caveat below) |
| Answers measured | 17 of 18                                                          |
| Claims           | 50, of which 25 cited                                             |

|                        |          |
| ---------------------- | -------- |
| **grounded**           | **0.36** |
| faithfulness           | 0.72     |
| citation coverage      | 0.50     |
| unsupported claims     | 7        |
| unclear                | 0        |
| **uncited figures**    | **18**   |
| **invented citations** | **0**    |

**The finding is the citation discipline, not the support score.** Half the claims carry no citation at
all, and eighteen sentences state a figure with nothing behind them — several quoting the source text
almost verbatim, like _"Overdue undisputed amounts accrue interest at one percent (1%)…"_, with no
marker. Against a contract whose first rule is that the LLM never computes numbers, that is the
result that matters.

**Zero invented citations** across 50 claims. The enforcement that does exist holds: the model cites
real source numbers or does not cite at all.

### Why 0.72 is a ceiling, not a measurement

The generator and the judge were both `llama3.1`. A model grading its own output is
known-optimistic, so read `faithfulness 0.72` and `grounded 0.36` as upper bounds. Note which
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
- **One question could not be answered at all** — the local fallback returned an error, which is a
  gap in coverage rather than a grounding result. See the note on the context window in the devlog.
- **The judge is a measurement instrument with error.** Its verdicts and reasons are written to the
  `--json` output verbatim so a disagreement can be settled by reading them, not by trusting a score.
