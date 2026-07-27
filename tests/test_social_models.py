from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import Numeric, create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from laplace.db import Base
from laplace.models import User
from laplace.social.models import (
    AffiliateEvent,
    AffiliateProduct,
    MediaAsset,
    PublishAttempt,
    PublishJob,
    SocialAccount,
    SocialPost,
)
from laplace.social.schemas import (
    AffiliateEventCreate,
    AffiliateProductCreate,
    AuthType,
    MediaAssetCreate,
    Platform,
    PublishAttemptCreate,
    PublishAttemptStatus,
    PublishJobCreate,
    SocialAccountCreate,
    SocialPostCreate,
    SocialPostUpdate,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id=1))
        db.commit()
        yield db


def _account(**overrides) -> SocialAccount:
    values = {
        "user_id": 1,
        "platform": "facebook",
        "display_name": "Page A",
        "external_id": "page-123",
        "auth_type": "page_token",
        "auth_ref": "keychain://laplace/page-123",
    }
    values.update(overrides)
    return SocialAccount(**values)


def _media(**overrides) -> MediaAsset:
    values = {
        "user_id": 1,
        "type": "video",
        "local_path": "/tmp/video.mp4",
        "original_name": "video.mp4",
        "mime_type": "video/mp4",
        "size_bytes": 1234,
        "sha256": "a" * 64,
        "duration_seconds": 12.5,
    }
    values.update(overrides)
    return MediaAsset(**values)


