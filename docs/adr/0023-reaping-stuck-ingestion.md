# ADR-0023 — Reaping documents a dead worker left in PROCESSING

- **Status:** Accepted
- **Date:** 2026-10-22

## Context

ADR-0005 closed every way for ingestion to fail out of sight but one: a worker killed between phase 1
(which commits PROCESSING) and the task's `on_failure` leaves its document PROCESSING forever. The
tenant sees work in progress that will never finish, and the retry endpoint refuses the document,
because only a FAILED one can be retried. #55 is that hole.

It is not a rare event in this deployment. Reading the pinned Celery (5.6.3) showed how many paths
lead there, because `on_failure` runs inside the pool process that executed the task, and every one
of these kills that process:

- **The hard time limit itself.** When #47's limit fires, the pool kills the process and the parent
  records the failure in the result backend (`Request.on_timeout`), which never calls the task's own
  `on_failure`. The limit added for a crafted upload strands the document of the upload it stops.
- **The OOM killer**, for a parse that outgrows the worker's 2 GiB limit.
- **Every rollout.** Kubernetes sends the worker SIGTERM, Celery's warm shutdown waits for running
  tasks, and the kubelet sends SIGKILL after the 30-second grace period, while an ingestion may run
  for 300. Once #25's pipeline is deploying, every merge to main is a rollout.
- Node drains and lost nodes, and `docker compose restart worker` locally.

`task_acks_late` is off, so the message was acknowledged when the task started and nothing redelivers
it. `updated_at` makes such a document detectable by age; nothing acted on it.

Two constraints shape the fix. The sweep must never fail a document whose ingestion is still
running, or about to run its retry. And `Document` is tenant-owned (ADR-0002), so a sweep across
every tenant is a new cross-tenant query path, the kind isolation is easiest to lose on.

## Decision

**Celery beat sends a sweep every 5 minutes. It fails every PROCESSING document whose last change is
more than 30 minutes old, one tenant at a time, and the document becomes retryable.**

- **The rule is age.** Phase 1 stamps `updated_at` at the start of every attempt, and the hard limit
  ends every attempt, so during an attempt a document is never older than
  `TENANTIQ_INGEST_TIME_LIMIT` (300 s). Between attempts it waits for the retry's backoff (seconds)
  and then for a free worker. `TENANTIQ_INGEST_REAP_AFTER_SECONDS` (default 1800) must be at least
  twice the limit: a whole attempt, then at least that long again in the queue.
- **That bound is enforced, not documented.** A system check refuses a smaller value
  (`tenantiq.E001`) and a non-positive interval (`tenantiq.E002`). `manage.py migrate` runs the
  checks before it touches the database, and the migrate Job is the first step of a release, so a
  release carrying an unsafe value stops before any pod changes. A manifest test runs the same check
  against each overlay's rendered values, so the edit fails in CI before it reaches a release.
- **A reaped document is FAILED with a reason** ("Processing stopped before it finished. Retry the
  document to process it again."), which the existing retry endpoint accepts. Its `attempts` is
  unchanged, since reaping is not an attempt, and the sweep logs a warning naming the document, its
  tenant and when it last changed.
- **One tenant at a time, inside `tenant_context`.** Both isolation layers apply to the sweep exactly
  as they do to a request. Tests show that one tenant's sweep leaves another tenant's stuck document
  alone, and that with the ORM scope deliberately removed, row-level security alone still does.
- **Rows are claimed with `SELECT ... FOR UPDATE SKIP LOCKED`**, so the documents logged are the ones
  updated, and a row another transaction is writing at that moment is left for the next sweep:
  whatever is writing it is not a dead worker.
- **Beat is its own process**: a compose service, and in Kubernetes a Deployment pinned to one
  replica with the `Recreate` strategy. Every running beat sends every entry, so a second one, even
  for the overlap of a rolling update, doubles the schedule; a manifest test pins both settings.

**What a wrong reap costs**, because a deep enough queue can still produce one: a retry that waits
longer than the threshold behind other ingestions is reaped, and then its own phase 1 sets the
document back to PROCESSING and it finishes READY. If the tenant presses retry in between, the
document is ingested twice, which re-ingestion's idempotence (ADR-0005) already absorbs. That is
the trade the default makes: half an hour before a dead worker's document shows a reason, in
exchange for never failing work that would have finished.

### Rejected alternatives

- **`celery worker -B`, beat inside the worker.** Celery documents it as unsuitable for production,
  and the prod overlay runs two workers, so it would run two schedulers.
- **A Kubernetes CronJob running a management command.** No long-running scheduler, but compose
  would have no sweep at all and the schedule would live in two places. Beat keeps it in the app's
  settings, the same everywhere.
- **`task_acks_late` with `task_reject_on_worker_lost`**, so a dead worker's task is redelivered
  instead. With the Redis broker an unacknowledged message comes back only after the visibility
  timeout, an hour by default, so the document would still sit PROCESSING. A task that kills its
  worker would be redelivered to kill the next one. It may still be worth having beside the sweep,
  not instead of it.
- **Asking the workers what they are running** (`celery inspect active`) before reaping. The workers
  that answer are the live ones, and the case is a dead one; it also costs a broker round trip per
  sweep and races the answer.
- **Handling the `task_failure` signal in the worker's parent process.** It fires when only the pool
  process dies, which would catch the hard-limit and OOM cases at once, but not a pod or node that
  dies with its parent, which is what a rollout does. It could shorten one case; the sweep covers
  all of them.

## Consequences

- No document stays PROCESSING for longer than about 35 minutes (the threshold plus one interval)
  after its worker dies, and every stranded one ends with a reason and a retry.
- One more long-running process, small and stateless. Its schedule file lives in `/tmp` and is lost
  with the pod, which only means the sweep runs when beat starts.
- Each sweep costs one query per tenant. That is nothing at this scale; at thousands of tenants it
  would want one indexed query run with a privileged role, which is its own isolation decision.
- Lowering the threshold means lowering the time limit first, which the check makes explicit.

**Left open, deliberately.** A document can also stay PENDING forever, if publishing its task failed
after the upload committed; that is a different failure with different evidence, and #55 is about
PROCESSING. The worker's grace period is still shorter than an ingestion, so a rollout strands
in-flight documents and the sweep recovers them afterwards. A `terminationGracePeriodSeconds` above
the time limit would let warm shutdown finish them instead, at the cost of slower rollouts.
