"""Atomic CSV import for normalized affiliate events.

The importer deliberately accepts a small, documented schema instead of
guessing vendor-specific columns. Network exports should be transformed to
this schema before import so ownership, currency and idempotency checks remain
deterministic.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from laplace.config import get_settings
from laplace.db import init_db, session_scope
from laplace.models import User
from laplace.social.models import (
    AffiliateEvent,
    AffiliateProduct,
    PublishJob,
    SocialAccount,
    SocialPost,
)

MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_CSV_ROWS = 50_000
QUERY_BATCH_SIZE = 500

REQUIRED_COLUMNS = frozenset({"external_event_id", "event_type", "occurred_at"})
OPTIONAL_COLUMNS = frozenset(
    {
        "amount",
        "currency",
        "source",
        "sub_id",
        "social_post_id",
        "affiliate_product_id",
        "social_account_id",
        "publish_job_id",
        "metadata_json",
    }
)
ALLOWED_COLUMNS = REQUIRED_COLUMNS | OPTIONAL_COLUMNS
EVENT_TYPES = frozenset({"view", "click", "commission"})
SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SUB_ID_RE = re.compile(
    r"^(?:(?:arya)[-_:])?(?P<kind>job|post)[-_:](?P<id>[1-9][0-9]*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ImportIssue:
    row: int
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class AffiliateImportReport:
    user_id: int
    rows_total: int
    imported: int
    ready: int
    skipped_duplicates: int
    dry_run: bool
    issues: tuple[ImportIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


@dataclass(frozen=True, slots=True)
class _ParsedEvent:
    row: int
    user_id: int
    event_type: str
    amount: Decimal
    currency: str
    source: str
    external_event_id: str
    social_post_id: int | None
    affiliate_product_id: int | None
    social_account_id: int | None
    publish_job_id: int | None
    occurred_at: datetime
    metadata_json: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return self.source, self.external_event_id

    def signature(self) -> tuple[Any, ...]:
        return (
            self.event_type,
            self.amount,
            self.currency,
            self.social_post_id,
            self.affiliate_product_id,
            self.social_account_id,
            self.publish_job_id,
            self.occurred_at,
            json.dumps(self.metadata_json, ensure_ascii=False, sort_keys=True),
        )


@dataclass(frozen=True, slots=True)
class _ResolvedLinks:
    social_post_id: int | None = None
    affiliate_product_id: int | None = None
    social_account_id: int | None = None
    publish_job_id: int | None = None


class _OwnerResolver:
    def __init__(self, session: Session, user_id: int) -> None:
        self.session = session
        self.user_id = user_id
        self._jobs: dict[int, _ResolvedLinks | None] = {}
        self._job_keys: dict[str, _ResolvedLinks | None] = {}
        self._posts: dict[int, SocialPost | None] = {}
        self._products: dict[int, AffiliateProduct | None] = {}
        self._accounts: dict[int, SocialAccount | None] = {}

    def job(self, job_id: int) -> _ResolvedLinks | None:
        if job_id not in self._jobs:
            row = self.session.execute(
                select(PublishJob, SocialPost, SocialAccount)
                .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
                .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
                .where(
                    PublishJob.id == job_id,
                    SocialPost.user_id == self.user_id,
                    SocialAccount.user_id == self.user_id,
                )
            ).one_or_none()
            self._jobs[job_id] = self._links_from_job_row(row)
        return self._jobs[job_id]

    def job_by_key(self, idempotency_key: str) -> _ResolvedLinks | None:
        if idempotency_key not in self._job_keys:
            row = self.session.execute(
                select(PublishJob, SocialPost, SocialAccount)
                .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
                .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
                .where(
                    PublishJob.idempotency_key == idempotency_key,
                    SocialPost.user_id == self.user_id,
                    SocialAccount.user_id == self.user_id,
                )
            ).one_or_none()
            self._job_keys[idempotency_key] = self._links_from_job_row(row)
        return self._job_keys[idempotency_key]

    @staticmethod
    def _links_from_job_row(row: Any) -> _ResolvedLinks | None:
        if row is None:
            return None
        job, post, account = row
        return _ResolvedLinks(
            social_post_id=post.id,
            affiliate_product_id=post.affiliate_product_id,
            social_account_id=account.id,
            publish_job_id=job.id,
        )

    def post(self, post_id: int) -> SocialPost | None:
        if post_id not in self._posts:
            self._posts[post_id] = self.session.scalar(
                select(SocialPost).where(
                    SocialPost.id == post_id,
                    SocialPost.user_id == self.user_id,
                )
            )
        return self._posts[post_id]

    def product(self, product_id: int) -> AffiliateProduct | None:
        if product_id not in self._products:
            self._products[product_id] = self.session.scalar(
                select(AffiliateProduct).where(
                    AffiliateProduct.id == product_id,
                    AffiliateProduct.user_id == self.user_id,
                )
            )
        return self._products[product_id]

    def account(self, account_id: int) -> SocialAccount | None:
        if account_id not in self._accounts:
            self._accounts[account_id] = self.session.scalar(
                select(SocialAccount).where(
                    SocialAccount.id == account_id,
                    SocialAccount.user_id == self.user_id,
                )
            )
        return self._accounts[account_id]

    def sub_id(self, value: str) -> _ResolvedLinks | None:
        match = SUB_ID_RE.fullmatch(value)
        if match:
            entity_id = int(match.group("id"))
            if match.group("kind").lower() == "job":
                return self.job(entity_id)
            post = self.post(entity_id)
            if post is None:
                return None
            return _ResolvedLinks(
                social_post_id=post.id,
                affiliate_product_id=post.affiliate_product_id,
            )
        return self.job_by_key(value)


def _decode_csv(payload: str | bytes) -> str:
    if isinstance(payload, str):
        encoded_size = len(payload.encode("utf-8"))
        if encoded_size > MAX_CSV_BYTES:
            raise ValueError(f"CSV exceeds {MAX_CSV_BYTES} bytes")
        return payload.removeprefix("\ufeff")
    if len(payload) > MAX_CSV_BYTES:
        raise ValueError(f"CSV exceeds {MAX_CSV_BYTES} bytes")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8 encoded") from exc


def _normalise_headers(fieldnames: list[str | None] | None) -> list[str]:
    if not fieldnames:
        raise ValueError("CSV header is missing")
    headers = [(name or "").strip().lower() for name in fieldnames]
    if any(not name for name in headers):
        raise ValueError("CSV header contains a blank column")
    if len(set(headers)) != len(headers):
        raise ValueError("CSV header contains duplicate columns")
    missing = sorted(REQUIRED_COLUMNS - set(headers))
    if missing:
        raise ValueError(f"CSV is missing required columns: {', '.join(missing)}")
    unknown = sorted(set(headers) - ALLOWED_COLUMNS)
    if unknown:
        raise ValueError(f"CSV contains unsupported columns: {', '.join(unknown)}")
    return headers


def _positive_id(raw: str, field: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _aware_datetime(raw: str) -> datetime:
    value = raw.strip()
    if not value:
        raise ValueError("occurred_at is required")
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("occurred_at must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("occurred_at must include a timezone offset")
    return parsed.astimezone(UTC)


def _money(raw: str, event_type: str) -> Decimal:
    value = raw.strip() or "0"
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("amount must be a decimal number") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("amount must be a finite non-negative number")
    if event_type == "commission" and amount <= 0:
        raise ValueError("commission rows require amount > 0")
    if event_type != "commission" and amount != 0:
        raise ValueError("view/click rows require amount = 0")
    return amount


def _metadata(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("metadata_json must be valid JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("metadata_json must contain a JSON object")
    return value


def _merge_links(
    resolver: _OwnerResolver,
    row: dict[str, str],
) -> _ResolvedLinks:
    explicit_job_id = _positive_id(row.get("publish_job_id", ""), "publish_job_id")
    explicit_post_id = _positive_id(row.get("social_post_id", ""), "social_post_id")
    explicit_product_id = _positive_id(
        row.get("affiliate_product_id", ""),
        "affiliate_product_id",
    )
    explicit_account_id = _positive_id(
        row.get("social_account_id", ""),
        "social_account_id",
    )

    links = _ResolvedLinks(
        social_post_id=explicit_post_id,
        affiliate_product_id=explicit_product_id,
        social_account_id=explicit_account_id,
        publish_job_id=explicit_job_id,
    )

    candidates: list[_ResolvedLinks] = []
    if explicit_job_id is not None:
        job_links = resolver.job(explicit_job_id)
        if job_links is None:
            raise ValueError("publish_job_id was not found for this owner")
        candidates.append(job_links)

    sub_id = row.get("sub_id", "").strip()
    if sub_id:
        sub_links = resolver.sub_id(sub_id)
        if sub_links is None:
            raise ValueError("sub_id could not be mapped for this owner")
        candidates.append(sub_links)

    for candidate in candidates:
        links = _coalesce_links(links, candidate)

    if links.social_post_id is not None:
        post = resolver.post(links.social_post_id)
        if post is None:
            raise ValueError("social_post_id was not found for this owner")
        post_links = _ResolvedLinks(
            social_post_id=post.id,
            affiliate_product_id=post.affiliate_product_id,
        )
        links = _coalesce_links(links, post_links)

    if (
        links.affiliate_product_id is not None
        and resolver.product(links.affiliate_product_id) is None
    ):
        raise ValueError("affiliate_product_id was not found for this owner")
    if (
        links.social_account_id is not None
        and resolver.account(links.social_account_id) is None
    ):
        raise ValueError("social_account_id was not found for this owner")
    return links


def _coalesce_links(left: _ResolvedLinks, right: _ResolvedLinks) -> _ResolvedLinks:
    values: dict[str, int | None] = {}
    for field in (
        "social_post_id",
        "affiliate_product_id",
        "social_account_id",
        "publish_job_id",
    ):
        left_value = getattr(left, field)
        right_value = getattr(right, field)
        if left_value is not None and right_value is not None and left_value != right_value:
            raise ValueError(f"{field} conflicts with the resolved sub_id/publish job")
        values[field] = left_value if left_value is not None else right_value
    return _ResolvedLinks(**values)


def _parse_row(
    row_number: int,
    row: dict[str, str],
    *,
    user_id: int,
    default_source: str,
    expected_currency: str,
    resolver: _OwnerResolver,
) -> _ParsedEvent:
    event_type = row.get("event_type", "").strip().lower()
    if event_type not in EVENT_TYPES:
        raise ValueError("event_type must be view, click or commission")

    external_event_id = row.get("external_event_id", "").strip()
    if not external_event_id:
        raise ValueError("external_event_id is required")
    if len(external_event_id) > 255:
        raise ValueError("external_event_id exceeds 255 characters")

    source = row.get("source", "").strip() or default_source
    if not SOURCE_RE.fullmatch(source):
        raise ValueError("source must use 1-64 letters, digits, dot, dash or underscore")

    currency = (row.get("currency", "").strip() or expected_currency).upper()
    if currency != expected_currency:
        raise ValueError(f"currency must be {expected_currency}")

    links = _merge_links(resolver, row)
    metadata = _metadata(row.get("metadata_json", ""))
    sub_id = row.get("sub_id", "").strip()
    if sub_id:
        metadata = {**metadata, "sub_id": sub_id}

    return _ParsedEvent(
        row=row_number,
        user_id=user_id,
        event_type=event_type,
        amount=_money(row.get("amount", ""), event_type),
        currency=currency,
        source=source,
        external_event_id=external_event_id,
        social_post_id=links.social_post_id,
        affiliate_product_id=links.affiliate_product_id,
        social_account_id=links.social_account_id,
        publish_job_id=links.publish_job_id,
        occurred_at=_aware_datetime(row.get("occurred_at", "")),
        metadata_json=metadata,
    )


def _existing_keys(
    session: Session,
    keys: set[tuple[str, str]],
) -> dict[tuple[str, str], int]:
    existing: dict[tuple[str, str], int] = {}
    by_source: dict[str, list[str]] = {}
    for source, event_id in keys:
        by_source.setdefault(source, []).append(event_id)
    for source, event_ids in by_source.items():
        for offset in range(0, len(event_ids), QUERY_BATCH_SIZE):
            batch = event_ids[offset : offset + QUERY_BATCH_SIZE]
            rows = session.execute(
                select(
                    AffiliateEvent.source,
                    AffiliateEvent.external_event_id,
                    AffiliateEvent.user_id,
                ).where(
                    AffiliateEvent.source == source,
                    AffiliateEvent.external_event_id.in_(batch),
                )
            )
            for row in rows:
                if row.external_event_id is not None:
                    existing[(row.source, row.external_event_id)] = row.user_id
    return existing


def import_affiliate_csv(
    session: Session,
    payload: str | bytes,
    *,
    user_id: int,
    default_source: str,
    expected_currency: str = "VND",
    dry_run: bool = False,
) -> AffiliateImportReport:
    """Validate a complete CSV batch, then insert only new events.

    Validation is atomic: if any row is malformed or points at another owner,
    no event from the file is added to the session.
    """
    if user_id <= 0:
        raise ValueError("user_id must be positive")
    expected_currency = expected_currency.strip().upper()
    if not 3 <= len(expected_currency) <= 8:
        raise ValueError("expected_currency must contain 3-8 characters")
    if not SOURCE_RE.fullmatch(default_source):
        raise ValueError("default_source must use letters, digits, dot, dash or underscore")
    if session.get(User, user_id) is None:
        raise ValueError("user_id was not found")

    text = _decode_csv(payload)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = _normalise_headers(reader.fieldnames)
    reader.fieldnames = headers
    resolver = _OwnerResolver(session, user_id)
    parsed: list[_ParsedEvent] = []
    issues: list[ImportIssue] = []
    rows_total = 0

    try:
        for row_number, raw_row in enumerate(reader, start=2):
            rows_total += 1
            if rows_total > MAX_CSV_ROWS:
                raise ValueError(f"CSV exceeds {MAX_CSV_ROWS} data rows")
            if None in raw_row:
                issues.append(
                    ImportIssue(
                        row_number,
                        "row",
                        "row contains more values than the CSV header",
                    )
                )
                continue
            row = {key: (value or "").strip() for key, value in raw_row.items()}
            try:
                parsed.append(
                    _parse_row(
                        row_number,
                        row,
                        user_id=user_id,
                        default_source=default_source,
                        expected_currency=expected_currency,
                        resolver=resolver,
                    )
                )
            except (TypeError, ValueError) as exc:
                issues.append(ImportIssue(row_number, "row", str(exc)))
    except csv.Error as exc:
        raise ValueError(f"CSV parsing failed: {exc}") from exc

    if not rows_total:
        issues.append(ImportIssue(1, "file", "CSV has no data rows"))
    if issues:
        return AffiliateImportReport(
            user_id=user_id,
            rows_total=rows_total,
            imported=0,
            ready=0,
            skipped_duplicates=0,
            dry_run=dry_run,
            issues=tuple(issues),
        )

    unique: dict[tuple[str, str], _ParsedEvent] = {}
    duplicate_count = 0
    for event in parsed:
        previous = unique.get(event.key)
        if previous is None:
            unique[event.key] = event
            continue
        if previous.signature() != event.signature():
            issues.append(
                ImportIssue(
                    event.row,
                    "external_event_id",
                    "duplicate identifier has conflicting data in this CSV",
                )
            )
        else:
            duplicate_count += 1

    existing = _existing_keys(session, set(unique))
    new_events: list[_ParsedEvent] = []
    for key, event in unique.items():
        owner_id = existing.get(key)
        if owner_id is None:
            new_events.append(event)
        elif owner_id == user_id:
            duplicate_count += 1
        else:
            issues.append(
                ImportIssue(
                    event.row,
                    "external_event_id",
                    "event identifier is already in use",
                )
            )

    if issues:
        return AffiliateImportReport(
            user_id=user_id,
            rows_total=rows_total,
            imported=0,
            ready=0,
            skipped_duplicates=duplicate_count,
            dry_run=dry_run,
            issues=tuple(issues),
        )

    if not dry_run:
        session.add_all(
            [
                AffiliateEvent(
                    user_id=event.user_id,
                    event_type=event.event_type,
                    amount=float(event.amount),
                    currency=event.currency,
                    source=event.source,
                    external_event_id=event.external_event_id,
                    social_post_id=event.social_post_id,
                    affiliate_product_id=event.affiliate_product_id,
                    social_account_id=event.social_account_id,
                    publish_job_id=event.publish_job_id,
                    occurred_at=event.occurred_at,
                    metadata_json=event.metadata_json,
                )
                for event in new_events
            ]
        )
        session.flush()

    return AffiliateImportReport(
        user_id=user_id,
        rows_total=rows_total,
        imported=0 if dry_run else len(new_events),
        ready=len(new_events),
        skipped_duplicates=duplicate_count,
        dry_run=dry_run,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import normalized affiliate CSV events")
    parser.add_argument("file", type=Path, help="UTF-8 CSV file")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--source", required=True, help="Stable network/source identifier")
    parser.add_argument("--currency", default=None, help="Defaults to LAPLACE_SOCIAL_CURRENCY")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = args.file.read_bytes()
        init_db()
        with session_scope() as session:
            report = import_affiliate_csv(
                session,
                payload,
                user_id=args.user_id,
                default_source=args.source,
                expected_currency=args.currency or get_settings().social_currency,
                dry_run=args.dry_run,
            )
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
