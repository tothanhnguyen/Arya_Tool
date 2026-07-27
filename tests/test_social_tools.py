from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from laplace.models import User
from laplace.social.models import MediaAsset, PublishJob, SocialAccount, SocialPost
from laplace.tools.base import ToolContext, get_tool
from laplace.tools.social_account import SocialAccountParams, social_account
from laplace.tools.social_content import SocialContentParams, social_content
from laplace.tools.social_schedule import SocialScheduleParams, social_schedule


def _seed_users(session) -> None:
    session.add_all([User(id=1), User(id=2)])
    session.commit()


def _add_account(session, user_id: int, suffix: str, status: str = "active") -> SocialAccount:
    account = SocialAccount(
        user_id=user_id,
        platform="facebook",
        display_name=f"Page {suffix}",
        external_id=f"page-{suffix}",
        auth_type="page_token",
        auth_ref=f"keychain://social/page-{suffix}",
        status=status,
    )
    session.add(account)
    session.commit()
    return account


def _add_media(session, user_id: int, suffix: str) -> MediaAsset:
    media = MediaAsset(
        user_id=user_id,
        type="video",
        local_path=f"/private/media/{suffix}.mp4",
        original_name=f"{suffix}.mp4",
        mime_type="video/mp4",
        size_bytes=100,
        sha256=(suffix[0] * 64),
    )
    session.add(media)
    session.commit()
    return media


def _add_post(
    session, user_id: int, media_id: int, suffix: str, status: str = "approved"
) -> SocialPost:
    post = SocialPost(
        user_id=user_id,
        title=f"Post {suffix}",
        caption=f"Caption {suffix}",
        hashtags_json=["#demo"],
        media_asset_id=media_id,
        status=status,
        content_hash=(suffix[-1] * 64),
        approved_at=datetime.now(UTC) if status != "draft" else None,
    )
    session.add(post)
    session.commit()
    return post


