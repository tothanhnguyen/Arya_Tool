"""Tests for atomic, owner-scoped affiliate CSV import."""

import csv
import io
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from laplace.models import User
from laplace.social import affiliate_import as affiliate_import_module
from laplace.social.affiliate_import import import_affiliate_csv
from laplace.social.models import (
    AffiliateEvent,
    AffiliateProduct,
    MediaAsset,
    PublishJob,
    SocialAccount,
    SocialPost,
)


def _csv(rows: list[dict[str, str]]) -> str:
    fieldnames = [
        "external_event_id",
        "event_type",
        "occurred_at",
        "amount",
        "currency",
        "source",
        "sub_id",
        "social_post_id",
        "affiliate_product_id",
        "social_account_id",
        "publish_job_id",
        "metadata_json",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _owner_graph(session, *, suffix: str = "a"):
    user = User()
    session.add(user)
    session.flush()
    account = SocialAccount(
        user_id=user.id,
        platform="facebook",
        display_name=f"Page {suffix}",
        external_id=f"page-{suffix}",
        auth_type="mock",
        auth_ref=f"mock://page-{suffix}",
    )
    media = MediaAsset(
        user_id=user.id,
        type="image",
        local_path=f"/tmp/{suffix}.png",
        original_name=f"{suffix}.png",
        mime_type="image/png",
        size_bytes=10,
        sha256=suffix * 64,
    )
    product = AffiliateProduct(
        user_id=user.id,
        network="demo",
        merchant="Merchant",
        product_name=f"Product {suffix}",
        product_url="https://example.test/product",
        affiliate_url="https://example.test/go",
    )
    session.add_all([account, media, product])
    session.flush()
    post = SocialPost(
        user_id=user.id,
        title=f"Post {suffix}",
        caption="Caption",
        hashtags_json=[],
        media_asset_id=media.id,
        affiliate_product_id=product.id,
        status="published",
        content_hash=suffix * 64,
    )
    session.add(post)
    session.flush()
    job = PublishJob(
        social_post_id=post.id,
        social_account_id=account.id,
        scheduled_at=datetime(2026, 7, 28, tzinfo=UTC),
        status="published",
        idempotency_key=(suffix * 63) + "1",
    )
    session.add(job)
    session.flush()
    return user, account, product, post, job


def test_import_maps_sub_ids_and_is_idempotent(session):
    user, account, product, post, job = _owner_graph(session)
    payload = _csv(
        [
            {
                "external_event_id": "click-1",
                "event_type": "click",
                "occurred_at": "2026-07-28T08:00:00+07:00",
                "sub_id": f"job:{job.id}",
                "metadata_json": '{"campaign":"launch"}',
            },
            {
                "external_event_id": "order-1",
                "event_type": "commission",
                "occurred_at": "2026-07-28T08:05:00+07:00",
                "amount": "12500.50",
                "currency": "VND",
                "sub_id": job.idempotency_key,
            },
        ]
    )

    first = import_affiliate_csv(
        session,
        payload,
        user_id=user.id,
        default_source="network-a",
    )
    second = import_affiliate_csv(
        session,
        payload,
        user_id=user.id,
        default_source="network-a",
    )

    assert first.ok
    assert first.imported == 2
    assert first.ready == 2
    assert second.ok
    assert second.imported == 0
    assert second.skipped_duplicates == 2

    events = session.scalars(select(AffiliateEvent).order_by(AffiliateEvent.id)).all()
    assert len(events) == 2
    assert {event.social_post_id for event in events} == {post.id}
    assert {event.affiliate_product_id for event in events} == {product.id}
    assert {event.social_account_id for event in events} == {account.id}
    assert {event.publish_job_id for event in events} == {job.id}
    assert events[0].metadata_json["campaign"] == "launch"
    commission = next(event for event in events if event.event_type == "commission")
    assert commission.amount == Decimal("12500.500000")


def test_malformed_row_rejects_the_entire_batch(session):
    user, *_ = _owner_graph(session)
    payload = _csv(
        [
            {
                "external_event_id": "click-ok",
                "event_type": "click",
                "occurred_at": "2026-07-28T01:00:00Z",
            },
            {
                "external_event_id": "commission-bad",
                "event_type": "commission",
                "occurred_at": "2026-07-28T01:05:00Z",
                "amount": "0",
            },
        ]
    )

    report = import_affiliate_csv(
        session,
        payload,
        user_id=user.id,
        default_source="network-a",
    )

    assert not report.ok
    assert report.imported == 0
    assert report.issues[0].row == 3
    assert session.scalar(select(func.count()).select_from(AffiliateEvent)) == 0


def test_sub_id_cannot_cross_owner_boundary(session):
    owner, *_ = _owner_graph(session, suffix="a")
    _, _, _, _, other_job = _owner_graph(session, suffix="b")
    payload = _csv(
        [
            {
                "external_event_id": "order-cross-owner",
                "event_type": "commission",
                "occurred_at": "2026-07-28T01:05:00Z",
                "amount": "1000",
                "sub_id": f"job:{other_job.id}",
            }
        ]
    )

    report = import_affiliate_csv(
        session,
        payload,
        user_id=owner.id,
        default_source="network-a",
    )

    assert not report.ok
    assert "this owner" in report.issues[0].message
    assert session.scalar(select(func.count()).select_from(AffiliateEvent)) == 0


def test_duplicate_identifier_with_conflicting_rows_is_rejected(session):
    user, *_ = _owner_graph(session)
    payload = _csv(
        [
            {
                "external_event_id": "same-id",
                "event_type": "click",
                "occurred_at": "2026-07-28T01:00:00Z",
            },
            {
                "external_event_id": "same-id",
                "event_type": "view",
                "occurred_at": "2026-07-28T01:00:00Z",
            },
        ]
    )

    report = import_affiliate_csv(
        session,
        payload,
        user_id=user.id,
        default_source="network-a",
    )

    assert not report.ok
    assert "conflicting data" in report.issues[0].message
    assert session.scalar(select(func.count()).select_from(AffiliateEvent)) == 0


def test_dry_run_validates_without_writing(session):
    user, *_ = _owner_graph(session)
    payload = _csv(
        [
            {
                "external_event_id": "dry-run-1",
                "event_type": "view",
                "occurred_at": "2026-07-28T01:00:00Z",
            }
        ]
    )

    report = import_affiliate_csv(
        session,
        payload,
        user_id=user.id,
        default_source="network-a",
        dry_run=True,
    )

    assert report.ok
    assert report.dry_run
    assert report.ready == 1
    assert report.imported == 0
    assert session.scalar(select(func.count()).select_from(AffiliateEvent)) == 0


def test_existing_identifier_with_corrected_commission_is_rejected_atomically(session):
    user, *_ = _owner_graph(session)
    original = _csv(
        [
            {
                "external_event_id": "corrected-order",
                "event_type": "commission",
                "occurred_at": "2026-07-28T01:05:00Z",
                "amount": "1000",
            }
        ]
    )
    corrected = _csv(
        [
            {
                "external_event_id": "corrected-order",
                "event_type": "commission",
                "occurred_at": "2026-07-28T01:05:00Z",
                "amount": "1200",
            },
            {
                "external_event_id": "new-click-must-not-write",
                "event_type": "click",
                "occurred_at": "2026-07-28T01:06:00Z",
            },
        ]
    )

    first = import_affiliate_csv(
        session,
        original,
        user_id=user.id,
        default_source="network-a",
    )
    report = import_affiliate_csv(
        session,
        corrected,
        user_id=user.id,
        default_source="network-a",
    )

    assert first.ok
    assert not report.ok
    assert "conflicts with existing data" in report.issues[0].message
    events = session.scalars(select(AffiliateEvent)).all()
    assert len(events) == 1
    assert events[0].amount == 1000


def test_same_external_id_is_independent_between_owners(session):
    first_owner, *_ = _owner_graph(session, suffix="a")
    second_owner, *_ = _owner_graph(session, suffix="b")
    payload = _csv(
        [
            {
                "external_event_id": "shared-order",
                "event_type": "commission",
                "occurred_at": "2026-07-28T01:05:00Z",
                "amount": "123.456789",
            }
        ]
    )

    first = import_affiliate_csv(
        session,
        payload,
        user_id=first_owner.id,
        default_source="network-a",
    )
    second = import_affiliate_csv(
        session,
        payload,
        user_id=second_owner.id,
        default_source="network-a",
    )

    assert first.ok
    assert second.ok
    assert first.imported == second.imported == 1
    events = session.scalars(
        select(AffiliateEvent).where(
            AffiliateEvent.source == "network-a",
            AffiliateEvent.external_event_id == "shared-order",
        )
    ).all()
    assert {event.user_id for event in events} == {
        first_owner.id,
        second_owner.id,
    }
    assert {event.amount for event in events} == {Decimal("123.456789")}


def test_integrity_race_rolls_back_import_batch_safely(
    session,
    monkeypatch,
):
    user, *_ = _owner_graph(session)
    occurred_at = datetime(2026, 7, 28, 1, 5, tzinfo=UTC)
    session.add(
        AffiliateEvent(
            user_id=user.id,
            event_type="click",
            amount=Decimal(0),
            currency="VND",
            source="network-a",
            external_event_id="race-id",
            occurred_at=occurred_at,
        )
    )
    session.commit()
    monkeypatch.setattr(
        affiliate_import_module,
        "_existing_keys",
        lambda *_args, **_kwargs: {},
    )
    payload = _csv(
        [
            {
                "external_event_id": "race-id",
                "event_type": "click",
                "occurred_at": occurred_at.isoformat(),
            },
            {
                "external_event_id": "must-rollback",
                "event_type": "view",
                "occurred_at": occurred_at.isoformat(),
            },
        ]
    )

    report = import_affiliate_csv(
        session,
        payload,
        user_id=user.id,
        default_source="network-a",
    )

    assert not report.ok
    assert report.imported == 0
    assert report.ready == 0
    assert "atomic insert; retry" in report.issues[0].message
    assert "race-id" not in report.issues[0].message
    events = session.scalars(select(AffiliateEvent)).all()
    assert [event.external_event_id for event in events] == ["race-id"]


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        ("1e10000", "NUMERIC(18,6) range"),
        ("1000000000000", "NUMERIC(18,6) range"),
        ("0.0000001", "NUMERIC(18,6) scale"),
    ],
)
def test_commission_amount_enforces_numeric_precision_and_scale(
    session,
    amount,
    message,
):
    user, *_ = _owner_graph(session)
    payload = _csv(
        [
            {
                "external_event_id": f"invalid-{amount}",
                "event_type": "commission",
                "occurred_at": "2026-07-28T01:05:00Z",
                "amount": amount,
            }
        ]
    )

    report = import_affiliate_csv(
        session,
        payload,
        user_id=user.id,
        default_source="network-a",
    )

    assert not report.ok
    assert message in report.issues[0].message
    assert session.scalar(select(func.count()).select_from(AffiliateEvent)) == 0


