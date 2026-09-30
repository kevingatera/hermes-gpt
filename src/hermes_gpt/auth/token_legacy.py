"""Migration support for token files written before the SQLite store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from hermes_gpt.auth.token_crypto import ENVELOPE_FILENAME, TokenStoreError, _encrypt_record, _secrets_dir, _token_key, decrypt_envelope, load_envelope

LEGACY_ENVELOPE_FILENAME = ENVELOPE_FILENAME

LEGACY_EPOCH_FILENAME = "hermes_gpt_token_epoch"

LEGACY_LEDGER_FILENAME = "hermes_gpt_token_ledger"

_SQLITE_MAX_INT = 2**63 - 1


def _legacy_envelope_path(hermes_root: Path) -> Path:
    return _secrets_dir(hermes_root) / LEGACY_ENVELOPE_FILENAME


def _close_legacy_migration_locked(
    db: sqlite3.Connection,
    hermes_root: Path,
    ledger_path: Path,
    epoch_path: Path,
    now: float,
    reason: str,
) -> None:
    """Close legacy migration while preserving every durable fence.

    Imports retirement tombstones from the ledger and preserves the
    revocation epoch (ledger-authoritative, fail-closed) before writing the
    close marker — used by the envelope-absent and envelope-corrupt paths
    alike, so no close branch can erase retirement history.
    """
    tombstones = _parse_legacy_retired(hermes_root, ledger_path)
    for ledger_key in tombstones:
        exists = db.execute(
            "SELECT 1 FROM tokens WHERE token_key=?", (ledger_key,)
        ).fetchone()
        if not exists:
            db.execute(
                "INSERT INTO tokens(token_key,kind,nonce,ciphertext,expires_at,retired,retired_at) VALUES(?,?,?,?,?,1,?)",
                (ledger_key, "retired", b"", b"", 0.0, now),
            )
    legacy_epoch = _legacy_epoch_from_ledger_or_file(
        hermes_root, ledger_path, epoch_path
    )
    have_epoch = db.execute(
        "SELECT 1 FROM token_meta WHERE name='revocation_epoch'"
    ).fetchone()
    if not have_epoch and legacy_epoch > 0:
        db.execute(
            "INSERT OR REPLACE INTO token_meta(name,value) VALUES('revocation_epoch',?)",
            (str(legacy_epoch),),
        )
    db.execute(
        "INSERT OR REPLACE INTO token_meta(name,value) VALUES('legacy_migration',?)",
        (f"closed:{reason}",),
    )


def _legacy_epoch_from_ledger_or_file(
    hermes_root: Path, ledger_path: Path, epoch_path: Path
) -> int:
    """Revocation epoch from the legacy ledger (validated) or epoch file.

    The ledger's value is authoritative when present and well-formed
    (including negatives/booleans being fail-closed to 1)."""
    if ledger_path.exists():
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TokenStoreError("legacy retirement ledger is corrupt") from exc
        if isinstance(data, dict):
            raw = data.get("revocation_epoch")
            if raw is None:
                return _read_legacy_epoch_locked(hermes_root, epoch_path)
            if isinstance(raw, bool) or not isinstance(raw, int):
                return 1  # malformed -> fail closed
            if raw < 0 or raw > _SQLITE_MAX_INT:
                return 1
            return raw
    return _read_legacy_epoch_locked(hermes_root, epoch_path)


def _parse_legacy_retired(hermes_root: Path, ledger_path: Path) -> list[str]:
    """Parse the legacy retirement ledger's tombstone keys (fail closed).

    An absent ledger means no tombstones. A present-but-corrupt ledger is a
    hard error (unknown retirement history must not silently import live).
    """
    if not ledger_path.exists():
        return []
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TokenStoreError("legacy retirement ledger is corrupt") from exc
    if not isinstance(data, dict):
        raise TokenStoreError("legacy retirement ledger is malformed")
    retired = data.get("retired")
    if retired is None:
        return []
    if not isinstance(retired, dict):
        raise TokenStoreError("legacy retirement ledger is malformed")
    return [str(k) for k in retired]


def _read_legacy_epoch_locked(hermes_root: Path, epoch_path: Path) -> int:
    """Read the legacy epoch file inside the migration transaction.

    Fail-closed on malformed data: an unparseable epoch means unknown
    revocation history, which is treated as at-least-once revoked (epoch 1)
    rather than never-revoked (epoch 0).
    """
    if not epoch_path.exists():
        return 0
    try:
        value = int(epoch_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 1
    if value < 0 or value > _SQLITE_MAX_INT:
        return 1
    return value


def _migrate_legacy_locked(
    db: sqlite3.Connection, hermes_root: Path, key: bytes, kid: str, now: float
) -> int:
    """One-time import of the legacy JSON envelope (and hash ledger) into the DB.

    Runs inside the caller's write transaction. Rules:

    - A durable ``legacy_migration`` marker closes migration permanently once
      set; revocation sets it too, so leftover legacy files can never
      re-import revoked credentials (fail closed).
    - A corrupt/unparseable legacy ledger is a hard error: the transaction
      aborts rather than importing credentials whose retirement history
      cannot be established.
    - Legacy artifacts are NOT deleted inside this transaction; cleanup
      happens only after the enclosing transaction commits (the caller
      schedules it), so a rollback never loses the recovery source.
    - Imported records carry the internal markers needed to reconstruct
      caches (``_kind``/``_token_value``) exactly like fresh records.
    """
    marker = db.execute(
        "SELECT value FROM token_meta WHERE name='legacy_migration'"
    ).fetchone()
    if marker is not None:
        return 0  # already migrated (or closed by revocation)
    env_path = _legacy_envelope_path(hermes_root)
    ledger_path = _secrets_dir(hermes_root) / LEGACY_LEDGER_FILENAME
    epoch_path = _secrets_dir(hermes_root) / LEGACY_EPOCH_FILENAME
    if not env_path.exists():
        # No envelope anywhere: close migration so later stray files cannot
        # be imported after revocation has happened. But FIRST import any
        # retirement tombstones from the ledger (a rotated token's hash may
        # exist ONLY there) and preserve the legacy revocation epoch — a
        # prior revocation deleted the envelope, yet both fences must
        # survive, or a stale peer could repersist pre-revocation
        # credentials.
        _close_legacy_migration_locked(
            db, hermes_root, ledger_path, epoch_path, now, "empty"
        )
        return 0
    try:
        envelope = load_envelope(hermes_root)
        bundle = decrypt_envelope(envelope, hermes_root) if envelope else {}
    except TokenStoreError:
        # Corrupt/undecryptable legacy store: close migration, keep files,
        # but still preserve tombstones + the revocation epoch.
        _close_legacy_migration_locked(
            db, hermes_root, ledger_path, epoch_path, now, "corrupt"
        )
        return 0
    if not isinstance(bundle, dict):
        _close_legacy_migration_locked(
            db, hermes_root, ledger_path, epoch_path, now, "corrupt"
        )
        return 0
    legacy_ledger: dict[str, Any] = {}
    ledger_corrupt = False
    if ledger_path.exists():
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                legacy_ledger = data
            else:
                ledger_corrupt = True
        except (OSError, ValueError):
            ledger_corrupt = True
    if ledger_corrupt:
        # Fail closed: retirement history cannot be established.
        raise TokenStoreError("legacy retirement ledger is corrupt; refusing to import")
    retired_keys = legacy_ledger.get("retired")
    if retired_keys is None:
        retired = {}
    elif not isinstance(retired_keys, dict):
        # Parsed but structurally invalid: retirement history cannot be
        # established. Fail closed instead of importing everything live.
        raise TokenStoreError(
            "legacy retirement ledger is malformed; refusing to import"
        )
    else:
        retired = retired_keys
    legacy_epoch_raw = legacy_ledger.get("revocation_epoch")
    if legacy_epoch_raw is not None and (
        isinstance(legacy_epoch_raw, bool) or not isinstance(legacy_epoch_raw, int)
    ):
        raise TokenStoreError(
            "legacy retirement ledger is malformed; refusing to import"
        )
    migrated = 0
    # Import EVERY legacy retired hash as a permanent tombstone FIRST — a
    # rotated/revoked token normally no longer appears in the live envelope,
    # so its hash may exist ONLY in the ledger. Dropping those would erase
    # retirement history and let a stale peer re-persist the token.
    for ledger_key in retired:
        exists = db.execute(
            "SELECT 1 FROM tokens WHERE token_key=?", (ledger_key,)
        ).fetchone()
        if exists:
            continue
        db.execute(
            "INSERT INTO tokens(token_key,kind,nonce,ciphertext,expires_at,retired,retired_at) VALUES(?,?,?,?,?,1,?)",
            (ledger_key, "retired", b"", b"", 0.0, now),
        )
    for kind in ("access", "refresh"):
        section = bundle.get(f"{kind}_tokens")
        if not isinstance(section, dict):
            continue
        for value, item in section.items():
            if not (isinstance(item, dict) and item.get("expires_at", 0) > now):
                continue
            row_key = _token_key(kind, value)
            if row_key in retired:
                # Preserve the tombstone so rotated/revoked stay dead.
                db.execute(
                    "INSERT OR REPLACE INTO tokens(token_key,kind,nonce,ciphertext,expires_at,retired,retired_at) VALUES(?,?,?,?,?,1,?)",
                    (row_key, kind, b"", b"", item.get("expires_at", 0), now),
                )
                continue
            record = dict(item)
            record["_kind"] = kind
            record["_token_value"] = value
            nonce, ct = _encrypt_record(key, record)
            db.execute(
                "INSERT OR REPLACE INTO tokens(token_key,kind,nonce,ciphertext,expires_at,retired,retired_at) VALUES(?,?,?,?,?,0,NULL)",
                (row_key, kind, nonce, ct, item.get("expires_at", 0)),
            )
            migrated += 1
    # Preserve the legacy revocation epoch. Out-of-range or negative values
    # are unknown history: fail closed (epoch >= 1) rather than normalizing
    # to zero, which would erase the revocation fence.
    legacy_epoch = 0
    if isinstance(legacy_ledger.get("revocation_epoch"), int) and not isinstance(
        legacy_ledger.get("revocation_epoch"), bool
    ):
        legacy_epoch = int(legacy_ledger["revocation_epoch"])
    elif epoch_path.exists():
        try:
            legacy_epoch = int(epoch_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            legacy_epoch = 1  # unknown history -> treat as revoked once
    if legacy_epoch < 0 or legacy_epoch > _SQLITE_MAX_INT:
        legacy_epoch = 1  # out-of-range history -> fail closed
    have_epoch = db.execute(
        "SELECT 1 FROM token_meta WHERE name='revocation_epoch'"
    ).fetchone()
    if not have_epoch:
        db.execute(
            "INSERT OR REPLACE INTO token_meta(name,value) VALUES('revocation_epoch',?)",
            (str(legacy_epoch),),
        )
    db.execute(
        "INSERT OR REPLACE INTO token_meta(name,value) VALUES('legacy_migration','done')"
    )
    return migrated


def _cleanup_legacy_artifacts(hermes_root: Path) -> None:
    """Remove legacy artifacts AFTER the enclosing transaction committed.

    Safe to retry: each unlink is missing_ok. If cleanup fails the worst case
    is leftover files that the closed migration marker ignores.
    """
    try:
        _legacy_envelope_path(hermes_root).unlink(missing_ok=True)
        (_secrets_dir(hermes_root) / LEGACY_LEDGER_FILENAME).unlink(missing_ok=True)
        (_secrets_dir(hermes_root) / LEGACY_EPOCH_FILENAME).unlink(missing_ok=True)
    except OSError:
        pass


def _legacy_flat_records(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    sections = [v for v in bundle.values() if isinstance(v, dict)]
    for section in sections:
        flat.extend(i for i in section.values() if isinstance(i, dict))
    for item in bundle.values():
        if isinstance(item, dict) and "expires_at" in item and item not in flat:
            flat.append(item)
    return flat
