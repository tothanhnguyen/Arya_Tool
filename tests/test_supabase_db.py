"""Database and migration scaffolding tests for the Supabase cutover."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from laplace import db
from laplace.migrations.supabase_readiness import (
    render_cutover_checklist,
    write_cutover_checklist,
)
from laplace.services.artifacts import Artifact  # noqa: F401


def test_postgres_urls_use_psycopg_driver():
    assert db._normalize_db_url("postgres://u:p@example.test/db") == (
        "postgresql+psycopg://u:p@example.test/db"
    )
    assert db._normalize_db_url("postgresql://u:p@example.test/db") == (
        "postgresql+psycopg://u:p@example.test/db"
    )
    assert db._normalize_db_url("sqlite:///local.db") == "sqlite:///local.db"


def test_remote_postgres_engine_enforces_ssl_and_bounded_pool(monkeypatch):
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    monkeypatch.setattr(
        db,
        "get_settings",
        lambda: SimpleNamespace(
            db_connect_timeout_s=10,
            db_ssl_mode="require",
            db_pool_size=5,
            db_max_overflow=5,
        ),
    )

    db._make_engine("postgresql://arya:secret@db.example.test/postgres")

    assert captured["url"].startswith("postgresql+psycopg://")
    assert captured["kwargs"]["pool_pre_ping"] is True
    assert captured["kwargs"]["pool_recycle"] == 1800
    assert captured["kwargs"]["connect_args"]["sslmode"] == "require"
    assert captured["kwargs"]["connect_args"]["connect_timeout"] > 0


def test_sqlite_upgrade_adds_storage_reference_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/legacy.db")
    with engine.begin() as connection:
        connection.execute(
            text(
                "create table media_assets ("
                "id integer primary key, "
                "local_path text not null"
                ")"
            )
        )

    db._upgrade_sqlite_media_columns(engine)

    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("media_assets")
    }
    assert columns["storage_backend"]["nullable"] is False
    assert {"storage_bucket", "storage_key"} <= columns.keys()


def test_sqlite_upgrade_rebuilds_legacy_affiliate_event_integrity(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/legacy-affiliate.db")
    with engine.begin() as connection:
        connection.execute(text("create table users (id integer primary key)"))
        connection.execute(text("insert into users (id) values (1), (2)"))
        connection.execute(
            text(
                """
                create table affiliate_events (
                    id integer primary key,
                    user_id integer not null,
                    social_post_id integer,
                    affiliate_product_id integer,
                    social_account_id integer,
                    publish_job_id integer,
                    event_type varchar(24) not null,
                    amount float not null,
                    currency varchar(8) not null,
                    source varchar(64) not null,
                    external_event_id varchar(255),
                    metadata_json json not null,
                    occurred_at datetime not null,
                    created_at datetime not null,
                    constraint uq_affiliate_event_source_external
                        unique (source, external_event_id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                insert into affiliate_events (
                    id, user_id, event_type, amount, currency, source,
                    external_event_id, metadata_json, occurred_at, created_at
                ) values (
                    1, 1, 'commission', 12.5, 'VND', 'network-a',
                    'shared-order', '{}', '2026-07-28T01:00:00',
                    '2026-07-28T01:00:00'
                )
                """
            )
        )

    db._upgrade_sqlite_affiliate_event_integrity(engine)
    db._upgrade_sqlite_affiliate_event_integrity(engine)

    inspector = inspect(engine)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("affiliate_events")
    }
    unique_columns = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("affiliate_events")
    }
    assert columns["amount"]["type"].__class__.__name__ == "NUMERIC"
    assert ("user_id", "source", "external_event_id") in unique_columns
    assert {index["name"] for index in inspector.get_indexes("affiliate_events")} >= {
        "ix_affiliate_events_user_id",
        "ix_affiliate_events_user_occurred",
        "ix_affiliate_events_type_occurred",
        "ix_affiliate_events_post_type",
    }

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                insert into affiliate_events (
                    id, user_id, event_type, amount, currency, source,
                    external_event_id, metadata_json, occurred_at, created_at
                ) values (
                    2, 2, 'commission', 12.5, 'VND', 'network-a',
                    'shared-order', '{}', '2026-07-28T01:00:00',
                    '2026-07-28T01:00:00'
                )
                """
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                """
                insert into affiliate_events (
                    id, user_id, event_type, amount, currency, source,
                    external_event_id, metadata_json, occurred_at, created_at
                ) values (
                    3, 1, 'commission', 12.5, 'VND', 'network-a',
                    'shared-order', '{}', '2026-07-28T01:00:00',
                    '2026-07-28T01:00:00'
                )
                """
            )
        )


