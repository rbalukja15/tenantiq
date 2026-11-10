#!/usr/bin/env python3
"""Deploy one release to the cluster the current kubeconfig points at (#25, ADR-0022).

    scripts/deploy.py \\
        --backend  ghcr.io/rbalukja15/tenantiq-backend@sha256:... \\
        --frontend ghcr.io/rbalukja15/tenantiq-frontend@sha256:...

CI runs this on every green merge to main with the digests it has just pushed, and an operator runs
the same command by hand. Either way there is one ordering, and it is this file's:

1. check that the Secret the pods read exists and has every key they need, before anything changes;
2. apply everything that is not a workload: config, services, the media volume, ingress, policy;
3. replace the migrate Job (its pod template is immutable, so re-applying a changed one fails) and
   wait for it to finish, stopping with its logs if it fails;
4. only then apply the Deployments, and wait for each rollout.

Applying the whole overlay in one `kubectl apply` would roll the new API and worker straight away,
against a schema the migration has not reached yet, while the Job runs beside them. Kustomize has no
way to say "this, then that", which is why the sequence is code (ADR-0019 left it to #25).

Images are pinned by digest, never by tag: a tag can move between the build that CI tested and the
pull the cluster makes. `--render-only` prints the manifests this would apply, and changes nothing.

Standard library only, so it runs on a bare CI runner and on a laptop alike; kubectl is the one
dependency. backend/tests/test_deploy.py drives it against a recording stand-in for kubectl.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROD_OVERLAY = REPO_ROOT / "infra" / "k8s" / "overlays" / "prod"

SECRET = "tenantiq-secrets"
# What the backend, worker and migrate Job cannot start without. ANTHROPIC_API_KEY is absent on
# purpose: an empty or missing key is a supported state, in which questions fail loudly instead of
# billing an account (docs/deployment.md, section 2).
REQUIRED_SECRET_KEYS = ("DATABASE_URL", "DJANGO_SECRET_KEY", "REDIS_URL", "VOYAGE_API_KEY")
MIGRATE_JOB = "tenantiq-migrate"

# Values the prod overlay ships with until an operator replaces them. Deploying one would publish an
# app whose OIDC redirect_uri and Origin check name somebody else's domain.
PLACEHOLDERS = ("example.com", "example.internal")

DIGEST = re.compile(r"^(?P<name>[^@\s]+)@(?P<digest>sha256:[0-9a-f]{64})$")
KIND = re.compile(r"^kind: (\S+)$", re.MULTILINE)
IMAGE = re.compile(r"^\s*(?:- )?image: (\S+)$", re.MULTILINE)

Kubectl = Callable[..., str]


class DeployError(Exception):
    """A reason to stop, worded for the person reading the CI log."""


@dataclass
class Release:
    """The rendered overlay, split into the three phases it is applied in."""

    namespace: str
    foundation: list[str] = field(default_factory=list)
    migrate: list[str] = field(default_factory=list)
    workloads: list[str] = field(default_factory=list)

    @property
    def deployments(self) -> list[str]:
        return [_name(document) for document in self.workloads]


def parse_pin(reference: str) -> tuple[str, str]:
    """Split ``name@sha256:...`` into its name and digest; refuse anything else."""
    match = DIGEST.match(reference)
    if not match:
        raise DeployError(
            f"{reference!r} is not pinned by digest. Pass name@sha256:<64 hex digits>, the digest CI "
            "printed when it pushed the image: a tag can point at a different image by the time "
            "the cluster pulls it."
        )
    return match["name"], match["digest"]


def render(overlay: Path, images: Sequence[str], *, kubectl: Kubectl) -> str:
    """Render ``overlay`` with each of ``images`` pinned to its digest.

    The pins go in a throwaway overlay on top of the real one, created next to it so that the
    relative path Kustomize requires stays inside the repository. Nothing in git changes.
    """
    workdir = Path(tempfile.mkdtemp(prefix=".release-", dir=overlay.parent))
    try:
        entries = []
        for reference in images:
            name, digest = parse_pin(reference)
            entries.append(f"  - name: {name}\n    digest: {digest}\n")
        (workdir / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\n"
            "kind: Kustomization\n"
            f"resources:\n  - {os.path.relpath(overlay, workdir)}\n"
            "images:\n" + "".join(entries),
            encoding="utf-8",
        )
        return kubectl("kustomize", str(workdir))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def split(rendered: str, images: Sequence[str]) -> Release:
    """Check the rendered release, then sort it into foundation, migration and workloads."""
    stale = sorted({image for image in IMAGE.findall(rendered) if image not in images})
    if stale:
        raise DeployError(
            f"the release would run {', '.join(stale)}, which is not one of the images pinned for "
            f"it ({', '.join(images)}). The image names in the overlay's `images:` and the names CI "
            "pushes have drifted apart: they must match for the digest pin to apply."
        )
    for placeholder in PLACEHOLDERS:
        if placeholder in rendered:
            raise DeployError(
                f"the overlay still contains the placeholder {placeholder!r}. Set the Ingress host "
                "and APP_BASE_URL to the real domain in infra/k8s/overlays/prod (see "
                "docs/deployment.md, 'Continuous deployment')."
            )

    namespaces = set(re.findall(r"^  namespace: (\S+)$", rendered, re.MULTILINE))
    if len(namespaces) != 1:
        raise DeployError(f"expected one namespace in the release, found {sorted(namespaces)}")
    release = Release(namespace=namespaces.pop())

    for document in _documents(rendered):
        kind = KIND.search(document)
        if kind and kind.group(1) == "Job":
            release.migrate.append(document)
        elif kind and kind.group(1) == "Deployment":
            release.workloads.append(document)
        else:
            release.foundation.append(document)

    if [_name(document) for document in release.migrate] != [MIGRATE_JOB]:
        raise DeployError(f"expected the release to contain exactly one Job, {MIGRATE_JOB}")
    if not release.workloads:
        raise DeployError("the release contains no Deployments")
    return release


def deploy(release: Release, *, kubectl: Kubectl, timeout: int = 300, poll: float = 5.0) -> None:
    """Apply ``release`` in order, waiting for each phase before starting the next."""
    namespace = release.namespace

    _step(f"checking Secret {SECRET} in namespace {namespace}")
    _check_secret(namespace, kubectl=kubectl)

    _step(f"applying config, services, volume and network ({len(release.foundation)} objects)")
    _echo(kubectl("apply", "-f", "-", input=_join(release.foundation)))

    _step(f"running migrations (Job {MIGRATE_JOB})")
    _echo(kubectl("-n", namespace, "delete", "job", MIGRATE_JOB, "--ignore-not-found", "--wait"))
    _echo(kubectl("apply", "-f", "-", input=_join(release.migrate)))
    _wait_for_migrations(namespace, kubectl=kubectl, timeout=timeout, poll=poll)

    _step(f"rolling out {', '.join(release.deployments)}")
    _echo(kubectl("apply", "-f", "-", input=_join(release.workloads)))
    for name in release.deployments:
        status = f"deployment/{name}"
        _echo(kubectl("-n", namespace, "rollout", "status", status, f"--timeout={timeout}s"))

    _step("deployed")


def _check_secret(namespace: str, *, kubectl: Kubectl) -> None:
    # Key names only. The values travel over the API to this process either way, but nothing here
    # prints them or keeps them.
    keys_only = 'go-template={{range $key, $value := .data}}{{$key}}{{"\\n"}}{{end}}'
    try:
        listing = kubectl("-n", namespace, "get", "secret", SECRET, "-o", keys_only)
    except DeployError as error:
        raise DeployError(
            f"cannot read Secret {SECRET} in namespace {namespace}. Create it before the first "
            f"deploy, as docs/deployment.md section 2 shows. ({error})"
        ) from error
    missing = [key for key in REQUIRED_SECRET_KEYS if key not in listing.split()]
    if missing:
        raise DeployError(
            f"Secret {SECRET} has no {', '.join(missing)}. The pods would start and then fail; "
            "add the keys as docs/deployment.md section 2 shows, then re-run the deploy."
        )


def _wait_for_migrations(namespace: str, *, kubectl: Kubectl, timeout: int, poll: float) -> None:
    deadline = time.monotonic() + timeout
    conditions_only = 'jsonpath={range .status.conditions[*]}{.type}={.status}{"\\n"}{end}'
    while True:
        conditions = kubectl(
            "-n", namespace, "get", "job", MIGRATE_JOB, "-o", conditions_only
        ).split()
        if "Complete=True" in conditions:
            return
        if "Failed=True" in conditions:
            logs = kubectl("-n", namespace, "logs", f"job/{MIGRATE_JOB}", "--tail=100")
            raise DeployError(
                f"the migration failed, so nothing was rolled out. Its last log lines:\n{logs}"
            )
        if time.monotonic() >= deadline:
            raise DeployError(
                f"the migration did not finish within {timeout}s, so nothing was rolled out. See "
                f"`kubectl -n {namespace} describe job {MIGRATE_JOB}`."
            )
        time.sleep(poll)


def _documents(rendered: str) -> list[str]:
    return [document.strip() + "\n" for document in rendered.split("\n---\n") if document.strip()]


def _join(documents: Sequence[str]) -> str:
    return "---\n".join(documents)


def _name(document: str) -> str:
    """``metadata.name`` of one rendered document; Kustomize indents it by exactly two spaces."""
    metadata = document.split("\nmetadata:\n", 1)[-1]
    for line in metadata.splitlines():
        if not line.startswith(" "):
            break
        if line.startswith("  name: "):
            return line.removeprefix("  name: ").strip()
    raise DeployError(f"a rendered object has no metadata.name:\n{document}")


def _step(message: str) -> None:
    print(f"==> {message}", flush=True)


def _echo(output: str) -> None:
    if output:
        print(output, end="" if output.endswith("\n") else "\n", flush=True)


def kubectl_cli(*args: str, input: str | None = None) -> str:
    """Run kubectl and return its stdout, raising DeployError with its stderr on failure."""
    if shutil.which("kubectl") is None:
        raise DeployError("kubectl is not installed")
    result = subprocess.run(["kubectl", *args], input=input, capture_output=True, text=True)
    if result.returncode != 0:
        raise DeployError(f"`kubectl {' '.join(args[:4])} ...` failed: {result.stderr.strip()}")
    return result.stdout


def main(argv: Sequence[str] | None = None, *, kubectl: Kubectl = kubectl_cli) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--backend", required=True, help="backend image, name@sha256:...")
    parser.add_argument("--frontend", required=True, help="frontend image, name@sha256:...")
    parser.add_argument(
        "--overlay", type=Path, default=PROD_OVERLAY, help="overlay to deploy (default: prod)"
    )
    parser.add_argument(
        "--render-only", action="store_true", help="print the manifests and change nothing"
    )
    args = parser.parse_args(argv)

    images = [args.backend, args.frontend]
    try:
        rendered = render(args.overlay.resolve(), images, kubectl=kubectl)
        release = split(rendered, images)
        if args.render_only:
            print(rendered, end="")
            return 0
        deploy(release, kubectl=kubectl)
    except DeployError as error:
        print(f"deploy: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
