"""Transactional SQLite storage for encrypted Hermes OAuth tokens.

The module coordinates token rows, revocation epochs, and atomic mutations.
Key management lives in ``token_store_crypto``; pre-SQLite JSON migration
lives in ``token_store_legacy``. OAuth code is the only runtime caller.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

try:
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None
    import msvcrt as _msvcrt
else:
    _msvcrt = None

from hermes_gpt.auth import token_crypto as _crypto
from hermes_gpt.auth import token_legacy as _legacy

# Keep the established token_store helper surface for callers and fixtures.
ENVELOPE_VERSION = _crypto.ENVELOPE_VERSION
ENVELOPE_FILENAME = _crypto.ENVELOPE_FILENAME
KEY_FILENAME = _crypto.KEY_FILENAME
SECRETS_DIR = _crypto.SECRETS_DIR
MASTER_KEY_ENV = _crypto.MASTER_KEY_ENV
SERVICE_NAME = _crypto.SERVICE_NAME
USERNAME = _crypto.USERNAME
TokenStoreError = _crypto.TokenStoreError
_secrets_dir = _crypto._secrets_dir
envelope_path = _crypto.envelope_path
key_file_path = _crypto.key_file_path
_b64 = _crypto._b64
_unb64 = _crypto._unb64
_key_from_env = _crypto._key_from_env
_key_from_keyring = _crypto._key_from_keyring
_store_key_in_keyring = _crypto._store_key_in_keyring
_key_from_file = _crypto._key_from_file
_write_key_file = _crypto._write_key_file
_rotate_active_key = _crypto._rotate_active_key
_resolve_key = _crypto._resolve_key
load_envelope = _crypto.load_envelope
decrypt_envelope = _crypto.decrypt_envelope
_write_envelope = _crypto._write_envelope
save_tokens = _crypto.save_tokens
_token_key = _crypto._token_key
issue_key = _crypto.issue_key
_encrypt_record = _crypto._encrypt_record
_decrypt_record = _crypto._decrypt_record
LEGACY_ENVELOPE_FILENAME = _legacy.LEGACY_ENVELOPE_FILENAME
LEGACY_EPOCH_FILENAME = _legacy.LEGACY_EPOCH_FILENAME
LEGACY_LEDGER_FILENAME = _legacy.LEGACY_LEDGER_FILENAME
_SQLITE_MAX_INT = _legacy._SQLITE_MAX_INT
_legacy_envelope_path = _legacy._legacy_envelope_path
_close_legacy_migration_locked = _legacy._close_legacy_migration_locked
_legacy_epoch_from_ledger_or_file = _legacy._legacy_epoch_from_ledger_or_file
_parse_legacy_retired = _legacy._parse_legacy_retired
_read_legacy_epoch_locked = _legacy._read_legacy_epoch_locked
_migrate_legacy_locked = _legacy._migrate_legacy_locked
_cleanup_legacy_artifacts = _legacy._cleanup_legacy_artifacts
_legacy_flat_records = _legacy._legacy_flat_records


DB_FILENAME = "hermes_gpt_tokens.db"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_meta (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    expires_at REAL NOT NULL,
    retired INTEGER NOT NULL DEFAULT 0,
    retired_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tokens_expiry ON tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_tokens_kind ON tokens(kind, retired);
"""


def _store_lock_path(hermes_root: Path) -> Path:
    return _secrets_dir(hermes_root) / (DB_FILENAME + ".lock")


