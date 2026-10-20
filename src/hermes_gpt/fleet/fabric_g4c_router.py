"""G4-C capability-aware routing and runtime backend registration."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hermes_gpt.fleet import fabric_artifacts as artifacts
from hermes_gpt.fleet import fabric_write_guard as write_guard
from hermes_gpt.fleet import fabric as base
from hermes_gpt.fleet import fabric_router as router
from hermes_gpt.execution import runners
from hermes_gpt.fleet.fabric_g4c_coordinator import FabricCoordinator

FabricNode = base.FabricNode


class AutoRouter(router.AutoRouter):
    """Unlock only remote G4-B bridge exclusions proven by live G4-C features."""

    def __init__(
        self,
        *,
        remote_probe: Callable[[FabricNode, int], dict[str, Any]] | None = None,
        hermes_root: Path | None = None,
        **kwargs: Any,
    ) -> None:
        self._features: dict[str, set[str]] = {}
        if remote_probe is None:
            coordinator = FabricCoordinator(hermes_root=hermes_root)

            def selected_probe(node: FabricNode, timeout: int) -> dict[str, Any]:
                started = time.perf_counter()
                snapshot = coordinator._capabilities(node, timeout)
                features = set(snapshot.get("features") or [])
                self._features[node.name] = features
                return {
                    "healthy": True,
                    "latency_ms": (time.perf_counter() - started) * 1000.0,
                    "snapshot_sha256": snapshot.get("snapshot_sha256", ""),
                    "features": sorted(features),
                }

        else:

            def selected_probe(node: FabricNode, timeout: int) -> dict[str, Any]:
                result = remote_probe(node, timeout)
                features = result.get("features") if isinstance(result, dict) else None
                self._features[node.name] = (
                    set(features) if isinstance(features, (list, tuple, set)) else set()
                )
                return result

        super().__init__(remote_probe=selected_probe, hermes_root=hermes_root, **kwargs)

    def _audit_decision(self, decision: dict[str, Any], *, dry_run: bool) -> None:
        # The base G4-B decision is preliminary for write/artifact-capable remote
        # candidates. Defer its audit until ``route`` applies the live G4-C
        # feature gates so the durable audit trail describes the actual winner.
        return None

    def route(
        self,
        contract: dict[str, Any],
        *,
        timeout: int = 15,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        decision = super().route(contract, timeout=timeout, dry_run=dry_run)
        try:
            needs_write = write_guard.is_write(contract)
            needs_artifacts = bool(contract.get("expected_artifacts"))
            if not (needs_write or needs_artifacts):
                router._audit_route(
                    decision,
                    success=decision.get("selected") is not None,
                    dry_run=dry_run,
                )
                return decision
            for candidate in decision.get("candidates", []):
                if not candidate.get("remote"):
                    continue
                features = self._features.get(str(candidate.get("node") or ""), set())
                filtered: list[dict[str, str]] = []
                for exclusion in candidate.get("exclusions") or []:
                    code = exclusion.get("code")
                    unlock_write = (
                        code == "WRITE_CONFLICT_GUARD_UNAVAILABLE"
                        and needs_write
                        and write_guard.WRITE_FEATURES <= features
                    )
                    unlock_artifact = (
                        code == "REMOTE_ARTIFACT_ADMISSION_UNAVAILABLE"
                        and needs_artifacts
                        and artifacts.ARTIFACT_FEATURES <= features
                    )
                    if not (unlock_write or unlock_artifact):
                        filtered.append(exclusion)
                candidate["exclusions"] = filtered
                candidate["eligible"] = not filtered
                candidate["g4c_features"] = sorted(features)
            eligible = sorted(
                (item for item in decision.get("candidates", []) if item.get("eligible")),
                key=lambda item: tuple(item.get("rank") or []),
            )
            decision["selected"] = None
            if eligible:
                winner = eligible[0]
                decision["selected"] = {
                    "node": winner["node"],
                    "backend": winner["backend"],
                    "transport_backend": winner["transport_backend"],
                    "remote": winner["remote"],
                    "healthy": winner["healthy"],
                    "capability_fresh": winner["capability_fresh"],
                    "authority_ceiling": winner["authority_ceiling"],
                    "eligible": winner["eligible"],
                    "exclusions": list(winner["exclusions"]),
                    "rank": winner["rank"],
                }
        except Exception:
            # G4-C feature-gate processing raised after the preliminary base
            # route. The base audit was deferred, so emit exactly one failure
            # record now. The preliminary winner is never claimed as selected:
            # the gate evaluation that proves eligibility did not complete.
            failed = dict(decision)
            failed["selected"] = None
            router._audit_route(failed, success=False, dry_run=dry_run)
            raise
        decision["g4c_guards"] = {
            "write_required": needs_write,
            "artifact_required": needs_artifacts,
        }
        router._audit_route(
            decision,
            success=decision.get("selected") is not None,
            dry_run=dry_run,
        )
        return decision


def register_runtime() -> None:
    """Replace only runtime backend registrations; leave G4-A classes intact."""
    runners.register_backend(
        base.FabricBackend(coordinator_factory=FabricCoordinator),
        replace=True,
    )
    runners.register_backend(
        router.AutoBackend(router_factory=AutoRouter),
        replace=True,
    )
