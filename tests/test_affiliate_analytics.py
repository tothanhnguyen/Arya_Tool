"""Tests for owner-scoped affiliate metric reconciliation."""

from datetime import UTC, datetime

from laplace.models import User
from laplace.social.analytics import collect_affiliate_analytics
from laplace.social.models import (
    AffiliateEvent,
    AffiliateProduct,
    MediaAsset,
    SocialAccount,
    SocialPost,
)


def _seed_owner(session, suffix: str):
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
    product = AffiliateProduct(
        user_id=user.id,
        network="demo",
        merchant="Merchant",
        product_name=f"Product {suffix}",
        product_url="https://example.test/product",
        affiliate_url="https://example.test/go",
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
    session.add_all([account, product, media])
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
    return user, account, product, post


def test_analytics_reconciles_owner_product_account_and_local_hour(session):
    owner, account, product, post = _seed_owner(session, "a")
    other, other_account, other_product, other_post = _seed_owner(session, "b")
    occurred_at = datetime(2026, 7, 28, 1, 15, tzinfo=UTC)
    session.add_all(
        [
            AffiliateEvent(
                user_id=owner.id,
                social_post_id=post.id,
                affiliate_product_id=product.id,
                social_account_id=account.id,
                event_type="view",
                occurred_at=occurred_at,
            ),
            AffiliateEvent(
                user_id=owner.id,
                social_post_id=post.id,
                affiliate_product_id=product.id,
                social_account_id=account.id,
                event_type="click",
                occurred_at=occurred_at,
            ),
            AffiliateEvent(
                user_id=owner.id,
                social_post_id=post.id,
                affiliate_product_id=product.id,
                social_account_id=account.id,
                event_type="commission",
                amount=12_500,
                currency="VND",
                occurred_at=occurred_at,
            ),
            AffiliateEvent(
                user_id=other.id,
                social_post_id=other_post.id,
                affiliate_product_id=other_product.id,
                social_account_id=other_account.id,
                event_type="commission",
                amount=999_999,
                currency="VND",
                occurred_at=occurred_at,
            ),
        ]
    )
    session.flush()

    report = collect_affiliate_analytics(
        session,
        user_id=owner.id,
        currency="VND",
        timezone_name="Asia/Ho_Chi_Minh",
    )

    assert report["owner_user_id"] == owner.id
    assert report["kpis"][3]["v"] == "12,500 ₫"
    assert report["kpis"][4]["v"] == "12,500 ₫"
    assert report["content_rows"][0]["label"] == "Post a"
    assert report["product_rows"][0]["label"] == "Product a"
    assert report["account_rows"][0]["label"] == "Page a"
    assert report["time_rows"][0]["label"] == "08:00–08:59"
    assert "999,999" not in str(report)


def test_epc_is_missing_instead_of_zero_without_clicks(session):
    owner, account, product, post = _seed_owner(session, "a")
    session.add(
        AffiliateEvent(
            user_id=owner.id,
            social_post_id=post.id,
            affiliate_product_id=product.id,
            social_account_id=account.id,
            event_type="commission",
            amount=10_000,
            currency="VND",
            occurred_at=datetime(2026, 7, 28, 1, 15, tzinfo=UTC),
        )
    )
    session.flush()

    report = collect_affiliate_analytics(
        session,
        user_id=owner.id,
        currency="VND",
        timezone_name="Asia/Ho_Chi_Minh",
    )

    assert report["kpis"][4]["v"] == "—"
    assert report["kpis"][5]["v"] == "—"
    assert report["content_rows"][0]["epc"] is None
    assert report["content_rows"][0]["epc_display"] == "—"
    assert report["daily_rows"][0]["epc_display"] == "—"
