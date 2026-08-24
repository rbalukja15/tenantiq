"""The toolchain is part of the system under test (#54).

Three defects motivated this module, and each has a test below:

- dependency resolution floated, so CI installed whatever PyPI had released that morning;
- the lint gates disagreed — pre-commit pinned ruff 0.6.9 while CI installed `ruff>=0.6` and got
  0.16, which selects a different ruleset;
- nothing checked that a lock still satisfies what `pyproject.toml` declares, so a bound added
  after a lock was generated (the Django ceiling in #71 is the live example) could be silently
  violated by the very file CI installs from.

These are offline checks on files in the repo. They cannot tell you the lock is *fresh* — only
`make lock` and a diff can — but they can tell you it is *wrong*, which is the failure that hurts.
"""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import PackageNotFoundError, version as installed_version
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
PYPROJECT = BACKEND / "pyproject.toml"
RUNTIME_LOCK = BACKEND / "requirements.lock"
DEV_LOCK = BACKEND / "requirements-dev.lock"
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"

# The tools whose version silently changes what `make lint` accepts. A ruff minor bump can broaden
# the default ruleset and a black bump can restyle the whole tree, so a developer running a
# different version than CI gets a green local run and a red pull request.
LINTERS = ("ruff", "black")


def _pins(lock: Path) -> dict[str, tuple[str, list[str]]]:
    """Parse a pip-compile lock into {canonical name: (version, hashes)}.

    Deliberately a small parser rather than a dependency: the lock is the input to this check, and
    reading it with the same library that writes it would let a format change pass unnoticed.
    """
    text = lock.read_text(encoding="utf-8")
    text = re.sub(r"\\\n", " ", text)  # join the backslash continuations pip-compile emits
    found: dict[str, tuple[str, list[str]]] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        spec, *rest = line.split()
        if "==" not in spec:
            continue
        name, _, pinned = spec.partition("==")
        found[canonicalize_name(name)] = (
            pinned,
            [token for token in rest if token.startswith("--hash=")],
        )
    return found


def _declared() -> tuple[list[Requirement], list[Requirement]]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    runtime = [Requirement(item) for item in data["project"]["dependencies"]]
    dev = [Requirement(item) for item in data["project"]["optional-dependencies"]["dev"]]
    return runtime, dev


@pytest.mark.parametrize("lock", [RUNTIME_LOCK, DEV_LOCK], ids=lambda p: p.name)
def test_a_lock_exists_and_is_fully_pinned_with_hashes(lock: Path) -> None:
    """A lock without hashes pins a version but not the artefact behind it."""
    assert lock.exists(), f"{lock.name} is missing — run `make lock`"

    pins = _pins(lock)
    assert pins, f"{lock.name} pinned nothing"

    unhashed = sorted(name for name, (_, hashes) in pins.items() if not hashes)
    assert not unhashed, f"{lock.name} pins these without a hash: {unhashed}"


@pytest.mark.parametrize("kind", ["runtime", "dev"])
def test_every_pin_satisfies_what_pyproject_declares(kind: str) -> None:
    """The #71 failure mode, made impossible to ship.

    `pyproject.toml` carries the reasoning — the Django ceiling exists because DRF imports a symbol
    Django 6.1 removed. A lock generated before that ceiling was added would keep installing the
    broken pair while the file explaining why it must not sat right next to it.
    """
    runtime, dev = _declared()
    requirements = runtime if kind == "runtime" else runtime + dev
    pins = _pins(RUNTIME_LOCK if kind == "runtime" else DEV_LOCK)

    for requirement in requirements:
        name = canonicalize_name(requirement.name)
        assert name in pins, f"{requirement.name} is declared in pyproject but absent from the lock"
        pinned, _ = pins[name]
        assert requirement.specifier.contains(pinned, prereleases=True), (
            f"the lock pins {requirement.name}=={pinned}, which violates the declared "
            f"'{requirement}' — regenerate with `make lock`"
        )


def test_dev_and_runtime_locks_agree_on_every_shared_package() -> None:
    """Tests must exercise the versions production runs.

    Two independently resolved locks can drift apart on a shared transitive dependency, and then the
    suite is green against a library the deployed image does not contain.
    """
    runtime_pins, dev_pins = _pins(RUNTIME_LOCK), _pins(DEV_LOCK)

    missing = sorted(set(runtime_pins) - set(dev_pins))
    assert not missing, f"the dev lock is missing runtime packages: {missing}"

    disagreements = {
        name: (pinned, dev_pins[name][0])
        for name, (pinned, _) in runtime_pins.items()
        if dev_pins[name][0] != pinned
    }
    assert not disagreements, f"dev and runtime locks disagree: {disagreements}"


@pytest.mark.parametrize("linter", LINTERS)
def test_the_installed_linter_is_the_one_the_lock_chose(linter: str) -> None:
    """Catches the stale environment, which is what made the pins disagree in the first place.

    A venv built before a lock bump keeps running the old linter, so `make lint` passes locally and
    CI — which installs from the lock every time — fails on rules the developer never saw.
    """
    pinned, _ = _pins(DEV_LOCK)[canonicalize_name(linter)]
    try:
        running = installed_version(linter)
    except PackageNotFoundError:  # pragma: no cover - only on a broken environment
        pytest.fail(f"{linter} is not installed — run `make bootstrap`")

    assert Version(running) == Version(pinned), (
        f"{linter} {running} is installed but the lock pins {pinned}; this environment lints "
        f"differently than CI — run `make bootstrap`"
    )


@pytest.mark.parametrize("linter", LINTERS)
def test_pre_commit_does_not_pin_a_linter_independently(linter: str) -> None:
    """One source of truth, enforced structurally rather than by remembering to update two files.

    pre-commit's remote hooks carry their own `rev:`, which is a second place a version can be
    written and therefore a second place it can be wrong. The local hooks run the interpreter the
    lock installed, so there is no rev to drift.
    """
    offenders = [
        f"{repo} pinned at {rev}" for repo, rev in _pre_commit_remote_revs() if linter in repo
    ]
    assert not offenders, (
        f"pre-commit supplies {linter} from a pinned remote hook ({offenders}); the version "
        f"belongs only in requirements-dev.lock"
    )


def _pre_commit_remote_revs() -> list[tuple[str, str]]:
    """Pair each `repo:` in the pre-commit config with the `rev:` that follows it.

    A hand parser rather than PyYAML: this asserts on two literal keys, and adding a YAML
    dependency to the backend so a test can read a repo-root config file is a worse trade than
    twelve lines.
    """
    pairs: list[tuple[str, str]] = []
    repo = ""
    for line in PRE_COMMIT.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().lstrip("-").strip()
        if stripped.startswith("repo:"):
            repo = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("rev:") and repo:
            pairs.append((repo, stripped.split(":", 1)[1].strip()))
    return pairs