def test_nested_sensitive_metadata_is_rejected(session):
    user, *_ = _owner_graph(session)
    payload = _csv(
        [
            {
                "external_event_id": "nested-secret",
                "event_type": "click",
                "occurred_at": "2026-07-28T01:05:00Z",
                "metadata_json": '{"campaign":{"credentials":{"access_token":"do-not-store"}}}',
            }
        ]
    )

    report = import_affiliate_csv(
        session,
        payload,
        user_id=user.id,
        default_source="network-a",
    )

    assert not report.ok
    assert "sensitive key" in report.issues[0].message
    assert "do-not-store" not in report.issues[0].message
    assert session.scalar(select(func.count()).select_from(AffiliateEvent)) == 0


@pytest.mark.parametrize(
    "metadata",
    [
        {
            "nested": {
                "too": {
                    "deep": {
                        "for": {
                            "the": {
                                "configured": {
                                    "limit": {"has": {"another": "container"}}
                                }
                            }
                        }
                    }
                }
            }
        },
        {"items": list(range(201))},
        {"payload": "x" * (16 * 1024)},
    ],
)
def test_metadata_resource_limits_reject_the_batch(session, metadata):
    user, *_ = _owner_graph(session)
    payload = _csv(
        [
            {
                "external_event_id": "metadata-over-limit",
                "event_type": "view",
                "occurred_at": "2026-07-28T01:05:00Z",
                "metadata_json": json.dumps(metadata),
            }
        ]
    )

    report = import_affiliate_csv(
        session,
        payload,
        user_id=user.id,
        default_source="network-a",
    )

    assert not report.ok
    assert "metadata_json exceeds" in report.issues[0].message
    assert session.scalar(select(func.count()).select_from(AffiliateEvent)) == 0


def test_valid_nested_metadata_is_preserved(session):
    user, *_ = _owner_graph(session)
    metadata = {
        "campaign": {
            "author": "affiliate-team",
            "segments": ["launch", {"region": "vn"}],
        },
        "attribution_window_days": 7,
    }
    payload = _csv(
        [
            {
                "external_event_id": "valid-metadata",
                "event_type": "view",
                "occurred_at": "2026-07-28T01:05:00Z",
                "metadata_json": json.dumps(metadata),
            }
        ]
    )

    report = import_affiliate_csv(
        session,
        payload,
        user_id=user.id,
        default_source="network-a",
    )

    assert report.ok
    event = session.scalar(
        select(AffiliateEvent).where(AffiliateEvent.external_event_id == "valid-metadata")
    )
    assert event.metadata_json == metadata