def test_supabase_migrations_cover_schema_rls_and_private_buckets():
    root = Path(__file__).resolve().parents[1]
    core = (
        root / "supabase/migrations/20260727000100_arya_core.sql"
    ).read_text()
    security = (
        root / "supabase/migrations/20260727000200_auth_rls_storage.sql"
    ).read_text()
    artifacts = (
        root / "supabase/migrations/20260728000100_artifact_metadata.sql"
    ).read_text()
    affiliate_integrity = (
        root / "supabase/migrations/20260728000200_affiliate_event_integrity.sql"
    ).read_text()

    assert core.count("create table public.") == 17
    assert security.count("enable row level security") == 17
    assert "'arya-media'" in security
    assert "'arya-artifacts'" in security
    assert "false,\n        52428800" in security
    assert "storage.foldername(name)" in security
    assert "create table public.artifacts" in artifacts
    assert "alter table public.artifacts enable row level security" in artifacts
    assert "create policy artifacts_owner_all" in artifacts
    assert "storage_bucket = 'arya-artifacts'" in artifacts
    assert "ck_artifact_owner_key" in artifacts
    assert "drop constraint if exists uq_affiliate_event_source_external" in (
        affiliate_integrity
    )
    assert "unique (user_id, source, external_event_id)" in affiliate_integrity


class _FakeInspector:
    def __init__(self, *, revision_ready: bool = True):
        self.revision_ready = revision_ready

    def get_table_names(self, schema=None):
        if schema == "public":
            return list(db.Base.metadata.tables)
        if schema == "supabase_migrations" and self.revision_ready:
            return ["schema_migrations"]
        return []

    def get_columns(self, table_name, schema=None):
        del schema
        return [
            {"name": column.name}
            for column in db.Base.metadata.tables[table_name].columns
        ]


class _FakeConnection:
    def __init__(self, revision):
        self.revision = revision

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def scalar(self, _statement):
        return self.revision


class _FakePostgresEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, revision):
        self.revision = revision

    def connect(self):
        return _FakeConnection(self.revision)


def test_postgres_schema_health_requires_tables_columns_and_revision(monkeypatch):
    engine = _FakePostgresEngine(db.REQUIRED_SUPABASE_REVISION)
    monkeypatch.setattr(db, "inspect", lambda _engine: _FakeInspector())

    health = db.check_postgres_schema(engine)

    assert health.ready
    assert health.current_revision == db.REQUIRED_SUPABASE_REVISION
    assert not health.missing_tables
    assert not health.missing_columns


def test_postgres_schema_health_fails_closed_without_revision(monkeypatch):
    engine = _FakePostgresEngine(None)
    monkeypatch.setattr(
        db,
        "inspect",
        lambda _engine: _FakeInspector(revision_ready=False),
    )

    health = db.check_postgres_schema(engine)

    assert not health.ready
    with pytest.raises(RuntimeError, match="revision is unavailable"):
        db._verify_postgres_schema(engine)


def test_postgres_schema_health_rejects_malformed_revision(monkeypatch):
    engine = _FakePostgresEngine("newest")
    monkeypatch.setattr(db, "inspect", lambda _engine: _FakeInspector())

    health = db.check_postgres_schema(engine)

    assert health.current_revision is None
    assert not health.ready


def test_cutover_checklist_is_safe_complete_and_never_overwritten(tmp_path):
    generated_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    rendered = render_cutover_checklist(
        environment="staging",
        generated_at=generated_at,
    )

    assert "dry-run" in rendered
    assert "write-freeze" in rendered
    assert "second time" in rendered
    assert "staging rollback drill" in rendered
    assert "Freeze writes before rollback" in rendered
    assert "do not reset, unlink, or delete" in rendered
    assert db.REQUIRED_SUPABASE_REVISION in rendered
    assert "password" not in rendered.lower()

    destination = tmp_path / "cutover.md"
    write_cutover_checklist(
        destination,
        environment="staging",
        generated_at=generated_at,
    )
    with pytest.raises(RuntimeError, match="already exists"):
        write_cutover_checklist(
            destination,
            environment="production",
            generated_at=generated_at,
        )
    assert "Environment: `staging`" in destination.read_text()


@pytest.mark.skipif(
    os.environ.get("LAPLACE_RUN_POSTGRES_INTEGRATION") != "1",
    reason="Postgres integration is explicitly opt-in",
)
def test_opt_in_postgres_schema_health():
    target_url = os.environ.get("LAPLACE_TEST_POSTGRES_URL")
    if not target_url:
        pytest.fail(
            "Set LAPLACE_TEST_POSTGRES_URL when enabling Postgres integration"
        )
    engine = create_engine(db._normalize_db_url(target_url), pool_pre_ping=True)
    try:
        assert db.check_postgres_schema(engine).ready
    finally:
        engine.dispose()