def _post(media_id: int, **overrides) -> SocialPost:
    values = {
        "user_id": 1,
        "title": "Demo",
        "caption": "Caption",
        "hashtags_json": ["#demo"],
        "media_asset_id": media_id,
        "status": "approved",
        "content_hash": "b" * 64,
        "approved_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SocialPost(**values)


def test_all_social_tables_are_registered() -> None:
    expected = {
        "social_accounts",
        "media_assets",
        "affiliate_products",
        "social_posts",
        "publish_jobs",
        "publish_attempts",
        "affiliate_events",
        "content_generations",
    }
    assert expected <= set(Base.metadata.tables)


def test_social_account_has_auth_reference_but_no_raw_cookie_column() -> None:
    columns = {column.name for column in inspect(SocialAccount).columns}
    assert "auth_ref" in columns
    assert "cookie" not in columns
    assert "raw_cookie" not in columns
    assert "access_token" not in columns


def test_social_account_round_trip_and_defaults(session: Session) -> None:
    account = _account()
    session.add(account)
    session.commit()

    assert account.id is not None
    assert account.status == "active"
    assert account.timezone == "Asia/Ho_Chi_Minh"
    assert account.daily_post_limit == 2
    assert account.cooldown_seconds == 1800


def test_external_account_is_unique_per_platform(session: Session) -> None:
    session.add(_account())
    session.commit()
    session.add(_account(display_name="Duplicate"))

    with pytest.raises(IntegrityError):
        session.commit()


def test_media_product_post_job_and_attempt_round_trip(session: Session) -> None:
    account = _account()
    media = _media()
    product = AffiliateProduct(
        user_id=1,
        network="Shopee",
        merchant="Merchant",
        product_name="Product",
        product_url="https://example.test/product",
        affiliate_url="https://example.test/affiliate",
        sub_id_template="?sub_id={sub_id}",
    )
    session.add_all([account, media, product])
    session.flush()

    post = _post(media.id, affiliate_product_id=product.id)
    session.add(post)
    session.flush()
    job = PublishJob(
        social_post_id=post.id,
        social_account_id=account.id,
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
        idempotency_key="c" * 64,
    )
    session.add(job)
    session.flush()
    attempt = PublishAttempt(
        publish_job_id=job.id,
        attempt_no=1,
        status="published",
        request_json={"media_asset_id": media.id},
        response_json={"remote_post_id": "remote-1"},
        latency_ms=50,
    )
    session.add(attempt)
    session.commit()

    assert post.media_asset is media
    assert post.affiliate_product is product
    assert job.social_account is account
    assert job.social_post is post
    assert job.attempts == [attempt]


def test_publish_job_idempotency_key_is_unique(session: Session) -> None:
    account = _account()
    media = _media()
    session.add_all([account, media])
    session.flush()
    post = _post(media.id)
    session.add(post)
    session.flush()
    when = datetime.now(UTC) + timedelta(hours=1)
    session.add_all(
        [
            PublishJob(
                social_post_id=post.id,
                social_account_id=account.id,
                scheduled_at=when,
                idempotency_key="d" * 64,
            ),
            PublishJob(
                social_post_id=post.id,
                social_account_id=account.id,
                scheduled_at=when + timedelta(hours=1),
                idempotency_key="d" * 64,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_affiliate_event_round_trip_and_schema(session: Session) -> None:
    event = AffiliateEvent(
        user_id=1,
        event_type="commission",
        amount=Decimal("25000.123456"),
        currency="VND",
        source="manual",
        external_event_id="order-1",
    )
    session.add(event)
    session.commit()

    assert event.id is not None
    assert event.amount == Decimal("25000.123456")
    amount_type = AffiliateEvent.__table__.c.amount.type
    assert isinstance(amount_type, Numeric)
    assert amount_type.precision == 18
    assert amount_type.scale == 6
    owner_constraint = next(
        constraint
        for constraint in AffiliateEvent.__table__.constraints
        if constraint.name == "uq_affiliate_event_owner_source_external"
    )
    assert tuple(owner_constraint.columns.keys()) == (
        "user_id",
        "source",
        "external_event_id",
    )

    parsed = AffiliateEventCreate(
        user_id=1,
        event_type="click",
        occurred_at=datetime.now(UTC),
    )
    assert parsed.event_type.value == "click"
    assert parsed.amount == Decimal(0)

    with pytest.raises(ValidationError):
        AffiliateEventCreate(
            user_id=1,
            event_type="commission",
            amount=0,
            occurred_at=datetime.now(UTC),
        )

    for invalid_amount in ("1000000000000", "0.0000001", "NaN", "Infinity"):
        with pytest.raises(ValidationError):
            AffiliateEventCreate(
                user_id=1,
                event_type="commission",
                amount=invalid_amount,
                occurred_at=datetime.now(UTC),
            )


def test_affiliate_event_external_id_is_unique_within_owner(session: Session) -> None:
    session.add(User(id=2))
    session.commit()
    session.add_all(
        [
            AffiliateEvent(
                user_id=1,
                event_type="click",
                source="network-a",
                external_event_id="shared-id",
            ),
            AffiliateEvent(
                user_id=2,
                event_type="click",
                source="network-a",
                external_event_id="shared-id",
            ),
        ]
    )
    session.commit()

    assert session.query(AffiliateEvent).count() == 2

    session.add(
        AffiliateEvent(
            user_id=1,
            event_type="click",
            source="network-a",
            external_event_id="shared-id",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_social_account_schema_validates_limits_and_enums() -> None:
    data = SocialAccountCreate(
        user_id=1,
        platform="facebook",
        display_name="  Page A  ",
        external_id="page-1",
        auth_type="page_token",
        auth_ref="keychain://page-1",
    )
    assert data.platform is Platform.FACEBOOK
    assert data.auth_type is AuthType.PAGE_TOKEN
    assert data.display_name == "Page A"

    with pytest.raises(ValidationError):
        SocialAccountCreate(
            user_id=1,
            platform="facebook",
            display_name="Page A",
            external_id="page-1",
            auth_type="page_token",
            auth_ref="keychain://page-1",
            daily_post_limit=0,
        )


def test_media_schema_requires_valid_sha_and_nonnegative_size() -> None:
    with pytest.raises(ValidationError):
        MediaAssetCreate(
            user_id=1,
            type="video",
            local_path="/tmp/a.mp4",
            original_name="a.mp4",
            mime_type="video/mp4",
            size_bytes=-1,
            sha256="not-a-sha",
        )


def test_affiliate_template_requires_sub_id_and_aware_expiry() -> None:
    with pytest.raises(ValidationError):
        AffiliateProductCreate(
            user_id=1,
            network="Shopee",
            merchant="Merchant",
            product_name="Product",
            product_url="https://example.test/p",
            affiliate_url="https://example.test/a",
            sub_id_template="?campaign=fixed",
        )

    with pytest.raises(ValidationError):
        AffiliateProductCreate(
            user_id=1,
            network="Shopee",
            merchant="Merchant",
            product_name="Product",
            product_url="https://example.test/p",
            affiliate_url="https://example.test/a",
            valid_until=datetime.now(UTC).replace(tzinfo=None),
        )


def test_social_post_schema_cleans_caption_and_hashtags() -> None:
    data = SocialPostCreate(
        user_id=1,
        title=" Demo ",
        caption="  Useful caption  ",
        hashtags=["#Deals", "deals", " Laptop "],
        media_asset_id=1,
        content_hash="e" * 64,
    )
    assert data.caption == "Useful caption"
    assert data.hashtags == ["#Deals", "#Laptop"]

    with pytest.raises(ValidationError):
        SocialPostCreate(
            user_id=1,
            title="Demo",
            caption="   ",
            media_asset_id=1,
            content_hash="e" * 64,
        )


def test_social_post_update_requires_at_least_one_change() -> None:
    with pytest.raises(ValidationError):
        SocialPostUpdate()


def test_publish_job_requires_aware_schedule_and_sha256_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        PublishJobCreate(
            social_post_id=1,
            social_account_id=1,
            scheduled_at=datetime.now(UTC).replace(tzinfo=None),
            idempotency_key="f" * 64,
        )

    with pytest.raises(ValidationError):
        PublishJobCreate(
            social_post_id=1,
            social_account_id=1,
            scheduled_at=datetime.now(UTC),
            idempotency_key="short",
        )


def test_attempt_finished_at_cannot_precede_started_at() -> None:
    started = datetime.now(UTC)
    with pytest.raises(ValidationError):
        PublishAttemptCreate(
            publish_job_id=1,
            attempt_no=1,
            status=PublishAttemptStatus.PUBLISHED,
            started_at=started,
            finished_at=started - timedelta(seconds=1),
        )
