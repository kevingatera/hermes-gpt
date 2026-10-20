"""G4-C coordinator for write safety, retries, and admitted artifacts."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from hermes_gpt.fleet import fabric_artifacts as artifacts
from hermes_gpt.fleet import fabric_write_guard as write_guard
from hermes_gpt.fleet import fabric as base
from hermes_gpt.fleet import fabric_router as router
from hermes_gpt.fleet.fabric_g4c_protocol import _bounded_peer_observation

FabricError = base.FabricError
FabricNode = base.FabricNode
sha256_json = base.sha256_json


class FabricCoordinator(base.FabricCoordinator):
    """Coordinator with G4-C feature preflight, artifacts, and retry safety."""

    def _ensure_g4c(self) -> artifacts.CoordinatorArtifactStore:
        write_guard.migrate_coordinator(self.db_path)
        return artifacts.CoordinatorArtifactStore(
            self.db_path,
            base._root(self.hermes_root) / "fabric" / "admitted",
        )

    def dispatch(
        self,
        contract: dict[str, Any],
        *,
        dry_run: bool,
        confirm: bool,
        timeout: int,
    ) -> dict[str, Any]:
        store = self._ensure_g4c()
        specs = artifacts.contract_specs(contract)
        node_name, _backend, _workspace, _options, _evidence = base._fabric_options(contract)
        node = self._node(node_name)
        if not dry_run and (specs or write_guard.is_write(contract)):
            capabilities = self._capabilities(node, timeout)
            features = set(capabilities.get("features") or [])
            if specs and not artifacts.ARTIFACT_FEATURES <= features:
                raise FabricError(
                    "FABRIC_ARTIFACT_ADMISSION_UNAVAILABLE",
                    "managed peer lacks immutable bounded artifact transfer",
                )
            if write_guard.is_write(contract) and not write_guard.WRITE_FEATURES <= features:
                raise FabricError(
                    "FABRIC_EXECUTION_UNIT_UNAVAILABLE",
                    "managed peer lacks verified write ownership/containment",
                )
        result = super().dispatch(
            contract,
            dry_run=dry_run,
            confirm=confirm,
            timeout=timeout,
        )
        if not dry_run and specs and result.get("attempt_id") and result.get("dispatch_id"):
            with base._connect_readonly(self.db_path) as db:
                dispatch = db.execute(
                    "SELECT contract_sha256 FROM dispatches WHERE dispatch_id=?",
                    (result["dispatch_id"],),
                ).fetchone()
            if dispatch is not None:
                store.remember(
                    attempt_id=result["attempt_id"],
                    dispatch_id=result["dispatch_id"],
                    contract_sha256=dispatch["contract_sha256"],
                    specs=specs,
                )
        return result

    def poll(
        self,
        attempt_id: str,
        *,
        reconcile: bool = False,
        timeout: int = 15,
    ) -> dict[str, Any]:
        self._ensure_g4c()
        try:
            result = super().poll(attempt_id, reconcile=reconcile, timeout=timeout)
        except FabricError as exc:
            attempt, dispatch, node = self._attempt(attempt_id)
            state = (
                "RECONCILING"
                if exc.code in {"FABRIC_TRANSPORT_TIMEOUT", "FABRIC_PEER_UNAVAILABLE"}
                else "BLOCKED"
            )
            with base._connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state=?,error_code=?,updated_at=? WHERE attempt_id=?",
                    (state, exc.code, base._now(), attempt_id),
                )
            return {
                "success": False,
                "backend": "fabric",
                "node": node.name,
                "dispatch_id": attempt["dispatch_id"],
                "attempt_id": attempt_id,
                "task_id": dispatch["task_id"],
                "state": state,
                "code": exc.code,
            }
        return result

    def cancel(self, attempt_id: str, *, timeout: int = 15) -> dict[str, Any]:
        self._ensure_g4c()
        try:
            return super().cancel(attempt_id, timeout=timeout)
        except FabricError as exc:
            if exc.code not in {"FABRIC_TRANSPORT_TIMEOUT", "FABRIC_PEER_UNAVAILABLE"}:
                raise
            attempt, dispatch, node = self._attempt(attempt_id)
            with base._connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state='CANCEL_AMBIGUOUS',error_code=?,updated_at=?"
                    " WHERE attempt_id=?",
                    (exc.code, base._now(), attempt_id),
                )
            return {
                "success": False,
                "changed": True,
                "backend": "fabric",
                "node": node.name,
                "attempt_id": attempt_id,
                "dispatch_id": attempt["dispatch_id"],
                "task_id": dispatch["task_id"],
                "state": "CANCEL_AMBIGUOUS",
                "code": exc.code,
                "suggested_action": "Reconcile this exact attempt before any retry.",
            }

    def _validate_manifest(
        self,
        manifest: Any,
        *,
        attempt: sqlite3.Row,
        dispatch: sqlite3.Row,
        node: FabricNode,
        specs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return artifacts.CoordinatorArtifactStore.validate_manifest(
            manifest,
            attempt=attempt,
            dispatch=dispatch,
            node=node,
            specs=specs,
        )

    def _pull_artifact(
        self,
        attempt: sqlite3.Row,
        node: FabricNode,
        item: dict[str, Any],
        *,
        timeout: int,
    ) -> dict[str, Any]:
        store = self._ensure_g4c()
        return store.pull(
            attempt=attempt,
            node=node,
            item=item,
            rpc=self.rpc,
            timeout=timeout,
        )

    def collect_artifacts(
        self,
        attempt_id: str,
        *,
        timeout: int = 15,
    ) -> list[dict[str, Any]]:
        store = self._ensure_g4c()
        specs = store.specs(attempt_id)
        if specs is None:
            return []
        attempt, dispatch, node = self._attempt(attempt_id)
        _, response = self.rpc(
            node,
            base._request(
                "artifact_manifest",
                node.coordinator_principal,
                data={
                    "artifacts": specs,
                    "max_artifact_bytes": artifacts.MAX_ARTIFACT_BYTES,
                    "max_total_bytes": artifacts.MAX_TOTAL_ARTIFACT_BYTES,
                },
                dispatch_id=attempt["dispatch_id"],
                attempt_id=attempt_id,
            ),
            timeout,
        )
        response = base._validate_response(response, operation="artifact_manifest")
        wrapper = base._closed(
            response["data"],
            required={"manifest"},
            name="artifact manifest response",
        )
        items = self._validate_manifest(
            wrapper["manifest"],
            attempt=attempt,
            dispatch=dispatch,
            node=node,
            specs=specs,
        )
        spec_map = {spec["path"]: spec for spec in specs}
        admitted: list[dict[str, Any]] = []
        for item in items:
            receipt = self._pull_artifact(attempt, node, item, timeout=timeout)
            if receipt["size_bytes"] < int(spec_map[item["logical_name"]]["min_bytes"]):
                raise FabricError(
                    "FABRIC_ARTIFACT_TOO_SMALL",
                    "artifact is below the contract minimum size",
                )
            admitted.append(receipt)
        return admitted

    def collect(self, attempt_id: str, *, timeout: int = 15) -> dict[str, Any]:
        store = self._ensure_g4c()
        has_artifacts = store.specs(attempt_id) is not None
        admitted: list[dict[str, Any]] = []
        if has_artifacts:
            try:
                admitted = self.collect_artifacts(attempt_id, timeout=timeout)
            except FabricError:
                with base._connect(self.db_path) as db:
                    db.execute(
                        "UPDATE attempts SET state='EVIDENCE_PENDING',updated_at=? WHERE attempt_id=?",
                        (base._now(), attempt_id),
                    )
                raise
        result = super().collect(attempt_id, timeout=timeout)
        if has_artifacts:
            result["artifacts"] = admitted
        return result

    def retry(
        self,
        contract: dict[str, Any],
        prior_attempt_id: str,
        *,
        confirm: bool,
        timeout: int = 15,
    ) -> dict[str, Any]:
        if not confirm:
            raise FabricError("CONFIRMATION_REQUIRED", "Fabric retry requires confirm=true")
        self._ensure_g4c()
        prior, dispatch, node = self._attempt(prior_attempt_id)
        if base._contract_sha(contract) != dispatch["contract_sha256"]:
            raise FabricError(
                "FABRIC_RETRY_LINEAGE_MISMATCH",
                "retry contract differs from original dispatch",
            )
        status = self.poll(prior_attempt_id, reconcile=True, timeout=timeout)
        if status.get("write_claim_state") == "ACTIVE":
            raise FabricError(
                "FABRIC_WRITE_OWNERSHIP_BLOCKED",
                "prior write ownership remains active",
            )
        if status.get("state") not in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "BLOCKED",
            "TERMINAL_REPORTED",
        }:
            raise FabricError(
                "FABRIC_RETRY_BLOCKED",
                "prior attempt is not safely reconciled",
            )
        node_name, backend, workspace, options, evidence = base._fabric_options(contract)
        if node_name != node.name:
            raise FabricError(
                "FABRIC_RETRY_LINEAGE_MISMATCH",
                "retry targets a different managed node",
            )
        capabilities = self._capabilities(node, timeout)
        with base._connect_readonly(self.db_path) as db:
            count = int(
                db.execute(
                    "SELECT COUNT(*) AS n FROM attempts WHERE dispatch_id=?",
                    (prior["dispatch_id"],),
                ).fetchone()["n"]
            )
        envelope = base._build_envelope(
            contract,
            node,
            remote_backend=backend,
            logical_workspace=workspace,
            remote_options=options,
            evidence_policy=evidence,
            capability_sha=capabilities["snapshot_sha256"],
        )
        attempt_id = base._attempt_id(prior["dispatch_id"], count + 1)
        envelope["attempt_id"] = attempt_id
        envelope["retry_parent_attempt_id"] = prior_attempt_id
        now = base._now()
        with base._connect(self.db_path) as db:
            db.execute(
                "INSERT INTO attempts"
                "(attempt_id,dispatch_id,envelope_sha256,node_name,peer_name,remote_backend,coordinator_principal,"
                "capability_sha256,state,created_at,updated_at,retry_parent_attempt_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    prior["dispatch_id"],
                    sha256_json(envelope),
                    node.name,
                    node.a2a_peer_name,
                    backend,
                    node.coordinator_principal,
                    capabilities["snapshot_sha256"],
                    "SUBMITTING",
                    now,
                    now,
                    prior_attempt_id,
                ),
            )
        try:
            remote_task_id, response = self.rpc(
                node,
                base._request(
                    "accept",
                    node.coordinator_principal,
                    data={"envelope": envelope},
                    dispatch_id=prior["dispatch_id"],
                    attempt_id=attempt_id,
                ),
                timeout,
            )
        except FabricError as exc:
            state = "SUBMISSION_AMBIGUOUS" if exc.ambiguous else "BLOCKED"
            with base._connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state=?,error_code=?,updated_at=? WHERE attempt_id=?",
                    (state, exc.code, base._now(), attempt_id),
                )
            return {
                "success": False,
                "changed": bool(exc.ambiguous),
                "backend": "fabric",
                "dispatch_id": prior["dispatch_id"],
                "attempt_id": attempt_id,
                "retry_parent_attempt_id": prior_attempt_id,
                "state": state,
                "code": exc.code,
            }
        response = base._validate_response(response, operation="accept")
        data = response["data"]
        if data.get("dispatch_id") != prior["dispatch_id"] or data.get("attempt_id") != attempt_id:
            raise FabricError("FABRIC_PROTOCOL_ERROR", "peer retry accept lineage mismatch")
        state = "SUBMITTED" if response["ok"] else "BLOCKED"
        epoch_value = data.get("write_epoch")
        epoch = (
            epoch_value
            if isinstance(epoch_value, int) and not isinstance(epoch_value, bool)
            else None
        )
        claim_state = _bounded_peer_observation(
            "write_claim_state", data.get("write_claim_state")
        )
        unit_state = _bounded_peer_observation(
            "execution_unit_state", data.get("execution_unit_state")
        )
        with base._connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,remote_task_id=?,peer_policy_sha256=?,write_epoch=?,"
                "write_claim_state=?,execution_unit_state=?,error_code=?,updated_at=?"
                " WHERE attempt_id=?",
                (
                    state,
                    remote_task_id,
                    data.get("policy_sha256"),
                    epoch,
                    claim_state,
                    unit_state,
                    None if response["ok"] else response["code"],
                    base._now(),
                    attempt_id,
                ),
            )
        specs = artifacts.contract_specs(contract)
        if specs:
            self._ensure_g4c().remember(
                attempt_id=attempt_id,
                dispatch_id=prior["dispatch_id"],
                contract_sha256=dispatch["contract_sha256"],
                specs=specs,
            )
        return {
            "success": bool(response["ok"]),
            "changed": bool(response["ok"]),
            "backend": "fabric",
            "node": node.name,
            "dispatch_id": prior["dispatch_id"],
            "attempt_id": attempt_id,
            "retry_parent_attempt_id": prior_attempt_id,
            "state": state,
            "write_epoch": epoch,
            "write_claim_state": claim_state,
            "execution_unit_state": unit_state,
            "code": response["code"],
        }

    def reconcile_active(self, *, timeout: int = 10) -> list[dict[str, Any]]:
        self._ensure_g4c()
        with base._connect_readonly(self.db_path) as db:
            rows = db.execute(
                "SELECT attempt_id FROM attempts "
                "WHERE state NOT IN ('COMPLETED','FAILED','CANCELLED') "
                "ORDER BY created_at LIMIT 128"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                out.append(self.poll(row["attempt_id"], reconcile=True, timeout=timeout))
            except FabricError as exc:
                out.append(
                    {
                        "attempt_id": row["attempt_id"],
                        "success": False,
                        "state": "BLOCKED",
                        "code": exc.code,
                    }
                )
        return out

    def observed_artifacts(
        self,
        task_id: str,
        *,
        contract_sha256: str,
    ) -> list[dict[str, Any]]:
        """Return re-verified coordinator-admitted artifacts for one contract lineage.

        Auto placement changes the canonical contract before Fabric dispatch. The
        routing journal is coordinator-local evidence linking the original Work
        Contract hash to that placed hash. Only completed attempts with admitted
        run evidence are eligible, and the admission bytes are re-hashed before
        they are returned as completion evidence.
        """
        if not base._ID_RE.fullmatch(task_id or "") or not base._SHA_RE.fullmatch(contract_sha256 or ""):
            return []
        # Observation must remain strictly read-only. Ordinary Work Contract
        # validation may construct the Fabric backend even when Fabric has never
        # run; do not migrate/create a coordinator journal merely to look for
        # artifact evidence.
        if not self.db_path.is_file():
            return []
        try:
            with base._connect_readonly(self.db_path) as db:
                tables = {
                    str(row["name"])
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
        except sqlite3.Error:
            return []
        if not {"attempts", "dispatches", "artifact_admissions"} <= tables:
            return []

        allowed_shas = {contract_sha256}
        journal = router._journal_path(self.hermes_root)
        try:
            # Stream the whole journal: an older but still-valid routing
            # decision must not silently vanish merely because the journal grew
            # past a fixed tail window. Parsing stays bounded per record (each
            # line is length-capped and independently JSON-validated), and the
            # file is opened read-only for the duration of the scan.
            with journal.open("rb") as fh:
                for raw_line in fh:
                    if not raw_line or len(raw_line) > 128_000:
                        continue
                    try:
                        record = base.strict_json_loads(raw_line, maximum=128_000)
                    except FabricError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    selected = record.get("selected")
                    if (
                        record.get("schema") != router.ROUTING_DECISION_SCHEMA
                        or record.get("task_id") != task_id
                        or record.get("original_contract_sha256") != contract_sha256
                        or not isinstance(selected, dict)
                        or selected.get("remote") is not True
                        or selected.get("transport_backend") != "fabric"
                    ):
                        continue
                    placed_sha = record.get("placed_contract_sha256")
                    if isinstance(placed_sha, str) and base._SHA_RE.fullmatch(placed_sha):
                        allowed_shas.add(placed_sha)
        except OSError:
            pass

        placeholders = ",".join("?" for _ in allowed_shas)
        with base._connect_readonly(self.db_path) as db:
            rows = db.execute(
                "SELECT aa.*,d.contract_sha256,a.state,a.evidence_json,a.created_at "
                "FROM artifact_admissions aa "
                "JOIN dispatches d ON d.dispatch_id=aa.dispatch_id "
                "JOIN attempts a ON a.attempt_id=aa.attempt_id "
                f"WHERE d.task_id=? AND d.contract_sha256 IN ({placeholders}) "
                "AND a.state='COMPLETED' AND a.evidence_json IS NOT NULL "
                "ORDER BY a.created_at,aa.logical_name",
                (task_id, *sorted(allowed_shas)),
            ).fetchall()

        admission_root = (base._root(self.hermes_root) / "fabric" / "admitted").resolve()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                candidate = Path(str(row["admission_path"]))
                if candidate.is_symlink():
                    continue
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(admission_root) or not resolved.is_file():
                    continue
                expected_size = int(row["size_bytes"])
                before_stat = resolved.stat()
                if before_stat.st_size != expected_size:
                    continue
                first_digest = hashlib.sha256()
                with resolved.open("rb") as fh:
                    while chunk := fh.read(1024 * 1024):
                        first_digest.update(chunk)
                # Re-read independently before accepting the admission. A
                # same-size rewrite can occur within one filesystem timestamp
                # tick, so mtime/ctime identity checks alone are not a
                # deterministic stability proof.
                second_digest = hashlib.sha256()
                with resolved.open("rb") as fh:
                    while chunk := fh.read(1024 * 1024):
                        second_digest.update(chunk)
                after_stat = resolved.stat()
                if (
                    after_stat.st_size != expected_size
                    or after_stat.st_dev != before_stat.st_dev
                    or after_stat.st_ino != before_stat.st_ino
                    or after_stat.st_mtime_ns != before_stat.st_mtime_ns
                    or after_stat.st_ctime_ns != before_stat.st_ctime_ns
                ):
                    continue
                expected_digest = row["sha256"]
                if first_digest.hexdigest() != expected_digest or second_digest.hexdigest() != expected_digest:
                    continue
            except (OSError, TypeError, ValueError):
                continue
            out.append(
                {
                    "logical_name": row["logical_name"],
                    "size_bytes": expected_size,
                    "sha256": row["sha256"],
                    "media_type": row["media_type"],
                    "active_content": bool(row["active_content"]),
                    "attempt_id": row["attempt_id"],
                    "dispatch_id": row["dispatch_id"],
                    "provenance": "coordinator_verified_artifact",
                }
            )
        return out
