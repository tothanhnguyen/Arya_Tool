"""End-to-end local API flow from upload to a queued mock publish job."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from laplace.config import get_settings
from laplace.models import User
from laplace.web.app import create_app


def test_mock_vertical_flow(session, tmp_path, monkeypatch):
    user = User()
    session.add(user)
    session.commit()
    monkeypatch.setattr(get_settings(), "social_media_dir", str(tmp_path / "media"))

    with TestClient(create_app()) as client:
        account = client.post(
            "/api/social/accounts/mock",
            json={
                "user_id": user.id,
                "platform": "facebook",
                "display_name": "Page Demo",
                "external_id": "page-demo",
            },
        )
        assert account.status_code == 201
        assert "auth_ref" not in account.text

        media = client.post(
            f"/api/social/media?user_id={user.id}",
            files={
                "file": (
                    "demo.png",
                    b"\x89PNG\r\n\x1a\nminimal-test-payload",
                    "image/png",
                )
            },
        )
        assert media.status_code == 201

        product = client.post(
            "/api/social/products",
            json={
                "user_id": user.id,
                "network": "Affiliate Demo",
                "merchant": "Merchant",
                "product_name": "Sản phẩm A",
                "product_url": "https://example.test/product",
                "affiliate_url": "https://example.test/go",
            },
        )
        assert product.status_code == 201

        draft = client.post(
            "/api/social/content/drafts",
            json={
                "user_id": user.id,
                "title": "Review sản phẩm",
                "caption": "Nội dung đã kiểm tra",
                "hashtags": ["review"],
                "media_asset_id": media.json()["id"],
                "affiliate_product_id": product.json()["id"],
            },
        )
        assert draft.status_code == 201
        assert draft.json()["status"] == "draft"

        approved = client.post(
            f"/api/social/content/{draft.json()['id']}/approve",
            json={"user_id": user.id},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        scheduled = client.post(
            "/api/social/jobs",
            json={
                "user_id": user.id,
                "social_post_id": draft.json()["id"],
                "social_account_id": account.json()["id"],
                "scheduled_at": (
                    datetime.now(UTC) + timedelta(hours=1)
                ).isoformat(),
            },
        )
        assert scheduled.status_code == 201
        assert scheduled.json()["status"] == "queued"

        dashboard = client.get("/social/")
        assert dashboard.status_code == 200
        assert 'aria-label="Accounts: 1.' in dashboard.text
        assert 'aria-label="Content: 1.' in dashboard.text
        assert 'aria-label="Scheduled: 1.' in dashboard.text


def test_media_upload_rejects_unsupported_type(session):
    user = User()
    session.add(user)
    session.commit()

    with TestClient(create_app()) as client:
        response = client.post(
            f"/api/social/media?user_id={user.id}",
            files={"file": ("payload.html", b"<script>x</script>", "text/html")},
        )

    assert response.status_code == 415


def test_media_upload_rejects_spoofed_mime(session):
    user = User()
    session.add(user)
    session.commit()

    with TestClient(create_app()) as client:
        response = client.post(
            f"/api/social/media?user_id={user.id}",
            files={"file": ("payload.png", b"<script>x</script>", "image/png")},
        )

    assert response.status_code == 415


def test_social_write_api_requires_key(session, monkeypatch):
    user = User()
    session.add(user)
    session.commit()
    monkeypatch.setattr(get_settings(), "api_key", "secret-key")

    with TestClient(create_app()) as client:
        denied = client.post(
            "/api/social/accounts/mock",
            json={
                "user_id": user.id,
                "platform": "facebook",
                "display_name": "Page",
                "external_id": "page",
            },
        )
        allowed = client.post(
            "/api/social/accounts/mock",
            headers={"X-API-Key": "secret-key"},
            json={
                "user_id": user.id,
                "platform": "facebook",
                "display_name": "Page",
                "external_id": "page",
            },
        )

    assert denied.status_code == 401
    assert allowed.status_code == 201


def test_affiliate_event_api_records_dashboard_data(session):
    user = User()
    session.add(user)
    session.commit()
    occurred_at = datetime.now(UTC).isoformat()

    with TestClient(create_app()) as client:
        view = client.post(
            "/api/social/events",
            json={
                "user_id": user.id,
                "event_type": "view",
                "occurred_at": occurred_at,
            },
        )
        click = client.post(
            "/api/social/events",
            json={
                "user_id": user.id,
                "event_type": "click",
                "occurred_at": occurred_at,
            },
        )
        commission = client.post(
            "/api/social/events",
            json={
                "user_id": user.id,
                "event_type": "commission",
                "amount": 15_000,
                "currency": "VND",
                "occurred_at": occurred_at,
            },
        )
        analytics = client.get("/stats")

    assert view.status_code == 201
    assert click.status_code == 201
    assert commission.status_code == 201
    assert "15,000 ₫" in analytics.text
    assert "100.0%" in analytics.text
