"""Runner-backend adapter for the A2A Fabric coordinator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import operator_policy as op
import operator_runners as op_runners
from operator_fabric_coordinator import FabricCoordinator
from operator_fabric_core import _EXPECTED_ERRORS, FabricError, load_node_registry


@dataclass
class FabricBackend:
    name: str = "fabric"
    coordinator_factory: Callable[..., FabricCoordinator] = FabricCoordinator

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]:
        try:
            enabled = sorted(
                name
                for name, node in load_node_registry(hermes_root=hermes_root).items()
                if node.enabled
            )
            return {
                "available": bool(enabled),
                "node_count": len(enabled),
                "reason": None if enabled else "no enabled Fabric nodes",
            }
        except _EXPECTED_ERRORS as exc:
            return {
                "available": False,
                "node_count": 0,
                "reason": op.redact_output(str(exc))[:300],
            }

    def dispatch(
        self,
        contract: dict[str, Any],
        *,
        confirm: bool,
        dry_run: bool,
        timeout: int,
        hermes_root: Path | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        coordinator = kwargs.get("fabric_coordinator") or self.coordinator_factory(
            hermes_root=hermes_root
        )
        try:
            return coordinator.dispatch(
                contract,
                dry_run=dry_run,
                confirm=confirm,
                timeout=timeout,
            )
        except FabricError as exc:
            return {
                "success": False,
                "ok": False,
                "changed": bool(exc.ambiguous),
                "backend": self.name,
                "code": exc.code,
                "safe_message": op.redact_output(str(exc))[:300],
                "submission_may_have_succeeded": bool(exc.ambiguous),
            }

    def observed_runs(
        self,
        task_id: str,
        *,
        hermes_root: Path | None = None,
    ) -> list[dict[str, Any]]:
        # Work Contract validation is observational. It must only read evidence
        # previously admitted by an explicit Fabric status/evidence operation.
        return self.coordinator_factory(hermes_root=hermes_root).observed_runs(
            task_id,
            refresh=False,
        )

    def observed_artifacts(
        self,
        task_id: str,
        *,
        contract_sha256: str,
        hermes_root: Path | None = None,
    ) -> list[dict[str, Any]]:
        """Return coordinator-verified admitted artifact metadata, when supported.

        The base G4-A coordinator has no artifact admission store. G4-C installs
        an enhanced coordinator that exposes this read-only evidence surface.
        Keeping the adapter here lets Work Contract validation consume admitted
        artifacts without importing Fabric internals or materializing active
        remote content into an allowed workspace.
        """
        coordinator = self.coordinator_factory(hermes_root=hermes_root)
        observer = getattr(coordinator, "observed_artifacts", None)
        if not callable(observer):
            return []
        try:
            value = observer(task_id, contract_sha256=contract_sha256)
        except FabricError:
            return []
        return value if isinstance(value, list) else []

    def cancel(self, task_id: str, *, hermes_root: Path | None = None) -> dict[str, Any]:
        try:
            return self.coordinator_factory(hermes_root=hermes_root).cancel(task_id)
        except FabricError as exc:
            return {
                "success": False,
                "changed": False,
                "backend": self.name,
                "code": exc.code,
                "safe_message": op.redact_output(str(exc))[:300],
            }


def register_runner_backend() -> None:
    try:
        if isinstance(op_runners.get_backend("fabric"), FabricBackend):
            return
    except LookupError:
        pass
    op_runners.register_backend(FabricBackend(), replace=True)
