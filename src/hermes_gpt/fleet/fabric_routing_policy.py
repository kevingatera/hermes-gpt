"""Fabric routing policy: models, errors, and bounded policy loading.

This module owns the policy half of the G4-B capability-aware placement router:
the immutable ``TargetFacts``/``RoutingPolicy`` models, the shared
``RoutingError`` type, and the fail-closed loader/validator for the
``fabric-routing.json`` policy document (``ROUTING_POLICY_ENV``).

Property checks, eligibility filtering, soft ranking, decision journaling, and
backend dispatch stay in ``operator_fabric_router``. That module imports these
names back, so historical import paths and the raised error type keep their
identity; this module never imports the router.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_gpt.fleet import fabric
from hermes_gpt.policy import authorization as op

ROUTING_POLICY_SCHEMA = "hermes.fabric-routing-policy/v1"
ROUTING_POLICY_ENV = "HERMES_GPT_FABRIC_ROUTING_POLICY"

_TARGET_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}$")
_MAX_TARGETS = 129
_MAX_LIST = 64
_MAX_AGE_SECONDS = 86_400


class RoutingError(RuntimeError):
    def __init__(self, code: str, message: str, *, decision: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.decision = decision


@dataclass(frozen=True)
class TargetFacts:
    observed_at: datetime
    max_age_seconds: int
    os_names: frozenset[str]
    runtimes: frozenset[str]
    runners: frozenset[str]
    providers: frozenset[str]
    models: frozenset[str]
    tools: frozenset[str]
    browser: bool | None
    vision: bool | None
    gpu_available: bool | None
    gpu_vendor: str
    gpu_memory_mb: int | None
    capacity: int | None
    active: int | None
    cost_bucket: int | None
    locality_bucket: int | None

    def fresh(self, now: datetime) -> bool:
        age = (now - self.observed_at).total_seconds()
        return -60 <= age <= self.max_age_seconds


@dataclass(frozen=True)
class RoutingPolicy:
    targets: dict[str, TargetFacts]


def _safe_text(value: Any, *, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} is invalid")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} contains control characters")
    return value


def _tokens(value: Any, *, field: str, lower: bool = False) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or len(value) > _MAX_LIST:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} must be a bounded list")
    out: set[str] = set()
    for raw in value:
        item = _safe_text(raw, field=field)
        if not _TOKEN_RE.fullmatch(item):
            raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} contains an invalid value")
        out.add(item.lower() if lower else item)
    return frozenset(out)


def _iso_datetime(value: Any, *, field: str) -> datetime:
    text = _safe_text(value, field=field, maximum=128)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _bucket(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} must be an integer from 0 through 9")
    return value


def _count(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} must be a bounded non-negative integer")
    return value


def _bool_or_none(value: Any, *, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} must be boolean")
    return value


def _routing_policy_path(hermes_root: Path | None = None) -> Path:
    configured = os.environ.get(ROUTING_POLICY_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute() or op.is_denied_path(path) or path.is_symlink():
            raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "routing policy path is not allowed")
        return path
    return fabric._root(hermes_root) / "config" / "fabric-routing.json"


def load_routing_policy(
    path: Path | None = None,
    *,
    hermes_root: Path | None = None,
) -> RoutingPolicy:
    target_path = path or _routing_policy_path(hermes_root)
    if not target_path.is_file():
        if os.environ.get(ROUTING_POLICY_ENV, "").strip():
            raise RoutingError("FABRIC_ROUTING_CONFIG_MISSING", "configured Fabric routing policy is missing")
        return RoutingPolicy(targets={})
    try:
        # Keep file reads bounded before the strict parser checks payload size.
        with target_path.open("rb") as handle:
            raw = fabric.strict_json_loads(handle.read(fabric._MAX_BODY + 1))
    except OSError as exc:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "routing policy could not be read") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema", "version", "targets"}:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "routing policy has unknown or missing fields")
    if raw["schema"] != ROUTING_POLICY_SCHEMA or raw["version"] != 1:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "routing policy schema/version is unsupported")
    targets_raw = raw["targets"]
    if not isinstance(targets_raw, dict) or len(targets_raw) > _MAX_TARGETS:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "routing policy targets are invalid")

    targets: dict[str, TargetFacts] = {}
    allowed = {
        "observed_at",
        "max_age_seconds",
        "os",
        "runtimes",
        "runners",
        "providers",
        "models",
        "tools",
        "browser",
        "vision",
        "gpu",
        "capacity",
        "active",
        "cost_bucket",
        "locality_bucket",
    }
    for target_name, value in targets_raw.items():
        name = _safe_text(target_name, field="routing target", maximum=64)
        if not _TARGET_RE.fullmatch(name):
            raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "routing target name is invalid")
        if not isinstance(value, dict) or "observed_at" not in value or set(value) - allowed:
            raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"routing target {name!r} has invalid fields")
        max_age = value.get("max_age_seconds", 300)
        if isinstance(max_age, bool) or not isinstance(max_age, int) or not 1 <= max_age <= _MAX_AGE_SECONDS:
            raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "max_age_seconds is invalid")
        gpu_raw = value.get("gpu")
        gpu_available: bool | None = None
        gpu_vendor = ""
        gpu_memory_mb: int | None = None
        if gpu_raw is not None:
            if not isinstance(gpu_raw, dict) or set(gpu_raw) - {"available", "vendor", "memory_mb"}:
                raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "gpu capability is invalid")
            gpu_available = _bool_or_none(gpu_raw.get("available"), field="gpu.available")
            if "vendor" in gpu_raw:
                gpu_vendor = _safe_text(gpu_raw["vendor"], field="gpu.vendor", maximum=64)
            gpu_memory_mb = _count(gpu_raw.get("memory_mb"), field="gpu.memory_mb")
        targets[name] = TargetFacts(
            observed_at=_iso_datetime(value["observed_at"], field=f"targets.{name}.observed_at"),
            max_age_seconds=max_age,
            os_names=_tokens(value.get("os"), field=f"targets.{name}.os", lower=True),
            runtimes=_tokens(value.get("runtimes"), field=f"targets.{name}.runtimes", lower=True),
            runners=_tokens(value.get("runners"), field=f"targets.{name}.runners", lower=True),
            providers=_tokens(value.get("providers"), field=f"targets.{name}.providers", lower=True),
            models=_tokens(value.get("models"), field=f"targets.{name}.models"),
            tools=_tokens(value.get("tools"), field=f"targets.{name}.tools", lower=True),
            browser=_bool_or_none(value.get("browser"), field="browser"),
            vision=_bool_or_none(value.get("vision"), field="vision"),
            gpu_available=gpu_available,
            gpu_vendor=gpu_vendor,
            gpu_memory_mb=gpu_memory_mb,
            capacity=_count(value.get("capacity"), field="capacity"),
            active=_count(value.get("active"), field="active"),
            cost_bucket=_bucket(value.get("cost_bucket"), field="cost_bucket"),
            locality_bucket=_bucket(value.get("locality_bucket"), field="locality_bucket"),
        )
    return RoutingPolicy(targets=targets)


__all__ = [
    "ROUTING_POLICY_ENV",
    "ROUTING_POLICY_SCHEMA",
    "RoutingError",
    "RoutingPolicy",
    "TargetFacts",
    "load_routing_policy",
]
