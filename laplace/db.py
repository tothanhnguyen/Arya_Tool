from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from laplace.config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None
REQUIRED_SUPABASE_REVISION = "20260728000200"


@dataclass(frozen=True, slots=True)
class SchemaHealth:
    missing_tables: tuple[str, ...]
    missing_columns: tuple[str, ...]
    current_revision: str | None
    required_revision: str

    @property
    def ready(self) -> bool:
        return (
            not self.missing_tables
            and not self.missing_columns
            and self.current_revision is not None
            and self.current_revision >= self.required_revision
        )


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _make_engine(url: str):
    """SQLite: bat WAL + busy_timeout de nhieu thread (API, bot, scheduler)
    doc/ghi dong thoi khong dinh 'database is locked' ngay lap tuc."""
    normalized_url = _normalize_db_url(url)
    if normalized_url.startswith("sqlite"):
        engine = create_engine(
            normalized_url,
            connect_args={"check_same_thread": False, "timeout": 15},
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.close()

        return engine
    if normalized_url.startswith("postgresql+psycopg"):
        settings = get_settings()
        parsed = make_url(normalized_url)
        connect_args: dict[str, object] = {
            "connect_timeout": settings.db_connect_timeout_s,
        }
        is_local = parsed.host in {"127.0.0.1", "localhost", "::1"}
        if "sslmode" not in parsed.query and not is_local:
            connect_args["sslmode"] = settings.db_ssl_mode
        return create_engine(
            normalized_url,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=1800,
            connect_args=connect_args,
        )
    return create_engine(normalized_url)


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = _make_engine(get_settings().db_url)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def _upgrade_sqlite_media_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    if "media_assets" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("media_assets")}
    additions = {
        "storage_backend": (
            "ALTER TABLE media_assets ADD COLUMN "
            "storage_backend VARCHAR(16) NOT NULL DEFAULT 'local'"
        ),
        "storage_bucket": (
            "ALTER TABLE media_assets ADD COLUMN storage_bucket VARCHAR(100)"
        ),
        "storage_key": "ALTER TABLE media_assets ADD COLUMN storage_key TEXT",
    }
    with engine.begin() as connection:
        for column_name, statement in additions.items():
            if column_name not in columns:
                connection.execute(text(statement))


def _upgrade_sqlite_user_auth_column(engine: Engine) -> None:
    """Add the nullable Supabase Auth owner mapping to legacy SQLite databases."""

    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("users")}
    with engine.begin() as connection:
        if "auth_user_id" not in columns:
            connection.execute(
                text("ALTER TABLE users ADD COLUMN auth_user_id CHAR(32)")
            )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_auth_user_id "
                "ON users (auth_user_id)"
            )
        )


