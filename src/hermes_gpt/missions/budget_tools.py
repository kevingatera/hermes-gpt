"""Policy-gated MCP actions for mission budget accounts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from hermes_gpt.missions import runtime as mission
from hermes_gpt.policy import authorization as op
from hermes_gpt.missions.budget_breaker import enforce_budget_breaker
from hermes_gpt.missions.budget_policy import MAX_AMOUNT, MAX_QUOTA, MAX_REASON, MAX_REF, MAX_SPEND, MISSION_ID_RE, REF_RE, SCHEMA_VERSION, _account_sha256, _clean_num, _clean_policy, _clean_text, _envelope_status, _would_block
from hermes_gpt.missions.budget_store import _account_table_exists, _audit, _begin_write, _connect, _db_path, _error, _get_account_row, _now, _read_account


def hermes_budget_set(
    mission_id: str,
    quota: float,
    policy_json: str = "",
    *,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Create (or update) the mission-scoped spend envelope (budget_accounts).

    ``quota`` is the spend limit; ``policy_json`` carries unit, hard-block flag
    (default off), on-crossing behavior, and breaker signal. This writes only the
    budget store; it never pauses a Mission and never mutates its lifecycle.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if not MISSION_ID_RE.fullmatch(mission_id):
            raise ValueError("mission_id is invalid")
        quota_num = _clean_num(
            quota, field="quota", minimum=0.000001, maximum=MAX_QUOTA
        )
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct budget set requires confirm=true")

        parsed_policy = _clean_policy(
            json.loads(policy_json) if policy_json.strip() else {}
        )
        acct_sha = _account_sha256(quota_num, parsed_policy)

        if effective_dry:
            # Read current (if any) to report the delta; never write.
            view = None
            path = _db_path(hermes_root)
            if path.is_file():
                with _connect(path, write=False) as db:
                    try:
                        if _account_table_exists(db):
                            view = _read_account(db, mission_id)
                    except LookupError:
                        view = None
                    except sqlite3.OperationalError:
                        view = None
            reported = {"version": 0, **({"prior": view} if view else {})}
            _audit(
                "hermes_budget_set",
                policy,
                dry_run=True,
                success=True,
                changed=False,
                mission_id=mission_id,
                extra={"quota": quota_num, "unit": parsed_policy["unit"]},
            )
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "tool": "hermes_budget_set",
                    "mission_id": mission_id,
                    "quota": quota_num,
                    "unit": parsed_policy["unit"],
                    "policy": parsed_policy,
                    "account_sha256": acct_sha,
                    "changed": False,
                    "dry_run": True,
                    **reported,
                }
            )

        path = _db_path(hermes_root)
        with _connect(path, write=True) as db:
            _begin_write(db)
            mission._get_row(db, mission_id)  # verify mission exists
            now = _now()
            existing = db.execute(
                "SELECT quota, unit, policy_json FROM budget_accounts WHERE mission_id=?",
                (mission_id,),
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE budget_accounts SET quota=?, unit=?, policy_json=?, account_sha256=?, updated_at=? WHERE mission_id=?",
                    (
                        quota_num,
                        parsed_policy["unit"],
                        json.dumps(parsed_policy, sort_keys=True),
                        acct_sha,
                        now,
                        mission_id,
                    ),
                )
            else:
                db.execute(
                    "INSERT INTO budget_accounts(mission_id,quota,spend,unit,policy_json,account_sha256,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        mission_id,
                        quota_num,
                        0.0,
                        parsed_policy["unit"],
                        json.dumps(parsed_policy, sort_keys=True),
                        acct_sha,
                        now,
                        now,
                    ),
                )
            db.commit()
        _audit(
            "hermes_budget_set",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            extra={
                "quota": quota_num,
                "unit": parsed_policy["unit"],
                "updated_existing": bool(existing),
            },
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "tool": "hermes_budget_set",
                "mission_id": mission_id,
                "quota": quota_num,
                "unit": parsed_policy["unit"],
                "policy": parsed_policy,
                "account_sha256": acct_sha,
                "changed": True,
                "dry_run": False,
                "updated_existing": bool(existing),
            }
        )
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        _audit(
            "hermes_budget_set",
            policy,
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return _error(
            exc,
            "BUDGET_SET_REJECTED",
            "Check mission id, quota, policy schema, and Operator workspace/direct policy.",
        )


def hermes_budget_get(mission_id: str, hermes_root: Path | None = None) -> str:
    """Read the mission spend envelope (read-only)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "mission_id": mission_id,
                    "found": False,
                }
            )
        with _connect(path, write=False) as db:
            if not _account_table_exists(db):
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "mission_id": mission_id,
                        "found": False,
                    }
                )
            try:
                view = _read_account(db, mission_id)
            except LookupError:
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "mission_id": mission_id,
                        "found": False,
                    }
                )
        view["success"] = True
        view["tool"] = "hermes_budget_get"
        view["found"] = True
        _audit(
            "hermes_budget_get",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
        )
        return json.dumps(view)
    except FileNotFoundError:
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "mission_id": mission_id,
                "found": False,
            }
        )
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc, "BUDGET_READ_FAILED", "Check mission id and Operator read access."
        )


