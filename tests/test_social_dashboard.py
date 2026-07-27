"""Tests for the read-only Social Affiliate dashboard skeleton."""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from laplace.models import User
from laplace.social.models import (
    AffiliateProduct,
    MediaAsset,
    PublishJob,
    SocialAccount,
    SocialPost,
)
from laplace.web.social.views import router

AUTH_REF = "keychain://must-never-appear-in-html"


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _seed_social_data(session) -> None:
    now = datetime.now(UTC)
    user = User(id=101)
    account = SocialAccount(
        user_id=user.id,
        platform="facebook",
        display_name="Laptop Deals VN",
        external_id="page-101",
        auth_type="page_token",
        auth_ref=AUTH_REF,
        status="active",
        daily_post_limit=3,
    )
    media = MediaAsset(
        user_id=user.id,
        type="video",
        local_path="/private/media/review.mp4",
        original_name="review-laptop.mp4",
        mime_type="video/mp4",
        size_bytes=2048,
        sha256="a" * 64,
        duration_seconds=31,
    )
    product = AffiliateProduct(
        user_id=user.id,
        network="Shopee",
        merchant="Laptop Store",
        product_name="Laptop Air 14",
        product_url="https://example.test/product",
        affiliate_url="https://example.test/affiliate",
    )
    session.add_all([user, account, media, product])
    session.flush()

    approved_post = SocialPost(
        user_id=user.id,
        title="Review Laptop Air 14",
        caption="Ba điểm đáng cân nhắc trước khi mua.",
        hashtags_json=["#laptop"],
        media_asset_id=media.id,
        affiliate_product_id=product.id,
        status="approved",
        content_hash="b" * 64,
        approved_at=now,
    )
    draft_post = SocialPost(
        user_id=user.id,
        title="Laptop checklist",
        caption="Checklist chọn laptop văn phòng.",
        hashtags_json=["#checklist"],
        media_asset_id=media.id,
        status="draft",
        content_hash="c" * 64,
    )
    session.add_all([approved_post, draft_post])
    session.flush()

    session.add_all(
        [
            PublishJob(
                social_post_id=approved_post.id,
                social_account_id=account.id,
                scheduled_at=now + timedelta(hours=1),
                status="queued",
                idempotency_key="d" * 64,
            ),
            PublishJob(
                social_post_id=draft_post.id,
                social_account_id=account.id,
                scheduled_at=now + timedelta(hours=2),
                status="retry",
                attempt_count=1,
                idempotency_key="e" * 64,
            ),
            PublishJob(
                social_post_id=approved_post.id,
                social_account_id=account.id,
                scheduled_at=now - timedelta(hours=2),
                status="failed",
                attempt_count=2,
                idempotency_key="f" * 64,
                last_error=f"redacted; do not render {AUTH_REF}",
            ),
        ]
    )
    session.commit()


def test_social_overview_renders_product_zero_state(session):
    with TestClient(_test_app()) as client:
        response = client.get("/social/")

    assert response.status_code == 200
    assert "Social Affiliate" in response.text
    assert "Workspace social chưa có dữ liệu." in response.text
    assert "<title>Social Affiliate · Arya_Tool</title>" in response.text

    for label in ("Accounts", "Content", "Scheduled", "Failed"):
        assert label in response.text

    # An empty database must remain a useful product-specific zero state.
    assert response.text.count('<div class="v">0</div>') == 4


def test_social_overview_renders_database_counts(session):
    _seed_social_data(session)

    with TestClient(_test_app()) as client:
        response = client.get("/social/")

    assert response.status_code == 200
    for label, value in (
        ("Accounts", 1),
        ("Content", 2),
        ("Scheduled", 2),
        ("Failed", 1),
    ):
        assert f'aria-label="{label}: {value}.' in response.text
    assert "Workspace đã sẵn sàng." in response.text
    assert AUTH_REF not in response.text


def test_social_overview_has_accessible_navigation_to_planned_routes(session):
    with TestClient(_test_app()) as client:
        response = client.get("/social/")

    html = response.text
    assert 'aria-label="Điều hướng Social Affiliate"' in html
    assert 'href="/social/" aria-current="page"' in html
    for path in (
        "/social/accounts",
        "/social/content",
        "/social/calendar",
        "/social/jobs",
        "/social/affiliate",
        "/social/settings",
    ):
        assert f'href="{path}"' in html


def test_read_only_pages_render_rows_without_auth_reference(session):
    _seed_social_data(session)

    with TestClient(_test_app()) as client:
        accounts = client.get("/social/accounts")
        content = client.get("/social/content")
        calendar = client.get("/social/calendar")
        jobs = client.get("/social/jobs")

    for response in (accounts, content, calendar, jobs):
        assert response.status_code == 200
        assert AUTH_REF not in response.text

    assert "Laptop Deals VN" in accounts.text
    assert "page_token" in accounts.text
    assert "3 connected" not in accounts.text
    assert "1 connected" in accounts.text

    assert "Review Laptop Air 14" in content.text
    assert "Laptop checklist" in content.text
    assert "review-laptop.mp4" in content.text
    assert "Laptop Air 14" in content.text

    assert "2 upcoming" in calendar.text
    assert '<span class="status-text">queued</span>' in calendar.text
    assert '<span class="status-text">retry</span>' in calendar.text
    assert '<span class="status-text">failed</span>' not in calendar.text

    assert "3" in jobs.text
    assert "queued" in jobs.text
    assert "retry" in jobs.text
    assert "failed" in jobs.text


def test_jobs_status_filter_only_renders_matching_rows(session):
    _seed_social_data(session)

    with TestClient(_test_app()) as client:
        response = client.get("/social/jobs?status=failed")

    assert response.status_code == 200
    assert "1 · status=failed" in response.text
    assert '<span class="status-text">failed</span>' in response.text
    assert '<span class="status-text">queued</span>' not in response.text
    assert '<span class="status-text">retry</span>' not in response.text
    assert AUTH_REF not in response.text


def test_read_only_pages_render_product_specific_zero_states(session):
    with TestClient(_test_app()) as client:
        responses = {
            "/social/accounts": "Chưa kết nối Page hoặc tài khoản nào.",
            "/social/content": "Kho nội dung đang trống.",
            "/social/calendar": "Chưa có bài nào trong lịch đăng.",
            "/social/jobs": "Chưa có publish job nào.",
        }
        for path, message in responses.items():
            response = client.get(path)
            assert response.status_code == 200
            assert message in response.text


def test_social_router_exposes_no_mutating_methods(session):
    with TestClient(_test_app()) as client:
        for path in (
            "/social/",
            "/social/accounts",
            "/social/content",
            "/social/calendar",
            "/social/jobs",
        ):
            assert client.post(path).status_code == 405


def test_all_social_pages_keep_api_key_dependency(session, monkeypatch):
    from laplace.config import get_settings

    monkeypatch.setattr(get_settings(), "api_key", "dashboard-key")
    with TestClient(_test_app()) as client:
        for path in (
            "/social/",
            "/social/accounts",
            "/social/content",
            "/social/calendar",
            "/social/jobs",
        ):
            assert client.get(path).status_code == 401
            assert client.get(path, headers={"X-API-Key": "dashboard-key"}).status_code == 200