def _upgrade_sqlite_affiliate_event_integrity(engine: Engine) -> None:
    """Rebuild the legacy leaf table with Decimal money and owner-scoped IDs."""

    inspector = inspect(engine)
    if "affiliate_events" not in inspector.get_table_names():
        return
    columns = {
        column["name"]: column
        for column in inspector.get_columns("affiliate_events")
    }
    expected_columns = {
        "id",
        "user_id",
        "social_post_id",
        "affiliate_product_id",
        "social_account_id",
        "publish_job_id",
        "event_type",
        "amount",
        "currency",
        "source",
        "external_event_id",
        "metadata_json",
        "occurred_at",
        "created_at",
    }
    missing = expected_columns - columns.keys()
    if missing:
        raise RuntimeError(
            "Legacy affiliate_events schema is incomplete; missing columns: "
            + ", ".join(sorted(missing))
        )
    unique_columns = {
        tuple(constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints("affiliate_events")
    }
    amount_type = columns["amount"]["type"].__class__.__name__.upper()
    if (
        ("user_id", "source", "external_event_id") in unique_columns
        and amount_type == "NUMERIC"
    ):
        return
    if "affiliate_events_v2" in inspector.get_table_names():
        raise RuntimeError(
            "SQLite affiliate event upgrade cannot continue while "
            "affiliate_events_v2 exists"
        )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE affiliate_events_v2 (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users (id),
                    social_post_id INTEGER REFERENCES social_posts (id),
                    affiliate_product_id INTEGER REFERENCES affiliate_products (id),
                    social_account_id INTEGER REFERENCES social_accounts (id),
                    publish_job_id INTEGER REFERENCES publish_jobs (id),
                    event_type VARCHAR(24) NOT NULL,
                    amount NUMERIC(18, 6) NOT NULL,
                    currency VARCHAR(8) NOT NULL,
                    source VARCHAR(64) NOT NULL,
                    external_event_id VARCHAR(255),
                    metadata_json JSON NOT NULL,
                    occurred_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT ck_affiliate_event_type
                        CHECK (event_type IN ('view', 'click', 'commission')),
                    CONSTRAINT ck_affiliate_event_amount CHECK (amount >= 0),
                    CONSTRAINT uq_affiliate_event_owner_source_external
                        UNIQUE (user_id, source, external_event_id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO affiliate_events_v2 (
                    id, user_id, social_post_id, affiliate_product_id,
                    social_account_id, publish_job_id, event_type, amount,
                    currency, source, external_event_id, metadata_json,
                    occurred_at, created_at
                )
                SELECT
                    id, user_id, social_post_id, affiliate_product_id,
                    social_account_id, publish_job_id, event_type, amount,
                    currency, source, external_event_id, metadata_json,
                    occurred_at, created_at
                FROM affiliate_events
                """
            )
        )
        connection.execute(text("DROP TABLE affiliate_events"))
        connection.execute(
            text("ALTER TABLE affiliate_events_v2 RENAME TO affiliate_events")
        )
        for statement in (
            (
                "CREATE INDEX ix_affiliate_events_user_id "
                "ON affiliate_events (user_id)"
            ),
            (
                "CREATE INDEX ix_affiliate_events_user_occurred "
                "ON affiliate_events (user_id, occurred_at)"
            ),
            (
                "CREATE INDEX ix_affiliate_events_type_occurred "
                "ON affiliate_events (event_type, occurred_at)"
            ),
            (
                "CREATE INDEX ix_affiliate_events_post_type "
                "ON affiliate_events (social_post_id, event_type)"
            ),
        ):
            connection.execute(text(statement))


def check_postgres_schema(engine: Engine) -> SchemaHealth:
    """Read-only schema and migration revision health for Supabase Postgres."""

    if engine.dialect.name != "postgresql":
        raise RuntimeError("Schema health requires a PostgreSQL engine")
    for module_name in (
        "laplace.models",
        "laplace.services.artifacts",
        "laplace.social.models",
    ):
        import_module(module_name)

    inspector = inspect(engine)
    expected = set(Base.metadata.tables)
    existing = set(inspector.get_table_names(schema="public"))
    missing_tables = tuple(sorted(expected - existing))
    required_columns = {
        "users": {"auth_user_id"},
        "media_assets": {"storage_backend", "storage_bucket", "storage_key"},
        "artifacts": {
            "user_id",
            "kind",
            "sha256",
            "storage_bucket",
            "storage_key",
            "status",
            "deleted_at",
        },
    }
    missing_columns: list[str] = []
    for table_name, columns in required_columns.items():
        if table_name not in existing:
            continue
        actual = {
            column["name"]
            for column in inspector.get_columns(table_name, schema="public")
        }
        missing_columns.extend(
            f"{table_name}.{column_name}"
            for column_name in sorted(columns - actual)
        )

    current_revision: str | None = None
    migration_tables = set(
        inspector.get_table_names(schema="supabase_migrations")
    )
    if "schema_migrations" in migration_tables:
        try:
            with engine.connect() as connection:
                value = connection.scalar(
                    text(
                        "select max(version) "
                        "from supabase_migrations.schema_migrations"
                    )
                )
        except SQLAlchemyError as exc:
            raise RuntimeError(
                "Supabase schema revision health check failed"
            ) from exc
        if value is not None:
            candidate = str(value)
            if len(candidate) == 14 and candidate.isdigit():
                current_revision = candidate

    return SchemaHealth(
        missing_tables=missing_tables,
        missing_columns=tuple(missing_columns),
        current_revision=current_revision,
        required_revision=REQUIRED_SUPABASE_REVISION,
    )


def _verify_postgres_schema(engine: Engine) -> None:
    health = check_postgres_schema(engine)
    if health.missing_tables:
        joined = ", ".join(health.missing_tables)
        raise RuntimeError(
            "Supabase schema is not ready; missing public tables: "
            f"{joined}. Run 'supabase db push' before starting Arya_Tool."
        )
    if health.missing_columns:
        joined = ", ".join(health.missing_columns)
        raise RuntimeError(
            "Supabase schema is not ready; missing columns: "
            f"{joined}. Run 'supabase db push' before starting Arya_Tool."
        )
    if health.current_revision is None:
        raise RuntimeError(
            "Supabase schema revision is unavailable; run migrations before "
            "starting Arya_Tool."
        )
    if health.current_revision < health.required_revision:
        raise RuntimeError(
            "Supabase schema revision is behind the application requirement; "
            "run 'supabase db push' before starting Arya_Tool."
        )


def init_db(engine=None) -> None:
    """Create SQLite dev tables or verify the migrated Supabase schema."""
    import laplace.models
    import laplace.services.artifacts
    import laplace.social.models  # noqa: F401

    target = engine or get_engine()
    if target.dialect.name == "sqlite":
        Base.metadata.create_all(target)
        _upgrade_sqlite_user_auth_column(target)
        _upgrade_sqlite_media_columns(target)
        _upgrade_sqlite_affiliate_event_integrity(target)
        return
    if target.dialect.name == "postgresql":
        _verify_postgres_schema(target)
        return
    raise RuntimeError(f"Unsupported database backend: {target.dialect.name}")


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_for_tests(url: str) -> None:
    """Chi dung trong test: tro engine sang DB tam."""
    global _engine, _SessionLocal
    _engine = _make_engine(url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