class _StoreLock:
    """Cross-process mutex around credential mutations.

    POSIX uses flock; Windows uses a one-byte msvcrt.locking region.
    Both are OS-owned and released when the process/file descriptor closes,
    so there is no stale-lock file to break. Used to serialize revocation
    and master-key rotation against new issuance.
    """

    def __init__(self, hermes_root: Path) -> None:
        self.path = _store_lock_path(hermes_root)
        self.fd: int | None = None

    def _acquire(self) -> None:
        assert self.fd is not None
        if _fcntl is not None:
            _fcntl.flock(self.fd, _fcntl.LOCK_EX)
            return
        if os.fstat(self.fd).st_size == 0:
            os.write(self.fd, b"\0")
        os.lseek(self.fd, 0, os.SEEK_SET)
        while True:
            try:
                _msvcrt.locking(self.fd, _msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)

    def _release(self) -> None:
        assert self.fd is not None
        if _fcntl is not None:
            _fcntl.flock(self.fd, _fcntl.LOCK_UN)
            return
        os.lseek(self.fd, 0, os.SEEK_SET)
        _msvcrt.locking(self.fd, _msvcrt.LK_UNLCK, 1)

    def __enter__(self) -> "_StoreLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self._acquire()
        except Exception:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            try:
                self._release()
            finally:
                os.close(self.fd)
                self.fd = None


def _db_path(hermes_root: Path) -> Path:
    return _secrets_dir(hermes_root) / DB_FILENAME


