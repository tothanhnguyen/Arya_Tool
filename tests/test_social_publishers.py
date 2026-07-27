"""Tests for the credential-free publisher boundary and deterministic mock."""

from dataclasses import FrozenInstanceError

import pytest

from laplace.social.publishers import (
    AccountCheckResult,
    MockPublisher,
    PublishErrorKind,
    PublishRequest,
    PublishResult,
    SocialPublisher,
)


def _request(*, key: str = "idem-1") -> PublishRequest:
    return PublishRequest(
        account_id="page-1",
        post_id="post-1",
        idempotency_key=key,
        caption="A useful review",
        media_paths=("/tmp/review.mp4",),
        affiliate_url="https://example.test/product",
    )


def test_mock_satisfies_runtime_protocol_and_defaults_to_success():
    publisher = MockPublisher()

    assert isinstance(publisher, SocialPublisher)
    result = publisher.publish(_request())

    assert result.success
    assert result.remote_post_id == "mock:page-1:post-1:idem-1"
    assert result.error_kind is None
    assert not result.retryable
    assert publisher.call_count == 1
    assert publisher.side_effect_count == 1
    assert publisher.idempotency_keys == ["idem-1"]


@pytest.mark.parametrize(
    ("outcome", "kind", "retryable"),
    [
        ("network", PublishErrorKind.NETWORK, True),
        ("rate_limit", PublishErrorKind.RATE_LIMIT, True),
        ("auth", PublishErrorKind.AUTH, False),
        ("checkpoint", PublishErrorKind.CHECKPOINT, False),
    ],
)
def test_scripted_publish_errors(outcome, kind, retryable):
    publisher = MockPublisher([outcome])

    result = publisher.publish(_request())

    assert not result.success
    assert result.error_kind is kind
    assert result.retryable is retryable
    assert result.remote_post_id is None
    assert result.retry_after_seconds == (60 if kind is PublishErrorKind.RATE_LIMIT else None)


def test_outcomes_are_fifo_and_failures_do_not_poison_idempotency_key():
    publisher = MockPublisher(["network", "success"])
    request = _request()

    first = publisher.publish(request)
    second = publisher.publish(request)

    assert first.error_kind is PublishErrorKind.NETWORK
    assert second.success
    assert publisher.call_count == 2
    assert publisher.idempotency_keys == ["idem-1", "idem-1"]


def test_success_is_cached_by_idempotency_key_without_consuming_next_outcome():
    publisher = MockPublisher(["success", "checkpoint"])
    request = _request()

    first = publisher.publish(request)
    repeated = publisher.publish(request)
    next_result = publisher.publish(_request(key="idem-2"))

    assert repeated is first
    assert next_result.error_kind is PublishErrorKind.CHECKPOINT
    assert publisher.call_count == 3
    assert publisher.side_effect_count == 1


def test_account_check_can_be_scripted_independently():
    publisher = MockPublisher(
        outcomes=["success"],
        account_outcomes=["auth", "checkpoint", "success"],
    )

    auth = publisher.check_account("page-1")
    checkpoint = publisher.check_account("page-1")
    healthy = publisher.check_account("page-1")

    assert auth == AccountCheckResult(
        account_id="page-1",
        available=False,
        error_kind=PublishErrorKind.AUTH,
        message="account authentication failed",
    )
    assert checkpoint.error_kind is PublishErrorKind.CHECKPOINT
    assert healthy.ok
    assert publisher.account_checks == ["page-1", "page-1", "page-1"]
    assert publisher.publish(_request()).success


def test_enqueue_supports_enum_and_custom_rate_limit_delay():
    publisher = MockPublisher()
    publisher.enqueue_outcome(
        PublishErrorKind.RATE_LIMIT,
        message="wait before retry",
        retry_after_seconds=12,
    )

    result = publisher.publish(_request())

    assert result.error_kind is PublishErrorKind.RATE_LIMIT
    assert result.message == "wait before retry"
    assert result.retry_after_seconds == 12


def test_invalid_mock_outcome_is_rejected():
    with pytest.raises(ValueError, match="unknown mock outcome"):
        MockPublisher(["not-real"])


def test_value_objects_validate_invariants():
    with pytest.raises(ValueError, match="account_id"):
        _request().__class__(account_id="", post_id="p", idempotency_key="k")
    with pytest.raises(ValueError, match="remote_post_id"):
        PublishResult(success=True, idempotency_key="k")
    with pytest.raises(ValueError, match="error_kind"):
        PublishResult(success=False, idempotency_key="k")
    with pytest.raises(ValueError, match="unavailable"):
        AccountCheckResult(account_id="a", available=False)


def test_requests_are_frozen_and_do_not_have_credential_fields():
    request = PublishRequest(
        account_id="page-1",
        post_id="post-1",
        idempotency_key="idem-1",
        metadata={"campaign": "summer"},
    )

    with pytest.raises(FrozenInstanceError):
        request.caption = "changed"
    with pytest.raises(TypeError):
        request.metadata["campaign"] = "changed"

    assert "cookie" not in request.__dataclass_fields__
    assert "token" not in request.__dataclass_fields__
    assert "password" not in request.__dataclass_fields__
    assert "credential" not in repr(MockPublisher()).lower()
