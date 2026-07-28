"""Database and migration scaffolding tests for the Supabase cutover."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from laplace import db
from laplace.migrations.supabase_readiness import (
    render_cutover_checklist,
    write_cutover_checklist,
)
from laplace.services.artifacts import Artifact  # noqa: F401

ARYA_TABLES = (
    "users",
    "conversations",
    "messages",
    "tasks",
    "steps",
    "llm_calls",
    "scheduled_jobs",
    "notes",
    "todos",
    "social_accounts",
    "media_assets",
    "affiliate_products",
    "social_posts",
    "content_generations",
    "publish_jobs",
    "publish_attempts",
    "affiliate_events",
    "artifacts",
)
EXPECTED_SUPABASE_REVISIONS = (
    "20260727000100",
    "20260727000200",
    "20260728000100",
    "20260728000200",
)
EXPECTED_PUBLIC_POLICIES = {
    ("users", "users_select_own"): "SELECT",
    ("users", "users_update_own"): "UPDATE",
    ("conversations", "conversations_owner_all"): "ALL",
    ("messages", "messages_owner_all"): "ALL",
    ("tasks", "tasks_owner_all"): "ALL",
    ("steps", "steps_owner_all"): "ALL",
    ("llm_calls", "llm_calls_owner_all"): "ALL",
    ("scheduled_jobs", "scheduled_jobs_owner_all"): "ALL",
    ("notes", "notes_owner_all"): "ALL",
    ("todos", "todos_owner_all"): "ALL",
    ("social_accounts", "social_accounts_owner_all"): "ALL",
    ("media_assets", "media_assets_owner_all"): "ALL",
    ("affiliate_products", "affiliate_products_owner_all"): "ALL",
    ("social_posts", "social_posts_owner_all"): "ALL",
    ("content_generations", "content_generations_owner_all"): "ALL",
    ("publish_jobs", "publish_jobs_owner_all"): "ALL",
    ("publish_attempts", "publish_attempts_owner_all"): "ALL",
    ("affiliate_events", "affiliate_events_owner_all"): "ALL",
    ("artifacts", "artifacts_owner_all"): "ALL",
}
EXPECTED_STORAGE_POLICIES = {
    "arya_private_objects_select": "SELECT",
    "arya_private_objects_insert": "INSERT",
    "arya_private_objects_update": "UPDATE",
    "arya_private_objects_delete": "DELETE",
}
EXPECTED_BUSINESS_CONSTRAINTS = {
    "uq_social_account_external_id",
    "ck_social_account_daily_limit",
    "ck_social_account_cooldown",
    "uq_media_asset_user_sha256",
    "ck_media_asset_size",
    "ck_media_asset_duration",
    "ck_media_asset_storage",
    "ck_media_asset_owner_storage",
    "uq_content_generation_post",
    "ck_content_generation_retries",
    "ck_content_generation_prompt_tokens",
    "ck_content_generation_completion_tokens",
    "ck_content_generation_latency",
    "ck_content_generation_cost",
    "uq_publish_job_idempotency_key",
    "ck_publish_job_attempt_count",
    "uq_publish_attempt_number",
    "ck_publish_attempt_number",
    "ck_publish_attempt_latency",
    "ck_affiliate_event_type",
    "ck_affiliate_event_amount",
    "uq_affiliate_event_owner_source_external",
    "uq_artifact_storage_object",
    "ck_artifact_size",
    "ck_artifact_sha256",
    "ck_artifact_status",
    "ck_artifact_safe_name",
    "ck_artifact_owner_key",
}
EXPECTED_EXPLICIT_INDEXES = {
    "ix_conversations_user_id",
    "ix_messages_conversation_id",
    "ix_tasks_user_id",
    "ix_tasks_conversation_id",
    "ix_steps_task_id",
    "ix_llm_calls_task_id",
    "ix_llm_calls_step_id",
    "ix_scheduled_jobs_user_id",
    "ix_notes_user_id",
    "ix_todos_user_id",
    "ix_social_accounts_user_id",
    "ix_social_accounts_user_status",
    "ix_media_assets_user_id",
    "ix_affiliate_products_user_id",
    "ix_affiliate_products_user_status",
    "ix_social_posts_user_id",
    "ix_social_posts_media_asset_id",
    "ix_social_posts_affiliate_product_id",
    "ix_social_posts_user_status",
    "ix_social_posts_content_hash",
    "ix_content_generations_social_post_id",
    "ix_publish_jobs_social_post_id",
    "ix_publish_jobs_social_account_id",
    "ix_publish_jobs_due",
    "ix_publish_jobs_account_status",
    "ix_publish_attempts_publish_job_id",
    "ix_affiliate_events_user_id",
    "ix_affiliate_events_social_post_id",
    "ix_affiliate_events_product_id",
    "ix_affiliate_events_account_id",
    "ix_affiliate_events_job_id",
    "ix_affiliate_events_user_occurred",
    "ix_affiliate_events_type_occurred",
    "ix_affiliate_events_post_type",
    "ix_artifacts_user_id",
    "ix_artifacts_user_created",
    "ix_artifacts_user_kind",
}


def _normalized_sql(value: str) -> str:
    return " ".join(value.lower().split())


def _has_obvious_policy_bypass(expression: str) -> bool:
    normalized = _normalized_sql(expression)
    return any(
        (
            re.fullmatch(r"\(*\s*true\s*\)*", normalized) is not None,
            re.search(r"\bor\s+\(*\s*true\b", normalized) is not None,
            re.search(r"\btrue\s*\)*\s+or\b", normalized) is not None,
            re.search(r"(?<!\d)1\s*=\s*1(?!\d)", normalized) is not None,
            "current_app_user_id() is null" in normalized,
        )
    )


def _policy_sql(sql: str, policy_name: str, table_name: str) -> str:
    normalized = _normalized_sql(sql)
    marker = f"create policy {policy_name} on {table_name}"
    start = normalized.index(marker)
    end = normalized.index(";", start)
    return normalized[start:end]


def _policy_with_check(sql: str, policy_name: str, table_name: str) -> str:
    return _policy_clauses(sql, policy_name, table_name)["with_check"]


def _parenthesized_expression(value: str, start: int) -> str:
    opening = value.index("(", start)
    depth = 0
    quoted = False
    index = opening
    while index < len(value):
        character = value[index]
        if character == "'":
            if quoted and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    return value[opening + 1:index]
        index += 1
    raise ValueError("Policy clause has unbalanced parentheses")


def _policy_clauses(
    sql: str,
    policy_name: str,
    table_name: str,
) -> dict[str, str]:
    policy = _policy_sql(sql, policy_name, table_name)
    clauses = {}
    for clause_name, marker in (
        ("using", " using "),
        ("with_check", " with check "),
    ):
        start = policy.find(marker)
        if start >= 0:
            clauses[clause_name] = _parenthesized_expression(
                policy,
                start + len(marker),
            )
    return clauses


def _constraint_sql(sql: str, constraint_name: str) -> str:
    normalized = _normalized_sql(sql)
    marker = f"constraint {constraint_name}"
    start = normalized.index(marker)
    end = normalized.index(";", start)
    return normalized[start:end]


def _assert_private_storage_policy_checks(security: str) -> None:
    expected_clauses = {
        "arya_private_objects_select": {"using"},
        "arya_private_objects_insert": {"with_check"},
        "arya_private_objects_update": {"using", "with_check"},
        "arya_private_objects_delete": {"using"},
    }
    required_fragments = (
        "bucket_id in ('arya-media', 'arya-artifacts')",
        "storage.foldername(name)",
        "[1] = 'users'",
        "[2] = public.current_app_user_id()::text",
        "name !~ '(^|/)[.]{1,2}(/|$)'",
        "bucket_id = 'arya-media'",
        "/[0-9a-f]{2}/[0-9a-f]{64}[.](jpg|jpeg|png|webp|mp4)$",
        "bucket_id = 'arya-artifacts'",
        "/[0-9a-f]{2}/[0-9a-f]{64}-",
        "[a-z0-9][a-z0-9._-]{0,119}$",
        "split_part(name, '/', 3) = left(split_part(name, '/', 4), 2)",
    )
    for policy_name, clause_names in expected_clauses.items():
        clauses = _policy_clauses(
            security,
            policy_name,
            "storage.objects",
        )
        assert set(clauses) == clause_names
        for clause in clauses.values():
            for fragment in required_fragments:
                assert fragment in clause


def _assert_cross_owner_policy_checks(security: str) -> None:
    tasks = _policy_with_check(security, "tasks_owner_all", "public.tasks")
    for fragment in (
        "conversation_id is null",
        "from public.conversations as c",
        "c.id = tasks.conversation_id",
        "c.user_id = public.current_app_user_id()",
    ):
        assert fragment in tasks

    llm_calls = _policy_with_check(
        security,
        "llm_calls_owner_all",
        "public.llm_calls",
    )
    for fragment in (
        "task_id is not null or step_id is not null",
        "task_id is null or exists",
        "step_id is null or exists",
        "s.id = llm_calls.step_id",
        "s.task_id = llm_calls.task_id",
    ):
        assert fragment in llm_calls
    assert llm_calls.count("user_id = public.current_app_user_id()") >= 2

    social_posts = _policy_with_check(
        security,
        "social_posts_owner_all",
        "public.social_posts",
    )
    for fragment in (
        "from public.media_assets as m",
        "m.id = social_posts.media_asset_id",
        "m.user_id = public.current_app_user_id()",
        "affiliate_product_id is null",
        "from public.affiliate_products as a",
        "a.id = social_posts.affiliate_product_id",
        "a.user_id = public.current_app_user_id()",
    ):
        assert fragment in social_posts

    affiliate_events = _policy_with_check(
        security,
        "affiliate_events_owner_all",
        "public.affiliate_events",
    )
    for column_name, table_name in (
        ("social_post_id", "public.social_posts"),
        ("affiliate_product_id", "public.affiliate_products"),
        ("social_account_id", "public.social_accounts"),
        ("publish_job_id", "public.publish_jobs"),
    ):
        assert f"{column_name} is null" in affiliate_events
        assert f"from {table_name}" in affiliate_events
    assert "join public.social_posts" in affiliate_events
    assert affiliate_events.count("user_id = public.current_app_user_id()") >= 5

    media_assets = _policy_with_check(
        security,
        "media_assets_owner_all",
        "public.media_assets",
    )
    for fragment in (
        "storage_backend = 'supabase'",
        "storage_bucket = 'arya-media'",
        "sha256 ~ '^[0-9a-f]{64}$'",
        "mime_type in",
        "storage_key !~ '(^|/)[.]{1,2}(/|$)'",
        "'users/' || public.current_app_user_id()::text || '/'",
        "left(sha256, 2) || '/' || sha256",
        "when 'image/jpeg' then '.jpg'",
        "mime_type = 'image/jpeg'",
        "|| sha256 || '.jpeg'",
    ):
        assert fragment in media_assets
    media_policy = _policy_sql(
        security,
        "media_assets_owner_all",
        "public.media_assets",
    )
    assert "storage_backend = 'local'" not in media_policy
    assert media_policy.count("storage_backend = 'supabase'") == 2
    assert media_policy.count("storage_key !~ '(^|/)[.]{1,2}(/|$)'") == 2
    assert media_policy.count("when 'image/jpeg' then '.jpg'") == 2
    assert media_policy.count("|| sha256 || '.jpeg'") == 2
    assert ".gif" not in media_policy


def test_obvious_policy_bypass_detection():
    for expression in (
        "true",
        "(true)",
        "user_id = current_app_user_id() or true",
        "true or user_id = current_app_user_id()",
        "user_id = current_app_user_id() or (true)",
        "1=1",
        "user_id = current_app_user_id() or 1 = 1",
        "public.current_app_user_id() is null",
    ):
        assert _has_obvious_policy_bypass(expression)
    assert not _has_obvious_policy_bypass(
        "user_id = public.current_app_user_id()"
    )


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


def test_supabase_security_migration_enforces_cross_owner_integrity():
    root = Path(__file__).resolve().parents[1]
    security = (
        root / "supabase/migrations/20260727000200_auth_rls_storage.sql"
    ).read_text()
    normalized = _normalized_sql(security)

    broad_grant = normalized.index(
        "grant select, insert, update, delete on all tables in schema public "
        "to authenticated"
    )
    users_revoke = normalized.index(
        "revoke insert, update, delete on public.users from authenticated"
    )
    users_sequence_revoke = normalized.index(
        "revoke all on sequence public.users_id_seq from authenticated"
    )
    profile_grant = normalized.index(
        "grant update (profile_json) on public.users to authenticated"
    )
    assert broad_grant < users_revoke < users_sequence_revoke < profile_grant

    users_update = _policy_sql(
        security,
        "users_update_own",
        "public.users",
    )
    assert users_update.count("auth_user_id = auth.uid()") == 2

    media_constraint = _constraint_sql(
        security,
        "ck_media_asset_owner_storage",
    )
    for fragment in (
        "storage_backend = 'local'",
        "storage_backend = 'supabase'",
        "storage_bucket = 'arya-media'",
        "sha256 ~ '^[0-9a-f]{64}$'",
        "mime_type in",
        "storage_key !~ '(^|/)[.]{1,2}(/|$)'",
        "'users/' || user_id::text || '/' || left(sha256, 2) || '/'",
        "|| sha256 || case mime_type",
        "when 'image/jpeg' then '.jpg'",
        "mime_type = 'image/jpeg'",
        "|| sha256 || '.jpeg'",
    ):
        assert fragment in media_constraint
    assert ".gif" not in media_constraint

    _assert_cross_owner_policy_checks(security)
    _assert_private_storage_policy_checks(security)


def test_artifact_metadata_rejects_owner_path_traversal():
    root = Path(__file__).resolve().parents[1]
    artifacts = (
        root / "supabase/migrations/20260728000100_artifact_metadata.sql"
    ).read_text()
    normalized = _normalized_sql(artifacts)

    anon_table_revoke = normalized.index(
        "revoke all on public.artifacts from anon"
    )
    anon_sequence_revoke = normalized.index(
        "revoke all on sequence public.artifacts_id_seq from anon"
    )
    authenticated_table_grant = normalized.index(
        "grant select, insert, update, delete on public.artifacts to authenticated"
    )
    authenticated_sequence_grant = normalized.index(
        "grant usage, select on sequence public.artifacts_id_seq to authenticated"
    )
    assert max(anon_table_revoke, anon_sequence_revoke) < min(
        authenticated_table_grant,
        authenticated_sequence_grant,
    )

    safe_name_constraint = _constraint_sql(
        artifacts,
        "ck_artifact_safe_name",
    )
    for fragment in (
        "char_length(original_name) between 1 and 120",
        "original_name ~ '^[a-z0-9][a-z0-9._-]*$'",
        "right(original_name, 1) ~ '^[a-z0-9]$'",
    ):
        assert fragment in safe_name_constraint

    artifact_constraint = _constraint_sql(artifacts, "ck_artifact_owner_key")
    for fragment in (
        "storage_bucket = 'arya-artifacts'",
        "storage_key !~ '(^|/)[.]{1,2}(/|$)'",
        "storage_key = ( 'users/' || user_id::text || '/' || left(sha256, 2)",
        "|| '/' || sha256 || '-' || original_name",
    ):
        assert fragment in artifact_constraint

    artifact_policy = _policy_with_check(
        artifacts,
        "artifacts_owner_all",
        "public.artifacts",
    )
    for fragment in (
        "storage_bucket = 'arya-artifacts'",
        "sha256 ~ '^[0-9a-f]{64}$'",
        "char_length(original_name) between 1 and 120",
        "original_name ~ '^[a-z0-9][a-z0-9._-]*$'",
        "right(original_name, 1) ~ '^[a-z0-9]$'",
        "storage_key !~ '(^|/)[.]{1,2}(/|$)'",
        "storage_key = ( 'users/' || public.current_app_user_id()::text || '/'",
        "|| left(sha256, 2) || '/' || sha256 || '-' || original_name",
    ):
        assert fragment in artifact_policy
    full_artifact_policy = _policy_sql(
        artifacts,
        "artifacts_owner_all",
        "public.artifacts",
    )
    assert full_artifact_policy.count("storage_bucket = 'arya-artifacts'") == 2
    assert full_artifact_policy.count(
        "storage_key !~ '(^|/)[.]{1,2}(/|$)'"
    ) == 2
    assert full_artifact_policy.count(
        "char_length(original_name) between 1 and 120"
    ) == 2
    assert full_artifact_policy.count(
        "storage_key = ( 'users/' || public.current_app_user_id()::text || '/'"
    ) == 2


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


def _postgres_integration_engine():
    target_url = os.environ.get("LAPLACE_TEST_POSTGRES_URL")
    if not target_url:
        pytest.fail(
            "Set LAPLACE_TEST_POSTGRES_URL when enabling Postgres integration"
        )
    return create_engine(
        db._normalize_db_url(target_url),
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "options": (
                "-c default_transaction_read_only=on "
                "-c statement_timeout=15000"
            )
        },
    )


def _assert_postgres_security_metadata(connection) -> None:
    revisions = tuple(
        connection.scalars(
            text(
                "select version::text "
                "from supabase_migrations.schema_migrations "
                "order by version"
            )
        )
    )
    if revisions != EXPECTED_SUPABASE_REVISIONS:
        pytest.fail("Postgres migration history is not the expected four revisions")

    rls_rows = connection.execute(
        text(
            """
            select c.relname, c.relrowsecurity
            from pg_class as c
            join pg_namespace as n on n.oid = c.relnamespace
            where n.nspname = 'public'
              and c.relname = any(:table_names)
              and c.relkind in ('r', 'p')
            """
        ),
        {"table_names": list(ARYA_TABLES)},
    )
    rls = {row.relname: row.relrowsecurity for row in rls_rows}
    if set(rls) != set(ARYA_TABLES) or not all(rls.values()):
        pytest.fail("Not all 18 Arya Postgres tables have RLS enabled")

    anon_table_privileges = tuple(
        connection.scalars(
            text(
                """
                select c.relname
                from pg_class as c
                join pg_namespace as n on n.oid = c.relnamespace
                where n.nspname = 'public'
                  and c.relname = any(:table_names)
                  and c.relkind in ('r', 'p')
                  and (
                    has_table_privilege('anon', c.oid, 'SELECT')
                    or has_table_privilege('anon', c.oid, 'INSERT')
                    or has_table_privilege('anon', c.oid, 'UPDATE')
                    or has_table_privilege('anon', c.oid, 'DELETE')
                    or has_table_privilege('anon', c.oid, 'TRUNCATE')
                    or has_table_privilege('anon', c.oid, 'REFERENCES')
                    or has_table_privilege('anon', c.oid, 'TRIGGER')
                  )
                order by c.relname
                """
            ),
            {"table_names": list(ARYA_TABLES)},
        )
    )
    if anon_table_privileges:
        pytest.fail("Anon retains effective privileges on Arya public tables")

    anon_sequence_privileges = tuple(
        connection.scalars(
            text(
                """
                select sequence_class.relname
                from pg_class as sequence_class
                join pg_namespace as sequence_namespace
                  on sequence_namespace.oid = sequence_class.relnamespace
                join pg_depend as dependency
                  on dependency.classid = 'pg_class'::regclass
                 and dependency.objid = sequence_class.oid
                 and dependency.deptype in ('a', 'i')
                join pg_class as table_class
                  on table_class.oid = dependency.refobjid
                join pg_namespace as table_namespace
                  on table_namespace.oid = table_class.relnamespace
                where sequence_class.relkind = 'S'
                  and sequence_namespace.nspname = 'public'
                  and table_namespace.nspname = 'public'
                  and table_class.relname = any(:table_names)
                  and (
                    has_sequence_privilege(
                      'anon', sequence_class.oid, 'USAGE'
                    )
                    or has_sequence_privilege(
                      'anon', sequence_class.oid, 'SELECT'
                    )
                    or has_sequence_privilege(
                      'anon', sequence_class.oid, 'UPDATE'
                    )
                  )
                order by sequence_class.relname
                """
            ),
            {"table_names": list(ARYA_TABLES)},
        )
    )
    if anon_sequence_privileges:
        pytest.fail("Anon retains effective privileges on Arya public sequences")

    policy_rows = connection.execute(
        text(
            """
            select schemaname, tablename, policyname, cmd, roles, qual, with_check
            from pg_policies
            where (
                schemaname = 'public'
                and tablename = any(:table_names)
            ) or (
                schemaname = 'storage'
                and tablename = 'objects'
            )
            """
        ),
        {"table_names": list(ARYA_TABLES)},
    )
    public_policies = {}
    storage_policies = {}
    for row in policy_rows:
        policy = {
            "command": row.cmd,
            "roles": set(row.roles),
            "using": _normalized_sql(row.qual) if row.qual is not None else None,
            "with_check": (
                _normalized_sql(row.with_check)
                if row.with_check is not None
                else None
            ),
            "has_using": row.qual is not None,
            "has_check": row.with_check is not None,
        }
        if row.schemaname == "public":
            public_policies[(row.tablename, row.policyname)] = policy
        else:
            storage_policies[row.policyname] = policy

    if set(public_policies) != set(EXPECTED_PUBLIC_POLICIES):
        pytest.fail("Postgres public owner-policy inventory differs from migrations")
    if set(storage_policies) != set(EXPECTED_STORAGE_POLICIES):
        pytest.fail("Postgres Storage policy inventory differs from migrations")

    for policy_inventory in (public_policies, storage_policies):
        for policy in policy_inventory.values():
            for clause_name in ("using", "with_check"):
                expression = policy[clause_name]
                if (
                    expression is not None
                    and _has_obvious_policy_bypass(expression)
                ):
                    pytest.fail("A policy clause contains an obvious fail-open bypass")

    for key, expected_command in EXPECTED_PUBLIC_POLICIES.items():
        policy = public_policies[key]
        if policy["command"] != expected_command:
            pytest.fail("A Postgres public owner policy has the wrong command")
        if policy["roles"] != {"authenticated"} or not policy["has_using"]:
            pytest.fail("A Postgres public owner policy has unsafe role or USING metadata")
        expected_check = key != ("users", "users_select_own")
        if policy["has_check"] is not expected_check:
            pytest.fail("A Postgres public owner policy has unsafe WITH CHECK metadata")

    for policy_name, expected_command in EXPECTED_STORAGE_POLICIES.items():
        policy = storage_policies[policy_name]
        if policy["command"] != expected_command:
            pytest.fail("A Postgres Storage policy has the wrong command")
        if policy["roles"] != {"authenticated"}:
            pytest.fail("A Postgres Storage policy has the wrong role")
    if storage_policies["arya_private_objects_select"]["has_check"]:
        pytest.fail("Storage SELECT policy unexpectedly has WITH CHECK")
    if storage_policies["arya_private_objects_insert"]["has_using"]:
        pytest.fail("Storage INSERT policy unexpectedly has USING")
    if not all(
        storage_policies[name]["has_using"]
        for name in (
            "arya_private_objects_select",
            "arya_private_objects_update",
            "arya_private_objects_delete",
        )
    ):
        pytest.fail("A Postgres Storage policy is missing USING")
    if not all(
        storage_policies[name]["has_check"]
        for name in (
            "arya_private_objects_insert",
            "arya_private_objects_update",
        )
    ):
        pytest.fail("A Postgres Storage policy is missing WITH CHECK")

    owner_equality = ("user_id =", "current_app_user_id()")
    critical_public_tokens = {
        ("users", "users_select_own"): {
            "using": ("id =", "current_app_user_id()"),
        },
        ("users", "users_update_own"): {
            "using": (
                "id =",
                "current_app_user_id()",
                "auth_user_id =",
                "auth.uid()",
            ),
            "with_check": (
                "id =",
                "current_app_user_id()",
                "auth_user_id =",
                "auth.uid()",
            ),
        },
        ("messages", "messages_owner_all"): {
            "using": (
                "conversations",
                "c.id =",
                "conversation_id",
                "c.user_id =",
                "current_app_user_id()",
            ),
            "with_check": (
                "conversations",
                "c.id =",
                "conversation_id",
                "c.user_id =",
                "current_app_user_id()",
            ),
        },
        ("tasks", "tasks_owner_all"): {
            "using": owner_equality,
            "with_check": (
                "user_id =",
                "current_app_user_id()",
                "conversation_id is null",
                "conversations",
                "c.id =",
                "c.user_id =",
            ),
        },
        ("steps", "steps_owner_all"): {
            "using": (
                "tasks",
                "t.id =",
                "task_id",
                "t.user_id =",
                "current_app_user_id()",
            ),
            "with_check": (
                "tasks",
                "t.id =",
                "task_id",
                "t.user_id =",
                "current_app_user_id()",
            ),
        },
        ("llm_calls", "llm_calls_owner_all"): {
            "using": (
                "current_app_user_id()",
                "task_id is not null",
                "step_id is not null",
                "tasks",
                "steps",
                "t.user_id =",
                "s.id =",
                "s.task_id =",
            ),
            "with_check": (
                "current_app_user_id()",
                "task_id is not null",
                "step_id is not null",
                "tasks",
                "steps",
                "t.user_id =",
                "s.id =",
                "s.task_id =",
            ),
        },
        ("media_assets", "media_assets_owner_all"): {
            "using": (
                "user_id =",
                "current_app_user_id()",
                "storage_backend =",
                "supabase",
                "arya-media",
                "sha256",
                "storage_key =",
                "image/jpeg",
                ".jpg",
                ".jpeg",
                "[.]{1,2}",
            ),
            "with_check": (
                "user_id =",
                "current_app_user_id()",
                "storage_backend =",
                "supabase",
                "arya-media",
                "sha256",
                "storage_key =",
                "image/jpeg",
                ".jpg",
                ".jpeg",
                "[.]{1,2}",
            ),
        },
        ("social_posts", "social_posts_owner_all"): {
            "using": owner_equality,
            "with_check": (
                "user_id =",
                "current_app_user_id()",
                "media_asset_id",
                "media_assets",
                "m.id =",
                "m.user_id =",
                "affiliate_product_id",
                "affiliate_products",
                "a.id =",
                "a.user_id =",
            ),
        },
        ("content_generations", "content_generations_owner_all"): {
            "using": (
                "social_posts",
                "p.id =",
                "social_post_id",
                "p.user_id =",
                "current_app_user_id()",
            ),
            "with_check": (
                "social_posts",
                "p.id =",
                "social_post_id",
                "p.user_id =",
                "current_app_user_id()",
            ),
        },
        ("publish_jobs", "publish_jobs_owner_all"): {
            "using": (
                "social_posts",
                "p.id =",
                "social_post_id",
                "p.user_id =",
                "current_app_user_id()",
            ),
            "with_check": (
                "social_posts",
                "social_accounts",
                "p.id =",
                "social_post_id",
                "a.id =",
                "social_account_id",
                "p.user_id =",
                "a.user_id =",
                "current_app_user_id()",
            ),
        },
        ("publish_attempts", "publish_attempts_owner_all"): {
            "using": (
                "publish_jobs",
                "social_posts",
                "j.id =",
                "publish_job_id",
                "p.user_id =",
                "current_app_user_id()",
            ),
            "with_check": (
                "publish_jobs",
                "social_posts",
                "j.id =",
                "publish_job_id",
                "p.user_id =",
                "current_app_user_id()",
            ),
        },
        ("affiliate_events", "affiliate_events_owner_all"): {
            "using": owner_equality,
            "with_check": (
                "user_id =",
                "current_app_user_id()",
                "social_post_id is null",
                "social_posts",
                "affiliate_product_id is null",
                "affiliate_products",
                "social_account_id is null",
                "social_accounts",
                "publish_job_id is null",
                "publish_jobs",
                "p.id =",
                "a.id =",
                "j.id =",
            ),
        },
        ("artifacts", "artifacts_owner_all"): {
            "using": (
                "user_id =",
                "current_app_user_id()",
                "arya-artifacts",
                "sha256",
                "original_name",
                "storage_key =",
                "[.]{1,2}",
            ),
            "with_check": (
                "user_id =",
                "current_app_user_id()",
                "arya-artifacts",
                "sha256",
                "original_name",
                "storage_key =",
                "[.]{1,2}",
            ),
        },
    }
    for key in (
        ("conversations", "conversations_owner_all"),
        ("scheduled_jobs", "scheduled_jobs_owner_all"),
        ("notes", "notes_owner_all"),
        ("todos", "todos_owner_all"),
        ("social_accounts", "social_accounts_owner_all"),
        ("affiliate_products", "affiliate_products_owner_all"),
    ):
        critical_public_tokens[key] = {
            "using": owner_equality,
            "with_check": owner_equality,
        }
    if set(critical_public_tokens) != set(EXPECTED_PUBLIC_POLICIES):
        pytest.fail("Critical public policy semantic coverage is incomplete")
    for key, clause_tokens in critical_public_tokens.items():
        policy = public_policies[key]
        for clause_name, required_tokens in clause_tokens.items():
            expression = policy[clause_name]
            if expression is None or any(
                token not in expression for token in required_tokens
            ):
                pytest.fail("A critical public policy has unsafe SQL semantics")

    expected_storage_clauses = {
        "arya_private_objects_select": ("using",),
        "arya_private_objects_insert": ("with_check",),
        "arya_private_objects_update": ("using", "with_check"),
        "arya_private_objects_delete": ("using",),
    }
    storage_clause_tokens = (
        "bucket_id",
        "arya-media",
        "arya-artifacts",
        "foldername",
        "current_app_user_id()",
        "[.]{1,2}",
        "jpg|jpeg|png|webp|mp4",
        "[a-z0-9][a-z0-9._-]{0,119}",
        "split_part",
    )
    for policy_name, clause_names in expected_storage_clauses.items():
        policy = storage_policies[policy_name]
        for clause_name in clause_names:
            expression = policy[clause_name]
            if expression is None or any(
                token not in expression for token in storage_clause_tokens
            ):
                pytest.fail("A Storage policy clause has unsafe SQL semantics")

    constraint_rows = connection.execute(
        text(
            """
            select co.conname, co.convalidated
            from pg_constraint as co
            join pg_class as c on c.oid = co.conrelid
            join pg_namespace as n on n.oid = c.relnamespace
            where n.nspname = 'public'
              and c.relname = any(:table_names)
            """
        ),
        {"table_names": list(ARYA_TABLES)},
    )
    constraints = {row.conname: row.convalidated for row in constraint_rows}
    if not EXPECTED_BUSINESS_CONSTRAINTS <= set(constraints):
        pytest.fail("Postgres is missing required Arya business constraints")
    if not all(constraints[name] for name in EXPECTED_BUSINESS_CONSTRAINTS):
        pytest.fail("A required Arya business constraint is not validated")
    if "uq_affiliate_event_source_external" in constraints:
        pytest.fail("Obsolete cross-owner affiliate constraint remains in Postgres")

    index_rows = connection.execute(
        text(
            """
            select i.relname, x.indisvalid, x.indisready
            from pg_index as x
            join pg_class as i on i.oid = x.indexrelid
            join pg_class as t on t.oid = x.indrelid
            join pg_namespace as n on n.oid = t.relnamespace
            where n.nspname = 'public'
              and t.relname = any(:table_names)
            """
        ),
        {"table_names": list(ARYA_TABLES)},
    )
    indexes = {
        row.relname: row.indisvalid and row.indisready for row in index_rows
    }
    if not EXPECTED_EXPLICIT_INDEXES <= set(indexes):
        pytest.fail("Postgres is missing required Arya indexes")
    if not all(indexes[name] for name in EXPECTED_EXPLICIT_INDEXES):
        pytest.fail("A required Arya index is not valid and ready")

    trigger_rows = connection.execute(
        text(
            """
            select t.tgenabled, p.proname, p.prosecdef
            from pg_trigger as t
            join pg_class as c on c.oid = t.tgrelid
            join pg_namespace as n on n.oid = c.relnamespace
            join pg_proc as p on p.oid = t.tgfoid
            where n.nspname = 'auth'
              and c.relname = 'users'
              and t.tgname = 'on_auth_user_created'
              and not t.tgisinternal
            """
        )
    ).all()
    if len(trigger_rows) != 1:
        pytest.fail("Supabase Auth owner-mapping trigger is missing or duplicated")
    if (
        trigger_rows[0].tgenabled == "D"
        or trigger_rows[0].proname != "handle_new_auth_user"
        or not trigger_rows[0].prosecdef
    ):
        pytest.fail("Supabase Auth owner-mapping trigger is disabled or incorrect")

    bucket_rows = connection.execute(
        text(
            """
            select id, public
            from storage.buckets
            where id in ('arya-media', 'arya-artifacts')
            """
        )
    )
    buckets = {row.id: row.public for row in bucket_rows}
    if buckets != {"arya-media": False, "arya-artifacts": False}:
        pytest.fail("Required Arya Storage buckets are missing or public")

    user_table_privileges = connection.execute(
        text(
            """
            select
              has_table_privilege(
                'authenticated', 'public.users', 'INSERT'
              ) as can_insert,
              has_table_privilege(
                'authenticated', 'public.users', 'UPDATE'
              ) as can_update_table,
              has_table_privilege(
                'authenticated', 'public.users', 'DELETE'
              ) as can_delete
            """
        )
    ).one()
    if any(user_table_privileges):
        pytest.fail("Authenticated retains unsafe table-level user mutation privileges")

    update_columns = set(
        connection.scalars(
            text(
                """
                select column_name
                from information_schema.column_privileges
                where table_schema = 'public'
                  and table_name = 'users'
                  and grantee = 'authenticated'
                  and privilege_type = 'UPDATE'
                """
            )
        )
    )
    if update_columns != {"profile_json"}:
        pytest.fail("Authenticated user UPDATE privileges are not profile-only")

    user_sequence_privileges = connection.execute(
        text(
            """
            select
              has_sequence_privilege(
                'authenticated', 'public.users_id_seq', 'USAGE'
              ) as can_use,
              has_sequence_privilege(
                'authenticated', 'public.users_id_seq', 'SELECT'
              ) as can_select,
              has_sequence_privilege(
                'authenticated', 'public.users_id_seq', 'UPDATE'
              ) as can_update
            """
        )
    ).one()
    if any(user_sequence_privileges):
        pytest.fail("Authenticated retains unsafe public.users sequence privileges")


@pytest.mark.skipif(
    os.environ.get("LAPLACE_RUN_POSTGRES_INTEGRATION") != "1",
    reason="Postgres integration is explicitly opt-in",
)
def test_opt_in_postgres_schema_health():
    engine = _postgres_integration_engine()
    try:
        assert db.check_postgres_schema(engine).ready
        with engine.connect() as connection:
            _assert_postgres_security_metadata(connection)
    finally:
        engine.dispose()


@pytest.mark.skipif(
    os.environ.get("LAPLACE_RUN_POSTGRES_RLS_INTEGRATION") != "1",
    reason="Authenticated-role Postgres RLS verification is explicitly opt-in",
)
def test_opt_in_authenticated_role_user_rls_isolation():
    claim_values = (
        os.environ.get("LAPLACE_TEST_AUTH_USER_A"),
        os.environ.get("LAPLACE_TEST_AUTH_USER_B"),
    )
    if not all(claim_values):
        pytest.fail(
            "Set two mapped Auth UUID variables when enabling authenticated-role RLS"
        )
    try:
        claims = tuple(str(UUID(value)) for value in claim_values if value is not None)
    except ValueError:
        pytest.fail("Authenticated-role RLS claim variables must be UUIDs")
    if len(set(claims)) != 2:
        pytest.fail("Authenticated-role RLS claims must identify two different users")

    engine = _postgres_integration_engine()
    try:
        with engine.connect() as connection, connection.begin():
            owner_rows = connection.execute(
                text(
                    """
                    select id, auth_user_id::text as auth_user_id
                    from public.users
                    where auth_user_id::text = any(:claims)
                    """
                ),
                {"claims": list(claims)},
            )
            owners = {row.auth_user_id: row.id for row in owner_rows}
            if set(owners) != set(claims):
                pytest.fail(
                    "Authenticated-role RLS claims must map to two existing app users"
                )

            connection.execute(text("set local role authenticated"))
            for claim in claims:
                connection.execute(
                    text(
                        "select set_config("
                        "'request.jwt.claim.sub', :claim, true"
                        ")"
                    ),
                    {"claim": claim},
                )
                resolved = connection.scalar(
                    text("select public.current_app_user_id()")
                )
                visible_ids = tuple(
                    connection.scalars(
                        text("select id from public.users order by id")
                    )
                )
                if resolved != owners[claim] or visible_ids != (owners[claim],):
                    pytest.fail(
                        "Authenticated Postgres role did not isolate public.users"
                    )
    finally:
        engine.dispose()
