"""Execute the gated Mission budget breaker action set."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import operator_mission_runtime as mission
import operator_policy as op

# v0.12 slice-2 (Pack A): budget D3 hard-block enforcement (Phase 4/5).
# Global machine gate for the enforcement executor — process-wide capability
# switch, DEFAULT OFF. When off, every surface in this module behaves
# byte-identically to the Phase 2 dry-run (no pause, no signal, no writes).
BUDGET_HARD_BLOCK_ENV = "HERMES_GPT_BUDGET_HARD_BLOCK"

# The only new budget_events event_type value (design §4: no new tables).
EVENT_TYPE_BREAK = "break"

# Stable refusal reason codes for the enforcement path (design §2.1/§5).
ENFORCE_NOT_CROSSING = "not_crossing"
ENFORCE_DISABLED = "disabled"
ENFORCE_CONFIRM_REQUIRED = "confirm_required"
ENFORCE_GATE_OFF = "hard_block_gate_off"
ENFORCE_NOT_ENABLED = "not_enabled"
ENFORCE_NOT_DIRECT = "not_direct"
ENFORCE_POLICY_CHANGED = "policy_changed"
ENFORCE_NOT_PAUSABLE = "not_pausable"
ENFORCE_ALREADY_ENFORCED = "already_enforced"
ENFORCE_ENFORCED = "enforced"


class BudgetSpoolFailure(RuntimeError):
    """INV-10 loud failure: the breaker signal could not be spooled.

    Deliberately NOT in the error-envelope conversion tuple of the enforcement
    path — a lost human-gate signal must escape the tool as a raised exception
    instead of being flattened into a JSON error envelope.
    """


# ---------------------------------------------------------------------------
# v0.12 slice-2 (Pack A): budget D3 hard-block ENFORCEMENT (Phase 4/5)
# ---------------------------------------------------------------------------


def _breaker_envelope(
    *,
    mission_id: str,
    enforced: bool,
    reason: str,
    would_pause: bool,
    policy: dict[str, Any] | None,
    spend_after: float | None,
    need_attention: bool = False,
    transition: dict[str, Any] | None = None,
    spooled: bool = False,
) -> dict[str, Any]:
    """Bounded result envelope for the enforcement path (INV-9: ids/enums only)."""
    return {
        "success": True,
        "mission_id": mission_id,
        "enforced": enforced,
        "reason": reason,
        "need_attention": need_attention,
        "would_pause": would_pause,
        "breaker_signal": (policy or {}).get("breaker_signal", "budget_breaker"),
        "spend_after": spend_after,
        "transition": transition,
        "spooled": spooled,
    }


def _latest_break_row(db: sqlite3.Connection, mission_id: str) -> sqlite3.Row | None:
    row = db.execute(
        "SELECT seq, mission_id, spend_after, quota, status, hard_block, "
        "event_type, ref, reason_sha256, created_at FROM budget_events "
        "WHERE mission_id=? AND event_type=? ORDER BY seq DESC LIMIT 1",
        (mission_id, EVENT_TYPE_BREAK),
    ).fetchone()
    return row


def _mission_pausable(status: str) -> bool:
    """A mission is pausable iff ``paused`` is a legal transition from status."""
    return "paused" in mission.MISSION_TRANSITIONS.get(status, set())


def _break_reason_sha(policy: dict[str, Any]) -> str:
    """Bounded INV-9 ref for the break row: sha over the signal + unit enums."""
    skeleton = {
        "signal": policy["breaker_signal"],
        "unit": policy["unit"],
    }
    enc = json.dumps(skeleton, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(enc.encode("utf-8")).hexdigest()


def enforce_budget_breaker(
    mission_id: str, *, hermes_root: Path | None = None, confirm: bool = False
) -> str:
    """Execute the D3 hard-block action set for a crossed envelope (Phase 4/5).

    Design: ``docs/design/v0.12-budget-enforcement.md`` §2.1. Gate set (ALL
    required; evaluated fresh in order — first failure returns its stable
    reason code, NO writes, NO partial action):

    1. ``confirm=True`` (per-call confirmation; dry-run-first convention).
    2. ``HERMES_GPT_BUDGET_HARD_BLOCK=1`` (machine gate, default off).
    3. Operator enabled + ``apply_mode == "direct"`` (live policy snapshot).
    4. Per-mission policy: ``hard_block_enabled`` and ``pause_on_cross``.

    On pass, acts atomically from the caller's perspective: INV-11
    anti-TOCTOU re-snapshot of the operator policy → pause via the existing
    ``hermes_mission_transition`` (reason ``budget_breaker``) → ONE
    fleet-attention INTERRUPT envelope through the existing controller spool
    → one ``budget_events`` ``break`` row (bounded fields only, INV-9).
    Idempotent: an already-paused, already-broken mission returns
    ``already_enforced`` with no second ``break`` row.
    """
    # Import at call time to share budget DB helpers without a startup import cycle.
    import operator_mission_budget as budget

    policy = op.OperatorPolicy()
    try:
        if not budget.MISSION_ID_RE.fullmatch(mission_id):
            raise ValueError("mission_id is invalid")
        path = budget._db_path(hermes_root)
        if not path.is_file():
            raise LookupError("missions database not found; no budget account available")

        # ---- read-only evaluation (no gates needed to look) ----
        with budget._connect(path, write=False) as db:
            if not budget._account_table_exists(db):
                raise LookupError(f"budget account {mission_id!r} not found")
            account = budget._get_account_row(db, mission_id)
            spend = float(account["spend"])
            quota = float(account["quota"])
            unit = str(account["unit"])
            policy_obj = json.loads(account["policy_json"])
            env = budget._envelope_status(spend, quota, unit)
            try:
                mrow = mission._get_row(db, mission_id)
                m_status = str(mrow["status"])
            except LookupError:
                m_status = ""
            latest_break = _latest_break_row(db, mission_id)

        # Gate 0: the crossing edge is the only trigger (INV-8). No crossing →
        # not_crossing; crossing but the per-mission policy flags are off →
        # disabled (design §2.1 bullet 1: reason not_crossing|disabled).
        if not env["crosses_envelope"]:
            reason = ENFORCE_NOT_CROSSING
        elif not (policy_obj.get("hard_block_enabled") and policy_obj.get("pause_on_cross")):
            reason = ENFORCE_DISABLED
        else:
            reason = ""
        if reason:
            out = _breaker_envelope(
                mission_id=mission_id,
                enforced=False,
                reason=reason,
                would_pause=False,
                policy=policy_obj,
                spend_after=spend,
            )
            budget._audit(
                "enforce_budget_breaker",
                policy,
                dry_run=True,
                success=True,
                changed=False,
                mission_id=mission_id,
                extra={"reason": reason, "status": env["status"]},
            )
            return json.dumps(out)

        # Idempotency (design §2.1): an already-broken mission appends no
        # second break row and spools no second envelope. "Already broken" =
        # a break row exists AND the mission is paused (the enforced outcome)
        # or still not pausable (the fail-closed outcome — design §5 keeps
        # one spool entry max).
        if latest_break is not None and (
            m_status == "paused" or not _mission_pausable(m_status)
        ):
            out = _breaker_envelope(
                mission_id=mission_id,
                enforced=False,
                reason=ENFORCE_ALREADY_ENFORCED,
                would_pause=True,
                policy=policy_obj,
                spend_after=spend,
            )
            budget._audit(
                "enforce_budget_breaker",
                policy,
                dry_run=True,
                success=True,
                changed=False,
                mission_id=mission_id,
                extra={"reason": ENFORCE_ALREADY_ENFORCED},
            )
            return json.dumps(out)

        # ---- full gate set, evaluated fresh, in order (design §2.1) ----
        def _refuse(reason: str, *, would_pause: bool = True) -> str:
            budget._audit(
                "enforce_budget_breaker",
                policy,
                dry_run=True,
                success=True,
                changed=False,
                mission_id=mission_id,
                extra={"reason": reason},
            )
            return json.dumps(
                _breaker_envelope(
                    mission_id=mission_id,
                    enforced=False,
                    reason=reason,
                    would_pause=would_pause,
                    policy=policy_obj,
                    spend_after=spend,
                )
            )

        if not confirm:
            return _refuse(ENFORCE_CONFIRM_REQUIRED)
        if not op.env_truthy(BUDGET_HARD_BLOCK_ENV):
            return _refuse(ENFORCE_GATE_OFF)
        if not policy.enabled:
            return _refuse(ENFORCE_NOT_ENABLED)
        if policy.apply_mode != "direct":
            return _refuse(ENFORCE_NOT_DIRECT)
        # Per-mission policy gates (hard_block_enabled / pause_on_cross) are
        # already folded into gate 0 above; this is the belt-and-braces re-check.
        if not (policy_obj.get("hard_block_enabled") and policy_obj.get("pause_on_cross")):
            return _refuse(ENFORCE_DISABLED, would_pause=False)

        # ---- breaker action set (D3) ----
        # 1. INV-11 anti-TOCTOU: re-snapshot the operator policy immediately
        #    before acting; abort with zero writes if it changed.
        resnap = op.OperatorPolicy()
        if (
            resnap.enabled != policy.enabled
            or resnap.apply_mode != policy.apply_mode
            or resnap.level != policy.level
        ):
            out = _breaker_envelope(
                mission_id=mission_id,
                enforced=False,
                reason=ENFORCE_POLICY_CHANGED,
                would_pause=True,
                policy=policy_obj,
                spend_after=spend,
            )
            budget._audit(
                "enforce_budget_breaker",
                policy,
                dry_run=True,
                success=False,
                changed=False,
                mission_id=mission_id,
                extra={"reason": ENFORCE_POLICY_CHANGED},
            )
            return json.dumps(out)

        pausable = _mission_pausable(m_status)
        transition: dict[str, Any] | None = None
        need_attention = False
        if pausable:
            # 2. Pause via the existing lifecycle transition (reason MUST be
            #    "budget_breaker" in the transition audit).
            t_raw = mission.hermes_mission_transition(
                mission_id,
                "paused",
                reason="budget_breaker",
                confirm=True,
                dry_run=False,
                hermes_root=hermes_root,
            )
            t_out = json.loads(t_raw)
            if not t_out.get("success"):
                # Pause refused despite a pausable state (e.g. concurrent
                # status change) — fail closed without dispatching anything.
                need_attention = True
            else:
                transition = {
                    "applied": bool(t_out.get("changed", True)),
                    "from": t_out.get("from_status", ""),
                    "to": t_out.get("to_status", ""),
                }
        else:
            # Not pausable (terminal / awaiting_approval): fail closed — no
            # dispatch, mark need_attention.
            need_attention = True

        # 3. Signal: ONE fleet-attention INTERRUPT envelope through the
        #    existing controller spool (never self-send). A spool failure
        #    raises BudgetSpoolFailure (INV-10 loud) — it is deliberately not
        #    converted to a JSON error envelope. proposed_action mirrors the
        #    outcome: "pause_mission" after a successful pause, "attention" in
        #    the fail-closed path.
        import operator_controller as controller  # local: avoid import cycle

        envelope = controller.build_attention_envelope(
            mission_id=mission_id,
            node_id="",
            classification="budget_crossing",
            row_key="budget_breaker",
            proposed_action="pause_mission" if transition else "attention",
            reasons=[ENFORCE_NOT_PAUSABLE] if need_attention else [],
            uncertainty="",
            tier_reasons=["budget_crossing"],
            pass_seq=0,
        )
        envelope["dedupe_key"] = f"budget_breaker:{mission_id}"[:300]
        try:
            controller.spool_attention_envelope(envelope, hermes_root)
        except OSError as exc:
            raise BudgetSpoolFailure(
                f"budget breaker signal could not be spooled for {mission_id}"
            ) from exc

        # 4. Record: one budget_events "break" row (bounded fields only).
        now = budget._now()
        with budget._connect(path, write=True) as db:
            budget._begin_write(db)
            db.execute(
                "INSERT INTO budget_events(mission_id,amount,spend_after,quota,status,hard_block,event_type,ref,reason_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    mission_id,
                    0.0,
                    spend,
                    quota,
                    env["status"],
                    int(bool(policy_obj["hard_block_enabled"])),
                    EVENT_TYPE_BREAK,
                    "budget_breaker",
                    _break_reason_sha(policy_obj),
                    now,
                ),
            )
            db.commit()

        out = _breaker_envelope(
            mission_id=mission_id,
            enforced=transition is not None,
            reason=ENFORCE_ENFORCED if transition else ENFORCE_NOT_PAUSABLE,
            would_pause=True,
            policy=policy_obj,
            spend_after=spend,
            need_attention=need_attention,
            transition=transition,
            spooled=True,
        )
        budget._audit(
            "enforce_budget_breaker",
            policy,
            dry_run=False,
            success=True,
            changed=bool(transition),
            mission_id=mission_id,
            extra={
                "reason": out["reason"],
                "need_attention": need_attention,
                "spend_after": spend,
            },
        )
        return json.dumps(out)
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        budget._audit(
            "enforce_budget_breaker",
            policy,
            dry_run=True,
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return budget._error(
            exc,
            "BUDGET_ENFORCE_REJECTED",
            "Check mission id, envelope state, and the budget hard-block gate set.",
        )
