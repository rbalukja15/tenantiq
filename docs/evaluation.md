# Evaluation

The differentiator: TenantIQ's retrieval and answers are *measured*, not assumed.

## Retrieval quality (M5)
- Dataset: curated question -> relevant-chunk pairs.
- Metrics: precision@k, recall@k. Runner: `make eval`.
- Results: _to be filled in M5._

## Answer faithfulness (M5)
- Method: LLM-as-judge scoring whether each answer is grounded in its cited context.
- Flags: hallucinated or uncited claims.
- Results: _to be filled in M5._
