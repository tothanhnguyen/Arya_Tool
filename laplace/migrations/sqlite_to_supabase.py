"""Copy Arya_Tool data from SQLite into a migrated Supabase Postgres project.

The default command is a read-only dry run. A real migration requires
``--execute`` and a Postgres URL supplied through an environment variable.
No connection URL, API key, row payload, cookie, or browser profile is logged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, create_engine, func, inspect, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError

from laplace.config import get_settings
from laplace.db import _normalize_db_url
from laplace.social.storage import (
    StorageConfigurationError,
    StorageError,
    get_media_storage,
)

TABLE_ORDER = (
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
)


class MigrationError(RuntimeError):
    """The migration cannot continue safely."""


@dataclass(slots=True)
class TableResult:
    source_rows: int
    inserted_rows: int = 0
    target_rows: int | None = None
    verified_source_ids: int = 0


@dataclass(slots=True)
class MigrationReport:
    mode: str
    source_backend: str
    target_backend: str | None = None
    tables: dict[str, TableResult] = field(default_factory=dict)
    missing_tables: list[str] = field(default_factory=list)
    local_media_rows: int = 0
    missing_media_files: int = 0
    media_bytes: int = 0
    orphan_foreign_keys: dict[str, int] = field(default_factory=dict)
    social_post_statuses: dict[str, int] = field(default_factory=dict)
    publish_job_statuses: dict[str, int] = field(default_factory=dict)
    affiliate_event_counts: dict[str, int] = field(default_factory=dict)
    commission_by_currency: dict[str, str] = field(default_factory=dict)
    completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported report value: {type(value).__name__}")


def _iter_batches(
    connection: Connection,
    table: Table,
    batch_size: int,
) -> Iterator[list[dict[str, Any]]]:
    result = connection.execute(select(table).order_by(table.c.id))
    while rows := result.fetchmany(batch_size):
        yield [dict(row._mapping) for row in rows]


def _inventory_media(
    connection: Connection,
    table: Table,
    report: MigrationReport,
) -> None:
    available = set(table.c.keys())
    selected = [table.c.local_path, table.c.size_bytes]
    if "storage_backend" in available:
        selected.append(table.c.storage_backend)
    for row in connection.execute(select(*selected)):
        values = row._mapping
        if values.get("storage_backend", "local") != "local":
            continue
        report.local_media_rows += 1
        path = Path(values["local_path"]).expanduser()
        if not path.is_file():
            report.missing_media_files += 1
            continue
        report.media_bytes += path.stat().st_size


def inspect_source(source_url: str) -> MigrationReport:
    source = create_engine(source_url)
    if source.dialect.name != "sqlite":
        raise MigrationError("Source must be a SQLite database")

    report = MigrationReport(mode="dry-run", source_backend="sqlite")
    metadata = MetaData()
    existing = set(inspect(source).get_table_names())
    with source.connect() as connection:
        for table_name in TABLE_ORDER:
            if table_name not in existing:
                report.missing_tables.append(table_name)
                continue
            table = Table(table_name, metadata, autoload_with=connection)
            count = int(
                connection.scalar(select(func.count()).select_from(table)) or 0
            )
            report.tables[table_name] = TableResult(source_rows=count)
            if table_name == "media_assets":
                _inventory_media(connection, table, report)

        if "social_posts" in existing:
            rows = connection.execute(
                text(
                    "select status, count(*) as total "
                    "from social_posts group by status"
                )
            )
            report.social_post_statuses = {
                str(row.status): int(row.total) for row in rows
            }
        if "publish_jobs" in existing:
            rows = connection.execute(
                text(
                    "select status, count(*) as total "
                    "from publish_jobs group by status"
                )
            )
            report.publish_job_statuses = {
                str(row.status): int(row.total) for row in rows
            }
        if "affiliate_events" in existing:
            rows = connection.execute(
                text(
                    "select event_type, count(*) as total "
                    "from affiliate_events group by event_type"
                )
            )
            report.affiliate_event_counts = {
                str(row.event_type): int(row.total) for row in rows
            }
            rows = connection.execute(
                text(
                    "select currency, sum(amount) as total "
                    "from affiliate_events "
                    "where event_type = 'commission' "
                    "group by currency"
                )
            )
            report.commission_by_currency = {
                str(row.currency): str(row.total) for row in rows
            }

        orphan_rows = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        for orphan in orphan_rows:
            table_name = str(orphan[0])
            report.orphan_foreign_keys[table_name] = (
                report.orphan_foreign_keys.get(table_name, 0) + 1
            )
    report.completed = not (
        report.missing_tables
        or report.missing_media_files
        or report.orphan_foreign_keys
    )
    source.dispose()
    return report


def _resolve_target_url(env_name: str) -> str:
    target_url = os.environ.get(env_name)
    if not target_url and env_name == "LAPLACE_DB_URL":
        target_url = get_settings().db_url
    if not target_url:
        raise MigrationError(f"Set {env_name} before using --execute")
    parsed = make_url(target_url)
    if parsed.get_backend_name() != "postgresql":
        raise MigrationError("Migration target must be PostgreSQL/Supabase")
    return target_url


def _prepare_media_row(
    row: dict[str, Any],
    *,
    storage,
) -> dict[str, Any]:
    if row.get("storage_backend", "local") == "supabase":
        return row
    path = Path(row["local_path"]).expanduser()
    if not path.is_file():
        raise MigrationError(
            f"Media asset {row.get('id')} is missing its local file"
        )
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    expected_digest = row.get("sha256")
    if expected_digest and digest != expected_digest:
        raise MigrationError(
            f"Media asset {row.get('id')} does not match its stored hash"
        )
    expected_size = row.get("size_bytes")
    if expected_size is not None and len(payload) != expected_size:
        raise MigrationError(
            f"Media asset {row.get('id')} does not match its stored size"
        )
    suffix = Path(row.get("original_name") or path.name).suffix.lower()
    stored = storage.store(
        user_id=int(row["user_id"]),
        digest=digest,
        suffix=suffix,
        payload=payload,
        content_type=row["mime_type"],
    )
    row.update(
        local_path=stored.location,
        storage_backend=stored.backend,
        storage_bucket=stored.bucket,
        storage_key=stored.key,
    )
    return row


def _reset_identity(connection: Connection, table_name: str) -> None:
    if table_name not in TABLE_ORDER:
        raise MigrationError("Refusing to reset an unknown table sequence")
    connection.execute(
        text(
            "select setval("
            f"pg_get_serial_sequence('public.{table_name}', 'id'), "
            f"coalesce((select max(id) from public.{table_name}), 1), "
            f"(select count(*) > 0 from public.{table_name})"
            ")"
        )
    )


def _verify_ids(
    target_connection: Connection,
    target_table: Table,
    source_ids: Sequence[int],
) -> int:
    if not source_ids:
        return 0
    return len(
        target_connection.execute(
            select(target_table.c.id).where(target_table.c.id.in_(source_ids))
        ).all()
    )


def migrate(
    *,
    source_url: str,
    target_url: str,
    batch_size: int = 500,
) -> MigrationReport:
    if batch_size < 1:
        raise MigrationError("batch_size must be at least 1")

    inventory = inspect_source(source_url)
    inventory.mode = "execute"
    if inventory.missing_tables:
        names = ", ".join(inventory.missing_tables)
        raise MigrationError(f"SQLite source is missing tables: {names}")
    if inventory.missing_media_files:
        raise MigrationError(
            "SQLite media inventory contains missing files; repair them first"
        )

    try:
        storage = get_media_storage() if inventory.local_media_rows else None
    except StorageConfigurationError as exc:
        raise MigrationError(str(exc)) from exc
    if storage is not None and get_settings().social_media_backend != "supabase":
        raise MigrationError(
            "Set LAPLACE_SOCIAL_MEDIA_BACKEND=supabase before migrating media"
        )

    source = create_engine(source_url)
    target = create_engine(_normalize_db_url(target_url), pool_pre_ping=True)
    if target.dialect.name != "postgresql":
        raise MigrationError("Migration target must be PostgreSQL/Supabase")

    target_tables = set(inspect(target).get_table_names(schema="public"))
    missing_target = [name for name in TABLE_ORDER if name not in target_tables]
    if missing_target:
        names = ", ".join(missing_target)
        raise MigrationError(
            f"Supabase schema is missing tables: {names}. Run supabase db push."
        )

    source_metadata = MetaData()
    target_metadata = MetaData()
    try:
        with source.connect() as source_connection, target.begin() as target_connection:
            for table_name in TABLE_ORDER:
                source_table = Table(
                    table_name,
                    source_metadata,
                    autoload_with=source_connection,
                )
                target_table = Table(
                    table_name,
                    target_metadata,
                    schema="public",
                    autoload_with=target_connection,
                )
                target_columns = set(target_table.c.keys())
                result = inventory.tables[table_name]

                for rows in _iter_batches(
                    source_connection,
                    source_table,
                    batch_size,
                ):
                    prepared = []
                    for row in rows:
                        cleaned = {
                            key: value
                            for key, value in row.items()
                            if key in target_columns
                        }
                        if table_name == "media_assets" and storage is not None:
                            cleaned = _prepare_media_row(cleaned, storage=storage)
                        prepared.append(cleaned)

                    statement = (
                        postgres_insert(target_table)
                        .values(prepared)
                        .on_conflict_do_nothing(index_elements=[target_table.c.id])
                    )
                    insert_result = target_connection.execute(statement)
                    result.inserted_rows += int(insert_result.rowcount or 0)
                    source_ids = [int(row["id"]) for row in prepared]
                    result.verified_source_ids += _verify_ids(
                        target_connection,
                        target_table,
                        source_ids,
                    )

                _reset_identity(target_connection, table_name)
                result.target_rows = int(
                    target_connection.scalar(
                        select(func.count()).select_from(target_table)
                    )
                    or 0
                )
                if result.verified_source_ids != result.source_rows:
                    raise MigrationError(
                        f"Target verification failed for table {table_name}"
                    )
        inventory.target_backend = "postgresql"
        inventory.completed = True
        return inventory
    finally:
        source.dispose()
        target.dispose()


def _render_markdown(report: MigrationReport) -> str:
    lines = [
        "# Arya_Tool migration report",
        "",
        f"- Mode: `{report.mode}`",
        f"- Source: `{report.source_backend}`",
        f"- Target: `{report.target_backend or 'not connected'}`",
        f"- Completed: `{'yes' if report.completed else 'no'}`",
        f"- Local media: `{report.local_media_rows}` rows / `{report.media_bytes}` bytes",
        f"- Missing media files: `{report.missing_media_files}`",
        f"- Foreign-key orphans: `{sum(report.orphan_foreign_keys.values())}`",
        "",
        "| Table | Source | Inserted | Target | Verified IDs |",
        "|---|---:|---:|---:|---:|",
    ]
    for table_name, result in report.tables.items():
        target_rows = "-" if result.target_rows is None else str(result.target_rows)
        lines.append(
            f"| {table_name} | {result.source_rows} | {result.inserted_rows} "
            f"| {target_rows} | {result.verified_source_ids} |"
        )
    lines.extend(
        [
            "",
            "## Business reconciliation",
            "",
            f"- Social post statuses: `{json.dumps(report.social_post_statuses)}`",
            f"- Publish job statuses: `{json.dumps(report.publish_job_statuses)}`",
            f"- Affiliate events: `{json.dumps(report.affiliate_event_counts)}`",
            f"- Commission by currency: `{json.dumps(report.commission_by_currency)}`",
            "",
        ]
    )
    return "\n".join(lines)


def _write_report(report: MigrationReport, output_path: str | None) -> None:
    rendered = json.dumps(
        report.to_dict(),
        ensure_ascii=False,
        indent=2,
        default=_json_default,
    )
    if output_path:
        destination = Path(output_path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n", encoding="utf-8")
        markdown_destination = destination.with_suffix(".md")
        markdown_destination.write_text(
            _render_markdown(report),
            encoding="utf-8",
        )
    print(rendered)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run or migrate Arya_Tool SQLite data to Supabase",
    )
    parser.add_argument(
        "--source",
        default="sqlite:///./arya-tool.db",
        help="SQLite SQLAlchemy URL (default: sqlite:///./arya-tool.db)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Perform the migration; without this flag the command is read-only",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit read-only mode (this is also the default)",
    )
    parser.add_argument(
        "--target-env",
        default="LAPLACE_DB_URL",
        help="Environment variable containing the Supabase Postgres URL",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--report", help="Optional path for the JSON report")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.execute:
            report = migrate(
                source_url=args.source,
                target_url=_resolve_target_url(args.target_env),
                batch_size=args.batch_size,
            )
        else:
            report = inspect_source(args.source)
        _write_report(report, args.report)
        return 0 if report.completed else 2
    except (MigrationError, OSError, StorageError) as exc:
        print(f"Migration stopped safely: {exc}")
        return 1
    except SQLAlchemyError:
        print("Migration stopped safely: database operation failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
