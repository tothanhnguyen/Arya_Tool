"""Consistent local SQLite backups and non-executing Supabase backup plans."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
HASH_CHUNK_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024


class BackupError(RuntimeError):
    """A backup failed without exposing database content or credentials."""


class BackupExistsError(BackupError):
    """The requested backup or manifest already exists."""


class VerificationCode(StrEnum):
    VERIFIED = "verified"
    MANIFEST_INVALID = "manifest_invalid"
    HASH_MISMATCH = "hash_mismatch"
    DATABASE_INVALID = "database_invalid"
    BACKUP_UNAVAILABLE = "backup_unavailable"


@dataclass(frozen=True, slots=True)
class SQLiteBackup:
    backup_path: Path
    manifest_path: Path
    sha256: str
    size_bytes: int
    created_at: str


@dataclass(frozen=True, slots=True)
class BackupVerification:
    valid: bool
    code: VerificationCode


@dataclass(frozen=True, slots=True)
class PlannedCommand:
    purpose: str
    argv: tuple[str, ...]
    remote_access: str = "read_only"
    executes: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "argv": list(self.argv),
            "remote_access": self.remote_access,
            "executes": self.executes,
        }


@dataclass(frozen=True, slots=True)
class SupabaseBackupPlan:
    database: PlannedCommand
    storage_destination: str
    storage_action: str
    remote_mutation: bool = False
    executes: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database.as_dict(),
            "storage": {
                "destination": self.storage_destination,
                "action": self.storage_action,
                "executes": False,
            },
            "remote_mutation": self.remote_mutation,
            "executes": self.executes,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _manifest_path(destination: Path) -> Path:
    return destination.with_name(f"{destination.name}.manifest.json")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _publish_no_overwrite(staged: Path, destination: Path) -> None:
    try:
        os.link(staged, destination)
    except FileExistsError as exc:
        raise BackupExistsError("backup destination already exists") from exc
    except OSError as exc:
        raise BackupError("backup could not be published safely") from exc


def backup_sqlite(
    source: str | Path,
    destination: str | Path,
    *,
    now: datetime | None = None,
) -> SQLiteBackup:
    """Create and atomically publish a consistent SQLite online backup."""
    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    manifest_path = _manifest_path(destination_path)
    if not source_path.is_file():
        raise BackupError("SQLite source is unavailable")
    if source_path == destination_path:
        raise BackupError("SQLite source and backup destination must differ")
    if destination_path.exists() or manifest_path.exists():
        raise BackupExistsError("backup destination already exists")
    destination_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    backup_fd, backup_name = tempfile.mkstemp(
        dir=destination_path.parent,
        prefix=f".{destination_path.name}-",
        suffix=".sqlite.tmp",
    )
    os.close(backup_fd)
    staged_backup = Path(backup_name)
    manifest_fd, manifest_name = tempfile.mkstemp(
        dir=destination_path.parent,
        prefix=f".{destination_path.name}-",
        suffix=".manifest.tmp",
    )
    staged_manifest = Path(manifest_name)
    created_destination = False
    try:
        try:
            with (
                closing(
                    sqlite3.connect(
                        f"{source_path.as_uri()}?mode=ro",
                        uri=True,
                        timeout=30,
                    )
                ) as source_connection,
                closing(sqlite3.connect(staged_backup)) as destination_connection,
            ):
                source_connection.backup(destination_connection)
                integrity = destination_connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise BackupError("SQLite backup failed integrity validation")
                destination_connection.commit()
        except sqlite3.Error as exc:
            raise BackupError("SQLite backup failed") from exc

        _fsync_file(staged_backup)
        digest = _sha256(staged_backup)
        size_bytes = staged_backup.stat().st_size
        created_at = _as_utc(now or datetime.now(UTC)).isoformat()
        manifest = {
            "version": MANIFEST_VERSION,
            "created_at": created_at,
            "backup_file": destination_path.name,
            "size_bytes": size_bytes,
            "sha256": digest,
            "sqlite_integrity_check": "ok",
            "method": "sqlite_online_backup",
        }
        manifest_payload = (
            json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        with os.fdopen(manifest_fd, "wb") as stream:
            manifest_fd = -1
            stream.write(manifest_payload)
            stream.flush()
            os.fsync(stream.fileno())

        _publish_no_overwrite(staged_backup, destination_path)
        created_destination = True
        try:
            _publish_no_overwrite(staged_manifest, manifest_path)
        except Exception:
            destination_path.unlink(missing_ok=True)
            created_destination = False
            raise
        _fsync_directory(destination_path.parent)
        return SQLiteBackup(
            backup_path=destination_path,
            manifest_path=manifest_path,
            sha256=digest,
            size_bytes=size_bytes,
            created_at=created_at,
        )
    finally:
        if manifest_fd >= 0:
            os.close(manifest_fd)
        staged_backup.unlink(missing_ok=True)
        staged_manifest.unlink(missing_ok=True)
        if created_destination and not manifest_path.exists():
            destination_path.unlink(missing_ok=True)


def verify_sqlite_backup(
    backup: str | Path,
    manifest: str | Path | None = None,
) -> BackupVerification:
    backup_path = Path(backup).expanduser().resolve()
    manifest_path = (
        Path(manifest).expanduser().resolve()
        if manifest is not None
        else _manifest_path(backup_path)
    )
    try:
        if (
            not backup_path.is_file()
            or not manifest_path.is_file()
            or manifest_path.stat().st_size > MAX_MANIFEST_BYTES
        ):
            return BackupVerification(False, VerificationCode.BACKUP_UNAVAILABLE)
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("version") != MANIFEST_VERSION
            or data.get("backup_file") != backup_path.name
            or not isinstance(data.get("sha256"), str)
            or not isinstance(data.get("size_bytes"), int)
        ):
            return BackupVerification(False, VerificationCode.MANIFEST_INVALID)
        if (
            backup_path.stat().st_size != data["size_bytes"]
            or _sha256(backup_path) != data["sha256"]
        ):
            return BackupVerification(False, VerificationCode.HASH_MISMATCH)
        connection = sqlite3.connect(
            f"{backup_path.as_uri()}?mode=ro",
            uri=True,
            timeout=30,
        )
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
        if integrity is None or integrity[0] != "ok":
            return BackupVerification(False, VerificationCode.DATABASE_INVALID)
        return BackupVerification(True, VerificationCode.VERIFIED)
    except (OSError, ValueError, json.JSONDecodeError):
        return BackupVerification(False, VerificationCode.MANIFEST_INVALID)
    except sqlite3.Error:
        return BackupVerification(False, VerificationCode.DATABASE_INVALID)


def plan_supabase_backup(
    database_dump: str | Path,
    storage_destination: str | Path,
) -> SupabaseBackupPlan:
    """Return an inert plan; this function never invokes a process or remote API."""
    dump_path = Path(database_dump).expanduser()
    storage_path = Path(storage_destination).expanduser()
    for value in (str(dump_path), str(storage_path)):
        if "\x00" in value or "\n" in value or "\r" in value:
            raise BackupError("backup plan path is invalid")
    command = PlannedCommand(
        purpose="logical_database_backup",
        argv=(
            "supabase",
            "db",
            "dump",
            "--linked",
            "--file",
            str(dump_path),
        ),
    )
    return SupabaseBackupPlan(
        database=command,
        storage_destination=str(storage_path),
        storage_action=(
            "export_private_storage_separately_with_an_approved_read_only_tool"
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe Arya_Tool backup helpers")
    subparsers = parser.add_subparsers(dest="action", required=True)
    sqlite_parser = subparsers.add_parser("sqlite")
    sqlite_parser.add_argument("source", type=Path)
    sqlite_parser.add_argument("destination", type=Path)
    verify_parser = subparsers.add_parser("verify-sqlite")
    verify_parser.add_argument("backup", type=Path)
    verify_parser.add_argument("--manifest", type=Path)
    plan_parser = subparsers.add_parser("plan-supabase")
    plan_parser.add_argument("database_dump", type=Path)
    plan_parser.add_argument("storage_destination", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "sqlite":
            result = backup_sqlite(args.source, args.destination)
            output = {
                **asdict(result),
                "backup_path": str(result.backup_path),
                "manifest_path": str(result.manifest_path),
            }
            print(json.dumps(output, ensure_ascii=True, indent=2))
            return 0
        if args.action == "verify-sqlite":
            result = verify_sqlite_backup(args.backup, args.manifest)
            print(
                json.dumps(
                    {"valid": result.valid, "code": result.code.value},
                    ensure_ascii=True,
                )
            )
            return 0 if result.valid else 2
        plan = plan_supabase_backup(
            args.database_dump,
            args.storage_destination,
        )
        print(json.dumps(plan.as_dict(), ensure_ascii=True, indent=2))
        return 0
    except BackupError:
        print(json.dumps({"ok": False, "error": "backup_operation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
