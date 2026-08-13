# ADR-0015 — Document deletion and citation resolution

- **Status:** Accepted
- **Date:** 2026-08-13

## Context

M4's UI needs two backend endpoints that no earlier issue owned (#51). Both look like routine CRUD
and both hide a decision worth recording.

**Deletion.** Until now a tenant could upload a document and retry a failed one, but never remove
one. A document is not one object: it is a row, the chunks it was split into (each carrying an
embedding in the shared vector index), and the raw uploaded file on storage. "Delete" has to mean all
three, or it means nothing — a chunk left behind is still retrievable, so a "deleted" document would
go on being cited in answers, and a raw file left behind is the tenant's content persisting after
they asked for it to be gone. ADR-0010 already leans on this: redaction never touches the original
upload, so *deleting the document* is the only full purge of a document's PII, which was a promise
the API could not yet keep.

Two sub-questions had no obvious answer:

- **Can a document be deleted while it is being ingested?** Ingestion is asynchronous, so a document
  can sit in PROCESSING for as long as the worker takes — and, when something goes wrong, for good
  (#55's sweeper does not exist yet).
- **In what order do the row and the file go?** `ATOMIC_REQUESTS` wraps the request in a
  transaction; storage is not in it. Any ordering has a window where one has gone and the other has
  not.

**Citation resolution.** A streamed answer closes with a citations frame that carries
`chunk_id`, `document_id`, `document_title`, `chunk_index` and the character offsets — but **no
text** (`app.generation.Citation`, ADR-0008). So the UI receives an answer whose `[1]` markers are
addresses with nothing at the far end. The evidence has to come from somewhere.

## Decision

**Delete removes the row, the chunks, and the file.** The chunks go by FK cascade; the file is
removed explicitly. `DELETE /api/documents/<id>` resolves through the tenant-scoped manager, so
another tenant's id is a 404 — the same response as an id that never existed.

**PROCESSING is not a lock.** A delete is accepted in any state. Rejecting it with a 409 while
ingestion is in flight is the tidier-looking option, and it was rejected: ingestion can wedge, and a
document that cannot be deleted while wedged is a document a tenant cannot delete at all — the exact
case where they most want to. The cost is moved to the worker instead, which now treats a vanished
row as a normal outcome and stops, rather than raising (which would burn its three backed-off retries
and then record a terminal failure for a document the tenant deliberately removed).

**The row goes first, and the file goes on commit.** `perform_destroy` deletes the row and registers
the storage delete with `transaction.on_commit`. The two alternatives are both worse:

- *File first, then the row.* If the row delete then fails, the file is destroyed but the document
  still lists — pointing at bytes that no longer exist.
- *Row first, then the file, inline.* The row delete is not durable until the response commits, so
  any error after that point rolls the row back and leaves a listed document whose file is gone.

Deleting on commit makes a rollback a complete no-op: nothing is destroyed unless the row is
genuinely, durably gone. The residual risk is a crash in the moment between commit and the callback,
which orphans a file but never destroys a referenced one — the failure that is cheapest to live with
and easiest to sweep later.

**Repeating a delete is a 404, not a 204.** The effect is idempotent — the document is gone either
way — but the status reports what this call found. It also keeps one uniform answer for "not yours",
"never existed" and "already deleted", so no verb on this route can be used to probe another tenant's
document ids.

**Evidence is fetched on demand: `GET /api/chunks/<id>`.** The alternative was to widen the citations
frame to carry each chunk's text. Rejected: it would push every retrieved passage down the answer
stream, including the ones the reader never opens, on the latency-critical path — and it would put
the same text in two places, so a later change could make the citation disagree with the chunk. The
endpoint serves the stored text verbatim (exactly `source[start_offset:end_offset]`, #45), the
offsets, and the document's id and title. It never serves the embedding: internal, large, and the
tenant's content in another form. `Chunk.objects` is tenant-scoped, so a citation can only ever be
resolved by the tenant whose corpus produced it.

## Consequences

- The API can finally keep ADR-0010's promise: deleting a document really is a full purge, raw file
  included. The threat model's T3 limit is updated accordingly.
- #19 can render a citation as evidence — the passage, at its real offsets, in the document it came
  from — and #20 can offer delete. Neither needs a new endpoint.
- One destructive route now exists, so the isolation proofs grow a shape they did not have before:
  not merely "B cannot read A's document" but "B's delete leaves A's row, chunks and file intact"
  (T14).
- The worker tolerates a missing document, which slightly weakens a previously safe assumption
  (a task always has its row). That is now explicit in `run_ingestion` rather than implied.
- Orphaned files are possible in one narrow crash window and are logged, not reconciled. A storage
  sweep is deferred; it pairs naturally with #55's stuck-PROCESSING sweeper.
- Deletes are charged against the tighter *upload* rate budget rather than the read budget, so bulk
  cleanup through the UI is bounded. If #20 grows a multi-select delete, that budget is what it will
  hit first.
