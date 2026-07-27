"""Database and migration scaffolding tests for the Supabase cutover."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, inspect, text

from laplace import db


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


def test_supabase_migrations_cover_schema_rls_and_private_buckets():
    root = Path(__file__).resolve().parents[1]
    core = (
        root / "supabase/migrations/20260727000100_arya_core.sql"
    ).read_text()
    security = (
        root / "supabase/migrations/20260727000200_auth_rls_storage.sql"
    ).read_text()

    assert core.count("create table public.") == 17
    assert security.count("enable row level security") == 17
    assert "'arya-media'" in security
    assert "'arya-artifacts'" in security
    assert "false,\n        52428800" in security
    assert "storage.foldername(name)" in security

