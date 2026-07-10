"""
Desired-vs-actual drift detection for SyrvisCore.

The audit found that validators check config/DNS/certs/ports but never compare
the *desired* compose state (what docker-compose.yaml declares) against the
*actual* running state (which containers exist, their images, their status).
This module closes that gap.

It is deliberately pure and read-only: it parses compose YAML and diffs plain
dictionaries. The caller supplies the "actual" state (gathered from docker
elsewhere), so this whole module is unit-testable without docker and performs
no privileged operation.
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

import yaml


class DriftKind(str, Enum):
    """A single kind of desired-vs-actual discrepancy."""

    MISSING = "missing"  # declared but no container exists
    STOPPED = "stopped"  # container exists but is not running
    IMAGE_MISMATCH = "image_mismatch"  # running a different image than declared
    UNEXPECTED = "unexpected"  # container exists that nothing declares


# Which drift kinds represent an unhealthy install (vs. merely informational).
FAILING_KINDS = frozenset({DriftKind.MISSING, DriftKind.STOPPED, DriftKind.IMAGE_MISMATCH})


@dataclass
class DriftItem:
    """One desired-vs-actual discrepancy for a single service."""

    service: str
    kind: DriftKind
    expected: Optional[str] = None
    actual: Optional[str] = None

    @property
    def is_failure(self) -> bool:
        return self.kind in FAILING_KINDS

    def describe(self) -> str:
        if self.kind is DriftKind.MISSING:
            return f"{self.service}: declared (image {self.expected}) but not running"
        if self.kind is DriftKind.STOPPED:
            return f"{self.service}: exists but status is '{self.actual}' (expected running)"
        if self.kind is DriftKind.IMAGE_MISMATCH:
            return f"{self.service}: running {self.actual}, declared {self.expected}"
        return f"{self.service}: running but not declared in compose"

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "service": self.service,
            "kind": self.kind.value,
            "expected": self.expected,
            "actual": self.actual,
            "failure": self.is_failure,
        }


@dataclass
class DriftReport:
    """The full drift result for one compose scope (core stack or one L2 service)."""

    scope: str
    items: List[DriftItem]

    @property
    def failures(self) -> List[DriftItem]:
        return [i for i in self.items if i.is_failure]

    @property
    def in_sync(self) -> bool:
        return not self.failures

    def to_dict(self) -> Dict[str, object]:
        return {
            "scope": self.scope,
            "in_sync": self.in_sync,
            "items": [i.to_dict() for i in self.items],
        }


def _normalize_image(image: str) -> str:
    """Normalize an image reference for comparison.

    Docker reports images with the default registry/namespace expanded
    (``library/traefik:v3.0.0``, ``docker.io/library/...``) while compose files
    often use the short form (``traefik:v3.0.0``). Strip those default prefixes
    so equivalent references compare equal, without being fooled by a genuinely
    different tag or digest.
    """
    if not image or image == "Unknown":
        return image
    ref = image
    for prefix in ("docker.io/", "library/", "index.docker.io/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix) :]
    # A repo with no registry host and no namespace is implicitly library/*
    if "/" not in ref.split(":")[0].split("@")[0]:
        return ref
    return ref


def images_match(expected: str, actual: str) -> bool:
    """True if two image references denote the same image (prefix-normalized)."""
    return _normalize_image(expected) == _normalize_image(actual)


def expected_services_from_compose(compose_path: Path) -> Dict[str, str]:
    """Parse a compose file into {service_name: image}.

    Raises:
        FileNotFoundError: if the compose file does not exist.
        ValueError: if the compose file is malformed.
    """
    if not compose_path.exists():
        raise FileNotFoundError(f"Compose file not found: {compose_path}")
    data = yaml.safe_load(compose_path.read_text())
    if not isinstance(data, dict) or "services" not in data:
        raise ValueError(f"Compose file has no services section: {compose_path}")

    services = {}
    for name, spec in (data.get("services") or {}).items():
        if isinstance(spec, dict) and "image" in spec:
            services[name] = spec["image"]
    return services


def detect_drift(
    scope: str,
    expected: Dict[str, str],
    actual: Dict[str, Dict[str, str]],
    *,
    flag_unexpected: bool = True,
) -> DriftReport:
    """Diff desired services against actual container state.

    Args:
        scope: a label for this comparison (e.g. "core" or "service:gollum").
        expected: {service_name: declared_image} from the compose file.
        actual: {service_name: {"status": ..., "image": ...}} from docker.
        flag_unexpected: report actual services not present in expected.

    Returns:
        A DriftReport listing every discrepancy.
    """
    items: List[DriftItem] = []

    for service, declared_image in expected.items():
        running = actual.get(service)
        if running is None:
            items.append(
                DriftItem(service=service, kind=DriftKind.MISSING, expected=declared_image)
            )
            continue

        status = (running.get("status") or "").lower()
        if status != "running":
            items.append(
                DriftItem(
                    service=service,
                    kind=DriftKind.STOPPED,
                    expected=declared_image,
                    actual=running.get("status") or "unknown",
                )
            )
            # A stopped container can still be on the wrong image; report both.

        actual_image = running.get("image") or "Unknown"
        if actual_image != "Unknown" and not images_match(declared_image, actual_image):
            items.append(
                DriftItem(
                    service=service,
                    kind=DriftKind.IMAGE_MISMATCH,
                    expected=declared_image,
                    actual=actual_image,
                )
            )

    if flag_unexpected:
        for service in actual:
            if service not in expected:
                items.append(
                    DriftItem(
                        service=service,
                        kind=DriftKind.UNEXPECTED,
                        actual=actual[service].get("image") or "Unknown",
                    )
                )

    return DriftReport(scope=scope, items=items)
