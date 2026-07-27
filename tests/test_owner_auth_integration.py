"""Negative integration tests for authenticated dashboard owner boundaries."""

from datetime import UTC, datetime, timedelta

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import select

from laplace.models import Task, User
from laplace.social.models import (
    AffiliateProduct,
    MediaAsset,
    PublishJob,
    SocialAccount,
    SocialPost,
)
from laplace.web import api as task_api
from laplace.web.app import create_app
from laplace.web.deps import require_api_key
from laplace.web.settings_page import CSRF_TOKEN

BEARER_HEADERS = {"Authorization": "Bearer authenticated-owner-session"}


def _authenticated_app(owner_id: int):
    app = create_app()

    def authenticated_owner(request: Request) -> None:
        request.state.user_id = owner_id

    app.dependency_overrides[require_api_key] = authenticated_owner
    return app


def _account(user_id: int, marker: str, *, platform: str = "facebook") -> SocialAccount:
    return SocialAccount(
        user_id=user_id,
        platform=platform,
        display_name=f"{marker} account",
        external_id=f"{marker}-external",
        auth_type="mock",
        auth_ref=f"mock://{marker}",
        status="active",
    )


def _seed_social_owner(session, user_id: int, marker: str) -> dict[str, int]:
    account = _account(user_id, marker)
    media = MediaAsset(
        user_id=user_id,
        type="image",
        local_path=f"/tmp/{marker}.png",
        original_name=f"{marker}.png",
        mime_type="image/png",
        size_bytes=12,
        sha256=(marker.encode().hex() + ("0" * 64))[:64],
    )
    product = AffiliateProduct(
        user_id=user_id,
        network=f"{marker} network",
        merchant=f"{marker} merchant",
        product_name=f"{marker} product",
        product_url=f"https://example.test/{marker}",
        affiliate_url=f"https://example.test/{marker}/go",
    )
    session.add_all([account, media, product])
    session.flush()
    post = SocialPost(
        user_id=user_id,
        title=f"{marker} post",
        caption=f"{marker} caption",
        media_asset_id=media.id,
        affiliate_product_id=product.id,
        status="approved",
        content_hash=(marker.encode().hex() + ("f" * 64))[:64],
    )
    session.add(post)
    session.flush()
    job = PublishJob(
        social_post_id=post.id,
        social_account_id=account.id,
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
        status="queued",
        idempotency_key=f"{marker}-job",
    )
    session.add(job)
    session.flush()
    return {
        "account_id": account.id,
        "media_id": media.id,
        "product_id": product.id,
        "post_id": post.id,
        "job_id": job.id,
    }


def test_authenticated_task_routes_hide_other_owner_and_reject_override(
    session,
    monkeypatch,
):
    owner = User()
    other = User()
    session.add_all([owner, other])
    session.flush()
    own_task = Task(user_id=owner.id, request="OWNER-TASK", status="awaiting_confirm")
    other_task = Task(user_id=other.id, request="OTHER-TASK", status="awaiting_confirm")
    session.add_all([own_task, other_task])
    session.commit()
    monkeypatch.setattr(task_api, "_run_task_bg", lambda _task_id: None)

    app = _authenticated_app(owner.id)
    with TestClient(app) as client:
        listing = client.get("/")
        hidden_routes = [
            client.get(f"/api/tasks/{other_task.id}"),
            client.get(f"/api/tasks/{other_task.id}/trace"),
            client.get(f"/tasks/{other_task.id}"),
            client.get(f"/tasks/{other_task.id}/replay"),
            client.get(f"/tasks/{other_task.id}/export.json"),
            client.get(f"/tasks/{other_task.id}/export.md"),
        ]
        denied_confirm = client.post(
            f"/api/tasks/{other_task.id}/confirm",
            headers=BEARER_HEADERS,
            json={"approved": True},
        )
        denied_override = client.post(
            "/api/tasks",
            headers=BEARER_HEADERS,
            json={"user_id": other.id, "request": "cross-owner"},
        )
        created = client.post(
            "/api/tasks",
            headers=BEARER_HEADERS,
            json={"request": "owner-created"},
        )

    assert listing.status_code == 200
    assert "OWNER-TASK" in listing.text
    assert "OTHER-TASK" not in listing.text
    assert all(response.status_code == 404 for response in hidden_routes)
    assert denied_confirm.status_code == 404
    assert denied_override.status_code == 403
    assert created.status_code == 202
    created_task = session.get(Task, created.json()["id"])
    assert created_task is not None
    assert created_task.user_id == owner.id