def _connect(hermes_root: Path) -> sqlite3.Connection:
    """Open the token DB read-write; initializes the schema. Fails closed.

    Raises TokenStoreError when the database is unreadable/corrupt — callers
    treat tokens as invalid rather than falling back to permissive behavior.
    """
    path = _db_path(hermes_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=15.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=15000")
        db.executescript(_SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return db
    except sqlite3.Error as exc:
        raise TokenStoreError(f"token database unavailable: {exc}") from exc


def read_revocation_epoch(hermes_root: Path) -> int:
    """Current durable revocation epoch (monotonic; 0 = never revoked).

    Fails closed on a corrupt database by raising TokenStoreError.
    """
    if not _db_path(hermes_root).exists():
        return 0
    try:
        db = _connect(hermes_root)
    except TokenStoreError:
        raise
    try:
        row = db.execute(
            "SELECT value FROM token_meta WHERE name='revocation_epoch'"
        ).fetchone()
        return int(row["value"]) if row else 0
    except (sqlite3.Error, ValueError) as exc:
        raise TokenStoreError(f"token database unreadable: {exc}") from exc
    finally:
        db.close()


def lookup_token(
    hermes_root: Path, kind: str, token_value: str
) -> dict[str, Any] | None:
    """Return the durable record for a live token, or None.

    None when the token is unknown, expired, or retired (revoked/rotated).
    Raises TokenStoreError when the store is unreadable — callers fail
    closed instead of treating corruption as permission.
    """
    row_key = _token_key(kind, token_value)
    db = _connect(hermes_root)
    try:
        row = db.execute(
            "SELECT nonce,ciphertext,expires_at,retired FROM tokens WHERE token_key=?",
            (row_key,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise TokenStoreError(f"token database unreadable: {exc}") from exc
    finally:
        db.close()
    if not row or row["retired"] or row["expires_at"] <= time.time():
        return None
    try:
        key, _, _ = _resolve_key_parts(hermes_root)
        return _decrypt_record(key, row["nonce"], row["ciphertext"])
    except Exception as exc:
        raise TokenStoreError("token record could not be decrypted") from exc


def load_live_tokens(hermes_root: Path) -> dict[str, Any]:
    """All live records, grouped envelope-style ({access_tokens, refresh_tokens}).

    Raises TokenStoreError on corruption (fail closed).
    """
    now = time.time()
    db = _connect(hermes_root)
    try:
        rows = db.execute(
            "SELECT token_key,kind,nonce,ciphertext,expires_at FROM tokens WHERE retired=0 AND expires_at>?",
            (now,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise TokenStoreError(f"token database unreadable: {exc}") from exc
    finally:
        db.close()
    key, _, _ = _resolve_key_parts(hermes_root)
    out: dict[str, Any] = {"access_tokens": {}, "refresh_tokens": {}}
    for row in rows:
        try:
            record = _decrypt_record(key, row["nonce"], row["ciphertext"])
        except Exception as exc:
            # A live (unretired, unexpired) record that cannot be decrypted
            # means a corrupt or key-mismatched store. Fail closed: a partial
            # restore would silently drop credentials (and their revocation
            # semantics) instead of surfacing the damage.
            raise TokenStoreError(
                "token database contains an undecryptable live record"
            ) from exc
        section = out.get(f"{row['kind']}_tokens")
        if isinstance(section, dict):
            value = record.get("_token_value")
            if isinstance(value, str) and value:
                clean = {k: v for k, v in record.items() if not k.startswith("_")}
                section[value] = clean
    return out


def _resolve_key_parts(hermes_root: Path) -> tuple[bytes, str, str]:
    key, kid, source = _resolve_key(hermes_root)
    return key, kid, source


def commit_tokens(
    hermes_root: Path,
    *,
    source_epoch: int,
    issue: dict[str, dict[str, Any]] | None = None,
    retire: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Atomically commit token issuance/retirement in one SQLite transaction.

    ``issue`` maps row keys (:func:`issue_key`) to records carrying internal
    ``_kind``/``_token_value`` markers. ``retire`` maps a kind to raw token
    values being consumed/rotated. The epoch check, retirement, issuance,
    expiry pruning, and legacy migration all commit (or roll back) together;
    SQLite's write lock serializes this against every other mutation,
    including revocation, so no interleaving can resurrect retired tokens.
    """
    issue = issue or {}
    retire = retire or {}
    now = time.time()
    with _StoreLock(hermes_root):
        return _commit_tokens_locked(
            hermes_root, source_epoch=source_epoch, issue=issue, retire=retire
        )


def _commit_tokens_locked(
    hermes_root: Path,
    *,
    source_epoch: int,
    issue: dict[str, dict[str, Any]],
    retire: dict[str, list[str]],
) -> dict[str, Any]:
    now = time.time()
    db = _connect(hermes_root)
    try:
        db.execute("BEGIN IMMEDIATE")
        key, kid, source = _resolve_key_parts(hermes_root)
        _migrate_legacy_locked(db, hermes_root, key, kid, now)
        row = db.execute(
            "SELECT value FROM token_meta WHERE name='revocation_epoch'"
        ).fetchone()
        current_epoch = int(row["value"]) if row else 0
        if current_epoch > source_epoch:
            db.execute("ROLLBACK")
            raise TokenStoreError(
                "token store was revoked after this view was built; refusing to persist"
            )
        retired_count = 0
        for kind, values in retire.items():
            for value in values or ():
                row_key = _token_key(kind, value)
                cur = db.execute(
                    "UPDATE tokens SET retired=1, retired_at=? WHERE token_key=? AND retired=0",
                    (now, row_key),
                )
                retired_count += cur.rowcount if cur.rowcount > 0 else 0
        issued = 0
        for row_key, item in issue.items():
            if not (isinstance(item, dict) and item.get("expires_at", 0) > now):
                continue
            kind = str(item.get("_kind") or "")
            value = str(item.get("_token_value") or "")
            if kind not in ("access", "refresh") or not value:
                continue
            # Retirement is permanent: a tombstoned key can never be
            # re-issued, even by a stale peer cache that still lists it.
            tomb = db.execute(
                "SELECT 1 FROM tokens WHERE token_key=? AND retired=1", (row_key,)
            ).fetchone()
            if tomb:
                continue
            nonce, ct = _encrypt_record(key, dict(item))
            db.execute(
                "INSERT OR REPLACE INTO tokens(token_key,kind,nonce,ciphertext,expires_at,retired,retired_at) VALUES(?,?,?,?,?,0,NULL)",
                (row_key, kind, nonce, ct, item["expires_at"]),
            )
            issued += 1
        # Prune expired LIVE credentials only. Retired rows are tombstones:
        # they must outlive their original expiry so a later stale reissue of
        # the same token value can never succeed after the row is gone.
        db.execute("DELETE FROM tokens WHERE expires_at<=? AND retired=0", (now,))
        live = db.execute("SELECT COUNT(*) AS c FROM tokens WHERE retired=0").fetchone()
        db.execute(
            "INSERT OR REPLACE INTO token_meta(name,value) VALUES('revocation_epoch',?)",
            (str(current_epoch),),
        )
        db.execute("COMMIT")
        _cleanup_legacy_artifacts(hermes_root)
        return {
            "kid": kid,
            "source": source,
            "epoch": current_epoch,
            "records": live["c"] if live else 0,
            "retired": retired_count,
            "issued": issued,
        }
    except sqlite3.Error as exc:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise TokenStoreError(f"token commit failed: {exc}") from exc
    finally:
        db.close()


def migrate_store(hermes_root: Path) -> dict[str, Any]:
    """Run the legacy -> SQLite migration as its own transaction.

    Migration is NOT credential issuance: it must faithfully import
    whatever revocation epoch the legacy artifacts carry, so the epoch
    fence used for grants does not apply here (a positive legacy epoch
    must commit, not roll back). Safe to call at startup and idempotent:
    the durable 'legacy_migration' marker closes it after the first run.
    """
    now = time.time()
    db = _connect(hermes_root)
    try:
        db.execute("BEGIN IMMEDIATE")
        key, kid, source = _resolve_key_parts(hermes_root)
        _migrate_legacy_locked(db, hermes_root, key, kid, now)
        meta = db.execute(
            "SELECT value FROM token_meta WHERE name='revocation_epoch'"
        ).fetchone()
        epoch = int(meta["value"]) if meta else 0
        live = db.execute(
            "SELECT COUNT(*) AS c FROM tokens WHERE retired=0"
        ).fetchone()
        db.execute("COMMIT")
        _cleanup_legacy_artifacts(hermes_root)
        return {
            "kid": kid,
            "source": source,
            "epoch": epoch,
            "records": live["c"] if live else 0,
        }
    except sqlite3.Error as exc:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise TokenStoreError(f"token store migration failed: {exc}") from exc
    finally:
        db.close()


def exchange_commit(
    hermes_root: Path,
    *,
    source_epoch: int,
    presented_kind: str,
    presented_value: str,
    issue: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Atomically CONSUME one presented token and publish replacements.

    The liveness check and the retirement of the presented token happen in
    the same transaction as the issuance of its replacements, so two peers
    racing to exchange the same refresh token cannot both succeed: the first
    commit retires it, the second sees the tombstone and is rejected whole
    (no replacement credentials are published).
    """
    issue = issue or {}
    with _StoreLock(hermes_root):
        return _exchange_commit_locked(
            hermes_root,
            source_epoch=source_epoch,
            presented_kind=presented_kind,
            presented_value=presented_value,
            issue=issue,
        )


def _exchange_commit_locked(
    hermes_root: Path,
    *,
    source_epoch: int,
    presented_kind: str,
    presented_value: str,
    issue: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    now = time.time()
    presented_key = _token_key(presented_kind, presented_value)
    db = _connect(hermes_root)
    try:
        db.execute("BEGIN IMMEDIATE")
        key, kid, source = _resolve_key_parts(hermes_root)
        _migrate_legacy_locked(db, hermes_root, key, kid, now)
        row = db.execute(
            "SELECT nonce,ciphertext,expires_at,retired FROM tokens WHERE token_key=?",
            (presented_key,),
        ).fetchone()
        if not row or row["retired"] or row["expires_at"] <= now:
            db.execute("ROLLBACK")
            raise TokenStoreError("presented token is not live")
        meta = db.execute(
            "SELECT value FROM token_meta WHERE name='revocation_epoch'"
        ).fetchone()
        current_epoch = int(meta["value"]) if meta else 0
        if current_epoch > source_epoch:
            db.execute("ROLLBACK")
            raise TokenStoreError(
                "token store was revoked after this view was built; refusing to persist"
            )
        presented_record = _decrypt_record(key, row["nonce"], row["ciphertext"])
        db.execute(
            "UPDATE tokens SET retired=1, retired_at=? WHERE token_key=?",
            (now, presented_key),
        )
        issued = 0
        for row_key, item in issue.items():
            if not (isinstance(item, dict) and item.get("expires_at", 0) > now):
                continue
            kind = str(item.get("_kind") or "")
            value = str(item.get("_token_value") or "")
            if kind not in ("access", "refresh") or not value:
                continue
            # Retirement is permanent: never let an issuance (even from the
            # exchange's own replacement set) overwrite a tombstone.
            tomb = db.execute(
                "SELECT 1 FROM tokens WHERE token_key=? AND retired=1", (row_key,)
            ).fetchone()
            if tomb:
                continue
            nonce, ct = _encrypt_record(key, dict(item))
            db.execute(
                "INSERT OR REPLACE INTO tokens(token_key,kind,nonce,ciphertext,expires_at,retired,retired_at) VALUES(?,?,?,?,?,0,NULL)",
                (row_key, kind, nonce, ct, item["expires_at"]),
            )
            issued += 1
        db.execute("DELETE FROM tokens WHERE expires_at<=? AND retired=0", (now,))
        db.execute("COMMIT")
        _cleanup_legacy_artifacts(hermes_root)
        return {
            "kid": kid,
            "source": source,
            "epoch": current_epoch,
            "issued": issued,
            "presented": presented_record,
        }
    except sqlite3.Error as exc:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise TokenStoreError(f"token exchange commit failed: {exc}") from exc
    finally:
        db.close()


def load_tokens(hermes_root: Path) -> dict[str, Any]:
    """Load the live token bundle (SQLite store first, legacy envelope fallback).

    Raises TokenStoreError on unreadable stores — callers fail closed.
    """
    if _db_path(hermes_root).exists():
        return load_live_tokens(hermes_root)
    envelope = load_envelope(hermes_root)
    if envelope is None:
        return {}
    plaintext = decrypt_envelope(envelope, hermes_root)
    if not isinstance(plaintext, dict):
        raise TokenStoreError("token envelope plaintext is not an object")
    return plaintext


def status(hermes_root: Path) -> dict[str, Any]:
    """Read-only store status: presence, expiry, revocation epoch. No material.

    Understands both the current SQLite store and a not-yet-migrated legacy
    envelope, so the browser account-status derivation keeps working across
    the upgrade.
    """
    if not _db_path(hermes_root).exists():
        envelope = load_envelope(hermes_root) if _legacy_envelope_path(hermes_root).exists() else None
        if envelope is None:
            return {
                "available": False,
                "presence": "absent",
                "expires_at": None,
                "revocation_epoch": 0,
                "kid": "",
            }
        try:
            bundle = decrypt_envelope(envelope, hermes_root)
        except TokenStoreError:
            return {
                "available": True,
                "presence": "corrupt",
                "expires_at": None,
                "revocation_epoch": 0,
                "kid": envelope.get("kid", ""),
            }
        flat = _legacy_flat_records(bundle)
        expiries = [v.get("expires_at") for v in flat if v.get("expires_at")]
        return {
            "available": True,
            "presence": "present",
            "expires_at": max(expiries) if expiries else None,
            "revocation_epoch": 0,
            "kid": envelope.get("kid", ""),
            "client_count": len([v for v in flat if v.get("expires_at", 0) > time.time()]),
        }
    try:
        db = _connect(hermes_root)
        rows = db.execute(
            "SELECT expires_at,retired FROM tokens"
        ).fetchall()
        meta = db.execute(
            "SELECT value FROM token_meta WHERE name='revocation_epoch'"
        ).fetchone()
        db.close()
    except (sqlite3.Error, TokenStoreError) as exc:
        return {
            "available": True,
            "presence": "corrupt",
            "expires_at": None,
            "revocation_epoch": None,
            "kid": "",
            "error": f"{exc}"[:120],
        }
    now = time.time()
    live = [r for r in rows if not r["retired"] and r["expires_at"] > now]
    expiries = [r["expires_at"] for r in rows]
    return {
        "available": True,
        "presence": "present",
        "expires_at": max(expiries) if expiries else None,
        "revocation_epoch": int(meta["value"]) if meta else 0,
        "kid": "",
        "client_count": len(live),
    }


def revoke_tokens(hermes_root: Path, *, rotate_key: bool = True) -> dict[str, Any]:
    """Revoke durable tokens in one SQLite transaction.

    Marks every live token retired and advances the durable revocation epoch
    in one SQLite transaction; when requested, rotates the ACTIVE master-key
    source AFTER the commit (SQLite cannot roll back an external key
    mutation, and a failed commit after an in-transaction rotation would
    leave resurrected live rows undecryptable). Post-commit there are no
    live rows left to lose. A grant racing the rotation window either
    completes under the old key and fails closed on lookup, or starts after
    and uses the new key — never a silent bypass. Also removes legacy
    artifacts. Returns a bounded summary; never exposes token material.
    """
    now = time.time()
    envelope_existed = _legacy_envelope_path(hermes_root).exists()
    with _StoreLock(hermes_root):
        return _revoke_tokens_locked(
            hermes_root, rotate_key=rotate_key, envelope_existed=envelope_existed
        )


def _revoke_tokens_locked(
    hermes_root: Path, *, rotate_key: bool, envelope_existed: bool
) -> dict[str, Any]:
    now = time.time()
    db = _connect(hermes_root)
    epoch = 0
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT value FROM token_meta WHERE name='revocation_epoch'"
        ).fetchone()
        epoch = int(row["value"]) if row else 0
        db.execute("UPDATE tokens SET retired=1, retired_at=? WHERE retired=0", (now,))
        db.execute(
            "INSERT OR REPLACE INTO token_meta(name,value) VALUES('revocation_epoch',?)",
            (str(epoch + 1),),
        )
        # Close legacy migration permanently: leftover JSON artifacts must
        # never re-import credentials this revocation just killed.
        db.execute(
            "INSERT OR REPLACE INTO token_meta(name,value) VALUES('legacy_migration','closed:revoked')"
        )
        db.execute("COMMIT")
        rotation: dict[str, Any] = {"outcome": "not_requested", "source": ""}
        if rotate_key:
            # Rotate the ACTIVE key source only AFTER the transaction
            # committed: SQLite cannot roll back an external key mutation,
            # and rotating mid-transaction would leave still-live rows
            # undecryptable if the commit then failed. Post-commit there are
            # no live rows left to lose (every token was retired above), so
            # the new key starts clean.
            rotation = _rotate_active_key(hermes_root)
    except sqlite3.Error as exc:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise TokenStoreError(f"token revocation failed: {exc}") from exc
    finally:
        db.close()
    # Legacy artifacts are obsolete once revoked (migration is closed inside
    # the transaction; deletion after commit is best-effort and retryable).
    _cleanup_legacy_artifacts(hermes_root)
    note = ""
    if rotate_key and rotation["outcome"] == "env_managed":
        note = (
            "master key is env-managed (HERMES_GPT_TOKEN_MASTER_KEY); "
            "rotate it externally"
        )
    elif rotate_key and rotation["outcome"] == "failed":
        note = (
            f"master-key rotation FAILED for the active "
            f"{rotation.get('source', 'unknown')} source; tokens are revoked "
            f"but the old key remains active — investigate and rotate manually"
        )
    return {
        "revoked": True,
        "envelope_removed": envelope_existed,
        "key_rotated": bool(rotate_key) and rotation["outcome"] == "rotated",
        "key_rotation_note": note,
        "epoch": epoch + 1,
    }
