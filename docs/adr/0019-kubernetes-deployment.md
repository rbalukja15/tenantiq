# ADR-0019 — Deploying with Kustomize, and what the manifests refuse to pretend

- **Status:** Accepted
- **Date:** 2026-08-24

## Context

#24 asks for base manifests and `dev` / `prod` overlays. Writing them is mostly mechanical. The
useful part turned out to be the opposite of mechanical: **a manifest is an executable claim about
what the system can do**, and three of the claims the obvious manifests would have made are false for
this codebase today.

Compose has been hiding that. `docker compose up` runs one of everything on one machine with one
filesystem, so nothing has ever forced the questions Kubernetes asks on the first line — how many
replicas, which volume access mode, what happens during a rolling update.

## Decision

**Kustomize, not Helm.** There are two environments and no third party consumes these manifests, so
the thing Helm adds — templating and packaging for other people — is cost without benefit here. A
Kustomize overlay is a diff against real YAML that still reads as YAML, and `kubectl` ships with it,
so rendering needs no extra tool. Reconsider if TenantIQ is ever installed by somebody else.

**Generated ConfigMaps, external Secrets.** `tenantiq-config` is a `configMapGenerator`, so its name
carries a content hash and every reference is rewritten. That hash is the point: editing a plain
ConfigMap changes nothing about running pods, so a config change silently does not take effect until
something unrelated restarts them. A new name is a new pod spec, so Deployments roll.

The prod Secret is deliberately *not* generated. A generated Secret is either committed or fictional.
The cost is stated rather than hidden: an unhashed name means rotating the Secret does not restart
anything, so a rotation must be followed by `rollout restart`, and `docs/deployment.md` says so.

**The full configuration surface, not the subset.** The ConfigMap carries all ~30 documented
`TENANTIQ_*` tunables. `docker-compose.yml`'s `x-backend-env` forwards nine of the ~35 the settings
module reads, so most documented tunables set in `.env` silently do nothing under compose — a knob an
operator can set that has no effect is worse than one that does not exist. A test asserts the mirror
image too: every key in the ConfigMap must be one `config/settings.py` actually reads, so a typo'd or
renamed setting cannot sit there looking configured.

**One copy of the Postgres bootstrap SQL.** Kustomize cannot read a file outside its own root, but it
can include another kustomization *directory* — so `infra/postgres/` has one, and the dev overlay
consumes the same two `.sql` files compose mounts. CI holds a third rendition as inline Python
because a GitHub Actions service container has no init hook; that duplication is real, predates this
change, and is not solved here.

**Invariants are tested, not asserted in comments.** `backend/tests/test_k8s_manifests.py` renders
both overlays with `kubectl kustomize` and checks what matters: every resource is namespaced, **no
Ingress routes to Django** (ADR-0013 §1 — an Ingress to the API would bypass the CSRF gate, the path
allowlist and both header allowlists), prod renders no Secret, every container declares resource
requests, our own images run as non-root, and the replica pins below hold. It shells out rather than
reimplementing overlay merging, because the artefact under test is what the cluster would receive.

## The three things the manifests refuse to pretend

These are the actual findings, and each is encoded as a pinned number with a test behind it rather
than a comment somebody can skim.

**1. The BFF cannot be replicated.** `frontend/lib/session.ts` keeps sessions in an in-process `Map`
on `globalThis`. A second replica has a different `Map`, so a request the load balancer sends there
finds no session and answers 401 — users are signed out intermittently, depending on which pod they
reach. `replicas: 1` and `strategy: Recreate`; the fix is a shared session store, not a bigger number.

**2. The API cannot be replicated either, for an unrelated reason.** The API writes uploads to a
volume the worker reads, so `tenantiq-media` is ReadWriteMany — which most cloud block storage does
not provide. Even where it does, more API replicas means more writers on one filesystem. `replicas: 1`;
the fix is object storage for uploads. The worker, which holds no session and serves no traffic, is
the one component that scales, and it is set to 2.

**3. The frontend image is a development server.** `frontend/Dockerfile`'s CMD is `next dev`. The
prod overlay deploys whatever image it is given, so it is production-grade only once a production
image exists. That belongs with #25 and is written at the top of `frontend.yaml` rather than
discovered in an incident.

Shipping `replicas: 3` would have looked more professional and been a lie. Two of these three are
invisible from compose, which is exactly why writing the manifests was worth doing before there was a
cluster to run them on.

## Consequences

**What this buys.** The deployment topology is now reviewable: a reader can see what runs, what it
connects to, and what it is not allowed to do. Two architectural invariants that previously held by
convention — nothing but the BFF talks to Django, and no secret material lives in the repo — are
enforced by a test and by a NetworkPolicy. And the scaling limits are documented at the exact place
someone would change them.

**What it costs.** `Recreate` on both public workloads means a short outage on every release. That is
a consequence of the constraints, not a preference, and it disappears when they do.

**Left open, deliberately.** No `/healthz`, so probes are TCP and a pod whose database is unreachable
is still marked Ready — application work, not #24's. No HorizontalPodAutoscaler, because nothing that
serves traffic can scale yet. No egress NetworkPolicy, because the backend's destinations depend on
the provider #53 chooses. `TENANTIQ_LLM_FACTORY` and `TENANTIQ_EMBEDDER_FACTORY` still default to
local Ollama backends that do not exist in the prod namespace, so prod renders correctly and would
not answer a question until #53 lands. Nothing here has been applied to a real cluster: these
manifests are verified to render and to satisfy their invariants, which is not the same as verified
to run.
