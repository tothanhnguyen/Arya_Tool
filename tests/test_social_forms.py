"""Browser form coverage for the local social publishing vertical flow."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from laplace.config import get_settings
from laplace.models import User
from laplace.social.models import (
    AffiliateProduct,
    MediaAsset,
    PublishJob,
    SocialAccount,
    SocialPost,
)
from laplace.web.settings_page import CSRF_TOKEN
from laplace.web.social.views import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(
        app,
        client=("127.0.0.1", 50_000),
        base_url="http://127.0.0.1:8010",
        follow_redirects=False,
    )


def _seed_owned_content(session, *, suffix: str, status: str = "approved") -> dict[str, int]:
    user = User()
    session.add(user)
    session.flush()
    account = SocialAccount(
        user_id=user.id,
        platform="facebook",
        display_name=f"Page {suffix}",
        external_id=f"page-{suffix}",
        auth_type="mock",
        auth_ref=f"mock://secret-{suffix}",
        status="active",
    )
    media = MediaAsset(
        user_id=user.id,
        type="image",
        local_path=f"/tmp/{suffix}.png",
        original_name=f"{suffix}.png",
        mime_type="image/png",
        size_bytes=8,
        sha256=suffix[0] * 64,
    )
    product = AffiliateProduct(
        user_id=user.id,
        network="Demo",
        merchant=f"Shop {suffix}",
        product_name=f"Product {suffix}",
        product_url=f"https://example.test/{suffix}",
        affiliate_url=f"https://example.test/go/{suffix}",
    )
    session.add_all([account, media, product])
    session.flush()
    post = SocialPost(
        user_id=user.id,
        title=f"Post {suffix}",
        caption="Nội dung đã được kiểm tra.",
        hashtags_json=["#review"],
        media_asset_id=media.id,
        affiliate_product_id=product.id,
        status=status,
        content_hash=suffix[-1] * 64,
        approved_at=datetime.now(UTC) if status != "draft" else None,
    )
    session.add(post)
    session.commit()
    return {
        "user_id": user.id,
        "account_id": account.id,
        "media_id": media.id,
        "product_id": product.id,
        "post_id": post.id,
    }


def test_browser_forms_complete_mock_vertical_flow(session, tmp_path, monkeypatch):
    user = User()
    session.add(user)
    session.commit()
    user_id = user.id
    monkeypatch.setattr(get_settings(), "social_media_dir", str(tmp_path / "media"))

    with _client() as client:
        accounts_page = client.get("/social/accounts")
        assert accounts_page.status_code == 200
        assert 'action="/social/accounts/mock"' in accounts_page.text

        account = client.post(
            "/social/accounts/mock",
            data={
                "csrf": CSRF_TOKEN,
                "user_id": user_id,
                "platform": "facebook",
                "display_name": "Page Demo",
                "external_id": "page-demo",
            },
        )
        assert account.status_code == 303

        media = client.post(
            "/social/media",
            data={"csrf": CSRF_TOKEN, "user_id": user_id},
            files={
                "file": (
                    "demo.png",
                    b"\x89PNG\r\n\x1a\nminimal-payload",
                    "image/png",
                )
            },
        )
        assert media.status_code == 303

        product = client.post(
            "/social/products",
            data={
                "csrf": CSRF_TOKEN,
                "user_id": user_id,
                "network": "Affiliate Demo",
                "merchant": "Demo Shop",
                "product_name": "Sản phẩm Demo",
                "product_url": "https://example.test/product",
                "affiliate_url": "https://example.test/go",
            },
        )
        assert product.status_code == 303

        session.expire_all()
        media_id = session.scalar(select(MediaAsset.id).where(MediaAsset.user_id == user_id))
        product_id = session.scalar(
            select(AffiliateProduct.id).where(AffiliateProduct.user_id == user_id)
        )
        account_id = session.scalar(
            select(SocialAccount.id).where(SocialAccount.user_id == user_id)
        )
        assert media_id is not None
        assert product_id is not None
        assert account_id is not None

        draft = client.post(
            "/social/content/drafts",
            data={
                "csrf": CSRF_TOKEN,
                "user_id": user_id,
                "title": "Review sản phẩm",
                "caption": "Nội dung đã kiểm tra trước khi duyệt.",
                "hashtags": "#review, deal",
                "media_asset_id": media_id,
                "affiliate_product_id": product_id,
            },
        )
        assert draft.status_code == 303

        session.expire_all()
        post_id = session.scalar(select(SocialPost.id).where(SocialPost.user_id == user_id))
        assert post_id is not None
        approved = client.post(
            f"/social/content/{post_id}/approve",
            data={"csrf": CSRF_TOKEN, "user_id": user_id},
        )
        assert approved.status_code == 303

        local_now = datetime.now(ZoneInfo(get_settings().social_timezone))
        scheduled = client.post(
            "/social/calendar/schedule",
            data={
                "csrf": CSRF_TOKEN,
                "user_id": user_id,
                "social_post_id": post_id,
                "social_account_id": account_id,
                "scheduled_at": (local_now + timedelta(hours=2)).strftime(
                    "%Y-%m-%dT%H:%M"
                ),
            },
        )
        assert scheduled.status_code == 303

        session.expire_all()
        job_id = session.scalar(
            select(PublishJob.id).where(PublishJob.social_post_id == post_id)
        )
        assert job_id is not None
        calendar = client.get("/social/calendar")
        jobs = client.get("/social/jobs")
        assert f'action="/social/calendar/jobs/{job_id}/cancel"' in calendar.text
        assert f'action="/social/jobs/{job_id}/cancel"' in jobs.text
        assert "mock://secret" not in accounts_page.text + calendar.text + jobs.text

        cancelled = client.post(
            f"/social/calendar/jobs/{job_id}/cancel",
            data={"csrf": CSRF_TOKEN, "user_id": user_id},
        )
        assert cancelled.status_code == 303

    session.expire_all()
    assert session.get(PublishJob, job_id).status == "cancelled"
    assert session.get(SocialPost, post_id).status == "approved"


@pytest.mark.parametrize(
    "path",
    [
        "/social/accounts/mock",
        "/social/media",
        "/social/products",
        "/social/content/drafts",
        "/social/content/999/approve",
        "/social/calendar/schedule",
        "/social/calendar/jobs/999/cancel",
        "/social/jobs/999/cancel",
    ],
)
def test_social_write_forms_reject_bad_csrf(session, path):
    with _client() as client:
        response = client.post(path, data={"csrf": "wrong"})

    assert response.status_code == 403
    assert "CSRF" in response.text
    assert session.scalar(select(func.count()).select_from(SocialAccount)) == 0
    assert session.scalar(select(func.count()).select_from(MediaAsset)) == 0
    assert session.scalar(select(func.count()).select_from(AffiliateProduct)) == 0
    assert session.scalar(select(func.count()).select_from(SocialPost)) == 0
    assert session.scalar(select(func.count()).select_from(PublishJob)) == 0


def test_social_write_forms_keep_api_key_dependency(session, monkeypatch):
    user = User()
    session.add(user)
    session.commit()
    monkeypatch.setattr(get_settings(), "api_key", "dashboard-key")
    payload = {
        "csrf": CSRF_TOKEN,
        "user_id": user.id,
        "platform": "facebook",
        "display_name": "Protected Page",
        "external_id": "protected-page",
    }

    with _client() as client:
        denied = client.post("/social/accounts/mock", data=payload)
        allowed = client.post(
            "/social/accounts/mock",
            data=payload,
            headers={"X-API-Key": "dashboard-key"},
        )

    assert denied.status_code == 401
    assert allowed.status_code == 303


def test_forms_enforce_owner_isolation(session):
    owner = _seed_owned_content(session, suffix="a1")
    other = User()
    session.add(other)
    session.commit()
    other_id = other.id
    job = PublishJob(
        social_post_id=owner["post_id"],
        social_account_id=owner["account_id"],
        scheduled_at=datetime.now(UTC) + timedelta(hours=3),
        status="queued",
        idempotency_key="j" * 64,
    )
    session.add(job)
    session.commit()
    job_id = job.id

    with _client() as client:
        cross_draft = client.post(
            "/social/content/drafts",
            data={
                "csrf": CSRF_TOKEN,
                "user_id": other_id,
                "title": "Không thuộc owner",
                "caption": "Không được tạo bằng media của owner khác.",
                "media_asset_id": owner["media_id"],
                "affiliate_product_id": owner["product_id"],
            },
        )
        cross_schedule = client.post(
            "/social/calendar/schedule",
            data={
                "csrf": CSRF_TOKEN,
                "user_id": other_id,
                "social_post_id": owner["post_id"],
                "social_account_id": owner["account_id"],
                "scheduled_at": (datetime.now(UTC) + timedelta(hours=4)).isoformat(),
            },
        )
        cross_cancel = client.post(
            f"/social/calendar/jobs/{job_id}/cancel",
            data={"csrf": CSRF_TOKEN, "user_id": other_id},
        )

    assert cross_draft.status_code == 409
    assert cross_schedule.status_code == 409
    assert cross_cancel.status_code == 409
    assert "mock://secret" not in (
        cross_draft.text + cross_schedule.text + cross_cancel.text
    )
    session.expire_all()
    assert (
        session.scalar(
            select(func.count())
            .select_from(SocialPost)
            .where(SocialPost.user_id == other_id)
        )
        == 0
    )
    assert session.get(PublishJob, job_id).status == "queued"


def test_media_form_rejects_spoofed_mime_and_oversize(
    session,
    tmp_path,
    monkeypatch,
):
    user = User()
    session.add(user)
    session.commit()
    user_id = user.id
    settings = get_settings()
    monkeypatch.setattr(settings, "social_media_dir", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "social_media_max_bytes", 100)

    with _client() as client:
        spoofed = client.post(
            "/social/media",
            data={"csrf": CSRF_TOKEN, "user_id": user_id},
            files={"file": ("fake.png", b"<script>x</script>", "image/png")},
        )
        monkeypatch.setattr(settings, "social_media_max_bytes", 8)
        oversize = client.post(
            "/social/media",
            data={"csrf": CSRF_TOKEN, "user_id": user_id},
            files={
                "file": (
                    "large.png",
                    b"\x89PNG\r\n\x1a\nextra",
                    "image/png",
                )
            },
        )

    assert spoofed.status_code == 415
    assert "định dạng" in spoofed.text
    assert oversize.status_code == 413
    assert "dung lượng" in oversize.text
    assert session.scalar(select(func.count()).select_from(MediaAsset)) == 0


def test_forms_reject_invalid_input_and_transitions(session):
    owned = _seed_owned_content(session, suffix="b2")
    draft = SocialPost(
        user_id=owned["user_id"],
        title="Draft chưa duyệt",
        caption="Chưa được phép lên lịch.",
        hashtags_json=[],
        media_asset_id=owned["media_id"],
        status="draft",
        content_hash="d" * 64,
    )
    published_job = PublishJob(
        social_post_id=owned["post_id"],
        social_account_id=owned["account_id"],
        scheduled_at=datetime.now(UTC) - timedelta(hours=1),
        status="published",
        idempotency_key="p" * 64,
        published_at=datetime.now(UTC),
    )
    session.add_all([draft, published_job])
    session.commit()
    draft_id = draft.id
    published_job_id = published_job.id

    with _client() as client:
        blank_account = client.post(
            "/social/accounts/mock",
            data={
                "csrf": CSRF_TOKEN,
                "user_id": owned["user_id"],
                "platform": "facebook",
                "display_name": "   ",
                "external_id": "blank-name",
            },
        )
        approve_twice = client.post(
            f"/social/content/{owned['post_id']}/approve",
            data={"csrf": CSRF_TOKEN, "user_id": owned["user_id"]},
        )
        schedule_draft = client.post(
            "/social/calendar/schedule",
            data={
                "csrf": CSRF_TOKEN,
                "user_id": owned["user_id"],
                "social_post_id": draft_id,
                "social_account_id": owned["account_id"],
                "scheduled_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        cancel_published = client.post(
            f"/social/jobs/{published_job_id}/cancel",
            data={"csrf": CSRF_TOKEN, "user_id": owned["user_id"]},
        )

    assert blank_account.status_code == 400
    assert approve_twice.status_code == 409
    assert schedule_draft.status_code == 409
    assert cancel_published.status_code == 409
    session.expire_all()
    assert session.get(SocialPost, owned["post_id"]).status == "approved"
    assert session.get(SocialPost, draft_id).status == "draft"
    assert session.get(PublishJob, published_job_id).status == "published"
