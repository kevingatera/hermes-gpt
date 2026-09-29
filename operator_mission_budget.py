"""Stable imports for mission budget policy, storage, and MCP actions.

Policy validation and envelope evaluation live in the policy module; SQLite
accounts and audit helpers live in the store; gated actions live in tools.
Hard-block enforcement remains in the separate breaker module and is off by
default. Existing callers keep the same functions, constants, and helpers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_mission_budget_breaker as budget_breaker
import operator_mission_runtime as mission
import operator_policy as op
from operator_mission_budget_breaker import (
    BUDGET_HARD_BLOCK_ENV,
    ENFORCE_ALREADY_ENFORCED,
    ENFORCE_CONFIRM_REQUIRED,
    ENFORCE_DISABLED,
    ENFORCE_ENFORCED,
    ENFORCE_GATE_OFF,
    ENFORCE_NOT_CROSSING,
    ENFORCE_NOT_DIRECT,
    ENFORCE_NOT_ENABLED,
    ENFORCE_NOT_PAUSABLE,
    ENFORCE_POLICY_CHANGED,
    EVENT_TYPE_BREAK,
    BudgetSpoolFailure,
    _break_reason_sha,
    _breaker_envelope,
    _latest_break_row,
    _mission_pausable,
    enforce_budget_breaker,
)

# Retain established imports while policy, storage and tool actions stay separate.
# The breaker imports this facade lazily during calls, after initialization.
from operator_mission_budget_policy import (
    ACCOUNT_SCHEMA,
    AUTH_CLASSES,
    DEFAULT_AUTH_CLASS,
    DEFAULT_UNIT,
    EVENT_SCHEMA,
    MAX_AMOUNT,
    MAX_QUOTA,
    MAX_REASON,
    MAX_REF,
    MAX_SIGNAL,
    MAX_SPEND,
    MISSION_ID_RE,
    POLICY_SCHEMA,
    REF_RE,
    SCHEMA_VERSION,
    SHA_RE,
    SIGNAL_RE,
    STATUS_CROSSING,
    STATUS_INVALID,
    STATUS_WITHIN,
    UNIT_RE,
    UNITS,
    _account_sha256,
    _clean_num,
    _clean_policy,
    _clean_text,
    _envelope_status,
    _would_block,
    validate_envelope,
    within_envelope,
)
from operator_mission_budget_store import (
    _account_table_exists,
    _account_view,
    _audit,
    _begin_write,
    _connect,
    _db_path,
    _error,
    _get_account_row,
    _init_budget_tables,
    _now,
    _read_account,
)
from operator_mission_budget_tools import (
    hermes_budget_check,
    hermes_budget_get,
    hermes_budget_record,
    hermes_budget_set,
)

__all__ = (
    "ACCOUNT_SCHEMA",
    "AUTH_CLASSES",
    "BUDGET_HARD_BLOCK_ENV",
    "DEFAULT_AUTH_CLASS",
    "DEFAULT_UNIT",
    "ENFORCE_ALREADY_ENFORCED",
    "ENFORCE_CONFIRM_REQUIRED",
    "ENFORCE_DISABLED",
    "ENFORCE_ENFORCED",
    "ENFORCE_GATE_OFF",
    "ENFORCE_NOT_CROSSING",
    "ENFORCE_NOT_DIRECT",
    "ENFORCE_NOT_ENABLED",
    "ENFORCE_NOT_PAUSABLE",
    "ENFORCE_POLICY_CHANGED",
    "EVENT_SCHEMA",
    "EVENT_TYPE_BREAK",
    "MAX_AMOUNT",
    "MAX_QUOTA",
    "MAX_REASON",
    "MAX_REF",
    "MAX_SIGNAL",
    "MAX_SPEND",
    "MISSION_ID_RE",
    "POLICY_SCHEMA",
    "REF_RE",
    "SCHEMA_VERSION",
    "SHA_RE",
    "SIGNAL_RE",
    "STATUS_CROSSING",
    "STATUS_INVALID",
    "STATUS_WITHIN",
    "UNITS",
    "UNIT_RE",
    "Any",
    "BudgetSpoolFailure",
    "Path",
    "_account_sha256",
    "_account_table_exists",
    "_account_view",
    "_audit",
    "_begin_write",
    "_break_reason_sha",
    "_breaker_envelope",
    "_clean_num",
    "_clean_policy",
    "_clean_text",
    "_connect",
    "_db_path",
    "_envelope_status",
    "_error",
    "_get_account_row",
    "_init_budget_tables",
    "_latest_break_row",
    "_mission_pausable",
    "_now",
    "_read_account",
    "_would_block",
    "budget_breaker",
    "datetime",
    "enforce_budget_breaker",
    "hashlib",
    "hermes_budget_check",
    "hermes_budget_get",
    "hermes_budget_record",
    "hermes_budget_set",
    "json",
    "math",
    "mission",
    "op",
    "re",
    "sqlite3",
    "timezone",
    "validate_envelope",
    "within_envelope",
)
