"""Bounded budget policy validation and envelope evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from hermes_gpt.missions import runtime as mission
from hermes_gpt.policy import authorization as op

SCHEMA_VERSION = "0.9-budget.1"
POLICY_SCHEMA = "hermes.budget-policy/v1"
ACCOUNT_SCHEMA = "hermes.budget-account/v1"
EVENT_SCHEMA = "hermes.budget-event/v1"
MISSION_ID_RE = mission.MISSION_ID_RE
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
UNIT_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
SIGNAL_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
UNITS = ("tokens", "minutes", "usd")
DEFAULT_UNIT = "tokens"
AUTH_CLASSES = ("read_only", "reversible_write", "high_impact")
DEFAULT_AUTH_CLASS = "reversible_write"
MAX_QUOTA = 1_000_000_000_000.0
MAX_SPEND = 1_000_000_000_000.0
MAX_AMOUNT = 1_000_000_000_000.0
MAX_REF = 256
MAX_REASON = 200
MAX_SIGNAL = 64
STATUS_WITHIN = "within"
STATUS_CROSSING = "crossing"
STATUS_INVALID = "invalid"


def _clean_text(value: Any, *, field: str, maximum: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


def _clean_num(value: Any, *, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or value is None:
        raise TypeError(f"{field} must be a number")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise TypeError(f"{field} must be a number") from None
    if not math.isfinite(num):
        raise ValueError(f"{field} must be finite")
    if num < minimum or num > maximum:
        raise ValueError(f"{field} out of range ({minimum}..{maximum})")
    return num


def _clean_policy(raw: Any) -> dict[str, Any]:
    """Validate + canonicalize a budget policy document (INV-9 checked)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TypeError("policy must be a JSON object")
    schema = str(raw.get("schema", POLICY_SCHEMA)).strip()
    if schema != POLICY_SCHEMA:
        raise ValueError(f"budget policy schema must be {POLICY_SCHEMA!r}")

    unit = str(raw.get("unit", DEFAULT_UNIT)).strip().lower()
    if unit not in UNITS:
        raise ValueError(f"budget policy unit must be one of {list(UNITS)}")

    hard_block = raw.get("hard_block_enabled", False)
    if not isinstance(hard_block, bool):
        raise TypeError("budget policy hard_block_enabled must be boolean")
    if hard_block:
        # Phase 2: the hard-stop semantics are designed (D3) but wired in Phase
        # 4/5. Tempting to reject enabling, but the card says "implemented behind
        # a flag, default off" — so we allow it on but never enforce here.
        pass

    pause_on_cross = raw.get("pause_on_cross", True)
    if not isinstance(pause_on_cross, bool):
        raise TypeError("budget policy pause_on_cross must be boolean")

    signal = str(raw.get("breaker_signal", "budget_breaker")).strip().lower()
    if not SIGNAL_RE.fullmatch(signal):
        raise ValueError(
            f"budget policy breaker_signal must match {SIGNAL_RE.pattern!r}"
        )

    auth_class = str(raw.get("authorization_class", DEFAULT_AUTH_CLASS)).strip().lower()
    if auth_class not in AUTH_CLASSES:
        raise ValueError(
            f"budget policy authorization_class must be one of {list(AUTH_CLASSES)}"
        )

    canonical = {
        "schema": POLICY_SCHEMA,
        "unit": unit,
        "hard_block_enabled": bool(hard_block),
        "pause_on_cross": bool(pause_on_cross),
        "breaker_signal": signal,
        "authorization_class": auth_class,
    }
    # INV-9: never allow a secret-like policy into a durable store.
    if op.redact_output(json.dumps(canonical)) != json.dumps(canonical):
        raise PermissionError("budget policy contains secret-like durable values")
    return canonical


def _account_sha256(quota: float, policy: dict[str, Any]) -> str:
    skeleton = {"quota": quota, "policy": policy}
    enc = json.dumps(
        skeleton, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(enc.encode("utf-8")).hexdigest()


def _envelope_status(spend: float, quota: float, unit: str) -> dict[str, Any]:
    """Evaluate spend vs quota. INV-8: within is un-gated, crossing is the edge."""
    if quota <= 0 or not math.isfinite(spend) or not math.isfinite(quota):
        return {
            "unit": unit,
            "spend": spend,
            "quota": quota,
            "status": STATUS_INVALID,
            "within_envelope": False,
            "crosses_envelope": False,
            "consumed": 0.0,
        }
    consumed = spend / quota
    crosses = spend >= quota
    return {
        "unit": unit,
        "spend": spend,
        "quota": quota,
        "status": STATUS_CROSSING if crosses else STATUS_WITHIN,
        "within_envelope": not crosses,
        "crosses_envelope": crosses,
        "consumed": round(consumed, 6),
        "utilization_percent": round(consumed * 100.0, 4),
        "remaining": round(max(0.0, quota - spend), 6),
    }


def _would_block(env: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """D3 on-crossing behavior: designed now; enforcement is Phase 4/5."""
    crossing = env.get("crosses_envelope", False)
    hb = bool(policy["hard_block_enabled"])
    pause = bool(policy["pause_on_cross"])
    return {
        "hard_block_enabled": hb,
        "pause_on_cross": pause,
        "breaker_signal": policy["breaker_signal"],
        # Would the designed D3 hard-stop fire? (Enforcement NOT executed here.)
        "would_pause": crossing and hb and pause,
        "crosses_envelope": crossing,
        # Phase 2 is always dry-run w.r.t. the Mission lifecycle.
        "enforced_now": False,
        "enforcement_phase": "phase_4_5",
    }


def validate_envelope(policy: dict[str, Any], spend: float, quota: float) -> bool:
    """Return whether a (policy, spend, quota) tuple is a valid INV-8 envelope."""
    try:
        canonical = _clean_policy(policy)
        quota_num = _clean_num(
            quota, field="quota", minimum=0.000001, maximum=MAX_QUOTA
        )
        spend_num = _clean_num(spend, field="spend", minimum=0.0, maximum=MAX_SPEND)
    except (ValueError, TypeError):
        return False
    return quota_num > 0 and spend_num >= 0 and canonical["unit"] in UNITS


def within_envelope(policy: dict[str, Any], spend: float, quota: float) -> bool:
    """INV-8 predicate: True when spend is strictly inside the envelope."""
    if not validate_envelope(policy, spend, quota):
        return False
    return float(spend) < float(quota)