def test_social_tools_confirmation_predicates() -> None:
    account_spec = get_tool("social_account")
    content_spec = get_tool("social_content")
    schedule_spec = get_tool("social_schedule")
    assert account_spec is not None
    assert content_spec is not None
    assert schedule_spec is not None

    assert account_spec.needs_confirm(SocialAccountParams(action="pause", account_id=1))
    assert account_spec.needs_confirm(SocialAccountParams(action="resume", account_id=1))
    assert not account_spec.needs_confirm(SocialAccountParams(action="list"))
    assert not account_spec.needs_confirm(SocialAccountParams(action="status", account_id=1))

    assert content_spec.needs_confirm(SocialContentParams(action="approve", post_id=1))
    assert content_spec.needs_confirm(SocialContentParams(action="delete", post_id=1))
    assert not content_spec.needs_confirm(SocialContentParams(action="list"))

    create = SocialScheduleParams(
        action="create",
        social_post_id=1,
        social_account_id=1,
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert schedule_spec.needs_confirm(create)
    assert schedule_spec.needs_confirm(SocialScheduleParams(action="cancel", job_id=1))
    assert not schedule_spec.needs_confirm(SocialScheduleParams(action="list"))


def test_account_list_is_owned_and_never_exposes_auth_ref(session) -> None:
    _seed_users(session)
    own = _add_account(session, 1, "own")
    _add_account(session, 2, "other")

    result = social_account(SocialAccountParams(action="list"), ToolContext(user_id=1))
    assert result.ok
    assert [item["id"] for item in result.data["accounts"]] == [own.id]
    serialized = str(result.data).lower()
    assert "auth_ref" not in serialized
    assert "keychain://" not in serialized
    assert "cookie" not in serialized


def test_account_pause_resume_transitions_and_ownership(session) -> None:
    _seed_users(session)
    own = _add_account(session, 1, "own")
    other = _add_account(session, 2, "other")

    denied = social_account(
        SocialAccountParams(action="pause", account_id=other.id), ToolContext(user_id=1)
    )
    assert not denied.ok
    assert "not found" in denied.error

    paused = social_account(
        SocialAccountParams(action="pause", account_id=own.id), ToolContext(user_id=1)
    )
    assert paused.ok
    assert paused.data["status"] == "paused"

    invalid = social_account(
        SocialAccountParams(action="pause", account_id=own.id), ToolContext(user_id=1)
    )
    assert not invalid.ok
    assert "cannot be paused" in invalid.error

    resumed = social_account(
        SocialAccountParams(action="resume", account_id=own.id), ToolContext(user_id=1)
    )
    assert resumed.ok
    assert resumed.data["status"] == "active"


def test_content_create_draft_requires_owned_media(session) -> None:
    _seed_users(session)
    other_media = _add_media(session, 2, "b")
    denied = social_content(
        SocialContentParams(
            action="create_draft",
            title="Title",
            caption="Caption",
            media_asset_id=other_media.id,
        ),
        ToolContext(user_id=1),
    )
    assert not denied.ok
    assert "not found" in denied.error


def test_content_create_approve_list_and_delete_transitions(session) -> None:
    _seed_users(session)
    media = _add_media(session, 1, "a")
    created = social_content(
        SocialContentParams(
            action="create_draft",
            title="  Laptop deal  ",
            caption="  Useful review  ",
            hashtags=["#Deal", "deal"],
            media_asset_id=media.id,
        ),
        ToolContext(user_id=1),
    )
    assert created.ok
    assert created.data["status"] == "draft"
    assert created.data["hashtags"] == ["#Deal"]

    approved = social_content(
        SocialContentParams(action="approve", post_id=created.data["id"]),
        ToolContext(user_id=1),
    )
    assert approved.ok
    assert approved.data["status"] == "approved"
    assert approved.data["approved_at"]

    approve_twice = social_content(
        SocialContentParams(action="approve", post_id=created.data["id"]),
        ToolContext(user_id=1),
    )
    assert not approve_twice.ok
    assert "cannot be approved" in approve_twice.error

    listed = social_content(
        SocialContentParams(action="list", status="approved"), ToolContext(user_id=1)
    )
    assert [post["id"] for post in listed.data["posts"]] == [created.data["id"]]

    deleted = social_content(
        SocialContentParams(action="delete", post_id=created.data["id"]),
        ToolContext(user_id=1),
    )
    assert deleted.ok


def test_content_approve_and_delete_cannot_cross_users(session) -> None:
    _seed_users(session)
    media = _add_media(session, 2, "b")
    post = _add_post(session, 2, media.id, "bb", status="draft")
    ctx = ToolContext(user_id=1)

    assert not social_content(
        SocialContentParams(action="approve", post_id=post.id), ctx
    ).ok
    assert not social_content(
        SocialContentParams(action="delete", post_id=post.id), ctx
    ).ok


def test_schedule_create_is_owned_idempotent_and_updates_post(session) -> None:
    _seed_users(session)
    account = _add_account(session, 1, "own")
    media = _add_media(session, 1, "a")
    post = _add_post(session, 1, media.id, "aa")
    when = datetime.now(UTC) + timedelta(hours=2)
    params = SocialScheduleParams(
        action="create",
        social_post_id=post.id,
        social_account_id=account.id,
        scheduled_at=when,
    )

    first = social_schedule(params, ToolContext(user_id=1))
    second = social_schedule(params, ToolContext(user_id=1))
    assert first.ok and second.ok
    assert first.data["id"] == second.data["id"]
    assert first.data["already_existed"] is False
    assert second.data["already_existed"] is True
    assert len(first.data["idempotency_key"]) == 64

    session.expire_all()
    assert session.get(SocialPost, post.id).status == "scheduled"
    assert len(session.scalars(select(PublishJob)).all()) == 1


def test_schedule_rejects_draft_paused_and_cross_user_resources(session) -> None:
    _seed_users(session)
    active = _add_account(session, 1, "active")
    paused = _add_account(session, 1, "paused", status="paused")
    other = _add_account(session, 2, "other")
    media = _add_media(session, 1, "a")
    post = _add_post(session, 1, media.id, "aa", status="draft")
    when = datetime.now(UTC) + timedelta(hours=1)
    ctx = ToolContext(user_id=1)

    draft_result = social_schedule(
        SocialScheduleParams(
            action="create",
            social_post_id=post.id,
            social_account_id=active.id,
            scheduled_at=when,
        ),
        ctx,
    )
    assert not draft_result.ok
    assert "cannot be scheduled" in draft_result.error

    post.status = "approved"
    session.commit()
    paused_result = social_schedule(
        SocialScheduleParams(
            action="create",
            social_post_id=post.id,
            social_account_id=paused.id,
            scheduled_at=when,
        ),
        ctx,
    )
    assert not paused_result.ok
    assert "not active" in paused_result.error

    cross_user = social_schedule(
        SocialScheduleParams(
            action="create",
            social_post_id=post.id,
            social_account_id=other.id,
            scheduled_at=when,
        ),
        ctx,
    )
    assert not cross_user.ok
    assert "not found" in cross_user.error


def test_schedule_cancel_transition_and_ownership(session) -> None:
    _seed_users(session)
    account = _add_account(session, 1, "own")
    media = _add_media(session, 1, "a")
    post = _add_post(session, 1, media.id, "aa")
    when = datetime.now(UTC) + timedelta(hours=1)
    created = social_schedule(
        SocialScheduleParams(
            action="create",
            social_post_id=post.id,
            social_account_id=account.id,
            scheduled_at=when,
        ),
        ToolContext(user_id=1),
    )
    job_id = created.data["id"]

    denied = social_schedule(
        SocialScheduleParams(action="cancel", job_id=job_id), ToolContext(user_id=2)
    )
    assert not denied.ok
    assert "not found" in denied.error

    cancelled = social_schedule(
        SocialScheduleParams(action="cancel", job_id=job_id), ToolContext(user_id=1)
    )
    assert cancelled.ok
    assert cancelled.data["status"] == "cancelled"
    session.expire_all()
    assert session.get(SocialPost, post.id).status == "approved"


def test_schedule_does_not_cancel_running_or_published(session) -> None:
    _seed_users(session)
    account = _add_account(session, 1, "own")
    media = _add_media(session, 1, "a")
    post = _add_post(session, 1, media.id, "aa")
    jobs = []
    for offset, status in enumerate(("running", "published"), start=1):
        job = PublishJob(
            social_post_id=post.id,
            social_account_id=account.id,
            scheduled_at=datetime.now(UTC) + timedelta(hours=offset),
            status=status,
            idempotency_key=str(offset) * 64,
        )
        session.add(job)
        jobs.append(job)
    session.commit()

    for job in jobs:
        result = social_schedule(
            SocialScheduleParams(action="cancel", job_id=job.id), ToolContext(user_id=1)
        )
        assert not result.ok
        assert "cannot be cancelled" in result.error

