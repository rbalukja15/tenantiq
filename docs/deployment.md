# Deploying TenantIQ to Kubernetes

Manifests live in [`infra/k8s/`](../infra/k8s): a `base` every environment shares, and `dev` / `prod`
overlays. Rendering needs nothing but `kubectl` — its built-in Kustomize is enough.

```bash
make k8s-dev     # render the dev overlay
make k8s-prod    # render the prod overlay
```

The reasoning behind the shape is in [ADR-0019](adr/0019-kubernetes-deployment.md). This page is the
operational half: what to run, in what order, and what will bite.

## What each overlay is

|                   | `dev`                                                | `prod`                                            |
| ----------------- | ---------------------------------------------------- | ------------------------------------------------- |
| Namespace         | `tenantiq-dev`                                        | `tenantiq`                                         |
| Postgres / Redis  | in-cluster, single node                               | **managed, external** — address comes from the Secret |
| Ollama            | in-cluster, model pulled by a Job                     | none — see #53                                     |
| Secret            | generated from obviously-fake literals                | **created out of band**                            |
| Public entrypoint | `kubectl port-forward`                                | Ingress + TLS, frontend only                       |

`dev` is meant for a single-node cluster — kind, minikube, Docker Desktop — and mirrors
`docker compose up`. If you only want the stack running locally, `make dev` is faster and simpler;
the dev overlay exists so the manifests themselves get exercised.

## Deploying

### 1. Images

