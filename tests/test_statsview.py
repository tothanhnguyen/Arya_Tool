"""Tests for affiliate traffic and commission analytics."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from laplace.models import User
from laplace.social.models import (
    AffiliateEvent,
    AffiliateProduct,
    MediaAsset,
    SocialAccount,
    SocialPost,
)


@pytest.fixture()
def client(session):
    from laplace.web.app import create_app

    with TestClient(create_app(), follow_redirects=False) as test_client:
        yield test_client


def _client_with_owner(user_id: object) -> TestClient:
    from laplace.web.app import create_app

    app = create_app()

    @app.middleware("http")
    async def _set_owner(request, call_next):
        request.state.user_id = user_id
        return await call_next(request)

    return TestClient(app, follow_redirects=False)


def _seed_affiliate_events(session) -> None:
    user = User()
    session.add(user)
    session.flush()
    account = SocialAccount(
        user_id=user.id,
        platform="facebook",
        display_name="Page Analytics",
        external_id="page-analytics",
        auth_type="mock",
        auth_ref="mock://page-analytics",
    )
    product = AffiliateProduct(
        user_id=user.id,
        network="Demo",
        merchant="Merchant",
        product_name="Sản phẩm Analytics",
        product_url="https://example.test/product",
        affiliate_url="https://example.test/go",
    )
    media = MediaAsset(
        user_id=user.id,
        type="image",
        local_path="/tmp/product.png",
        original_name="product.png",
        mime_type="image/png",
        size_bytes=100,
        sha256="a" * 64,
    )
    session.add_all([account, product, media])
    session.flush()
    post = SocialPost(
        user_id=user.id,
        title="Review sản phẩm A",
        caption="Caption",
        hashtags_json=[],
        media_asset_id=media.id,
        affiliate_product_id=product.id,
        status="published",
        content_hash="b" * 64,
    )
    session.add(post)
    session.flush()
    occurred_at = datetime.now(UTC) - timedelta(hours=1)
    events = [
        AffiliateEvent(
            user_id=user.id,
            social_post_id=post.id,
            affiliate_product_id=product.id,
            social_account_id=account.id,
            event_type="view",
            occurred_at=occurred_at,
        )
        for _ in range(10)
    ]
    events.extend(
        AffiliateEvent(
            user_id=user.id,
            social_post_id=post.id,
            affiliate_product_id=product.id,
            social_account_id=account.id,
            event_type="click",
            occurred_at=occurred_at,
        )
        for _ in range(4)
    )
    events.extend(
        [
            AffiliateEvent(
                user_id=user.id,
                social_post_id=post.id,
                affiliate_product_id=product.id,
                social_account_id=account.id,
                event_type="commission",
                amount=30_000,
                currency="VND",
                occurred_at=occurred_at,
            ),
            AffiliateEvent(
                user_id=user.id,
                social_post_id=post.id,
                affiliate_product_id=product.id,
                social_account_id=account.id,
                event_type="commission",
                amount=20_000,
                currency="VND",
                occurred_at=occurred_at,
            ),
        ]
    )
    session.add_all(events)
    session.commit()


def test_stats_page_shows_affiliate_funnel(session, client):
    _seed_affiliate_events(session)

    response = client.get("/stats")

    assert response.status_code == 200
    assert "Affiliate Analytics" in response.text
    assert "Lượt xem" in response.text
    assert "Lượt bấm" in response.text
    assert "CTR" in response.text
    assert "40.0%" in response.text
    assert "50,000 ₫" in response.text
    assert "12,500 ₫" in response.text
    assert "2 lượt ghi nhận" in response.text
    assert "Review sản phẩm A" in response.text
    assert "Sản phẩm Analytics" in response.text
    assert "Page Analytics" in response.text
    assert "Top khung giờ" in response.text
    assert "Asia/Ho_Chi_Minh" in response.text
    assert "Token theo ngày" not in response.text
    assert "Top tools" not in response.text
    assert 'href="/evals"' not in response.text


def test_stats_empty_db(client):
    response = client.get("/stats")

    assert response.status_code == 200
    assert 'class="empty"' in response.text
    assert "Chưa có dữ liệu traffic hoặc hoa hồng." in response.text
    assert "Lượt xem" in response.text
    assert "Lượt bấm" in response.text
    assert "Hoa hồng / click" in response.text


def test_stats_without_owner_fails_closed_for_multiple_users(session, client):
    _seed_affiliate_events(session)
    session.add(User())
    session.commit()

    response = client.get("/stats")

    assert response.status_code == 403
    assert "nhiều user" in response.text
    assert "Review sản phẩm A" not in response.text
    assert "50,000 ₫" not in response.text


def test_stats_authenticated_owner_is_scoped_in_multi_user_database(session):
    _seed_affiliate_events(session)
    selected_owner = User()
    session.add(selected_owner)
    session.flush()
    selected_owner_id = selected_owner.id
    session.add(
        AffiliateEvent(
            user_id=selected_owner_id,
            event_type="commission",
            amount=777_777,
            currency="VND",
            occurred_at=datetime.now(UTC),
        )
    )
    session.commit()

    with _client_with_owner(selected_owner_id) as owner_client:
        response = owner_client.get("/stats")

    assert response.status_code == 200
    assert f"Owner #{selected_owner_id}" in response.text
    assert "777,777 ₫" in response.text
    assert "Review sản phẩm A" not in response.text
    assert "50,000 ₫" not in response.text


@pytest.mark.parametrize("owner_context", [0, "1", True, 999_999])
def test_stats_rejects_malformed_or_stale_owner_context(
    session,
    owner_context,
):
    _seed_affiliate_events(session)

    with _client_with_owner(owner_context) as owner_client:
        response = owner_client.get("/stats")

    assert response.status_code == 403
    assert "owner context" in response.text
    assert "Review sản phẩm A" not in response.text
