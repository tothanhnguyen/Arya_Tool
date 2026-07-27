"""End-to-end local API flow from upload to a queued mock publish job."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from laplace.config import get_settings
from laplace.models import User
from laplace.social.models import AffiliateEvent
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


def test_affiliate_event_api_scopes_external_id_to_owner(session):
    first_owner = User()
    second_owner = User()
    session.add_all([first_owner, second_owner])
    session.commit()
    occurred_at = datetime.now(UTC).isoformat()

    def payload(user_id: int) -> dict:
        return {
            "user_id": user_id,
            "event_type": "commission",
            "amount": "123.456789",
            "currency": "VND",
            "source": "network-a",
            "external_event_id": "shared-order",
            "occurred_at": occurred_at,
        }

    with TestClient(create_app()) as client:
        first = client.post("/api/social/events", json=payload(first_owner.id))
        second = client.post("/api/social/events", json=payload(second_owner.id))
        duplicate = client.post("/api/social/events", json=payload(first_owner.id))

    assert first.status_code == 201
    assert second.status_code == 201
    assert duplicate.status_code == 409
    session.expire_all()
    events = session.scalars(
        select(AffiliateEvent).order_by(AffiliateEvent.user_id)
    ).all()
    assert len(events) == 2
    assert {event.user_id for event in events} == {first_owner.id, second_owner.id}
    assert {event.amount for event in events} == {Decimal("123.456789")}


def test_affiliate_event_api_validates_numeric_precision(session):
    user = User()
    session.add(user)
    session.commit()
    base = {
        "user_id": user.id,
        "event_type": "commission",
        "currency": "VND",
        "occurred_at": datetime.now(UTC).isoformat(),
    }

    with TestClient(create_app()) as client:
        too_wide = client.post(
            "/api/social/events",
            json={**base, "amount": "1000000000000"},
        )
        too_precise = client.post(
            "/api/social/events",
            json={**base, "amount": "0.0000001"},
        )

    assert too_wide.status_code == 422
    assert too_precise.status_code == 422
    assert session.scalar(select(AffiliateEvent.id)) is None


def test_affiliate_event_api_hides_integrity_race_details(
    session,
    monkeypatch,
):
    from laplace.web.social import api as social_api

    user = User()
    session.add(user)
    session.commit()
    original_scope = social_api.session_scope
    scope_calls = 0

    @contextmanager
    def racing_scope():
        nonlocal scope_calls
        scope_calls += 1
        with original_scope() as db:
            if scope_calls == 2:

                def fail_flush(*_args, **_kwargs):
                    raise IntegrityError(
                        "INSERT secret_table",
                        {"secret": "must-not-leak"},
                        RuntimeError("database-secret"),
                    )

                monkeypatch.setattr(db, "flush", fail_flush)
            yield db

    monkeypatch.setattr(social_api, "session_scope", racing_scope)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/social/events",
            json={
                "user_id": user.id,
                "event_type": "click",
                "source": "network-a",
                "external_event_id": "race-id",
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )

    assert response.status_code == 409
    assert "xung đột" in response.text
    assert "secret" not in response.text
    assert session.scalar(select(AffiliateEvent.id)) is None
