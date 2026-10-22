# ADR-0022 — Continuous deployment: production servers, pinned digests, and a release script

- **Status:** Accepted
- **Date:** 2026-10-22

## Context

#25 asks for a pipeline in which merging to main deploys. ADR-0019 wrote the manifests and named
two things it left for this issue: the frontend image ran `next dev`, and the order of a release
(migrations before the workloads) belonged to "the operator's, and from #25 the pipeline's" hands.
Reading the manifests again with a pipeline in mind found three more:

- **The API ran Django's development server in the cluster too**, not only in compose:
  `backend.yaml`'s command was `manage.py runserver`. Django's documentation says that server has
  not gone through security audits or performance tests and must not be used in production.
- **The documented order did not do what it said.** docs/deployment.md applied the whole overlay
  first and waited for the migrate Job afterwards. The first apply already rolls the Deployments,
  so on every release the new API and worker started against a schema the migration had not
  reached yet, which is the failure the wait was there to prevent.
- **The overlay referenced images no registry has** (`tenantiq/backend:latest`), and its Redis
  address was a placeholder in a ConfigMap, where a managed Redis's password cannot go.

Three constraints shape the answer. An answer streams over SSE (ADR-0009) for as long as the model
generates, often longer than 30 seconds. The BFF keeps sessions in process, so the frontend is one
replica (ADR-0019). And the repository is public: images published from its workflows inherit that
visibility.

## Decision

**CI builds a production image of each half on every pull request, pushes it to GHCR from main,
and a green push to main deploys it by digest through one script that owns the order.**

- **The API is served by gunicorn with the threaded worker**, two processes of eight threads
  (`backend/gunicorn.conf.py`). The default sync worker gives a whole process to one request, so a
  streaming answer would block every other request in it, and its 30-second timeout would kill any
  answer that took longer. Under gthread the timeout is a heartbeat that the worker's main loop
  keeps sending while its threads stream. `tests/test_gunicorn.py` runs a real gunicorn on the
  shipped file, and both of its tests fail with the worker switched back to sync. Compose keeps
  `runserver`, for its autoreload. WSGI rather than ASGI because ADR-0012's usage accounting does
  synchronous ORM work inside the streaming iterator, which ASGI would refuse.
- **The frontend image is Next's standalone output**, the default target of a multi-stage
  `frontend/Dockerfile`, with a `dev` target that compose builds. The image carries no
  environment: every setting is read at request time and nothing is `NEXT_PUBLIC_`, so one image
  serves every environment. It is about a tenth the size of a full install and runs as uid 1000.
  Its command sets `HOSTNAME=0.0.0.0`, because the server binds to `$HOSTNAME` and platforms set
  that to the container's name (some of them over the image's own value).
- **Every image is tagged `sha-<commit>` and deployed by digest.** No `latest`, no `main`: a tag can
  move between the build CI tested and the pull the cluster makes. The prod overlay names the
  registry and a tag CI never pushes (`pinned-at-deploy`), so a hand `kubectl apply` of the render
  fails to pull instead of running something unknown.
- **`scripts/deploy.py` owns the order**, for CI and an operator alike: check that `tenantiq-secrets`
  has every key the pods need, apply everything that is not a workload, delete the previous migrate
  Job and run the new one to completion (stopping with its logs if it fails), and only then apply
  the Deployments and wait for each rollout. It refuses an image that is not digest-pinned, a render
  whose images do not match the pins, and an overlay that still names `example.com`.
  `tests/test_deploy.py` drives it against a recording stand-in for kubectl.
- **The deploy job runs only for a push to main, after both test jobs and the image build pass**,
  in a GitHub environment named `production`, one deploy at a time. Until that environment has a
  `KUBECONFIG` secret, the job is a green no-op with a notice: images are published and nothing
  deploys.
- **`REDIS_URL` moved into the Secret.** A managed Redis URL carries a password. Over TLS it also
  needs `?ssl_cert_reqs=required`, or Celery refuses to start.

### Rejected alternatives

- **GitOps, with Argo CD or Flux pulling from the repository.** It is the better model at scale:
  the cluster converges on git and CI holds no cluster credential. Here it means running a
  controller on a one-VM cluster, and the ordering problem stays, moved into sync waves and hooks.
  Revisit with a second environment.
- **An initContainer that waits for migrations** (`manage.py migrate --check` in a loop), which
  would make a plain `kubectl apply` of everything safe in any order. Something still has to delete
  the immutable Job and create the new one, so the script would remain, and every pod start would
  gain a database round trip.
- **Helm with a pre-upgrade migration hook.** The standard answer, but ADR-0019 chose Kustomize for
  reasons that still hold.
- **Failing the deploy job when no cluster is configured.** Every merge would be red until a server
  exists, which teaches everyone to ignore red.
- **Building the images inside the deploy job.** The digests the cluster runs would then belong to a
  build that no pull request ever exercised.

## Consequences

**What this buys.** A merge is a release, and a release is reproducible: the digest in the CI log is
the image in the cluster. A failed migration stops before any pod changes, so the cluster keeps
serving the previous release. A broken Dockerfile fails the pull request that broke it. Streaming
and long answers work under a production server, and that is tested rather than assumed.

**What it costs.**

- *A standing cluster credential in CI.* The `KUBECONFIG` secret can change the cluster. Scope it to
  what a release touches and protect the environment; least-privilege RBAC for it is not done here.
- *A short outage per release*, unchanged from ADR-0019: both public workloads use `Recreate`.
- *Migrations must stay backwards compatible for one release.* Between the migrate Job and the
  rollout, the old pods run against the new schema. The window is now short and ordered, but it
  exists, so a destructive schema change needs the usual expand-then-contract.
- *Public images.* They inherit the repository's visibility. Nothing secret is built into them; if
  the repository goes private, the cluster needs an image pull secret.
- *Two image builds on every pull request.* They run beside the test jobs, so they cost runner
  minutes rather than time to a result.

**Left open, deliberately.** The host itself, and its specifics (ingress controller, storage class),
are the operator's choice; the prod overlay's Ingress still assumes ingress-nginx, which upstream
retired in March 2026. The probes are still TCP, because there is no `/healthz`. And nothing here
has run against a real cluster: the order is tested against a recording kubectl and the render
against a real one, so the first deploy is the first end-to-end check.
