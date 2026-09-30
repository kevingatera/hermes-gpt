"""Build and spool bounded controller attention envelopes."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from hermes_gpt.missions import failure_semantics as fs
from hermes_gpt.missions.controller_decisions import TIER_RED
from hermes_gpt.missions.controller_health import CONTROLLER_MODE
from hermes_gpt.missions.controller_store import _now, _root, _sanitize

def _attention_spool_path(hermes_root: Path | None) -> Path:
    return _root(hermes_root) / "missions" / "controller_attention_spool.jsonl"


def build_attention_envelope(
    *,
    mission_id: str,
    node_id: str,
    classification: str,
    row_key: str,
    proposed_action: str,
    reasons: list[str],
    uncertainty: str,
    tier_reasons: list[str],
    pass_seq: int,
) -> dict[str, Any]:
    """Build one fleet-attention INTERRUPT envelope for a RED pass (§12.2).

    Schema: fleet-attention-envelope/v1 (ops delivery contract). INV-9: only
    bounded enums, ids, and fixed matrix strings — never raw objective/error/
    secret text. Delivery is NOT performed here: the envelope is spooled for
    the existing delivery-broker lane (no self-send from inside the loop).
    """
    approval_wall = row_key == "park_authority"
    summary = (
        f"Supervised mission controller (shadow/observe) classified a pass as "
        f"RED — a human gate is genuinely required. mission={mission_id} "
        f"node={node_id or '-'} classification={classification} "
        f"row_key={row_key} proposed_action={_sanitize(proposed_action, 200)} "
        f"reasons={','.join(tier_reasons[:4])}"
    )
    if uncertainty:
        summary += f" uncertainty={_sanitize(uncertainty, 64)}"
    return {
        "schema_version": 1,
        "event_id": f"ctl-{uuid.uuid4().hex[:24]}",
        "source": {
            "kind": "system",
            "profile": "ops",
            "job_id": "hermes-gpt-controller",
            "job_name": "supervised-mission-controller (shadow)",
            "run_id": f"pass:{pass_seq}",
        },
        "domain": "operations",
        "severity": "P1",
        "attention_class": "INTERRUPT",
        "state": "open",
        "action_required": True,
        "approval_required": approval_wall,
        "title": f"Mission controller RED: {row_key} ({mission_id})"[:200],
        "summary": summary[:4000],
        "dedupe_key": f"controller:red:{mission_id}:{row_key}"[:300],
        "occurred_at": _now(),
        "evidence": [
            f"missions/missions.db#controller_telemetry(mission_id={mission_id})",
            f"missions/missions.db#controller_plan(mission_id={mission_id})",
            "missions/controller_heartbeat.json",
        ][:32],
        "metadata": {
            "tier": TIER_RED,
            "mode": CONTROLLER_MODE,
            "would_execute": False,
            "classification": classification,
            "row_key": row_key,
            "classification_uncertainty": _sanitize(uncertainty, 64),
            "escalation_reasons": [
                _sanitize(r, 64) for r in (*tier_reasons, *reasons[:2])
            ][:8],
            "proposed_action": _sanitize(proposed_action, 200),
        },
    }


def spool_attention_envelope(
    envelope: dict[str, Any], hermes_root: Path | None = None
) -> Path:
    """Append one RED envelope to the controller attention spool.

    INV-10 (loud as permitted): a spool failure raises — the pass fails loudly
    rather than silently dropping a human-gate signal. This never sends; the
    existing fleet-attention delivery lane owns transport.
    """
    path = _attention_spool_path(hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(envelope, sort_keys=True, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def attention_envelopes(hermes_root: Path | None = None) -> list[dict[str, Any]]:
    """Read-only: spooled RED envelopes awaiting broker flush (oldest first)."""
    path = _attention_spool_path(hermes_root)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _l2_escalate_no_target(
    *,
    mission_id: str,
    node_id: str,
    pass_seq: int,
    hermes_root: Path | None,
) -> None:
    """§3: no_capable_target escalates (spool INTERRUPT); never auto-resolve.

    Uses the existing RED attention helper; a spool failure raises (INV-10).
    """
    envelope = build_attention_envelope(
        mission_id=mission_id,
        node_id=node_id,
        classification=fs.CLASS_CAPABILITY,
        row_key="park_capability",
        proposed_action="escalate capability (placement no_capable_target)",
        reasons=["placement:no_capable_target"],
        uncertainty="",
        tier_reasons=["placement:no_capable_target"],
        pass_seq=pass_seq,
    )
    envelope["metadata"]["proposed_action"] = "escalate capability (placement no_capable_target)"
    envelope["dedupe_key"] = f"controller:l2-no-target:{mission_id}:{node_id}"[:300]
    spool_attention_envelope(envelope, hermes_root=hermes_root)