def hermes_budget_check(
    mission_id: str,
    hermes_root: Path | None = None,
    *,
    enforce: bool = False,
    confirm: bool = False,
) -> str:
    """The ``budget_check`` surface: evaluate the envelope against spend.

    Read-only w.r.t. the store. Reports whether spend is within or crossing the
    envelope and the would-be D3 behavior (pause + ``budget_breaker`` signal),
    with the note that enforcement is Phase 4/5 and is never executed here.

    v0.12 slice-2: ``enforce=False`` (default) is byte-identical to the Phase 2
    surface. ``enforce=True`` evaluates the full enforcement gate set through
    :func:`enforce_budget_breaker` (which writes only when EVERY gate passes —
    confirm, ``HERMES_GPT_BUDGET_HARD_BLOCK``, Operator enabled+direct, and the
    per-mission policy flags) and attaches an ``enforcement`` sub-object
    mirroring ``_would_block`` plus the enforcement outcome. Any missing gate →
    ``enforcement.enforced == false`` with a stable reason and zero writes, so
    ``enforce=True`` without ``confirm=True`` is a preview.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "mission_id": mission_id,
                    "found": False,
                }
            )
        with _connect(path, write=False) as db:
            if not _account_table_exists(db):
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "mission_id": mission_id,
                        "found": False,
                    }
                )
            try:
                view = _read_account(db, mission_id)
            except LookupError:
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "mission_id": mission_id,
                        "found": False,
                    }
                )
        env = view["envelope"]
        view["tool"] = "hermes_budget_check"
        view["found"] = True
        view["envelope_status"] = env["status"]
        view["crosses_envelope"] = env["crosses_envelope"]
        view["block"] = _would_block(env, view["policy"])
        view["success"] = True
        if enforce:
            # v0.12 slice-2: evaluate (and only on a full gate pass, execute)
            # the breaker action set (D3). enforce_budget_breaker returns its own
            # bounded envelope; attach it under "enforcement".
            enforcement_raw = enforce_budget_breaker(
                mission_id, hermes_root=hermes_root, confirm=confirm
            )
            view["enforcement"] = json.loads(enforcement_raw)
        _audit(
            "hermes_budget_check",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
            extra={"status": env["status"], "crosses": env["crosses_envelope"]},
        )
        return json.dumps(view)
    except FileNotFoundError:
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "mission_id": mission_id,
                "found": False,
            }
        )
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc, "BUDGET_CHECK_FAILED", "Check mission id and Operator read access."
        )


def hermes_budget_record(
    mission_id: str,
    amount: float,
    ref: str = "",
    reason: str = "",
    *,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Record a spend increment against a mission envelope (dry-run enforcement).

    Adds ``amount`` to the account ``spend`` and appends a ``budget_events`` row.
    Per D3, when the envelope crosses and ``hard_block_enabled``, the designed
    action is pause + ``budget_breaker`` signal — but Phase 2 is dry-run and
    **never executes pause or raises a signal**; it only reports ``would_pause``.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if not MISSION_ID_RE.fullmatch(mission_id):
            raise ValueError("mission_id is invalid")
        amount_num = _clean_num(amount, field="amount", minimum=0.0, maximum=MAX_AMOUNT)
        ref_clean = _clean_text(ref, field="ref", maximum=MAX_REF) if ref else ""
        if ref_clean and not REF_RE.fullmatch(ref_clean):
            raise ValueError("budget record ref contains unsupported characters")
        reason_clean = (
            _clean_text(reason, field="reason", maximum=MAX_REASON) if reason else ""
        )
        reason_sha = (
            hashlib.sha256(reason_clean.encode("utf-8")).hexdigest()
            if reason_clean
            else ""
        )
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct budget record requires confirm=true")

        path = _db_path(hermes_root)
        if not path.is_file():
            raise LookupError(
                "missions database not found; no budget account available"
            )

        with _connect(path, write=not effective_dry) as db:
            if not effective_dry:
                _begin_write(db)
            account = _get_account_row(db, mission_id)  # raises if not found
            spend_before = float(account["spend"])
            quota = float(account["quota"])
            unit = account["unit"]
            policy_obj = json.loads(account["policy_json"])
            spend_after = spend_before + amount_num
            if spend_after < 0 or spend_after > MAX_SPEND:
                raise ValueError("spend after record is out of range")
            env_after = _envelope_status(spend_after, quota, unit)
            block = _would_block(env_after, policy_obj)

            if effective_dry:
                db.rollback()
                _audit(
                    "hermes_budget_record",
                    policy,
                    dry_run=True,
                    success=True,
                    changed=False,
                    mission_id=mission_id,
                    extra={
                        "amount": amount_num,
                        "would_cross": env_after["crosses_envelope"],
                    },
                )
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "tool": "hermes_budget_record",
                        "mission_id": mission_id,
                        "amount": amount_num,
                        "spend_before": spend_before,
                        "spend_after": spend_after,
                        "quota": quota,
                        "status": env_after["status"],
                        "crosses_envelope": env_after["crosses_envelope"],
                        "block": block,
                        "changed": False,
                        "dry_run": True,
                        "enforcement": "not_executed_phase_2",
                    }
                )

            now = _now()
            db.execute(
                "UPDATE budget_accounts SET spend=?, updated_at=? WHERE mission_id=?",
                (spend_after, now, mission_id),
            )
            db.execute(
                "INSERT INTO budget_events(mission_id,amount,spend_after,quota,status,hard_block,event_type,ref,reason_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    mission_id,
                    amount_num,
                    spend_after,
                    quota,
                    env_after["status"],
                    int(policy_obj["hard_block_enabled"]),
                    "spend_record",
                    ref_clean,
                    reason_sha,
                    now,
                ),
            )
            db.commit()
        _audit(
            "hermes_budget_record",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            extra={
                "amount": amount_num,
                "spend_after": spend_after,
                "crosses": env_after["crosses_envelope"],
                "would_pause": block["would_pause"],
            },
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "tool": "hermes_budget_record",
                "mission_id": mission_id,
                "amount": amount_num,
                "spend_before": spend_before,
                "spend_after": spend_after,
                "quota": quota,
                "status": env_after["status"],
                "crosses_envelope": env_after["crosses_envelope"],
                "block": block,
                "changed": True,
                "dry_run": False,
                "enforcement": "not_executed_phase_2",
                "reason_sha256": reason_sha,
            }
        )
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
    ) as exc:
        _audit(
            "hermes_budget_record",
            policy,
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return _error(
            exc,
            "BUDGET_RECORD_REJECTED",
            "Check mission id, amount, ref, and Operator workspace/direct policy.",
        )