CI builds both images on every pull request and pushes them from main (#25, ADR-0022), as
`ghcr.io/rbalukja15/tenantiq-backend:sha-<commit>` and `ghcr.io/rbalukja15/tenantiq-frontend:sha-<commit>`.
A release is deployed by digest, which the push step prints in the CI log, never by tag. To build
them by hand:

```bash
docker build -t <registry>/tenantiq-backend:<tag> ./backend
docker build -t <registry>/tenantiq-frontend:<tag> ./frontend   # default target: production
```

Both are production servers. The API runs gunicorn with threads, configured in
`backend/gunicorn.conf.py`, so that a streaming answer holds one thread rather than a whole worker.
The frontend runs Next's standalone server as uid 1000. Compose builds the frontend's `dev` target
and runs the API with `runserver` instead, for their autoreload.

### 2. Create the namespace and the Secret (prod only)

Never in git. Five keys, consumed by the backend, the worker and the migrate Job, in a namespace
that has to exist first:

```bash
kubectl create namespace tenantiq
kubectl -n tenantiq create secret generic tenantiq-secrets \
  --from-literal=DATABASE_URL='postgresql://USER:PASS@HOST:5432/tenantiq' \
  --from-literal=REDIS_URL='rediss://USER:PASS@HOST:6379/0?ssl_cert_reqs=required' \
  --from-literal=DJANGO_SECRET_KEY="$(openssl rand -base64 48)" \
  --from-literal=VOYAGE_API_KEY='...' \
  --from-literal=ANTHROPIC_API_KEY=''
```

`scripts/deploy.py` checks that all of them but `ANTHROPIC_API_KEY` are present before it changes
anything, so a missing one stops the release instead of crash-looping its pods.

`REDIS_URL` is the Celery broker and the throttle cache. It is here rather than in the ConfigMap
because a managed Redis's URL carries its password. Over TLS (`rediss://`), keep
`?ssl_cert_reqs=required`: without it Celery refuses to start.

`VOYAGE_API_KEY` is required — the prod overlay points the embedder at Voyage (#53, ADR-0020), and
without a key it raises before making a request rather than handing you a vendor 401.

**`ANTHROPIC_API_KEY` is deliberately empty above.** Embeddings are a one-off per document; generation
is per query and bounded only by traffic. With no key the LLM factory falls back to Ollama, which does
not run in this namespace — so ingestion works, questions fail loudly, and nothing bills an account
by accident. Set it when you want the deployment to answer, and let #49's per-tenant quotas cap it.

External Secrets or Sealed Secrets work the same way as long as the resulting Secret is named
`tenantiq-secrets`.

> **Rotating this Secret does not restart anything.** It is referenced by a fixed name with no
> content hash — the price of not generating it from the repo — so a rotation must be followed by
> `kubectl -n tenantiq rollout restart deploy/tenantiq-backend deploy/tenantiq-worker`. The
> ConfigMap *is* generated and hashed, so config changes roll the pods on their own.

### 3. The database must be prepared before the first deploy

ADR-0002 requires the app to connect as a role that is **not** a superuser and does **not** have
`BYPASSRLS`, because row-level security does not apply to either. On a managed Postgres an
administrator has to do once, by hand, what `infra/postgres/init/*.sql` does for a local container:

```sql
CREATE ROLE tenantiq_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS CREATEDB;
GRANT ALL ON DATABASE tenantiq TO tenantiq_app;
ALTER SCHEMA public OWNER TO tenantiq_app;
CREATE EXTENSION IF NOT EXISTS vector;   -- as an admin: pgvector is not a trusted extension
```

Deploying against a superuser connection string will work, pass every request, and silently disable
the second layer of tenant isolation. Nothing in the cluster can detect that — check the role.

### 3b. Upgrading an existing deployment across the 1024-dim change

Migration `0014` widens the embedding column from 768 to 1024 and **discards every stored vector**
(ADR-0020 — a 768-dim vector has no meaningful 1024-dim equivalent). Documents keep their text,
chunks and offsets; only the vectors go, and retrieval returns nothing until they are rebuilt:

```bash
kubectl -n $NS exec deploy/tenantiq-backend -- python manage.py backfill_embeddings
```

It only touches chunks with no embedding, or one from another model, so it is idempotent and
resumable. Run it directly after the migrate Job and before announcing the deployment — between the
two, every query answers "no relevant context" rather than answering wrongly.

Expect pods still on the old 768-dim configuration to fail between the migrate Job and the rollout:
their inserts no longer fit the column (`expected 1024 dimensions, not 768`), and their queries fail
once the backfill starts writing 1024-dim vectors. The order in §4 keeps that window short; a
document that failed inside it can be retried with `manage.py reingest_documents --status failed`.

The same applies locally: after pulling this change, `docker compose exec backend python manage.py
migrate && docker compose exec backend python manage.py backfill_embeddings`. If `.env` still pins
`TENANTIQ_EMBEDDING_MODEL=nomic-embed-text`, ingestion fails with a message naming both dimensions —
that guard (#46) is working, not broken.

### 4. Release with `scripts/deploy.py`

`kubectl apply` has no ordering primitive, and Kustomize has no hooks, so the order of a release
lives in one script. CI runs it on every green merge to main (see "Continuous deployment" below),
and an operator runs the same thing by hand:

```bash
scripts/deploy.py \
  --backend  ghcr.io/rbalukja15/tenantiq-backend@sha256:<digest> \
  --frontend ghcr.io/rbalukja15/tenantiq-frontend@sha256:<digest>
```

It renders the prod overlay with both images pinned to those digests, then:

1. checks `tenantiq-secrets` for the keys in section 2, and stops before changing anything if one is
   missing;
2. applies everything that is not a workload: config, services, the media volume, the Ingress and
   the NetworkPolicy;
3. deletes the previous `tenantiq-migrate` Job, whose pod template is immutable, applies the new one
   and waits for it to finish. If the migration fails, it prints the Job's logs and stops, and the
   cluster keeps serving the release it had;
4. applies the Deployments and waits for each rollout.

`--render-only` prints the manifests and changes nothing. A digest is in the CI log of the push, or
`docker buildx imagetools inspect ghcr.io/rbalukja15/tenantiq-backend:sha-<commit>` prints it.

The order is the point. Applying the whole render at once, as an earlier version of this page did,
rolls the new API and worker while the migration is still running, so they start against a schema
it has not reached yet and fail in ways that look like application bugs.

### 5. Verify

```bash
kubectl -n $NS get pods
kubectl -n $NS logs deploy/tenantiq-backend --tail=50
```

There is no `/healthz` yet, so the probes are TCP — a pod whose database is unreachable is still
marked Ready. Read the logs on a first deploy rather than trusting green pods.

## Continuous deployment

`.github/workflows/ci.yml` releases on its own (#25, ADR-0022):

- **On a pull request** it builds both images without pushing them, so a broken Dockerfile fails
  the pull request that broke it.
- **On a push to main** it pushes both as `sha-<commit>`. If both test jobs passed too, the `deploy`
  job runs `scripts/deploy.py` with the digests it has just pushed, in the GitHub environment
  `production`, one deploy at a time.
- **Until `production` has a `KUBECONFIG` secret**, that job stops with a notice and stays green:
  the images are published and nothing is deployed.

To turn it on, once:

1. **A cluster** with an ingress controller, cert-manager, and a storage class that provides
   ReadWriteMany for the media volume (see "What will bite").
2. **Postgres and Redis**: Postgres prepared as in section 3, and a Redis the pods can reach.
3. **The namespace and `tenantiq-secrets`**, as in section 2.
4. **Your domain** in `infra/k8s/overlays/prod`: the Ingress host, twice in `ingress.yaml`, and
   `APP_BASE_URL` in `kustomization.yaml`. The deploy refuses to run while either still says
   `example.com`, because the OIDC `redirect_uri` and the Origin check would name someone else's
   domain. Point the domain's DNS at the ingress controller.
5. **An environment named `production`** in the repository's Settings > Environments, with a secret
   `KUBECONFIG` holding a kubeconfig for the cluster. Its credential has to create and update every
   object a release contains, in the `tenantiq` namespace, and read `tenantiq-secrets`. A
   protection rule on the environment (a required reviewer, a wait timer) applies to every deploy.

The next merge to main deploys, and re-running a `deploy` job redeploys the digests it was given.

The images are public because the repository is, and nothing secret is built into them. If the
repository goes private, the cluster needs an image pull secret for `ghcr.io`.

## Dev overlay: running it

```bash
kubectl kustomize infra/k8s/overlays/dev | kubectl apply -f -
kubectl -n tenantiq-dev wait --for=condition=complete job/tenantiq-migrate --timeout=300s
kubectl -n tenantiq-dev port-forward svc/tenantiq-frontend 3000:3000
```

`APP_BASE_URL` in the dev overlay is `http://localhost:3000`, which is why the port-forward must use
that exact port: it is both the OIDC `redirect_uri` and the `Origin` every state-changing request has
to match. Forward to 3001 and sign-in fails with a 403 that explains nothing.

The first apply pulls the embedding model (Job `tenantiq-ollama-pull`) and takes a few minutes.

## What will bite

**Both `tenantiq-frontend` and `tenantiq-backend` are pinned to one replica**, and neither is a
placeholder. The BFF keeps sessions in an in-process `Map` (`frontend/lib/session.ts`), so a second
replica signs users out depending on which pod they reach; the API and worker share a media volume,
so replicas multiply writers on one filesystem. Both want a code change first — a shared session
store, and object storage for uploads. A test fails if the numbers are raised, so that the person
raising them reads this.

**`tenantiq-media` is ReadWriteMany.** Most cloud block storage is ReadWriteOnce. On a single-node
dev cluster the overlay patches it down to RWO, which works only because every pod lands on the same
node. On a managed cluster you need a filesystem that provides RWX (EFS, Azure Files, Filestore) —
or the object-storage change above.

**The rollout strategy is `Recreate` for both**, which means a short outage on every release. That is
a consequence of the two constraints above, not a preference.

**Nothing here runs Keycloak.** Each tenant brings its own IdP; the issuer is stored on the `Tenant`
row and validated per request (ADR-0002). The local Keycloak in `docker-compose.yml` is a development
fixture — see [auth-keycloak.md](auth-keycloak.md).

**The Ingress is written for ingress-nginx, which upstream retired in March 2026** and no longer
patches. Its annotations do three things the product needs, so another controller needs its own
equivalent of each: no response buffering, without which a streamed answer arrives all at once at
the end; a read timeout of 300 seconds, longer than an answer takes; and a 25 MiB request body, the
upload cap.
