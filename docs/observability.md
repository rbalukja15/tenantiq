# Following a request through the logs

Every line the API, the worker and beat print names the request it belongs to and the tenant it ran
for, so one request can be followed from the API through retrieval and the model call, and into the
ingestion task an upload queued (#26, [ADR-0024](adr/0024-structured-logging.md)). This page says
what the lines carry and how to read them.

## What a line looks like

In a deployment, one JSON object per line on stderr (wrapped here to fit):

```json
{"time": "2026-10-22T20:07:21.051Z", "level": "INFO", "logger": "app.middleware",
 "message": "POST /api/query 200 in 38 ms", "request_id": "e2e-query-1",
 "tenant_id": "05877268-cfee-45fc-ba8c-dd119ab8b8b8", "event": "request", "method": "POST",
 "path": "/api/query", "status": 200, "duration_ms": 38, "route": "api/query", "streamed": true,
 "completed": true}
```

Under DEBUG, which is how compose runs, the same fields print as `key=value` after the message.

| Field | On | |
|---|---|---|
| `time`, `level`, `logger`, `message` | every line | `time` is UTC, to the millisecond |
| `request_id` | every line logged while a request is served, and every line of a task it queued | the caller's `X-Request-ID` if it looks like an identifier, otherwise a fresh UUID |
| `tenant_id` | every line after authentication, and every line inside a `tenant_context` | a label for following a request; nothing reads it back |
| `task_id`, `task_name` | every line logged inside a Celery task | |
| `event` | the lines below | what the line reports |
| `exception` | a line logged with a traceback | |

A field with no value is left out rather than printed as `null`.

## What the answer path logs

One line each, joined by `request_id`, in this order for a query:

| `event` | Logger | When | Fields |
|---|---|---|---|
| `retrieval` | `app.retrieval` | each search | `embedding_model`, `k`, `candidates`, `embed_ms`, `search_ms`, `duration_ms` |
| `sources` | `app.rag` | each query, once the floor has been applied | `candidates`, `sources`, `min_similarity`, `best_similarity`, `source_chunk_ids` |
| `generation` | `app.views` | when the answer ends, however it ends | `outcome`, `model`, `duration_ms`, `first_token_ms`, `input_tokens`, `output_tokens`, `citations`, `cited_chunk_ids` |
| `request` | `app.middleware` | when the request ends: for an answer, after the last frame or the hang-up | `method`, `path`, `route`, `status`, `duration_ms`, and for an answer `streamed`, `completed` |
| `ingestion` | `app.ingestion` | each document ingested, in the worker | `document_id`, `chunks`, `attempt`, `embedding_model` |

`outcome` is `answered`; `refused` when nothing cleared the floor, so the model was never called and
the line has no `model`; `failed` when the model call failed; `disconnected` when the client hung up
first, in which case the tokens it did produce were still charged; or `error` when the stream
raised. The token counts are the estimates the usage row is charged with (ADR-0012), so the line and
the bill agree. `best_similarity` is the field to read when a question was refused that should not
have been: it says how far the best passage fell short of the floor.

Django's and Celery's own lines carry the same fields: a 4xx as a `django.request` warning, a 5xx as
a `django.request` error with its traceback, and each task's `succeeded in`, `retry` or failure line
from `celery.app.trace`.

**What no line carries**: a question, an answer or a passage ([threat model](threat-model.md), T15).
The chunk ids lead to the passages, in the database, under the tenant's row-level security. The
request line logs the path without its query string.

## Following one request

Take the id from the `X-Request-ID` response header, which the BFF relays to the browser, so it is
in the network tab of whoever reported the problem, or from `request_id` on any line. Then, in
Kubernetes, read the API's and the worker's logs together and put them in time order:

```sh
ID=...  # the request id
kubectl -n tenantiq logs -l 'app.kubernetes.io/component in (backend, worker)' --tail=-1 --since=1h \
  | jq -cnR --arg id "$ID" '[inputs | fromjson? | select(.request_id == $id)] | sort_by(.time)[]'
```

`--tail=-1` because with a selector `kubectl logs` shows only the last 10 lines of each pod, and
`fromjson?` skips the lines that are not JSON, such as gunicorn's own.

Under compose the lines are text by default, and `grep` finds them:

```sh
docker compose logs backend worker | grep "request_id=$ID"
```

To use the queries here instead, set `TENANTIQ_LOG_FORMAT=json` in `.env` and recreate the stack.

## Questions worth asking

Each of these is a filter for `jq -cR 'fromjson? | <filter>'`:

```sh
# Slow answers, and how long the model took to start
select(.event == "generation" and .duration_ms > 10000) | {request_id, tenant_id, model, duration_ms, first_token_ms}

# Refusals, and how close the best passage came to the floor
select(.event == "sources" and .sources == 0) | {request_id, tenant_id, best_similarity, min_similarity}

# Clients that hung up mid-answer
select(.event == "generation" and .outcome == "disconnected") | {request_id, tenant_id, output_tokens}

# Server errors, with the request they happened in
select(.level == "ERROR") | {time, request_id, tenant_id, logger, message, exception}

# One tenant's requests
select(.tenant_id == "<tenant uuid>" and .event == "request") | {time, method, route, status, duration_ms}
```

## Settings

| Setting | Default | |
|---|---|---|
| `TENANTIQ_LOG_FORMAT` | `json`, or `text` under DEBUG | anything else stops the process at startup |
| `TENANTIQ_LOG_LEVEL` | `INFO` | every process: the API, the worker and beat. Django's loggers and the Anthropic SDK's never go below INFO, because below it they print SQL parameters and whole prompts |

`make eval` runs at `WARNING`, so its report is not interleaved with a line per search.

## Not in these logs yet

- Gunicorn's own error and access lines, which are in gunicorn's format with no request id.
- Celery's `Task ... received` line, logged before the task's fields exist. It names the task id,
  which every later line of the task carries.
- Anything from the BFF, which logs nothing.