def test_authenticated_api_mutations_require_bearer_not_cookie(session):
    owner = User()
    session.add(owner)
    session.commit()
    app = _authenticated_app(owner.id)

    with TestClient(app) as client:
        client.cookies.set("arya_supabase_access_token", "cookie-session")
        task_response = client.post("/api/tasks", json={"request": "cookie write"})
        social_response = client.post(
            "/api/social/accounts/mock",
            json={
                "user_id": owner.id,
                "platform": "facebook",
                "display_name": "Cookie account",
                "external_id": "cookie-account",
            },
        )

    assert task_response.status_code == 403
    assert social_response.status_code == 403
    assert session.scalar(select(Task.id)) is None
    assert session.scalar(select(SocialAccount.id)) is None


def test_authenticated_social_views_and_mutations_are_owner_scoped(
    session,
):
    owner = User()
    other = User()
    session.add_all([owner, other])
    session.flush()
    own = _seed_social_owner(session, owner.id, "OWNER")
    foreign = _seed_social_owner(session, other.id, "FOREIGN")
    session.commit()
    app = _authenticated_app(owner.id)

    with TestClient(
        app,
        client=("127.0.0.1", 50_000),
        base_url="http://127.0.0.1",
    ) as client:
        pages = [
            client.get("/social/accounts"),
            client.get("/social/content"),
            client.get("/social/calendar"),
            client.get("/social/jobs"),
        ]
        account_override = client.post(
            "/api/social/accounts/mock",
            headers=BEARER_HEADERS,
            json={
                "user_id": other.id,
                "platform": "facebook",
                "display_name": "Cross owner",
                "external_id": "cross-owner",
            },
        )
        owner_account = client.post(
            "/api/social/accounts/mock",
            headers=BEARER_HEADERS,
            json={
                "user_id": owner.id,
                "platform": "facebook",
                "display_name": "Owner bearer account",
                "external_id": "owner-bearer-account",
            },
        )
        media_override = client.post(
            f"/api/social/media?user_id={other.id}",
            headers=BEARER_HEADERS,
            files={
                "file": (
                    "foreign.png",
                    b"\x89PNG\r\n\x1a\npayload",
                    "image/png",
                )
            },
        )
        product_override = client.post(
            "/api/social/products",
            headers=BEARER_HEADERS,
            json={
                "user_id": other.id,
                "network": "network",
                "merchant": "merchant",
                "product_name": "product",
                "product_url": "https://example.test/product",
                "affiliate_url": "https://example.test/go",
            },
        )
        draft_override = client.post(
            "/api/social/content/drafts",
            headers=BEARER_HEADERS,
            json={
                "user_id": other.id,
                "title": "title",
                "caption": "caption",
                "media_asset_id": foreign["media_id"],
            },
        )
        approve_foreign = client.post(
            f"/api/social/content/{foreign['post_id']}/approve",
            headers=BEARER_HEADERS,
            json={"user_id": owner.id},
        )
        job_override = client.post(
            "/api/social/jobs",
            headers=BEARER_HEADERS,
            json={
                "user_id": other.id,
                "social_post_id": foreign["post_id"],
                "social_account_id": foreign["account_id"],
                "scheduled_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            },
        )
        cancel_foreign = client.post(
            f"/api/social/jobs/{foreign['job_id']}/cancel",
            headers=BEARER_HEADERS,
            json={"user_id": owner.id},
        )
        event_override = client.post(
            "/api/social/events",
            headers=BEARER_HEADERS,
            json={
                "user_id": other.id,
                "event_type": "view",
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        form_override = client.post(
            "/social/accounts/mock",
            data={
                "csrf": CSRF_TOKEN,
                "user_id": other.id,
                "platform": "facebook",
                "display_name": "Cross owner form",
                "external_id": "cross-owner-form",
            },
        )

    for page in pages:
        assert page.status_code == 200
        assert "OWNER" in page.text
        assert "FOREIGN" not in page.text
    assert account_override.status_code == 403
    assert owner_account.status_code == 201
    assert media_override.status_code == 403
    assert product_override.status_code == 403
    assert draft_override.status_code == 403
    assert approve_foreign.status_code == 409
    assert job_override.status_code == 403
    assert cancel_foreign.status_code == 409
    assert event_override.status_code == 403
    assert form_override.status_code == 403
    assert session.get(SocialPost, own["post_id"]).status == "approved"
    assert session.get(SocialPost, foreign["post_id"]).status == "approved"
    assert session.get(PublishJob, foreign["job_id"]).status == "queued"
    created_account = session.get(SocialAccount, owner_account.json()["id"])
    assert created_account is not None
    assert created_account.user_id == owner.id


def test_authenticated_connect_routes_reject_owner_query_and_form_override(session):
    owner = User()
    other = User()
    session.add_all([owner, other])
    session.flush()
    facebook = _account(other.id, "FOREIGN-FACEBOOK")
    facebook.auth_type = "browser_profile"
    facebook.auth_ref = f"browser-profile://{other.id}/pending"
    instagram = _account(other.id, "FOREIGN-INSTAGRAM", platform="instagram")
    instagram.auth_type = "browser_profile"
    session.add_all([facebook, instagram])
    session.flush()
    facebook.auth_ref = f"browser-profile://{other.id}/{facebook.id}"
    instagram.auth_ref = (
        f"browser-profile://instagram/{other.id}/{instagram.id}"
    )
    session.commit()
    app = _authenticated_app(owner.id)

    with TestClient(
        app,
        client=("127.0.0.1", 50_000),
        base_url="http://127.0.0.1",
    ) as client:
        facebook_query = client.get(
            "/social/facebook/connect",
            params={"user_id": other.id, "social_account_id": facebook.id},
        )
        instagram_query = client.get(
            "/social/instagram/connect",
            params={"user_id": other.id, "account_id": instagram.id},
        )
        facebook_foreign_resource = client.get(
            "/social/facebook/connect",
            params={"social_account_id": facebook.id},
        )
        instagram_foreign_resource = client.get(
            "/social/instagram/connect",
            params={"account_id": instagram.id},
        )
        facebook_form = client.post(
            f"/social/facebook/connect/{facebook.id}/launch",
            data={"csrf": CSRF_TOKEN, "user_id": other.id},
        )
        instagram_form = client.post(
            f"/social/instagram/{instagram.id}/launch",
            data={"csrf": CSRF_TOKEN, "user_id": other.id},
        )

    assert facebook_query.status_code == 403
    assert instagram_query.status_code == 403
    assert facebook_foreign_resource.status_code == 404
    assert instagram_foreign_resource.status_code == 404
    assert facebook_form.status_code == 403
    assert instagram_form.status_code == 403


def test_trace_destructive_posts_require_same_origin_and_owner(session):
    owner = User()
    other = User()
    session.add_all([owner, other])
    session.flush()
    own_task = Task(user_id=owner.id, request="owner delete", status="done")
    other_task = Task(user_id=other.id, request="foreign survives", status="done")
    own_cleanup_task = Task(
        user_id=owner.id,
        request="owner cleanup",
        status="done",
        created_at=datetime(2020, 1, 2, 12, tzinfo=UTC),
    )
    other_cleanup_task = Task(
        user_id=other.id,
        request="foreign cleanup survives",
        status="done",
        created_at=datetime(2020, 1, 2, 12, tzinfo=UTC),
    )
    session.add_all(
        [own_task, other_task, own_cleanup_task, other_cleanup_task]
    )
    session.commit()
    task_ids = {
        "own": own_task.id,
        "other": other_task.id,
        "own_cleanup": own_cleanup_task.id,
        "other_cleanup": other_cleanup_task.id,
    }
    app = _authenticated_app(owner.id)

    with TestClient(
        app,
        base_url="http://127.0.0.1",
        follow_redirects=False,
    ) as client:
        client.cookies.set("arya_supabase_access_token", "cookie-session")
        cross_site = client.post(
            f"/tasks/{own_task.id}/delete",
            headers={"Origin": "https://evil.example"},
        )
        missing_origin = client.post(f"/tasks/{own_task.id}/delete")
        cross_site_cleanup = client.post(
            "/tasks/cleanup",
            params={"day": "2020-01-02"},
            headers={"Origin": "https://evil.example"},
        )
        own_cleanup = client.post(
            "/tasks/cleanup",
            params={"day": "2020-01-02"},
            headers={"Origin": "http://127.0.0.1"},
        )
        foreign_delete = client.post(
            f"/tasks/{other_task.id}/delete",
            headers={"Origin": "http://127.0.0.1"},
        )
        own_delete = client.post(
            f"/tasks/{own_task.id}/delete",
            headers={"Origin": "http://127.0.0.1"},
        )

    assert cross_site.status_code == 403
    assert missing_origin.status_code == 403
    assert cross_site_cleanup.status_code == 403
    assert own_cleanup.status_code == 303
    assert foreign_delete.status_code == 404
    assert own_delete.status_code == 303
    session.expire_all()
    assert session.get(Task, task_ids["own"]) is None
    assert session.get(Task, task_ids["other"]) is not None
    assert session.get(Task, task_ids["own_cleanup"]) is None
    assert session.get(Task, task_ids["other_cleanup"]) is not None
