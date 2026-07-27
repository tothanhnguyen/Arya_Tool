from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from laplace.config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


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


def _verify_postgres_schema(engine: Engine) -> None:
    expected = set(Base.metadata.tables)
    existing = set(inspect(engine).get_table_names(schema="public"))
    missing = sorted(expected - existing)
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            "Supabase schema is not ready; missing public tables: "
            f"{joined}. Run 'supabase db push' before starting Arya_Tool."
        )


def init_db(engine=None) -> None:
    """Create SQLite dev tables or verify the migrated Supabase schema."""
    import laplace.models
    import laplace.social.models  # noqa: F401

    target = engine or get_engine()
    if target.dialect.name == "sqlite":
        Base.metadata.create_all(target)
        _upgrade_sqlite_media_columns(target)
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
