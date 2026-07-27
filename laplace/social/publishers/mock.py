"""Deterministic social publisher for tests and local development."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from laplace.social.publishers.base import (
    AccountCheckResult,
    PublishErrorKind,
    PublishRequest,
    PublishResult,
)


@dataclass(frozen=True, slots=True)
class _QueuedOutcome:
    error_kind: PublishErrorKind | None
    message: str = ""
    retry_after_seconds: int | None = None


OutcomeInput = str | PublishErrorKind


class MockPublisher:
    """Scriptable publisher with no network access and no credential storage.

    When the outcome queue is empty, calls succeed. Repeating an already
    completed idempotency key returns the exact cached result without consuming
    another queued outcome, mirroring an idempotent upstream API.
    """

    name = "mock"

    def __init__(
        self,
        outcomes: Iterable[OutcomeInput] | None = None,
        *,
        account_outcomes: Iterable[OutcomeInput] | None = None,
    ) -> None:
        self.calls: list[PublishRequest] = []
        self.account_checks: list[str] = []
        self.idempotency_keys: list[str] = []
        self._outcomes: deque[_QueuedOutcome] = deque()
        self._account_outcomes: deque[_QueuedOutcome] = deque()
        self._completed: dict[str, PublishResult] = {}
        self._side_effect_count = 0
        if outcomes:
            self.enqueue_outcome(*outcomes)
        if account_outcomes:
            self.enqueue_account_outcome(*account_outcomes)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def side_effect_count(self) -> int:
        """Number of unique successful posts created by this mock."""

        return self._side_effect_count

    def enqueue_outcome(
        self,
        *outcomes: OutcomeInput,
        message: str = "",
        retry_after_seconds: int | None = None,
    ) -> None:
        """Queue publish outcomes in first-in, first-out order."""

        for outcome in outcomes:
            self._outcomes.append(
                self._normalize_outcome(
                    outcome,
                    message=message,
                    retry_after_seconds=retry_after_seconds,
                )
            )

    def enqueue_account_outcome(
        self,
        *outcomes: OutcomeInput,
        message: str = "",
    ) -> None:
        """Queue deterministic account-check outcomes."""

        for outcome in outcomes:
            self._account_outcomes.append(
                self._normalize_outcome(outcome, message=message)
            )

    def check_account(self, account_id: str) -> AccountCheckResult:
        if not account_id.strip():
            raise ValueError("account_id must not be empty")
        self.account_checks.append(account_id)
        outcome = self._account_outcomes.popleft() if self._account_outcomes else _QueuedOutcome(None)
        if outcome.error_kind is None:
            return AccountCheckResult(account_id=account_id, available=True)
        return AccountCheckResult(
            account_id=account_id,
            available=False,
            error_kind=outcome.error_kind,
            message=outcome.message or self._default_message(outcome.error_kind),
        )

    def publish(self, request: PublishRequest) -> PublishResult:
        self.calls.append(request)
        self.idempotency_keys.append(request.idempotency_key)

        completed = self._completed.get(request.idempotency_key)
        if completed is not None:
            return completed

        outcome = self._outcomes.popleft() if self._outcomes else _QueuedOutcome(None)
        if outcome.error_kind is None:
            result = PublishResult(
                success=True,
                idempotency_key=request.idempotency_key,
                remote_post_id=self._remote_post_id(request),
                message=outcome.message or "published",
            )
            self._completed[request.idempotency_key] = result
            self._side_effect_count += 1
            return result

        return PublishResult(
            success=False,
            idempotency_key=request.idempotency_key,
            error_kind=outcome.error_kind,
            message=outcome.message or self._default_message(outcome.error_kind),
            retry_after_seconds=outcome.retry_after_seconds,
        )

    @staticmethod
    def _normalize_outcome(
        outcome: OutcomeInput,
        *,
        message: str = "",
        retry_after_seconds: int | None = None,
    ) -> _QueuedOutcome:
        if isinstance(outcome, PublishErrorKind):
            error_kind = outcome
        else:
            normalized = outcome.strip().lower()
            if normalized == "success":
                error_kind = None
            else:
                try:
                    error_kind = PublishErrorKind(normalized)
                except ValueError as exc:
                    choices = "success, " + ", ".join(kind.value for kind in PublishErrorKind)
                    raise ValueError(f"unknown mock outcome {outcome!r}; expected one of: {choices}") from exc

        if retry_after_seconds is not None and retry_after_seconds < 0:
            raise ValueError("retry_after_seconds cannot be negative")
        if error_kind is PublishErrorKind.RATE_LIMIT and retry_after_seconds is None:
            retry_after_seconds = 60
        return _QueuedOutcome(error_kind, message, retry_after_seconds)

    @staticmethod
    def _remote_post_id(request: PublishRequest) -> str:
        return f"mock:{request.account_id}:{request.post_id}:{request.idempotency_key}"

    @staticmethod
    def _default_message(error_kind: PublishErrorKind) -> str:
        return {
            PublishErrorKind.NETWORK: "temporary network error",
            PublishErrorKind.RATE_LIMIT: "publisher rate limit reached",
            PublishErrorKind.AUTH: "account authentication failed",
            PublishErrorKind.CHECKPOINT: "account requires manual checkpoint",
            PublishErrorKind.INVALID_REQUEST: "publish request is invalid",
            PublishErrorKind.UNKNOWN: "unknown publisher error",
        }[error_kind]
