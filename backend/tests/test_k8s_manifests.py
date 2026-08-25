"""The deployment manifests are part of the system under test (#24, ADR-0019).

`kubectl kustomize` tells you a manifest *parses*. It cannot tell you that the Ingress still refuses
to route to Django, that prod still ships no secret material, or that a tunable in the ConfigMap is
a key the settings module actually reads. Those are the properties that matter, and each of them is
one careless edit away at any time.

The tests shell out to `kubectl kustomize` rather than reimplementing overlay merging: the thing
being verified is what the cluster would receive, and a second renderer would verify a different
artefact. They skip when kubectl is absent so a fresh clone still runs green, and
``TENANTIQ_REQUIRE_KUBECTL`` turns that skip into a failure — the same guard as the Postgres proofs
in conftest, for the same reason: a check that silently skips in CI is not a check.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S = REPO_ROOT / "infra" / "k8s"
SETTINGS = REPO_ROOT / "backend" / "config" / "settings.py"

OVERLAYS = {"dev": "tenantiq-dev", "prod": "tenantiq"}

# Images this repository builds. Third-party images (postgres, redis, ollama) drop privileges on
# their own terms and are not ours to assert about.
OURS = ("tenantiq/backend", "tenantiq/frontend")


def _require_kubectl() -> bool:
    return os.environ.get("TENANTIQ_REQUIRE_KUBECTL", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _render(overlay: str) -> list[dict]:
    if shutil.which("kubectl") is None:
        if _require_kubectl():
            pytest.fail("TENANTIQ_REQUIRE_KUBECTL is set but kubectl is not installed")
        pytest.skip("kubectl not installed")
    result = subprocess.run(
        ["kubectl", "kustomize", str(K8S / "overlays" / overlay)],
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode == 0
    ), f"`kubectl kustomize overlays/{overlay}` failed:\n{result.stderr}"
    documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert documents, f"overlays/{overlay} rendered nothing"
    return documents


def _containers(document: dict) -> list[dict]:
    spec = document.get("spec", {})
    pod = spec.get("template", {}).get("spec") or spec.get("jobTemplate", {})
    return (pod or {}).get("containers", []) if isinstance(pod, dict) else []


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_the_overlay_builds(overlay: str) -> None:
    """The acceptance criterion, kept true rather than checked once by hand."""
    assert len(_render(overlay)) > 1


@pytest.mark.parametrize("overlay,namespace", sorted(OVERLAYS.items()))
def test_every_resource_lands_in_the_overlays_namespace(overlay: str, namespace: str) -> None:
    """A resource with no namespace goes to whatever `kubectl` happened to be pointed at."""
    stray = [
        f"{doc['kind']}/{doc['metadata']['name']}"
        for doc in _render(overlay)
        if doc["kind"] != "Namespace" and doc["metadata"].get("namespace") != namespace
    ]
    assert not stray, f"not namespaced into {namespace}: {stray}"


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_no_ingress_routes_to_the_backend(overlay: str) -> None:
    """ADR-0013 §1, enforced instead of assumed.

    The browser holds no token; the BFF is the only thing that attaches one. An Ingress rule to
    ``tenantiq-backend`` would let a caller skip the proxy — and with it the CSRF gate, the path
    allowlist and both header allowlists — while every unit test still passed.
    """
    for document in _render(overlay):
        if document["kind"] != "Ingress":
            continue
        for rule in document["spec"].get("rules", []):
            for path in rule.get("http", {}).get("paths", []):
                service = path["backend"]["service"]["name"]
                assert service != "tenantiq-backend", (
                    f"the {overlay} Ingress routes {rule.get('host')}{path.get('path')} straight to "
                    f"Django, bypassing the BFF (ADR-0013 §1)"
                )


def test_prod_ships_no_secret_material() -> None:
    """Production's Secret is created out of band, so a rendered Secret means one entered the repo."""
    secrets = [doc for doc in _render("prod") if doc["kind"] == "Secret"]
    assert not secrets, (
        "the prod overlay rendered a Secret; its values would be committed. Create "
        "`tenantiq-secrets` out of band — see infra/k8s/overlays/prod/kustomization.yaml"
    )


@pytest.mark.parametrize("workload", ["tenantiq-frontend", "tenantiq-backend"])
def test_prod_pins_the_unscalable_workloads_to_one_replica(workload: str) -> None:
    """Two hard constraints in the code, guarded so raising the number means reading why.

    ``frontend`` — `lib/session.ts` keeps sessions in an in-process Map, so a second replica signs
    users out depending on which pod they reach. ``backend`` — the API and worker share the media
    volume, so replicas multiply writers on one filesystem. Both want a code change first: a shared
    session store, and object storage for uploads.
    """
    deployment = next(
        doc
        for doc in _render("prod")
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == workload
    )
    assert deployment["spec"]["replicas"] == 1, (
        f"{workload} is set to {deployment['spec']['replicas']} replicas. Read the comment in "
        f"infra/k8s/overlays/prod/patch-{workload.split('-')[1]}-replicas.yaml before changing this."
    )


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_every_container_declares_resource_requests(overlay: str) -> None:
    """Without requests the scheduler is guessing and the pod is BestEffort — first to be evicted."""
    missing = [
        f"{doc['kind']}/{doc['metadata']['name']}:{container['name']}"
        for doc in _render(overlay)
        for container in _containers(doc)
        if not container.get("resources", {}).get("requests")
    ]
    assert not missing, f"no resource requests: {missing}"


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_our_own_images_run_as_non_root(overlay: str) -> None:
    """Verified against the images themselves: both run `manage.py check` / node cleanly as uid 1000."""
    offenders = []
    for document in _render(overlay):
        pod = (document.get("spec", {}).get("template", {}) or {}).get("spec", {})
        runs_ours = any(
            container.get("image", "").startswith(OURS) for container in _containers(document)
        )
        if runs_ours and not pod.get("securityContext", {}).get("runAsNonRoot"):
            offenders.append(f"{document['kind']}/{document['metadata']['name']}")
    assert not offenders, f"runs one of our images as root: {offenders}"


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_every_configured_tunable_is_one_the_settings_module_reads(overlay: str) -> None:
    """The compose failure mode, made impossible here.

    `x-backend-env` forwards nine of the ~35 variables `config/settings.py` reads, so most documented
    tunables set in `.env` do nothing under compose. The mirror image is just as quiet: a key in this
    ConfigMap that no code reads — a typo, or a setting that was renamed — looks configured and is
    inert. Nothing else would ever report it.
    """
    source = SETTINGS.read_text(encoding="utf-8")
    config = next(
        doc
        for doc in _render(overlay)
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"].startswith("tenantiq-config-")
    )
    unread = [key for key in config["data"] if not re.search(rf'"{re.escape(key)}"', source)]
    assert not unread, (
        f"the {overlay} ConfigMap sets keys config/settings.py never reads: {unread}. "
        f"They look configured and do nothing."
    )
