# ADR-0024 — Structured logs that follow one request end to end

- **Status:** Accepted
- **Date:** 2026-10-22

## Context

#26 asks for logs that let you follow a single request end to end: from the API, through retrieval
and the model call, and into the ingestion task an upload queues. Before it there was no `LOGGING`
setting, and running main showed what that meant:

- **With DEBUG off, which is every deployment, a 500's traceback was printed nowhere.** Django sends
  `django.request` errors to its console handler, which only prints under DEBUG, and to
  `mail_admins`, which has no `ADMINS` to mail. The handler chain was not empty, so Python's
  last-resort handler never ran either.
- In the API the root logger had no handler, so the app's own INFO lines were dropped and its
  warnings printed as a bare message: no time, no level, no logger, no request.
- The worker printed in Celery's own format, and nothing on a worker line said which request had
  queued the task, so the API and the worker had no field in common.
- Nothing on the answer path logged at all: not the search, not the scores against the similarity
  floor, not which model answered or how long it took.

Two things make the request hard to follow even with a format. The answer streams: its body, the
model call included, runs after the view has returned and after the middleware has unwound. And the
ingestion runs in another process, from a message.

The lines also leave the tenant boundary. An operator reads them and a collector stores them, and
none of the isolation the database enforces applies there (docs/threat-model.md, T15).

## Decision

**Every line, from the API, the worker and beat, is one JSON object that names the request it
belongs to and the tenant it ran for. A Celery task carries the fields of the request that queued
it. Lines carry identifiers, counts and timings, never a question, an answer or a passage.**

- **The fields live in one `ContextVar`** (`app/log_context.py`). `RequestLogMiddleware`, outermost
  in `MIDDLEWARE`, opens a scope per request with a `request_id`. The authenticator adds `tenant_id`
  once the token is verified. Inside any `tenant_context` the active tenant labels the line too, so
  a sweep or a management command is labelled without knowing about logging. The tenant id is a
  label for following a request: nothing reads it back, and scoping runs on `app.tenant_context`
  alone.
- **A filter on the one handler copies the fields onto every record**, so Django's, Celery's and the
  SDKs' lines carry them too. It sits on the handler because a logger's filters do not run for
  records that propagate up from its children. One Django line needed more: a 4xx that a view
  returned is logged after the whole middleware chain has returned, so the middleware leaves the
  fields on the request, and the filter reads them off the record's `request`.
- **The request id** is the caller's `X-Request-ID` if it looks like an identifier
  (`[A-Za-z0-9._:-]{1,128}`), so a proxy in front of the API can correlate its own lines with ours,
  and a fresh UUID otherwise. It goes back as `X-Request-ID`, and the BFF relays it to the browser,
  so a reported failure can be found. The BFF forwards no such header from a browser, so in this
  deployment the id is always ours.
- **A streamed answer runs inside its request's fields.** Each frame is produced inside them, and
  they are reset before the frame is sent. The response's `close()` runs inside them too, because
  Django closes the view's own generator before anything a middleware wraps around it, and that is
  where a hung-up client's usage row and generation line are written. The request line comes last,
  saying whether the body completed.
- **A task runs inside the fields of the request that published it.** `before_task_publish` puts
  `request_id` and `tenant_id` in a message header, `task_prerun` sets them with the task's own id
  and name, and `task_postrun` resets them. A retry republishes the headers, so every attempt keeps
  them. A field read back from a message must look like an identifier, or it is dropped, since it is
  printed on every line the task logs. A task nothing published from a request, like beat's, is
  named by its own id.
- **Celery logs through `LOGGING`.** Any receiver on `setup_logging` stops Celery replacing the root
  handlers with its own format, so the worker and beat apply the same dictionary the API does.
- **What the answer path logs**, one line each, joined by `request_id`. The search: the embedding
  model, k, the candidates, and the embed and search times. The floor: how many passages cleared it,
  its value, the chunk ids that became sources, and the best score, which is what explains a
  refusal. The generation: how it ended (`answered`, `refused`, `failed`, `disconnected` or
  `error`), the model, the total time and the time to first token, the token estimates the usage row
  is charged with, and the cited chunk ids. The request: method, path without the query string,
  route pattern, status and duration. Ingestion logs the document, its chunk count, the attempt and
  the embedding model.
- **Two formats.** `json` wherever DEBUG is off, for a collector to parse, and under DEBUG a
  readable line that prints the same fields as `key=value`. `TENANTIQ_LOG_FORMAT` overrides the
  choice and `TENANTIQ_LOG_LEVEL` sets the level, and an unknown value for either stops the process
  at startup.
- **DEBUG never prints tenant content.** Django's loggers and the Anthropic SDK's stop at INFO
  whatever the level: below it the SDK logs each request whole, prompt and passages included, and
  Django, with DEBUG on, logs each SQL statement with its parameters.

### Rejected alternatives

- **OpenTelemetry**, spans exported to a tracing backend. It is the right tool once there is a
  collector to send them to, and this deployment does not have one yet: it would mean an SDK and
  instrumentation for Django, Celery and the HTTP clients, plus somewhere to run the collector, to
  answer a question one shared id answers at this scale. A request id is what a trace id would
  replace, so adopting tracing later means swapping the id, not undoing this.
- **structlog or python-json-logger.** A dependency for what the standard library already does.
  Django, Celery and the SDKs log through stdlib `logging` regardless, so their lines would need the
  stdlib path anyway, and one path is simpler than two.
- **Passing the ids at each call** through `extra=` or a `LoggerAdapter`. It misses every line we do
  not write, which is most of them, and every new call site has to remember.
- **Holding the fields from a stream's first frame to its last.** That was the first version, and it
  covers a hung-up client. But two bodies iterated in turn on one thread then swap fields, and the
  first answer's last lines were logged under the second's request and tenant, which a test now
  shows. A WSGI server sends one response at a time on a thread, so production would not have hit
  it; the test client did, and nothing guarantees every server never will.
- **Logging the question or the prompt for debugging.** The ids are enough to look the content up in
  the database, under the tenant's row-level security, by someone entitled to read it.

## Consequences

- One request can be followed across processes by its id. Checked with gunicorn, Redis and a real
  worker: an upload's request line in the API's log and its ingestion lines in the worker's share
  `request_id` and `tenant_id`, and a query's search, floor, generation and request lines share them
  in order. `docs/observability.md` has the queries.
- A server error's traceback reaches the logs in a deployment, as one JSON line with the request it
  happened in.
- About four lines per query at INFO, plus the HTTP client's line for each call the Anthropic SDK
  makes, and per upload one in the API and three in the worker. `make eval` runs at WARNING, so its
  report is not interleaved with a line per search.
- The worker's `--loglevel` no longer sets its level; `TENANTIQ_LOG_LEVEL` does, for every process.
  Compose and the manifests pass `info`, which is the default.
- Every line names a tenant, so the log store itself becomes tenant-identifying metadata. It holds
  no tenant content, but who used the product, when and how much is in it.

**Left open, deliberately.** Gunicorn's own error and access lines (#25) are in gunicorn's format
and carry no request id; its configuration can route them through the same formatter, at which point
the access log repeats the request line and could go. Celery's "received" line is logged before the
task's fields exist; it names the task id, which every later line of the task carries. The BFF logs
nothing, so a failure inside the Next server is not in these logs at all. There are no metrics yet:
the durations are on the lines, but nothing aggregates them.
