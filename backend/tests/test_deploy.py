"""The release script and the pipeline that runs it (#25, ADR-0022).

``scripts/deploy.py`` exists because the order of a release matters and Kustomize cannot express
it: the migration has to finish before the new API and worker start. The steps docs/deployment.md
used to give applied the whole overlay first, which started them straight away. Most tests here
drive the script against a recording stand-in for kubectl, so the order is checked on every run
without a cluster. The one that renders the real overlay needs kubectl, and follows the same skip
and ``TENANTIQ_REQUIRE_KUBECTL`` rule as test_k8s_manifests.py.

The last tests read .github/workflows/ci.yml, because two properties of the pipeline are only true
while that file says so: it deploys nothing that is not a green push to main, and it pushes images
under the same names the prod overlay pulls.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_spec = importlib.util.spec_from_file_location("deploy", REPO_ROOT / "scripts" / "deploy.py")
deploy = importlib.util.module_from_spec(_spec)
sys.modules["deploy"] = deploy  # dataclasses resolve the module's annotations through sys.modules
_spec.loader.exec_module(deploy)

BACKEND = "ghcr.io/rbalukja15/tenantiq-backend@sha256:" + "b" * 64
FRONTEND = "ghcr.io/rbalukja15/tenantiq-frontend@sha256:" + "f" * 64
PINS = [BACKEND, FRONTEND]


def _document(kind: str, name: str, *, image: str | None = None) -> str:
    """One object as `kubectl kustomize` prints it: sorted keys, two-space indents."""
    namespace = "" if kind == "Namespace" else "  namespace: tenantiq\n"
    text = f"apiVersion: v1\nkind: {kind}\nmetadata:\n  labels:\n    app: tenantiq\n"
    text += f"  name: {name}\n{namespace}"
    if image:
        text += f"spec:\n  template:\n    spec:\n      containers:\n      - image: {image}\n"
    return text


def _rendered(*, backend: str = BACKEND, frontend: str = FRONTEND, host: str = "tenantiq.test"):
    return "---\n".join(
        [
            _document("Namespace", "tenantiq"),
            _document("ConfigMap", "tenantiq-config-abc123"),
            _document("Service", "tenantiq-backend"),
            _document("PersistentVolumeClaim", "tenantiq-media"),
            _document("Ingress", "tenantiq").replace("spec:", "") + f"spec:\n  host: {host}\n",
            _document("Job", "tenantiq-migrate", image=backend),
            _document("Deployment", "tenantiq-backend", image=backend),
            _document("Deployment", "tenantiq-frontend", image=frontend),
            _document("Deployment", "tenantiq-worker", image=backend),
        ]
    )


class RecordingKubectl:
    """Answers the handful of kubectl calls the script makes, and remembers them in order."""

    def __init__(
        self,
        *,
        secret_keys: tuple[str, ...] = deploy.REQUIRED_SECRET_KEYS,
        migration: list[list[str]] | None = None,
        secret_missing: bool = False,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.secret_keys = secret_keys
        self.secret_missing = secret_missing
        self.migration = iter(migration if migration is not None else [["Complete=True"]])

    def __call__(self, *args: str, input: str | None = None) -> str:
        self.calls.append((args, input))
        if args[2:4] == ("get", "secret"):
            if self.secret_missing:
                raise deploy.DeployError('secrets "tenantiq-secrets" not found')
            return "".join(f"{key}\n" for key in self.secret_keys)
        if args[2:4] == ("get", "job"):
            return "\n".join(next(self.migration, [])) + "\n"
        if args[2] == "logs":
            return "django.db.utils.OperationalError: connection refused\n"
        return f"kubectl {args[0]} ok\n"

    def applied(self) -> list[str]:
        """The kind of every object applied, in the order the applies happened."""
        kinds = []
        for args, input in self.calls:
            if args[:1] == ("apply",):
                kinds += [
                    line.split(": ")[1] for line in input.splitlines() if line[:6] == "kind: "
                ]
        return kinds

    def index(self, *needle: str) -> int:
        """Position of the first call whose arguments include every one of ``needle``."""
        for position, (args, _) in enumerate(self.calls):
            if all(part in args for part in needle):
                return position
        raise AssertionError(f"no kubectl call with {needle}: {[c for c, _ in self.calls]}")


def _release() -> deploy.Release:
    return deploy.split(_rendered(), PINS)


def test_the_migration_finishes_before_any_workload_is_applied():
    kubectl = RecordingKubectl()

    deploy.deploy(_release(), kubectl=kubectl, poll=0)

    applied = kubectl.applied()
    first_deployment = applied.index("Deployment")
    assert applied.index("Job") < first_deployment
    assert "Deployment" not in applied[: applied.index("Job")]
    # The Job is applied, and only then waited on to completion; the Deployments come after.
    job_wait = kubectl.index("get", "job")
    deployments_applied = next(
        position
        for position, (args, input) in enumerate(kubectl.calls)
        if args[:1] == ("apply",) and "kind: Deployment" in input
    )
    assert kubectl.index("apply") < job_wait < deployments_applied
    assert deployments_applied < kubectl.index("rollout", "status")


def test_the_previous_migrate_job_is_deleted_before_the_new_one_is_applied():
    # A Job's pod template is immutable, so applying the new release over the last one fails.
    kubectl = RecordingKubectl()

    deploy.deploy(_release(), kubectl=kubectl, poll=0)

    job_applied = next(
        position
        for position, (args, input) in enumerate(kubectl.calls)
        if args[:1] == ("apply",) and "kind: Job" in input
    )
    assert kubectl.index("delete", "job", "tenantiq-migrate") < job_applied


def test_every_deployment_is_waited_on():
    kubectl = RecordingKubectl()

    deploy.deploy(_release(), kubectl=kubectl, poll=0)

    waited = [args[4] for args, _ in kubectl.calls if args[2:4] == ("rollout", "status")]
    assert waited == [
        "deployment/tenantiq-backend",
        "deployment/tenantiq-frontend",
        "deployment/tenantiq-worker",
    ]


def test_a_migration_still_running_is_waited_for():
    kubectl = RecordingKubectl(migration=[[], ["Complete=False"], ["Complete=True"]])

    deploy.deploy(_release(), kubectl=kubectl, poll=0)

    assert sum(1 for args, _ in kubectl.calls if args[2:4] == ("get", "job")) == 3
    assert "Deployment" in kubectl.applied()


def test_a_failed_migration_stops_the_release_with_its_logs():
    kubectl = RecordingKubectl(migration=[["Failed=True"]])

    with pytest.raises(deploy.DeployError, match="migration failed") as failure:
        deploy.deploy(_release(), kubectl=kubectl, poll=0)

    assert "OperationalError: connection refused" in str(failure.value)
    # The cluster keeps the release it had: no new API or worker against a half-migrated schema.
    assert "Deployment" not in kubectl.applied()


def test_a_migration_that_never_finishes_times_out_without_rolling_out():
    kubectl = RecordingKubectl(migration=[])

    with pytest.raises(deploy.DeployError, match="did not finish within 0s"):
        deploy.deploy(_release(), kubectl=kubectl, timeout=0, poll=0)

    assert "Deployment" not in kubectl.applied()


@pytest.mark.parametrize("missing", deploy.REQUIRED_SECRET_KEYS)
def test_a_secret_without_a_required_key_stops_before_anything_changes(missing):
    keys = tuple(key for key in deploy.REQUIRED_SECRET_KEYS if key != missing)
    kubectl = RecordingKubectl(secret_keys=keys)

    with pytest.raises(deploy.DeployError, match=f"has no {missing}"):
        deploy.deploy(_release(), kubectl=kubectl, poll=0)

    assert kubectl.applied() == []


def test_no_secret_at_all_stops_before_anything_changes():
    kubectl = RecordingKubectl(secret_missing=True)

    with pytest.raises(deploy.DeployError, match="Create it before the first deploy"):
        deploy.deploy(_release(), kubectl=kubectl, poll=0)

    assert kubectl.applied() == []


def test_an_empty_anthropic_key_is_allowed():
    # Deliberately optional (docs/deployment.md, section 2): without it, ingestion works and
    # questions fail loudly instead of billing an account.
    assert "ANTHROPIC_API_KEY" not in deploy.REQUIRED_SECRET_KEYS


@pytest.mark.parametrize(
    "reference",
    [
        "ghcr.io/rbalukja15/tenantiq-backend:sha-0ca80fa",
        "ghcr.io/rbalukja15/tenantiq-backend",
        "ghcr.io/rbalukja15/tenantiq-backend@sha256:abc",
    ],
)
def test_an_image_not_pinned_by_digest_is_refused_before_kubectl_runs(reference, capsys):
    kubectl = RecordingKubectl()

    assert deploy.main(["--backend", reference, "--frontend", FRONTEND], kubectl=kubectl) == 1

    assert "not pinned by digest" in capsys.readouterr().err
    assert kubectl.calls == []


def test_an_image_the_release_did_not_pin_is_refused():
    # What a drift between the overlay's image names and CI's looks like after rendering: the digest
    # pin matched nothing, so the overlay's own placeholder tag is still there.
    rendered = _rendered(frontend="ghcr.io/someone-else/tenantiq-frontend:pinned-at-deploy")

    with pytest.raises(deploy.DeployError, match="drifted apart"):
        deploy.split(rendered, PINS)


def test_a_placeholder_domain_is_refused():
    with pytest.raises(deploy.DeployError, match="placeholder 'example.com'"):
        deploy.split(_rendered(host="tenantiq.example.com"), PINS)


def test_the_phases_hold_every_object_exactly_once():
    release = _release()

    foundation = [deploy.KIND.search(document).group(1) for document in release.foundation]
    assert foundation == ["Namespace", "ConfigMap", "Service", "PersistentVolumeClaim", "Ingress"]
    assert [deploy._name(document) for document in release.migrate] == ["tenantiq-migrate"]
    assert release.deployments == ["tenantiq-backend", "tenantiq-frontend", "tenantiq-worker"]
    assert release.namespace == "tenantiq"


def test_the_prod_overlay_renders_with_every_image_pinned_to_its_digest():
    if shutil.which("kubectl") is None:
        if os.environ.get("TENANTIQ_REQUIRE_KUBECTL", "").strip().lower() in {"1", "true", "yes"}:
            pytest.fail("TENANTIQ_REQUIRE_KUBECTL is set but kubectl is not installed")
        pytest.skip("kubectl not installed")

    rendered = deploy.render(deploy.PROD_OVERLAY, PINS, kubectl=deploy.kubectl_cli)

    assert sorted(set(deploy.IMAGE.findall(rendered))) == sorted(PINS)
    # Until the overlay names a real domain it refuses to deploy; past that, it splits cleanly.
    release = deploy.split(rendered.replace("example.com", "tenantiq.test"), PINS)
    assert [deploy._name(document) for document in release.migrate] == ["tenantiq-migrate"]
    assert release.deployments == ["tenantiq-backend", "tenantiq-frontend", "tenantiq-worker"]
    assert not list(deploy.PROD_OVERLAY.parent.glob(".release-*")), "render left its overlay behind"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_ci_pushes_the_images_the_prod_overlay_pulls():
    overlay = yaml.safe_load((deploy.PROD_OVERLAY / "kustomization.yaml").read_text("utf-8"))
    pulled = {entry["name"]: entry["newName"] for entry in overlay["images"]}
    pushed = _workflow()["env"]

    assert pulled == {
        "tenantiq/backend": pushed["BACKEND_IMAGE"],
        "tenantiq/frontend": pushed["FRONTEND_IMAGE"],
    }


def test_only_a_green_push_to_main_deploys():
    job = _workflow()["jobs"]["deploy"]

    assert "github.event_name == 'push'" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    # A failed test job or image build skips the deploy, so the cluster keeps its last good release.
    assert {"backend", "frontend", "images"} <= set(job["needs"])


def test_the_image_ci_ships_for_the_frontend_is_the_production_target():
    steps = _workflow()["jobs"]["images"]["steps"]
    frontend = next(step for step in steps if step.get("id") == "frontend")

    assert frontend["with"]["target"] == "production"
