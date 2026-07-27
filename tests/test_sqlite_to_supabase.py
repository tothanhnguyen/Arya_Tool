from __future__ import annotations

import hashlib

import pytest

from laplace import db
from laplace.db import Base
from laplace.migrations.sqlite_to_supabase import (
    MigrationError,
    _prepare_media_row,
    _resolve_target_url,
    inspect_source,
)
from laplace.models import User
from laplace.social.models import MediaAsset


def test_dry_run_inventories_all_tables_and_local_media(tmp_path):
    database = tmp_path / "source.db"
    media_path = tmp_path / "media.png"
    payload = b"media-payload"
    media_path.write_bytes(payload)
    db.reset_engine_for_tests(f"sqlite:///{database}")
    Base.metadata.create_all(db._engine)
    with db.session_scope() as session:
        user = User()
        session.add(user)
        session.flush()
        session.add(
            MediaAsset(
                user_id=user.id,
                type="image",
                local_path=str(media_path),
                original_name="media.png",
                mime_type="image/png",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )

    report = inspect_source(f"sqlite:///{database}")

    assert report.completed
    assert not report.missing_tables
    assert report.tables["users"].source_rows == 1
    assert report.tables["media_assets"].source_rows == 1
    assert report.local_media_rows == 1
    assert report.missing_media_files == 0
    assert report.media_bytes == len(payload)
    assert report.orphan_foreign_keys == {}


def test_target_url_must_come_from_postgres_env(monkeypatch):
    monkeypatch.setenv("ARYA_TEST_TARGET", "sqlite:///wrong.db")

    with pytest.raises(MigrationError):
        _resolve_target_url("ARYA_TEST_TARGET")


def test_media_preparation_uploads_by_hash(tmp_path):
    payload = b"verified-payload"
    path = tmp_path / "asset.png"
    path.write_bytes(payload)
    calls = []

    class FakeStorage:
        def store(self, **kwargs):
            calls.append(kwargs)
            return type(
                "Stored",
                (),
                {
                    "location": "supabase://arya-media/users/4/object.png",
                    "backend": "supabase",
                    "bucket": "arya-media",
                    "key": "users/4/object.png",
                },
            )()

    row = {
        "id": 9,
        "user_id": 4,
        "local_path": str(path),
        "original_name": "asset.png",
        "mime_type": "image/png",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }

    prepared = _prepare_media_row(row, storage=FakeStorage())

    assert prepared["storage_backend"] == "supabase"
    assert prepared["storage_bucket"] == "arya-media"
    assert calls[0]["payload"] == payload
    assert calls[0]["digest"] == row["sha256"]


def test_media_preparation_rejects_missing_file():
    with pytest.raises(MigrationError, match="missing its local file"):
        _prepare_media_row(
            {
                "id": 1,
                "user_id": 1,
                "local_path": "/definitely/not/present.png",
                "original_name": "missing.png",
                "mime_type": "image/png",
                "size_bytes": 1,
                "sha256": "a" * 64,
            },
            storage=object(),
        )
