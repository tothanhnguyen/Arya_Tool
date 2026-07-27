"""Tests for consistent SQLite backups and inert Supabase plans."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from laplace.ops.backup import (
    BackupError,
    BackupExistsError,
    VerificationCode,
    backup_sqlite,
    plan_supabase_backup,
    verify_sqlite_backup,
)


def _source_database(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO events(value) VALUES (?)",
        [("one",), ("two",), ("three",)],
    )
    connection.commit()
    return connection


def test_sqlite_backup_is_consistent_hashed_and_manifested(tmp_path) -> None:
    source = tmp_path / "live.sqlite"
    connection = _source_database(source)
    destination = tmp_path / "backups" / "snapshot.sqlite"
    created_at = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    try:
        backup = backup_sqlite(source, destination, now=created_at)
    finally:
        connection.close()

    copied = sqlite3.connect(destination)
    try:
        rows = copied.execute("SELECT value FROM events ORDER BY id").fetchall()
    finally:
        copied.close()
    payload = destination.read_bytes()
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))

    assert rows == [("one",), ("two",), ("three",)]
    assert backup.sha256 == hashlib.sha256(payload).hexdigest()
    assert backup.size_bytes == len(payload)
    assert manifest == {
        "backup_file": "snapshot.sqlite",
        "created_at": created_at.isoformat(),
        "method": "sqlite_online_backup",
        "sha256": backup.sha256,
        "size_bytes": backup.size_bytes,
        "sqlite_integrity_check": "ok",
        "version": 1,
    }
    assert verify_sqlite_backup(destination).valid


def test_sqlite_backup_never_overwrites_existing_files(tmp_path) -> None:
    source = tmp_path / "live.sqlite"
    connection = _source_database(source)
    destination = tmp_path / "snapshot.sqlite"
    try:
        first = backup_sqlite(source, destination)
        original_backup = destination.read_bytes()
        original_manifest = first.manifest_path.read_bytes()
        with pytest.raises(BackupExistsError):
            backup_sqlite(source, destination)
    finally:
        connection.close()

    assert destination.read_bytes() == original_backup
    assert first.manifest_path.read_bytes() == original_manifest


def test_sqlite_backup_treats_naive_injected_timestamp_as_utc(tmp_path) -> None:
    source = tmp_path / "live.sqlite"
    connection = _source_database(source)
    destination = tmp_path / "snapshot.sqlite"
    try:
        backup = backup_sqlite(
            source,
            destination,
            now=datetime(2026, 7, 28, 13, 0, tzinfo=UTC).replace(tzinfo=None),
        )
    finally:
        connection.close()

    assert backup.created_at == "2026-07-28T13:00:00+00:00"


def test_backup_is_a_snapshot_while_source_remains_online(tmp_path) -> None:
    source = tmp_path / "live.sqlite"
    connection = _source_database(source)
    destination = tmp_path / "snapshot.sqlite"
    try:
        backup_sqlite(source, destination)
        connection.execute("INSERT INTO events(value) VALUES ('after-backup')")
        connection.commit()
    finally:
        connection.close()

    copied = sqlite3.connect(destination)
    try:
        count = copied.execute("SELECT count(*) FROM events").fetchone()[0]
    finally:
        copied.close()

    assert count == 3
    assert verify_sqlite_backup(destination).code is VerificationCode.VERIFIED


def test_invalid_source_leaves_no_partial_backup(tmp_path) -> None:
    source = tmp_path / "not-sqlite.db"
    source.write_text("not a database", encoding="utf-8")
    destination = tmp_path / "snapshot.sqlite"

    with pytest.raises(BackupError):
        backup_sqlite(source, destination)

    assert not destination.exists()
    assert not (tmp_path / "snapshot.sqlite.manifest.json").exists()


def test_verification_fails_safely_after_tampering(tmp_path) -> None:
    source = tmp_path / "live.sqlite"
    connection = _source_database(source)
    destination = tmp_path / "snapshot.sqlite"
    try:
        backup_sqlite(source, destination)
    finally:
        connection.close()
    with destination.open("ab") as stream:
        stream.write(b"tampered")

    verification = verify_sqlite_backup(destination)

    assert not verification.valid
    assert verification.code is VerificationCode.HASH_MISMATCH


def test_supabase_backup_is_an_inert_read_only_plan(tmp_path) -> None:
    plan = plan_supabase_backup(
        tmp_path / "database.dump",
        tmp_path / "storage-export",
    )
    body = plan.as_dict()
    serialized = json.dumps(body)

    assert body["database"]["argv"][:4] == [
        "supabase",
        "db",
        "dump",
        "--linked",
    ]
    assert body["database"]["remote_access"] == "read_only"
    assert body["database"]["executes"] is False
    assert body["storage"]["executes"] is False
    assert body["remote_mutation"] is False
    assert body["executes"] is False
    assert "password" not in serialized
    assert "secret" not in serialized
    assert "https://" not in serialized
