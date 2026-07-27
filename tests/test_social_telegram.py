"""Tests for Telegram social status commands and owner-only state changes."""

from datetime import UTC, datetime, timedelta

from laplace.bot.social_handlers import (
    _account_action_preview,
    _accounts_text,
    _apply_account_action,
    _queue_text,
    _report_text,
)
from laplace.models import User
from laplace.social.models import MediaAsset, PublishJob, SocialAccount, SocialPost


def _seed(session):
    user = User(tg_id=111)
    other = User(tg_id=222)
    session.add_all([user, other])
    session.flush()
    account = SocialAccount(
        user_id=user.id,
        platform="facebook",
        display_name="Page Demo",
        external_id="page-demo",
        auth_type="page_token",
        auth_ref="keychain://must-not-leak",
    )
    media = MediaAsset(
        user_id=user.id,
        type="video",
        local_path="/private/demo.mp4",
        original_name="demo.mp4",
        mime_type="video/mp4",
        size_bytes=1,
        sha256="a" * 64,
    )
    session.add_all([account, media])
    session.flush()
    post = SocialPost(
        user_id=user.id,
        title="Video demo",
        caption="Caption",
        hashtags_json=[],
        media_asset_id=media.id,
        status="approved",
        content_hash="b" * 64,
    )
    session.add(post)
    session.flush()
    job = PublishJob(
        social_post_id=post.id,
        social_account_id=account.id,
        scheduled_at=datetime.now(UTC) + timedelta(minutes=10),
        status="queued",
        idempotency_key="c" * 64,
    )
    session.add(job)
    session.commit()
    return account


def test_social_status_commands_are_owned_and_secret_free(session):
    _seed(session)

    output = "\n".join(
        (_accounts_text(111), _queue_text(111), _report_text(111))
    )

    assert "Page Demo" in output
    assert "Video demo" in output
    assert "Đang chờ/retry: 1" in output
    assert "keychain://" not in output
    assert "auth_ref" not in output
    assert "Page Demo" not in _accounts_text(222)


def test_pause_resume_requires_preview_and_owner(session):
    account = _seed(session)

    text, allowed = _account_action_preview(111, account.id, "pause")
    assert allowed
    assert "Xác nhận" in text

    denied = _apply_account_action(222, account.id, "pause")
    assert "quyền" in denied

    paused = _apply_account_action(111, account.id, "pause")
    assert "paused" in paused
    invalid_text, invalid_allowed = _account_action_preview(111, account.id, "pause")
    assert not invalid_allowed
    assert "paused" in invalid_text

    resumed = _apply_account_action(111, account.id, "resume")
    assert "active" in resumed


def test_today_queue_uses_local_day_without_leaking_credentials(session):
    _seed(session)

    text = _queue_text(111, today_only=True)

    assert "Lịch hôm nay" in text
    assert "keychain://" not in text
