"""Fail-closed, offline-testable orchestration for the QA-03B remote smoke.

The CLI is plan-only by default and has no live remote driver. This module
defines the typed gates, evidence, restart, isolation, and cleanup contracts a
future driver must satisfy without ever loading a dotenv file.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import IO, Protocol
from uuid import UUID

from laplace.ops.preflight import (
    CheckState,
    PreflightReport,
    config_preflight,
    runtime_preflight,
)

EVIDENCE_SCHEMA_VERSION = 2
_ENV_FILE_GUARD = "LAPLACE_DISABLE_ENV_FILE"
_TAG_RE = re.compile(r"^[0-9a-f]{16}$")
_STATIC_CHECKS = frozenset(
    {
        "database_connection",
        "supabase_origin",
        "project_alignment",
        "server_secret_key",
        "auth_public_key",
        "supabase_auth",
        "storage_backend",
        "storage_buckets",
        "publisher",
        "browser_publisher",
    }
)
_RUNTIME_CHECKS = frozenset(
    {
        "runtime_liveness",
        "runtime_readiness",
        "runtime_database",
        "runtime_storage",
        "runtime_scheduler",
    }
)
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "credential",
    "email",
    "header",
    "jwt",
    "password",
    "path",
    "secret",
    "token",
    "url",
    "uuid",
)
_SAFE_STATE_KEYS = frozenset({"auth_public_key", "server_secret_key"})
_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "artifact_id",
        "auth_id",
        "container_id",
        "external_id",
        "id",
        "identifier",
        "idempotency_key",
        "job_id",
        "marker",
        "object_name",
        "owner_id",
        "pid",
        "remote_post_id",
        "user_id",
    }
)
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE),
    re.compile(r"\b(?:postgres|postgresql)\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9-]+\.supabase\.(?:co|com)\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"\bsb_(?:secret|publishable)_[A-Za-z0-9._-]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?:^|[\s\"'])users/[^\s\"']+"),
    re.compile(r"(?:^|[\s\"'])(?:/Users|/app|/home|/private|/tmp|/var)/"),
)


class HarnessCode(StrEnum):
    OPT_IN_REQUIRED = "opt_in_required"
    DOTENV_GUARD_REQUIRED = "dotenv_guard_required"
    STATIC_PREFLIGHT_BLOCKED = "static_preflight_blocked"
    REPLICA_GATE_BLOCKED = "replica_gate_blocked"
    RUNTIME_PREFLIGHT_BLOCKED = "runtime_preflight_blocked"
    INITIAL_CONTRACT_BLOCKED = "initial_contract_blocked"
    RESTART_CHECKPOINT_REQUIRED = "restart_checkpoint_required"
    RESTART_CONFIRMATION_INVALID = "restart_confirmation_invalid"
    RESTART_BOUNDARY_INVALID = "restart_boundary_invalid"
    RESTART_STATE_INVALID = "restart_state_invalid"
    FINISH_CONTRACT_BLOCKED = "finish_contract_blocked"
    CLEANUP_INCOMPLETE = "cleanup_incomplete"
    LIVE_DRIVER_UNAVAILABLE = "live_driver_unavailable"
    DRIVER_FAILED = "driver_failed"


class HarnessBlocked(RuntimeError):
    """A safe harness assertion failed without retaining remote error text."""

    def __init__(self, code: HarnessCode) -> None:
        super().__init__(code.value)
        self.code = code


class EvidenceViolation(ValueError):
    """A public evidence document violates its typed safety contract."""


def _is_tag(value: object) -> bool:
    return isinstance(value, str) and bool(_TAG_RE.fullmatch(value))


class OpaqueTagger:
    """HMAC raw identifiers with an erasable per-run key."""

    def __init__(self, key: bytearray) -> None:
        if not isinstance(key, bytearray) or len(key) < 32:
            raise ValueError("HMAC key must be an erasable bytearray of 32+ bytes")
        self._key = key
        self._erased = False

    @property
    def erased(self) -> bool:
        return self._erased

    def tag(self, *values: object) -> str:
        if self._erased:
            raise RuntimeError("HMAC key has been erased")
        digest = hmac.new(bytes(self._key), digestmod=hashlib.sha256)
        for value in values:
            encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()[:16]

    def erase(self) -> None:
        for index in range(len(self._key)):
            self._key[index] = 0
        self._erased = True


KeyFactory = Callable[[], bytearray]


def _secure_key_factory() -> bytearray:
    return bytearray(secrets.token_bytes(32))


@dataclass(frozen=True, slots=True)
class ReplicaGateObservation:
    source: str
    target_confirmed: bool
    replica_count: int

    @property
    def accepted(self) -> bool:
        return (
            self.source == "approved_runtime_inventory"
            and self.target_confirmed
            and self.replica_count == 1
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "replica_count": self.replica_count,
            "source": self.source,
            "target_confirmed": self.target_confirmed,
        }


def _report_is_exact(
    report: PreflightReport,
    *,
    source: str,
    remote_access: bool,
    required_checks: frozenset[str],
) -> bool:
    return (
        report.ready
        and report.source == source
        and report.remote_access is remote_access
        and {check.code for check in report.checks} == required_checks
        and all(check.state is CheckState.VALID for check in report.checks)
    )


@dataclass(frozen=True, slots=True)
class ValidatedExecutionGate:
    static_report: PreflightReport = field(repr=False)
    runtime_report: PreflightReport = field(repr=False)
    replica: ReplicaGateObservation
    remote_mutation_opt_in: bool
    dotenv_file_disabled: bool

    def __post_init__(self) -> None:
        if not self.remote_mutation_opt_in or not self.dotenv_file_disabled:
            raise ValueError("execution gate is not authorized")
        if not self.replica.accepted:
            raise ValueError("execution gate replica observation is invalid")
        if not _report_is_exact(
            self.static_report,
            source="process_environment_only",
            remote_access=False,
            required_checks=_STATIC_CHECKS,
        ):
            raise ValueError("execution gate static report is invalid")
        if not _report_is_exact(
            self.runtime_report,
            source="loopback_health_endpoints",
            remote_access=True,
            required_checks=_RUNTIME_CHECKS,
        ):
            raise ValueError("execution gate runtime report is invalid")

    def public_dict(self) -> dict[str, object]:
        return {
            "dotenv_file_disabled": self.dotenv_file_disabled,
            "remote_mutation_opt_in": self.remote_mutation_opt_in,
            "replica": self.replica.public_dict(),
            "runtime": self.runtime_report.public_dict(),
            "static": self.static_report.public_dict(),
        }


RuntimeProbe = Callable[[], PreflightReport]
ReplicaProbe = Callable[[], ReplicaGateObservation]


def evaluate_execution_gate(
    environment: Mapping[str, str],
    *,
    allow_remote_mutation: bool,
    replica_probe: ReplicaProbe,
    runtime_probe: RuntimeProbe,
) -> ValidatedExecutionGate:
    """Run prerequisite gates in a strictly non-mutating, fail-fast order."""

    if not allow_remote_mutation:
        raise HarnessBlocked(HarnessCode.OPT_IN_REQUIRED)
    if environment.get(_ENV_FILE_GUARD) != "1":
        raise HarnessBlocked(HarnessCode.DOTENV_GUARD_REQUIRED)

    static_report = config_preflight(environment)
    if not _report_is_exact(
        static_report,
        source="process_environment_only",
        remote_access=False,
        required_checks=_STATIC_CHECKS,
    ):
        raise HarnessBlocked(HarnessCode.STATIC_PREFLIGHT_BLOCKED)

    replica = replica_probe()
    if not isinstance(replica, ReplicaGateObservation) or not replica.accepted:
        raise HarnessBlocked(HarnessCode.REPLICA_GATE_BLOCKED)

    runtime_report = runtime_probe()
    if not _report_is_exact(
        runtime_report,
        source="loopback_health_endpoints",
        remote_access=True,
        required_checks=_RUNTIME_CHECKS,
    ):
        raise HarnessBlocked(HarnessCode.RUNTIME_PREFLIGHT_BLOCKED)
    return ValidatedExecutionGate(
        static_report=static_report,
        runtime_report=runtime_report,
        replica=replica,
        remote_mutation_opt_in=True,
        dotenv_file_disabled=True,
    )


class FlowPhase(StrEnum):
    INITIAL = "initial"
    FINISH = "finish"


@dataclass(frozen=True, slots=True)
class HttpExpectation:
    step: str
    phase: FlowPhase
    expected_status: int
    expected_created_count: int
    payload_hash_required: bool = False

    def public_dict(self) -> dict[str, object]:
        return {
            "expected_created_count": self.expected_created_count,
            "expected_status": self.expected_status,
            "payload_hash_required": self.payload_hash_required,
            "phase": self.phase.value,
            "step": self.step,
        }


INITIAL_HTTP_MATRIX = (
    HttpExpectation("unauthenticated_read", FlowPhase.INITIAL, 401, 0),
    HttpExpectation("owner_account_create", FlowPhase.INITIAL, 201, 1),
    HttpExpectation("owner_media_upload", FlowPhase.INITIAL, 201, 1, True),
    HttpExpectation("owner_product_create", FlowPhase.INITIAL, 201, 1),
    HttpExpectation("owner_draft_create", FlowPhase.INITIAL, 201, 1),
    HttpExpectation("owner_post_approve", FlowPhase.INITIAL, 200, 0),
    HttpExpectation("owner_job_schedule", FlowPhase.INITIAL, 201, 1),
    HttpExpectation("owner_event_view_create", FlowPhase.INITIAL, 201, 1),
    HttpExpectation("owner_event_click_create", FlowPhase.INITIAL, 201, 1),
    HttpExpectation("owner_event_commission_create", FlowPhase.INITIAL, 201, 1),
    HttpExpectation("owner_artifact_upload", FlowPhase.INITIAL, 201, 1, True),
    HttpExpectation("owner_artifact_download", FlowPhase.INITIAL, 200, 0, True),
    HttpExpectation("owner_artifact_sign", FlowPhase.INITIAL, 200, 0),
    HttpExpectation("owner_artifact_reconciliation", FlowPhase.INITIAL, 200, 0),
    HttpExpectation("cross_owner_account_override", FlowPhase.INITIAL, 403, 0),
    HttpExpectation("cross_owner_media_override", FlowPhase.INITIAL, 403, 0),
    HttpExpectation("cross_owner_product_override", FlowPhase.INITIAL, 403, 0),
    HttpExpectation("cross_owner_draft_override", FlowPhase.INITIAL, 403, 0),
    HttpExpectation("cross_owner_post_approve", FlowPhase.INITIAL, 409, 0),
    HttpExpectation("cross_owner_job_override", FlowPhase.INITIAL, 403, 0),
    HttpExpectation("cross_owner_job_cancel", FlowPhase.INITIAL, 409, 0),
    HttpExpectation("cross_owner_event_override", FlowPhase.INITIAL, 403, 0),
    HttpExpectation("cross_owner_artifact_download", FlowPhase.INITIAL, 404, 0),
    HttpExpectation("cross_owner_artifact_sign", FlowPhase.INITIAL, 404, 0),
    HttpExpectation("cross_owner_artifact_delete", FlowPhase.INITIAL, 404, 0),
    HttpExpectation(
        "cross_owner_artifact_reconciliation",
        FlowPhase.INITIAL,
        200,
        0,
    ),
)
FINISH_HTTP_MATRIX = (
    HttpExpectation("owner_artifact_delete", FlowPhase.FINISH, 204, 0),
    HttpExpectation(
        "owner_artifact_post_delete_reconciliation",
        FlowPhase.FINISH,
        200,
        0,
    ),
)
HTTP_STATUS_MATRIX = INITIAL_HTTP_MATRIX + FINISH_HTTP_MATRIX
_HTTP_BY_PHASE = {
    FlowPhase.INITIAL: {item.step: item for item in INITIAL_HTTP_MATRIX},
    FlowPhase.FINISH: {item.step: item for item in FINISH_HTTP_MATRIX},
}


@dataclass(frozen=True, slots=True)
class HttpObservation:
    step: str
    phase: FlowPhase
    actual_status: int
    created_count: int
    no_sensitive_disclosure: bool
    owner_state_valid: bool
    payload_hash_matches: bool | None = None

    def accepted(self, expectation: HttpExpectation) -> bool:
        return (
            self.step == expectation.step
            and self.phase is expectation.phase
            and self.actual_status == expectation.expected_status
            and self.created_count == expectation.expected_created_count
            and self.no_sensitive_disclosure
            and self.owner_state_valid
            and (not expectation.payload_hash_required or self.payload_hash_matches is True)
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "actual_status": self.actual_status,
            "created_count": self.created_count,
            "no_sensitive_disclosure": self.no_sensitive_disclosure,
            "owner_state_valid": self.owner_state_valid,
            "payload_hash_matches": self.payload_hash_matches,
            "phase": self.phase.value,
            "step": self.step,
        }


def assert_http_phase(
    observations: Sequence[HttpObservation],
    phase: FlowPhase,
) -> tuple[HttpObservation, ...]:
    expectations = _HTTP_BY_PHASE[phase]
    seen: dict[str, HttpObservation] = {}
    for observation in observations:
        expectation = expectations.get(observation.step)
        if expectation is None or observation.step in seen or not observation.accepted(expectation):
            raise HarnessBlocked(
                HarnessCode.INITIAL_CONTRACT_BLOCKED
                if phase is FlowPhase.INITIAL
                else HarnessCode.FINISH_CONTRACT_BLOCKED
            )
        seen[observation.step] = observation
    if set(seen) != set(expectations):
        raise HarnessBlocked(
            HarnessCode.INITIAL_CONTRACT_BLOCKED
            if phase is FlowPhase.INITIAL
            else HarnessCode.FINISH_CONTRACT_BLOCKED
        )
    return tuple(seen[step] for step in expectations)


STORAGE_DENIAL_STEPS = (
    "media_public_read",
    "media_cross_owner_read",
    "media_cross_owner_sign",
    "media_cross_owner_update",
    "media_cross_owner_delete",
    "artifact_public_read",
    "artifact_cross_owner_read",
    "artifact_cross_owner_sign",
    "artifact_cross_owner_update",
    "artifact_cross_owner_delete",
)
AUTHORIZED_STORAGE_STEPS = (
    "owner_media_download",
    "owner_media_sign",
    "owner_media_signed_download",
    "owner_artifact_download",
    "owner_artifact_sign",
    "owner_artifact_signed_download",
)


@dataclass(frozen=True, slots=True)
class StorageDenialObservation:
    step: str
    actual_status: int
    payload_disclosed: bool
    signed_reference_disclosed: bool
    owner_hash_before: str
    owner_hash_after: str

    @property
    def accepted(self) -> bool:
        return (
            self.step in STORAGE_DENIAL_STEPS
            and 100 <= self.actual_status <= 599
            and not 200 <= self.actual_status <= 299
            and not self.payload_disclosed
            and not self.signed_reference_disclosed
            and _is_tag(self.owner_hash_before)
            and self.owner_hash_after == self.owner_hash_before
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "actual_status": self.actual_status,
            "owner_hash_after": self.owner_hash_after,
            "owner_hash_before": self.owner_hash_before,
            "payload_disclosed": self.payload_disclosed,
            "signed_reference_disclosed": self.signed_reference_disclosed,
            "step": self.step,
        }


@dataclass(frozen=True, slots=True)
class AuthorizedStorageObservation:
    step: str
    actual_status: int
    payload_hash_matches: bool | None
    signed_reference_persisted: bool

    @property
    def accepted(self) -> bool:
        download_step = self.step.endswith("download")
        return (
            self.step in AUTHORIZED_STORAGE_STEPS
            and self.actual_status == 200
            and not self.signed_reference_persisted
            and (not download_step or self.payload_hash_matches is True)
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "actual_status": self.actual_status,
            "payload_hash_matches": self.payload_hash_matches,
            "signed_reference_persisted": self.signed_reference_persisted,
            "step": self.step,
        }


@dataclass(frozen=True, slots=True)
class BucketPrivacyObservation:
    media_private: bool
    artifact_private: bool

    @property
    def accepted(self) -> bool:
        return self.media_private and self.artifact_private

    def public_dict(self) -> dict[str, bool]:
        return {
            "artifact_private": self.artifact_private,
            "media_private": self.media_private,
        }


@dataclass(frozen=True, slots=True)
class StorageObservations:
    authorized: tuple[AuthorizedStorageObservation, ...]
    denials: tuple[StorageDenialObservation, ...]
    privacy: BucketPrivacyObservation

    @property
    def accepted(self) -> bool:
        authorized = {item.step: item for item in self.authorized}
        denials = {item.step: item for item in self.denials}
        return (
            len(authorized) == len(self.authorized) == len(AUTHORIZED_STORAGE_STEPS)
            and set(authorized) == set(AUTHORIZED_STORAGE_STEPS)
            and all(item.accepted for item in authorized.values())
            and len(denials) == len(self.denials) == len(STORAGE_DENIAL_STEPS)
            and set(denials) == set(STORAGE_DENIAL_STEPS)
            and all(item.accepted for item in denials.values())
            and self.privacy.accepted
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "authorized": [item.public_dict() for item in self.authorized],
            "denials": [item.public_dict() for item in self.denials],
            "privacy": self.privacy.public_dict(),
        }


RLS_RESOURCES = (
    "account",
    "media",
    "product",
    "post",
    "publish_job",
    "affiliate_event",
    "artifact",
)
RLS_OPERATIONS = ("read", "update", "delete")
RLS_SELECTOR_SHAPE = "id = :one_bound_id"


@dataclass(frozen=True, slots=True)
class RLSObservation:
    resource: str
    operation: str
    selector_shape: str
    actual_status: int
    affected_rows: int
    empty_result: bool
    owner_hash_before: str
    owner_hash_after: str

    @property
    def accepted(self) -> bool:
        return (
            self.resource in RLS_RESOURCES
            and self.operation in RLS_OPERATIONS
            and self.selector_shape == RLS_SELECTOR_SHAPE
            and self.actual_status == 200
            and self.affected_rows == 0
            and self.empty_result
            and _is_tag(self.owner_hash_before)
            and self.owner_hash_after == self.owner_hash_before
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "actual_status": self.actual_status,
            "affected_rows": self.affected_rows,
            "empty_result": self.empty_result,
            "operation": self.operation,
            "owner_hash_after": self.owner_hash_after,
            "owner_hash_before": self.owner_hash_before,
            "resource": self.resource,
            "selector_shape": self.selector_shape,
        }


def assert_rls_observations(
    observations: Sequence[RLSObservation],
) -> tuple[RLSObservation, ...]:
    seen = {(item.resource, item.operation): item for item in observations}
    expected = {(resource, operation) for resource in RLS_RESOURCES for operation in RLS_OPERATIONS}
    if (
        len(seen) != len(observations)
        or set(seen) != expected
        or not all(item.accepted for item in seen.values())
    ):
        raise HarnessBlocked(HarnessCode.INITIAL_CONTRACT_BLOCKED)
    return tuple(
        seen[(resource, operation)] for resource in RLS_RESOURCES for operation in RLS_OPERATIONS
    )


@dataclass(frozen=True, slots=True)
class AuthMappingObservation:
    auth_tags: tuple[str, ...]
    owner_tags: tuple[str, ...]
    preexisting_count: int
    one_to_one: bool

    @property
    def accepted(self) -> bool:
        return (
            len(self.auth_tags) == len(set(self.auth_tags)) == 2
            and len(self.owner_tags) == len(set(self.owner_tags)) == 2
            and all(_is_tag(tag) for tag in self.auth_tags + self.owner_tags)
            and self.preexisting_count == 0
            and self.one_to_one
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "auth_tags": list(self.auth_tags),
            "one_to_one": self.one_to_one,
            "owner_tags": list(self.owner_tags),
            "preexisting_count": self.preexisting_count,
        }


@dataclass(frozen=True, slots=True)
class AnalyticsObservation:
    event_tags: tuple[str, ...]
    view_count: int
    click_count: int
    commission_count: int
    expected_commission_amount: int
    observed_commission_amount: int
    expected_currency: str
    observed_currency: str

    @property
    def accepted(self) -> bool:
        return (
            len(self.event_tags) == len(set(self.event_tags)) == 3
            and all(_is_tag(tag) for tag in self.event_tags)
            and self.view_count == 1
            and self.click_count == 1
            and self.commission_count == 1
            and self.expected_commission_amount >= 0
            and self.observed_commission_amount == self.expected_commission_amount
            and self.expected_currency == "VND"
            and self.observed_currency == self.expected_currency
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "click_count": self.click_count,
            "commission_count": self.commission_count,
            "event_tags": list(self.event_tags),
            "expected_commission_amount": self.expected_commission_amount,
            "expected_currency": self.expected_currency,
            "observed_commission_amount": self.observed_commission_amount,
            "observed_currency": self.observed_currency,
            "view_count": self.view_count,
        }


@dataclass(frozen=True, slots=True)
class InitialFlowObservations:
    http: tuple[HttpObservation, ...]
    storage: StorageObservations
    rls: tuple[RLSObservation, ...]
    auth_mapping: AuthMappingObservation
    analytics: AnalyticsObservation


def validate_initial_flow(
    observations: InitialFlowObservations,
) -> InitialFlowObservations:
    assert_http_phase(observations.http, FlowPhase.INITIAL)
    assert_rls_observations(observations.rls)
    if (
        not observations.storage.accepted
        or not observations.auth_mapping.accepted
        or not observations.analytics.accepted
    ):
        raise HarnessBlocked(HarnessCode.INITIAL_CONTRACT_BLOCKED)
    return observations


class CleanupSelector(StrEnum):
    ID_EQ = "id_eq"
    AUTH_USER_ID_EQ = "auth_user_id_eq"
    BUCKET_AND_OBJECT_EQ = "bucket_eq_and_object_name_eq"


class CleanupResource(StrEnum):
    AFFILIATE_EVENT = "affiliate_event"
    PUBLISH_ATTEMPT = "publish_attempt"
    PUBLISH_JOB = "publish_job"
    SOCIAL_POST = "social_post"
    AFFILIATE_PRODUCT = "affiliate_product"
    SOCIAL_ACCOUNT = "social_account"
    ARTIFACT_OBJECT = "artifact_object"
    MEDIA_OBJECT = "media_object"
    MEDIA_ASSET = "media_asset"
    ARTIFACT_METADATA = "artifact_metadata"
    APP_USER = "app_user"
    AUTH_USER = "auth_user"


class InventoryClass(StrEnum):
    AUTH_USER = "auth_user"
    APP_USER = "app_user"
    SOCIAL_ACCOUNT = "social_account"
    MEDIA_ASSET = "media_asset"
    MEDIA_OBJECT = "media_object"
    AFFILIATE_PRODUCT = "affiliate_product"
    SOCIAL_POST = "social_post"
    ARTIFACT_METADATA = "artifact_metadata"
    ARTIFACT_OBJECT = "artifact_object"
    AFFILIATE_EVENT = "affiliate_event"
    PUBLISH_JOB_HAPPY = "publish_job_happy"
    PUBLISH_JOB_QUEUED = "publish_job_queued"
    PUBLISH_JOB_RETRY = "publish_job_retry"
    PUBLISH_JOB_FENCED = "publish_job_fenced"
    PUBLISH_ATTEMPT_HAPPY = "publish_attempt_happy"
    PUBLISH_ATTEMPT_QUEUED = "publish_attempt_queued"
    PUBLISH_ATTEMPT_RETRY = "publish_attempt_retry"
    PUBLISH_ATTEMPT_FENCED = "publish_attempt_fenced"


MANDATORY_INVENTORY_COUNTS = {
    InventoryClass.AUTH_USER: 2,
    InventoryClass.APP_USER: 2,
    InventoryClass.SOCIAL_ACCOUNT: 4,
    InventoryClass.MEDIA_ASSET: 1,
    InventoryClass.MEDIA_OBJECT: 1,
    InventoryClass.AFFILIATE_PRODUCT: 1,
    InventoryClass.SOCIAL_POST: 4,
    InventoryClass.ARTIFACT_METADATA: 1,
    InventoryClass.ARTIFACT_OBJECT: 1,
    InventoryClass.AFFILIATE_EVENT: 3,
    InventoryClass.PUBLISH_JOB_HAPPY: 1,
    InventoryClass.PUBLISH_JOB_QUEUED: 1,
    InventoryClass.PUBLISH_JOB_RETRY: 1,
    InventoryClass.PUBLISH_JOB_FENCED: 1,
    InventoryClass.PUBLISH_ATTEMPT_HAPPY: 1,
    InventoryClass.PUBLISH_ATTEMPT_QUEUED: 1,
    InventoryClass.PUBLISH_ATTEMPT_RETRY: 3,
    InventoryClass.PUBLISH_ATTEMPT_FENCED: 1,
}
INITIAL_INVENTORY_COUNTS = {
    InventoryClass.AUTH_USER: 2,
    InventoryClass.APP_USER: 2,
    InventoryClass.SOCIAL_ACCOUNT: 1,
    InventoryClass.MEDIA_ASSET: 1,
    InventoryClass.MEDIA_OBJECT: 1,
    InventoryClass.AFFILIATE_PRODUCT: 1,
    InventoryClass.SOCIAL_POST: 1,
    InventoryClass.ARTIFACT_METADATA: 1,
    InventoryClass.ARTIFACT_OBJECT: 1,
    InventoryClass.AFFILIATE_EVENT: 3,
    InventoryClass.PUBLISH_JOB_HAPPY: 1,
    InventoryClass.PUBLISH_ATTEMPT_HAPPY: 1,
}
_INVENTORY_RESOURCE = {
    InventoryClass.AUTH_USER: CleanupResource.AUTH_USER,
    InventoryClass.APP_USER: CleanupResource.APP_USER,
    InventoryClass.SOCIAL_ACCOUNT: CleanupResource.SOCIAL_ACCOUNT,
    InventoryClass.MEDIA_ASSET: CleanupResource.MEDIA_ASSET,
    InventoryClass.MEDIA_OBJECT: CleanupResource.MEDIA_OBJECT,
    InventoryClass.AFFILIATE_PRODUCT: CleanupResource.AFFILIATE_PRODUCT,
    InventoryClass.SOCIAL_POST: CleanupResource.SOCIAL_POST,
    InventoryClass.ARTIFACT_METADATA: CleanupResource.ARTIFACT_METADATA,
    InventoryClass.ARTIFACT_OBJECT: CleanupResource.ARTIFACT_OBJECT,
    InventoryClass.AFFILIATE_EVENT: CleanupResource.AFFILIATE_EVENT,
    InventoryClass.PUBLISH_JOB_HAPPY: CleanupResource.PUBLISH_JOB,
    InventoryClass.PUBLISH_JOB_QUEUED: CleanupResource.PUBLISH_JOB,
    InventoryClass.PUBLISH_JOB_RETRY: CleanupResource.PUBLISH_JOB,
    InventoryClass.PUBLISH_JOB_FENCED: CleanupResource.PUBLISH_JOB,
    InventoryClass.PUBLISH_ATTEMPT_HAPPY: CleanupResource.PUBLISH_ATTEMPT,
    InventoryClass.PUBLISH_ATTEMPT_QUEUED: CleanupResource.PUBLISH_ATTEMPT,
    InventoryClass.PUBLISH_ATTEMPT_RETRY: CleanupResource.PUBLISH_ATTEMPT,
    InventoryClass.PUBLISH_ATTEMPT_FENCED: CleanupResource.PUBLISH_ATTEMPT,
}
_CLEANUP_ORDER = {
    resource: index
    for index, resource in enumerate(
        (
            CleanupResource.AFFILIATE_EVENT,
            CleanupResource.PUBLISH_ATTEMPT,
            CleanupResource.PUBLISH_JOB,
            CleanupResource.SOCIAL_POST,
            CleanupResource.AFFILIATE_PRODUCT,
            CleanupResource.SOCIAL_ACCOUNT,
            CleanupResource.ARTIFACT_OBJECT,
            CleanupResource.MEDIA_OBJECT,
            CleanupResource.MEDIA_ASSET,
            CleanupResource.ARTIFACT_METADATA,
            CleanupResource.APP_USER,
            CleanupResource.AUTH_USER,
        )
    )
}


class DeletionProvenance(StrEnum):
    CLEANUP_EXACT = "cleanup_exact"
    ARTIFACT_API_204 = "artifact_api_204"


@dataclass(frozen=True, slots=True)
class ExactCleanupTarget:
    inventory_class: InventoryClass
    resource: CleanupResource
    selector: CleanupSelector
    values: tuple[object, ...] = field(repr=False)
    deletion_provenance: DeletionProvenance = DeletionProvenance.CLEANUP_EXACT

    def __post_init__(self) -> None:
        if _INVENTORY_RESOURCE.get(self.inventory_class) is not self.resource:
            raise ValueError("inventory class does not match cleanup resource")
        if self.resource is CleanupResource.AUTH_USER:
            raw_auth_id = self.values[0] if len(self.values) == 1 else None
            if isinstance(raw_auth_id, UUID):
                auth_id_valid = True
            elif isinstance(raw_auth_id, str):
                try:
                    auth_id_valid = str(UUID(raw_auth_id)) == raw_auth_id
                except ValueError:
                    auth_id_valid = False
            else:
                auth_id_valid = False
            valid = (
                self.selector is CleanupSelector.AUTH_USER_ID_EQ
                and len(self.values) == 1
                and auth_id_valid
            )
        elif self.resource in {
            CleanupResource.MEDIA_OBJECT,
            CleanupResource.ARTIFACT_OBJECT,
        }:
            valid = (
                self.selector is CleanupSelector.BUCKET_AND_OBJECT_EQ
                and len(self.values) == 2
                and all(isinstance(value, str) and value for value in self.values)
            )
        else:
            valid = (
                self.selector is CleanupSelector.ID_EQ
                and len(self.values) == 1
                and type(self.values[0]) is int
                and self.values[0] > 0
            )
        if not valid:
            raise ValueError("cleanup target is not equality-bound")
        for value in self.values:
            if isinstance(value, str) and any(
                character in value for character in ("\x00", "\r", "\n", "*", "?")
            ):
                raise ValueError("cleanup target contains an unsafe character")
        artifact_object = self.resource is CleanupResource.ARTIFACT_OBJECT
        artifact_api_delete = self.deletion_provenance is DeletionProvenance.ARTIFACT_API_204
        if artifact_object != artifact_api_delete:
            raise ValueError("artifact objects require API deletion provenance")

    @property
    def predicate_shape(self) -> str:
        if self.selector is CleanupSelector.BUCKET_AND_OBJECT_EQ:
            return "bucket = :one_bucket AND name = :one_complete_name"
        if self.selector is CleanupSelector.AUTH_USER_ID_EQ:
            return "admin_delete = :one_auth_id"
        table = {
            CleanupResource.AFFILIATE_EVENT: "affiliate_events",
            CleanupResource.PUBLISH_ATTEMPT: "publish_attempts",
            CleanupResource.PUBLISH_JOB: "publish_jobs",
            CleanupResource.SOCIAL_POST: "social_posts",
            CleanupResource.AFFILIATE_PRODUCT: "affiliate_products",
            CleanupResource.SOCIAL_ACCOUNT: "social_accounts",
            CleanupResource.MEDIA_ASSET: "media_assets",
            CleanupResource.ARTIFACT_METADATA: "artifacts",
            CleanupResource.APP_USER: "users",
        }[self.resource]
        return f"{table}.primary_key = :one_bound_id"


@dataclass(frozen=True, slots=True)
class ManifestObservation:
    stage: str
    counts: tuple[tuple[InventoryClass, int], ...]
    required_counts: tuple[tuple[InventoryClass, int], ...]
    complete: bool

    def public_dict(self) -> dict[str, object]:
        required = dict(self.required_counts)
        return {
            "complete": self.complete,
            "counts": [
                {
                    "actual_count": count,
                    "inventory_class": inventory_class.value,
                    "required_count": required.get(inventory_class, 0),
                }
                for inventory_class, count in self.counts
            ],
            "stage": self.stage,
        }


class FixtureLedger:
    """Own every created raw target in memory and prove inventory completeness."""

    def __init__(self) -> None:
        self._targets: list[ExactCleanupTarget] = []

    def register(self, target: ExactCleanupTarget) -> None:
        if target in self._targets:
            raise ValueError("cleanup target is already registered")
        self._targets.append(target)

    def manifest(self, required_stage: str | None = None) -> ManifestObservation:
        actual = Counter(target.inventory_class for target in self._targets)
        fixture_started = any(
            actual[inventory_class] > 0
            for inventory_class in (
                InventoryClass.PUBLISH_JOB_QUEUED,
                InventoryClass.PUBLISH_JOB_RETRY,
                InventoryClass.PUBLISH_JOB_FENCED,
                InventoryClass.PUBLISH_ATTEMPT_QUEUED,
                InventoryClass.PUBLISH_ATTEMPT_RETRY,
                InventoryClass.PUBLISH_ATTEMPT_FENCED,
            )
        )
        if required_stage not in {None, "empty", "initial", "complete"}:
            raise ValueError("inventory stage is invalid")
        if required_stage == "complete" or (required_stage is None and fixture_started):
            stage = "complete"
            required = MANDATORY_INVENTORY_COUNTS
        elif required_stage == "initial" or (required_stage is None and actual):
            stage = "initial"
            required = INITIAL_INVENTORY_COUNTS
        else:
            stage = "empty"
            required = {}
        counts = tuple(
            (inventory_class, actual[inventory_class]) for inventory_class in InventoryClass
        )
        return ManifestObservation(
            stage=stage,
            counts=counts,
            required_counts=tuple(required.items()),
            complete=all(
                actual[inventory_class] == expected_count
                for inventory_class, expected_count in required.items()
            )
            and {item for item, count in actual.items() if count}
            == {item for item, count in required.items() if count},
        )

    def cleanup_plan(self) -> tuple[ExactCleanupTarget, ...]:
        return tuple(
            target
            for _index, target in sorted(
                enumerate(self._targets),
                key=lambda item: (_CLEANUP_ORDER[item[1].resource], item[0]),
            )
        )


class MarkerScope(StrEnum):
    USERS = "users"
    SOCIAL_ACCOUNTS = "social_accounts"
    MEDIA_ASSETS = "media_assets"
    AFFILIATE_PRODUCTS = "affiliate_products"
    SOCIAL_POSTS = "social_posts"
    PUBLISH_JOBS = "publish_jobs"
    PUBLISH_ATTEMPTS = "publish_attempts"
    AFFILIATE_EVENTS = "affiliate_events"
    ARTIFACTS = "artifacts"
    CONTENT_GENERATIONS = "content_generations"
    AUTH_USERS = "auth_users"
    MEDIA_BUCKET_OBJECTS = "media_bucket_objects"
    ARTIFACT_BUCKET_OBJECTS = "artifact_bucket_objects"


@dataclass(frozen=True, slots=True)
class MarkerZeroObservation:
    scope: MarkerScope
    remaining_count: int

    def public_dict(self) -> dict[str, object]:
        return {
            "remaining_count": self.remaining_count,
            "scope": self.scope.value,
        }


@dataclass(frozen=True, slots=True)
class PostCleanupVerification:
    marker_counts: tuple[MarkerZeroObservation, ...]
    sentinel_unchanged: bool
    media_bucket_private: bool
    artifact_bucket_private: bool
    final_runtime_report: PreflightReport = field(repr=False)
    final_replica: ReplicaGateObservation

    @property
    def accepted(self) -> bool:
        by_scope = {item.scope: item for item in self.marker_counts}
        return (
            len(by_scope) == len(self.marker_counts) == len(MarkerScope)
            and set(by_scope) == set(MarkerScope)
            and all(item.remaining_count == 0 for item in by_scope.values())
            and self.sentinel_unchanged
            and self.media_bucket_private
            and self.artifact_bucket_private
            and self.final_replica.accepted
            and _report_is_exact(
                self.final_runtime_report,
                source="loopback_health_endpoints",
                remote_access=True,
                required_checks=_RUNTIME_CHECKS,
            )
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "artifact_bucket_private": self.artifact_bucket_private,
            "final_replica": self.final_replica.public_dict(),
            "final_runtime": self.final_runtime_report.public_dict(),
            "marker_counts": [item.public_dict() for item in self.marker_counts],
            "media_bucket_private": self.media_bucket_private,
            "sentinel_unchanged": self.sentinel_unchanged,
        }


class CleanupExecutor(Protocol):
    def delete_exact(self, target: ExactCleanupTarget) -> int: ...

    def remaining_exact(self, target: ExactCleanupTarget) -> int: ...

    def post_cleanup_verification(
        self,
        marker: str,
    ) -> PostCleanupVerification: ...


@dataclass(frozen=True, slots=True)
class CleanupObservation:
    inventory_class: InventoryClass
    resource: CleanupResource
    selector: CleanupSelector
    deletion_provenance: DeletionProvenance
    predicate_shape: str
    target_tag: str
    affected_count: int
    remaining_count: int
    operation_status: str

    @property
    def accepted(self) -> bool:
        if self.deletion_provenance is DeletionProvenance.ARTIFACT_API_204:
            return (
                self.resource is CleanupResource.ARTIFACT_OBJECT
                and self.affected_count == 0
                and self.remaining_count == 0
                and self.operation_status == "verified_api_delete"
            )
        return (
            self.affected_count == 1
            and self.remaining_count == 0
            and self.operation_status == "deleted"
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "affected_count": self.affected_count,
            "bound_cardinality": 1,
            "deletion_provenance": self.deletion_provenance.value,
            "inventory_class": self.inventory_class.value,
            "operation_status": self.operation_status,
            "predicate_shape": self.predicate_shape,
            "remaining_count": self.remaining_count,
            "resource": self.resource.value,
            "selector": self.selector.value,
            "target_tag": self.target_tag,
        }


@dataclass(frozen=True, slots=True)
class CleanupReport:
    manifest: ManifestObservation
    observations: tuple[CleanupObservation, ...]
    post_cleanup: PostCleanupVerification | None

    @property
    def ready(self) -> bool:
        return (
            self.manifest.complete
            and len(self.observations)
            == sum(count for _item, count in self.manifest.required_counts)
            and all(item.accepted for item in self.observations)
            and self.post_cleanup is not None
            and self.post_cleanup.accepted
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "exact_targets": [item.public_dict() for item in self.observations],
            "manifest": self.manifest.public_dict(),
            "post_cleanup": (
                self.post_cleanup.public_dict() if self.post_cleanup is not None else None
            ),
            "status": "ready" if self.ready else "blocked",
        }


def execute_cleanup(
    ledger: FixtureLedger,
    executor: CleanupExecutor,
    *,
    marker: str,
    tagger: OpaqueTagger,
    required_manifest_stage: str | None = None,
) -> CleanupReport:
    """Clean every exact registered target, then require typed final proof."""

    manifest = ledger.manifest(required_manifest_stage)
    observations: list[CleanupObservation] = []
    fatal_interrupt: BaseException | None = None
    for target in ledger.cleanup_plan():
        affected_count = -1
        remaining_count = -1
        operation_status = "blocked"
        interrupted = False
        if target.deletion_provenance is DeletionProvenance.ARTIFACT_API_204:
            try:
                remaining_count = executor.remaining_exact(target)
            except BaseException as exc:
                interrupted = not isinstance(exc, Exception)
                if (
                    interrupted
                    and not isinstance(exc, KeyboardInterrupt)
                    and fatal_interrupt is None
                ):
                    fatal_interrupt = exc
            if remaining_count == 0:
                affected_count = 0
                operation_status = "verified_api_delete"
            elif remaining_count == 1:
                try:
                    affected_count = executor.delete_exact(target)
                except BaseException as exc:
                    interrupted = interrupted or not isinstance(exc, Exception)
                    if (
                        not isinstance(exc, (Exception, KeyboardInterrupt))
                        and fatal_interrupt is None
                    ):
                        fatal_interrupt = exc
                try:
                    remaining_count = executor.remaining_exact(target)
                except BaseException as exc:
                    interrupted = interrupted or not isinstance(exc, Exception)
                    if (
                        not isinstance(exc, (Exception, KeyboardInterrupt))
                        and fatal_interrupt is None
                    ):
                        fatal_interrupt = exc
                    remaining_count = -1
                if not interrupted:
                    operation_status = "api_delete_residue_removed"
        else:
            try:
                affected_count = executor.delete_exact(target)
            except BaseException as exc:
                interrupted = not isinstance(exc, Exception)
                if (
                    interrupted
                    and not isinstance(exc, KeyboardInterrupt)
                    and fatal_interrupt is None
                ):
                    fatal_interrupt = exc
            try:
                remaining_count = executor.remaining_exact(target)
            except BaseException as exc:
                interrupted = interrupted or not isinstance(exc, Exception)
                if not isinstance(exc, (Exception, KeyboardInterrupt)) and fatal_interrupt is None:
                    fatal_interrupt = exc
                remaining_count = -1
            if not interrupted and affected_count == 1 and remaining_count == 0:
                operation_status = "deleted"
        if interrupted:
            operation_status = "interrupted"
        observations.append(
            CleanupObservation(
                inventory_class=target.inventory_class,
                resource=target.resource,
                selector=target.selector,
                deletion_provenance=target.deletion_provenance,
                predicate_shape=target.predicate_shape,
                target_tag=tagger.tag(
                    target.inventory_class.value,
                    target.resource.value,
                    *target.values,
                ),
                affected_count=affected_count,
                remaining_count=remaining_count,
                operation_status=operation_status,
            )
        )
    try:
        post_cleanup = executor.post_cleanup_verification(marker)
    except BaseException as exc:
        if not isinstance(exc, (Exception, KeyboardInterrupt)) and fatal_interrupt is None:
            fatal_interrupt = exc
        post_cleanup = None
    if fatal_interrupt is not None:
        raise fatal_interrupt
    return CleanupReport(manifest, tuple(observations), post_cleanup)


class RestartFixture(StrEnum):
    QUEUED = "queued_fixture"
    RETRY = "retry_fixture"
    FENCED = "fenced_fixture"


class NextRetryState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    DUE_AFTER_RESTART = "due_after_restart"
    NULL = "null"


@dataclass(frozen=True, slots=True)
class RestartFixtureSnapshot:
    fixture: RestartFixture
    job_tag: str
    idempotency_tag: str
    job_count: int
    job_status: str
    attempt_count: int
    attempt_numbers: tuple[int, ...]
    attempt_statuses: tuple[str, ...]
    finished_attempt_count: int
    intent_fence_count: int
    next_retry_state: NextRetryState
    scheduled_due_after_restart: bool
    stale_before_cutoff: bool
    error_code: str | None
    manual_reconciliation: bool
    side_effect_tags: tuple[str, ...]

    @property
    def tags_valid(self) -> bool:
        return (
            _is_tag(self.job_tag)
            and _is_tag(self.idempotency_tag)
            and all(_is_tag(tag) for tag in self.side_effect_tags)
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "attempt_count": self.attempt_count,
            "attempt_numbers": list(self.attempt_numbers),
            "attempt_statuses": list(self.attempt_statuses),
            "error_code": self.error_code,
            "finished_attempt_count": self.finished_attempt_count,
            "fixture": self.fixture.value,
            "idempotency_tag": self.idempotency_tag,
            "intent_fence_count": self.intent_fence_count,
            "job_count": self.job_count,
            "job_status": self.job_status,
            "job_tag": self.job_tag,
            "manual_reconciliation": self.manual_reconciliation,
            "next_retry_state": self.next_retry_state.value,
            "scheduled_due_after_restart": self.scheduled_due_after_restart,
            "side_effect_tags": list(self.side_effect_tags),
            "stale_before_cutoff": self.stale_before_cutoff,
        }


@dataclass(frozen=True, slots=True)
class RestartSnapshot:
    queued: RestartFixtureSnapshot
    retry: RestartFixtureSnapshot
    fenced: RestartFixtureSnapshot
    analytics: AnalyticsObservation
    media_object_tags: tuple[str, ...]
    artifact_tags: tuple[str, ...]
    media_bucket_private: bool
    artifact_bucket_private: bool
    attempt_unique_constraint_present: bool

    @property
    def tags_valid(self) -> bool:
        return (
            self.queued.tags_valid
            and self.retry.tags_valid
            and self.fenced.tags_valid
            and all(_is_tag(tag) for tag in self.media_object_tags + self.artifact_tags)
            and self.media_bucket_private
            and self.artifact_bucket_private
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "analytics": self.analytics.public_dict(),
            "artifact_tags": list(self.artifact_tags),
            "artifact_bucket_private": self.artifact_bucket_private,
            "attempt_unique_constraint_present": (self.attempt_unique_constraint_present),
            "fenced": self.fenced.public_dict(),
            "media_object_tags": list(self.media_object_tags),
            "media_bucket_private": self.media_bucket_private,
            "queued": self.queued.public_dict(),
            "retry": self.retry.public_dict(),
        }


@dataclass(frozen=True, slots=True)
class RestartValidation:
    before: RestartSnapshot
    after: RestartSnapshot
    settled: RestartSnapshot

    def public_dict(self) -> dict[str, object]:
        return {
            "after": self.after.public_dict(),
            "attempt_numbers_monotonic": True,
            "attempt_numbers_unique": True,
            "before": self.before.public_dict(),
            "duplicate_counts_stable": True,
            "settled": self.settled.public_dict(),
            "side_effects_exact": True,
        }


def _fixture_identity_stable(
    before: RestartFixtureSnapshot,
    after: RestartFixtureSnapshot,
    settled: RestartFixtureSnapshot,
) -> bool:
    return (
        before.fixture is after.fixture is settled.fixture
        and before.job_tag == after.job_tag == settled.job_tag
        and before.idempotency_tag == after.idempotency_tag == settled.idempotency_tag
    )


def validate_restart_transition(
    before: RestartSnapshot,
    after: RestartSnapshot,
    settled: RestartSnapshot,
) -> RestartValidation:
    """Require exact Q/R/U identity, recovery, side-effect, and count states."""

    fixtures = (
        (before.queued, after.queued, settled.queued),
        (before.retry, after.retry, settled.retry),
        (before.fenced, after.fenced, settled.fenced),
    )
    unique_monotonic = all(
        len(current.attempt_numbers) == len(set(current.attempt_numbers))
        and tuple(sorted(current.attempt_numbers)) == current.attempt_numbers
        for current in (after.queued, after.retry, after.fenced)
    )
    common = (
        before.tags_valid
        and after.tags_valid
        and settled.tags_valid
        and before.attempt_unique_constraint_present
        and after.attempt_unique_constraint_present
        and settled.attempt_unique_constraint_present
        and all(_fixture_identity_stable(*fixture) for fixture in fixtures)
        and before.analytics.accepted
        and after.analytics == before.analytics
        and settled.analytics == after.analytics
        and before.media_object_tags == after.media_object_tags == settled.media_object_tags
        and before.artifact_tags == after.artifact_tags == settled.artifact_tags
        and before.media_bucket_private
        == after.media_bucket_private
        == settled.media_bucket_private
        is True
        and before.artifact_bucket_private
        == after.artifact_bucket_private
        == settled.artifact_bucket_private
        is True
        and len(before.media_object_tags) == 1
        and len(before.artifact_tags) == 1
        and unique_monotonic
        and settled == after
    )
    queued_valid = (
        before.queued.fixture is RestartFixture.QUEUED
        and before.queued.job_count == 1
        and before.queued.job_status == "queued"
        and before.queued.attempt_count == 0
        and before.queued.attempt_numbers == ()
        and before.queued.attempt_statuses == ()
        and before.queued.finished_attempt_count == 0
        and before.queued.intent_fence_count == 0
        and before.queued.next_retry_state is NextRetryState.NOT_APPLICABLE
        and before.queued.scheduled_due_after_restart
        and not before.queued.stale_before_cutoff
        and before.queued.error_code is None
        and not before.queued.manual_reconciliation
        and before.queued.side_effect_tags == ()
        and after.queued.job_count == 1
        and after.queued.job_status == "published"
        and after.queued.attempt_count == 1
        and after.queued.attempt_numbers == (1,)
        and after.queued.attempt_statuses == ("published",)
        and after.queued.finished_attempt_count == 1
        and after.queued.intent_fence_count == 1
        and after.queued.next_retry_state is NextRetryState.NULL
        and len(after.queued.side_effect_tags) == len(set(after.queued.side_effect_tags)) == 1
        and after.queued.error_code is None
        and not after.queued.manual_reconciliation
    )
    retry_valid = (
        before.retry.fixture is RestartFixture.RETRY
        and before.retry.job_count == 1
        and before.retry.job_status == "retry"
        and before.retry.attempt_count == 1
        and before.retry.attempt_numbers == (1, 2)
        and before.retry.attempt_statuses == ("retryable_error", "retryable_error")
        and before.retry.finished_attempt_count == 2
        and before.retry.intent_fence_count == 0
        and before.retry.next_retry_state is NextRetryState.DUE_AFTER_RESTART
        and before.retry.scheduled_due_after_restart
        and not before.retry.stale_before_cutoff
        and before.retry.error_code is None
        and not before.retry.manual_reconciliation
        and before.retry.side_effect_tags == ()
        and after.retry.job_count == 1
        and after.retry.job_status == "published"
        and after.retry.attempt_count == 3
        and after.retry.attempt_numbers == (1, 2, 3)
        and after.retry.attempt_statuses == ("retryable_error", "retryable_error", "published")
        and after.retry.finished_attempt_count == 3
        and after.retry.intent_fence_count == 1
        and after.retry.next_retry_state is NextRetryState.NULL
        and len(after.retry.side_effect_tags) == len(set(after.retry.side_effect_tags)) == 1
        and after.retry.error_code is None
        and not after.retry.manual_reconciliation
    )
    fenced_valid = (
        before.fenced.fixture is RestartFixture.FENCED
        and before.fenced.job_count == 1
        and before.fenced.job_status == "running"
        and before.fenced.attempt_count == 1
        and before.fenced.attempt_numbers == (1,)
        and before.fenced.attempt_statuses == ("running",)
        and before.fenced.finished_attempt_count == 0
        and before.fenced.intent_fence_count == 1
        and before.fenced.next_retry_state is NextRetryState.NULL
        and not before.fenced.scheduled_due_after_restart
        and before.fenced.stale_before_cutoff
        and before.fenced.error_code is None
        and not before.fenced.manual_reconciliation
        and before.fenced.side_effect_tags == ()
        and after.fenced.job_count == 1
        and after.fenced.job_status == "failed"
        and after.fenced.attempt_count == 1
        and after.fenced.attempt_numbers == (1,)
        and after.fenced.attempt_statuses == ("unknown",)
        and after.fenced.finished_attempt_count == 1
        and after.fenced.intent_fence_count == 1
        and after.fenced.next_retry_state is NextRetryState.NULL
        and after.fenced.error_code == "publish_outcome_unknown"
        and after.fenced.manual_reconciliation
        and after.fenced.side_effect_tags == ()
    )
    if not (common and queued_valid and retry_valid and fenced_valid):
        raise HarnessBlocked(HarnessCode.RESTART_STATE_INVALID)
    return RestartValidation(before, after, settled)


@dataclass(frozen=True, slots=True)
class RestartBoundaryObservation:
    replica_count_before: int
    fixture_transaction_committed: bool
    graceful_stop_exit: int
    liveness_unavailable_while_stopped: bool
    start_exit: int
    dotenv_file_disabled_after: bool
    replica_count_after: int
    instance_tag_before: str
    instance_tag_after: str

    @property
    def accepted(self) -> bool:
        return (
            self.replica_count_before == 1
            and self.fixture_transaction_committed
            and self.graceful_stop_exit == 0
            and self.liveness_unavailable_while_stopped
            and self.start_exit == 0
            and self.dotenv_file_disabled_after
            and self.replica_count_after == 1
            and _is_tag(self.instance_tag_before)
            and _is_tag(self.instance_tag_after)
            and self.instance_tag_after != self.instance_tag_before
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "dotenv_file_disabled_after": self.dotenv_file_disabled_after,
            "fixture_transaction_committed": self.fixture_transaction_committed,
            "graceful_stop_exit": self.graceful_stop_exit,
            "instance_tag_after": self.instance_tag_after,
            "instance_tag_before": self.instance_tag_before,
            "liveness_unavailable_while_stopped": (self.liveness_unavailable_while_stopped),
            "replica_count_after": self.replica_count_after,
            "replica_count_before": self.replica_count_before,
            "start_exit": self.start_exit,
        }


class RestartCheckpoint(Protocol):
    def await_restart(self) -> RestartBoundaryObservation: ...


class InteractiveRestartCheckpoint:
    """Pause in one process so raw fixture identities remain memory-only."""

    def __init__(
        self,
        *,
        boundary_probe: Callable[[], RestartBoundaryObservation],
        input_stream: IO[str] | None = None,
        output_stream: IO[str] | None = None,
    ) -> None:
        self._boundary_probe = boundary_probe
        self._input = input_stream or sys.stdin
        self._output = output_stream or sys.stdout

    def await_restart(self) -> RestartBoundaryObservation:
        if not self._input.isatty():
            raise HarnessBlocked(HarnessCode.RESTART_CHECKPOINT_REQUIRED)
        self._output.write("qa03b_restart_checkpoint=ready\n")
        self._output.flush()
        if self._input.readline().strip() != "RESUME":
            raise HarnessBlocked(HarnessCode.RESTART_CONFIRMATION_INVALID)
        boundary = self._boundary_probe()
        if not boundary.accepted:
            raise HarnessBlocked(HarnessCode.RESTART_BOUNDARY_INVALID)
        return boundary


@dataclass(frozen=True, slots=True)
class FinishFlowObservations:
    http: tuple[HttpObservation, ...]
    artifact_metadata_deleted: bool
    artifact_object_absent: bool
    reconciliation_active_count: int
    reconciliation_verified_count: int

    @property
    def accepted(self) -> bool:
        try:
            assert_http_phase(self.http, FlowPhase.FINISH)
        except HarnessBlocked:
            return False
        return (
            self.artifact_metadata_deleted
            and self.artifact_object_absent
            and self.reconciliation_active_count == 0
            and self.reconciliation_verified_count == 0
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "artifact_metadata_deleted": self.artifact_metadata_deleted,
            "artifact_object_absent": self.artifact_object_absent,
            "http": [item.public_dict() for item in self.http],
            "reconciliation_active_count": self.reconciliation_active_count,
            "reconciliation_verified_count": self.reconciliation_verified_count,
        }


class SmokeDriver(CleanupExecutor, Protocol):
    """Remote seam. Every successful create must be registered immediately."""

    def run_initial_flow(
        self,
        *,
        marker: str,
        ledger: FixtureLedger,
        tagger: OpaqueTagger,
    ) -> InitialFlowObservations: ...

    def prepare_restart_fixtures(
        self,
        *,
        ledger: FixtureLedger,
        tagger: OpaqueTagger,
    ) -> RestartSnapshot: ...

    def resume_after_restart(self, *, tagger: OpaqueTagger) -> RestartSnapshot: ...

    def settled_snapshot(self, *, tagger: OpaqueTagger) -> RestartSnapshot: ...

    def finish_flow(self) -> FinishFlowObservations: ...


def _execution_gate_is_valid(gate: object) -> bool:
    return (
        isinstance(gate, ValidatedExecutionGate)
        and gate.remote_mutation_opt_in
        and gate.dotenv_file_disabled
        and gate.replica.accepted
        and _report_is_exact(
            gate.static_report,
            source="process_environment_only",
            remote_access=False,
            required_checks=_STATIC_CHECKS,
        )
        and _report_is_exact(
            gate.runtime_report,
            source="loopback_health_endpoints",
            remote_access=True,
            required_checks=_RUNTIME_CHECKS,
        )
    )


def _manifest_is_consistent(manifest: object, *, stage: str) -> bool:
    if not isinstance(manifest, ManifestObservation):
        return False
    required = INITIAL_INVENTORY_COUNTS if stage == "initial" else MANDATORY_INVENTORY_COUNTS
    if (
        manifest.stage != stage
        or manifest.required_counts != tuple(required.items())
        or len(manifest.counts) != len(InventoryClass)
    ):
        return False
    actual: dict[InventoryClass, int] = {}
    for expected_class, entry in zip(InventoryClass, manifest.counts, strict=True):
        inventory_class, count = entry
        if inventory_class is not expected_class or type(count) is not int or count < 0:
            return False
        actual[inventory_class] = count
    calculated_complete = all(
        actual[inventory_class] == required.get(inventory_class, 0)
        for inventory_class in InventoryClass
    )
    return manifest.complete is calculated_complete


def _cleanup_is_consistent(report: object, *, stage: str) -> bool:
    if not isinstance(report, CleanupReport) or not _manifest_is_consistent(
        report.manifest,
        stage=stage,
    ):
        return False
    if not all(isinstance(item, CleanupObservation) for item in report.observations):
        return False
    actual_counts = dict(report.manifest.counts)
    observed_counts = Counter(item.inventory_class for item in report.observations)
    if observed_counts != Counter(
        {inventory_class: count for inventory_class, count in actual_counts.items() if count}
    ):
        return False
    allowed_statuses = {
        "blocked",
        "deleted",
        "interrupted",
        "verified_api_delete",
        "api_delete_residue_removed",
    }
    for item in report.observations:
        expected_resource = _INVENTORY_RESOURCE.get(item.inventory_class)
        expected_selector = (
            CleanupSelector.AUTH_USER_ID_EQ
            if item.resource is CleanupResource.AUTH_USER
            else (
                CleanupSelector.BUCKET_AND_OBJECT_EQ
                if item.resource in {CleanupResource.MEDIA_OBJECT, CleanupResource.ARTIFACT_OBJECT}
                else CleanupSelector.ID_EQ
            )
        )
        artifact_object = item.resource is CleanupResource.ARTIFACT_OBJECT
        artifact_api_delete = item.deletion_provenance is DeletionProvenance.ARTIFACT_API_204
        if (
            item.resource is not expected_resource
            or item.selector is not expected_selector
            or artifact_object != artifact_api_delete
            or not _is_tag(item.target_tag)
            or item.operation_status not in allowed_statuses
        ):
            return False
    return report.post_cleanup is None or isinstance(
        report.post_cleanup,
        PostCleanupVerification,
    )


def _initial_observations_are_valid(observations: object) -> bool:
    if not isinstance(observations, InitialFlowObservations):
        return False
    try:
        validate_initial_flow(observations)
    except Exception:
        return False
    return True


def _restart_validation_is_valid(validation: object) -> bool:
    if not isinstance(validation, RestartValidation):
        return False
    try:
        validated = validate_restart_transition(
            validation.before,
            validation.after,
            validation.settled,
        )
    except Exception:
        return False
    return validated == validation


@dataclass(frozen=True, slots=True)
class HarnessEvidence:
    result: str
    failure_code: str | None
    gate: ValidatedExecutionGate
    initial: InitialFlowObservations | None
    restart_boundary: RestartBoundaryObservation | None
    restart: RestartValidation | None
    finish: FinishFlowObservations | None
    cleanup: CleanupReport

    def __post_init__(self) -> None:
        self.validate_semantics()

    def validate_semantics(self) -> None:
        """Reject forged result labels and internally inconsistent stage evidence."""

        if not _execution_gate_is_valid(self.gate):
            raise EvidenceViolation("harness gate evidence is inconsistent")
        if self.initial is not None and not _initial_observations_are_valid(self.initial):
            raise EvidenceViolation("initial evidence is inconsistent")
        if self.restart_boundary is not None and not isinstance(
            self.restart_boundary, RestartBoundaryObservation
        ):
            raise EvidenceViolation("restart boundary evidence is inconsistent")
        if self.restart is not None and not _restart_validation_is_valid(self.restart):
            raise EvidenceViolation("restart evidence is inconsistent")
        if self.finish is not None and (
            not isinstance(self.finish, FinishFlowObservations) or not self.finish.accepted
        ):
            raise EvidenceViolation("finish evidence is inconsistent")
        if self.restart_boundary is not None and self.initial is None:
            raise EvidenceViolation("restart boundary has no initial evidence")
        if self.restart is not None and (
            self.restart_boundary is None or not self.restart_boundary.accepted
        ):
            raise EvidenceViolation("restart evidence has no accepted boundary")
        if self.finish is not None and self.restart is None:
            raise EvidenceViolation("finish evidence has no restart evidence")

        cleanup_stage = "complete" if self.initial is not None else "initial"
        if not _cleanup_is_consistent(self.cleanup, stage=cleanup_stage):
            raise EvidenceViolation("cleanup evidence is inconsistent")

        blocked_codes = {
            HarnessCode.INITIAL_CONTRACT_BLOCKED.value,
            HarnessCode.RESTART_CHECKPOINT_REQUIRED.value,
            HarnessCode.RESTART_CONFIRMATION_INVALID.value,
            HarnessCode.RESTART_BOUNDARY_INVALID.value,
            HarnessCode.RUNTIME_PREFLIGHT_BLOCKED.value,
            HarnessCode.RESTART_STATE_INVALID.value,
            HarnessCode.FINISH_CONTRACT_BLOCKED.value,
            HarnessCode.CLEANUP_INCOMPLETE.value,
        }
        if self.result == "passed":
            stages = (
                self.initial,
                self.restart_boundary,
                self.restart,
                self.finish,
            )
            if (
                self.failure_code is not None
                or any(stage is None for stage in stages)
                or not self.restart_boundary.accepted
                or not self.cleanup.ready
            ):
                raise EvidenceViolation("passed evidence is incomplete")
        elif self.result == "blocked":
            cleanup_incomplete = self.failure_code == HarnessCode.CLEANUP_INCOMPLETE.value
            stages_present = (
                self.initial is not None,
                self.restart_boundary is not None,
                self.restart is not None,
                self.finish is not None,
            )
            if self.failure_code not in blocked_codes:
                raise EvidenceViolation("blocked evidence is inconsistent")
            if cleanup_incomplete:
                if self.cleanup.ready:
                    raise EvidenceViolation("blocked evidence is inconsistent")
            elif not self.cleanup.ready:
                raise EvidenceViolation("blocked evidence is inconsistent")
            elif self.failure_code == HarnessCode.INITIAL_CONTRACT_BLOCKED.value:
                if stages_present != (False, False, False, False):
                    raise EvidenceViolation("blocked evidence stage is inconsistent")
            elif self.failure_code in {
                HarnessCode.RESTART_CHECKPOINT_REQUIRED.value,
                HarnessCode.RESTART_CONFIRMATION_INVALID.value,
            }:
                if stages_present != (True, False, False, False):
                    raise EvidenceViolation("blocked evidence stage is inconsistent")
            elif self.failure_code == HarnessCode.RESTART_BOUNDARY_INVALID.value:
                if (
                    stages_present[0] is not True
                    or stages_present[2:]
                    != (
                        False,
                        False,
                    )
                    or (self.restart_boundary is not None and self.restart_boundary.accepted)
                ):
                    raise EvidenceViolation("blocked evidence stage is inconsistent")
            elif self.failure_code in {
                HarnessCode.RUNTIME_PREFLIGHT_BLOCKED.value,
                HarnessCode.RESTART_STATE_INVALID.value,
            }:
                if stages_present != (True, True, False, False) or (
                    not self.restart_boundary.accepted
                ):
                    raise EvidenceViolation("blocked evidence stage is inconsistent")
            elif stages_present != (True, True, True, False) or not self.restart_boundary.accepted:
                raise EvidenceViolation("blocked evidence stage is inconsistent")
        elif self.result == "failed":
            if (
                self.failure_code != HarnessCode.DRIVER_FAILED.value
                or not self.cleanup.ready
                or self.finish is not None
                or (self.restart_boundary is not None and not self.restart_boundary.accepted)
            ):
                raise EvidenceViolation("failed evidence is inconsistent")
        else:
            raise EvidenceViolation("harness result is invalid")

    def public_dict(self) -> dict[str, object]:
        return {
            "cleanup": self.cleanup.public_dict(),
            "failure_code": self.failure_code,
            "finish": self.finish.public_dict() if self.finish is not None else None,
            "gates": self.gate.public_dict(),
            "initial": (
                {
                    "analytics": self.initial.analytics.public_dict(),
                    "auth_mapping": self.initial.auth_mapping.public_dict(),
                    "http": [item.public_dict() for item in self.initial.http],
                    "postgrest_rls": [item.public_dict() for item in self.initial.rls],
                    "storage": self.initial.storage.public_dict(),
                }
                if self.initial is not None
                else None
            ),
            "mode": "remote_mutation",
            "restart": (self.restart.public_dict() if self.restart is not None else None),
            "restart_boundary": (
                self.restart_boundary.public_dict() if self.restart_boundary is not None else None
            ),
            "result": self.result,
            "safety": {
                "cleanup_predicates": "equality_only",
                "dotenv_file_loading": "disabled",
                "identifiers": "ephemeral_hmac_tags_only",
                "publisher": "mock",
            },
            "schema_version": EVIDENCE_SCHEMA_VERSION,
        }


def _runtime_report_after_restart_is_valid(report: PreflightReport) -> bool:
    return _report_is_exact(
        report,
        source="loopback_health_endpoints",
        remote_access=True,
        required_checks=_RUNTIME_CHECKS,
    )


def run_smoke(
    *,
    driver: SmokeDriver,
    gate: ValidatedExecutionGate,
    checkpoint: RestartCheckpoint,
    post_restart_runtime_probe: RuntimeProbe,
    marker: str,
    key_factory: KeyFactory = _secure_key_factory,
) -> HarnessEvidence:
    """Run all typed phases, validate fail-fast, and always clean exact targets."""

    if not isinstance(gate, ValidatedExecutionGate):
        raise TypeError("run_smoke requires a ValidatedExecutionGate")
    if not marker or len(marker) < 16:
        raise ValueError("marker must be an opaque 16+ character value")
    key = key_factory()
    tagger = OpaqueTagger(key)
    ledger = FixtureLedger()
    initial: InitialFlowObservations | None = None
    boundary: RestartBoundaryObservation | None = None
    restart: RestartValidation | None = None
    finish: FinishFlowObservations | None = None
    failure_code: str | None = None
    result = "failed"
    required_manifest_stage = "initial"
    try:
        try:
            initial_candidate = driver.run_initial_flow(
                marker=marker,
                ledger=ledger,
                tagger=tagger,
            )
            initial = validate_initial_flow(initial_candidate)
            required_manifest_stage = "complete"
            before = driver.prepare_restart_fixtures(ledger=ledger, tagger=tagger)
            boundary = checkpoint.await_restart()
            if not boundary.accepted:
                raise HarnessBlocked(HarnessCode.RESTART_BOUNDARY_INVALID)
            post_restart_report = post_restart_runtime_probe()
            if not _runtime_report_after_restart_is_valid(post_restart_report):
                raise HarnessBlocked(HarnessCode.RUNTIME_PREFLIGHT_BLOCKED)
            after = driver.resume_after_restart(tagger=tagger)
            settled = driver.settled_snapshot(tagger=tagger)
            restart = validate_restart_transition(before, after, settled)
            finish_candidate = driver.finish_flow()
            if not finish_candidate.accepted:
                raise HarnessBlocked(HarnessCode.FINISH_CONTRACT_BLOCKED)
            finish = finish_candidate
            result = "passed"
        except HarnessBlocked as exc:
            failure_code = exc.code.value
            result = "blocked"
        except Exception:
            failure_code = HarnessCode.DRIVER_FAILED.value
            result = "failed"
        finally:
            cleanup = execute_cleanup(
                ledger,
                driver,
                marker=marker,
                tagger=tagger,
                required_manifest_stage=required_manifest_stage,
            )

        if not cleanup.ready:
            result = "blocked"
            failure_code = HarnessCode.CLEANUP_INCOMPLETE.value
        return HarnessEvidence(
            result=result,
            failure_code=failure_code,
            gate=gate,
            initial=initial,
            restart_boundary=boundary,
            restart=restart,
            finish=finish,
            cleanup=cleanup,
        )
    finally:
        tagger.erase()


@dataclass(frozen=True, slots=True)
class BlockedEvidence:
    code: HarnessCode
    gate: ValidatedExecutionGate | None = None

    def public_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "gates": self.gate.public_dict() if self.gate is not None else None,
            "mode": "remote_mutation",
            "remote_mutation": False,
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "status": "blocked",
        }


EvidencePayload = HarnessEvidence | BlockedEvidence


def validate_public_evidence(payload: object) -> None:
    """Reject forbidden field classes and values from a structured document."""

    def visit(value: object, key: str | None = None) -> None:
        if key is not None:
            normalized_key = key.casefold()
            if normalized_key in _SAFE_STATE_KEYS:
                if not (
                    isinstance(value, dict)
                    and set(value) == {"state", "status"}
                    and value.get("state") in {state.value for state in CheckState}
                    and value.get("status") in {"pass", "fail"}
                ):
                    raise EvidenceViolation("safe state field is invalid")
            elif normalized_key in _SENSITIVE_EXACT_KEYS or any(
                part in normalized_key for part in _SENSITIVE_KEY_PARTS
            ):
                raise EvidenceViolation("evidence key is prohibited")
        if value is None or isinstance(value, (bool, int, float)):
            return
        if isinstance(value, str):
            if any(pattern.search(value) for pattern in _SENSITIVE_VALUE_PATTERNS):
                raise EvidenceViolation("evidence value is prohibited")
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if not isinstance(nested_key, str):
                    raise EvidenceViolation("evidence keys must be strings")
                visit(nested_value, nested_key)
            return
        raise EvidenceViolation("evidence value type is prohibited")

    visit(payload)


_EXACT_KEYS = {
    "blocked": {
        "code",
        "evidence_digest",
        "gates",
        "mode",
        "remote_mutation",
        "schema_version",
        "status",
    },
    "harness": {
        "cleanup",
        "evidence_digest",
        "failure_code",
        "finish",
        "gates",
        "initial",
        "mode",
        "restart",
        "restart_boundary",
        "result",
        "safety",
        "schema_version",
    },
    "gates": {
        "dotenv_file_disabled",
        "remote_mutation_opt_in",
        "replica",
        "runtime",
        "static",
    },
    "replica": {"replica_count", "source", "target_confirmed"},
    "preflight": {"checks", "remote_access", "source", "status"},
    "preflight_check": {"state", "status"},
    "initial": {"analytics", "auth_mapping", "http", "postgrest_rls", "storage"},
    "http_item": {
        "actual_status",
        "created_count",
        "no_sensitive_disclosure",
        "owner_state_valid",
        "payload_hash_matches",
        "phase",
        "step",
    },
    "auth_mapping": {"auth_tags", "one_to_one", "owner_tags", "preexisting_count"},
    "analytics": {
        "click_count",
        "commission_count",
        "event_tags",
        "expected_commission_amount",
        "expected_currency",
        "observed_commission_amount",
        "observed_currency",
        "view_count",
    },
    "storage": {"authorized", "denials", "privacy"},
    "storage_authorized_item": {
        "actual_status",
        "payload_hash_matches",
        "signed_reference_persisted",
        "step",
    },
    "storage_denial_item": {
        "actual_status",
        "owner_hash_after",
        "owner_hash_before",
        "payload_disclosed",
        "signed_reference_disclosed",
        "step",
    },
    "privacy": {"artifact_private", "media_private"},
    "rls_item": {
        "actual_status",
        "affected_rows",
        "empty_result",
        "operation",
        "owner_hash_after",
        "owner_hash_before",
        "resource",
        "selector_shape",
    },
    "boundary": {
        "dotenv_file_disabled_after",
        "fixture_transaction_committed",
        "graceful_stop_exit",
        "instance_tag_after",
        "instance_tag_before",
        "liveness_unavailable_while_stopped",
        "replica_count_after",
        "replica_count_before",
        "start_exit",
    },
    "restart": {
        "after",
        "attempt_numbers_monotonic",
        "attempt_numbers_unique",
        "before",
        "duplicate_counts_stable",
        "settled",
        "side_effects_exact",
    },
    "restart_snapshot": {
        "analytics",
        "artifact_bucket_private",
        "artifact_tags",
        "attempt_unique_constraint_present",
        "fenced",
        "media_bucket_private",
        "media_object_tags",
        "queued",
        "retry",
    },
    "fixture": {
        "attempt_count",
        "attempt_numbers",
        "attempt_statuses",
        "error_code",
        "finished_attempt_count",
        "fixture",
        "idempotency_tag",
        "intent_fence_count",
        "job_count",
        "job_status",
        "job_tag",
        "manual_reconciliation",
        "next_retry_state",
        "scheduled_due_after_restart",
        "side_effect_tags",
        "stale_before_cutoff",
    },
    "finish": {
        "artifact_metadata_deleted",
        "artifact_object_absent",
        "http",
        "reconciliation_active_count",
        "reconciliation_verified_count",
    },
    "cleanup": {"exact_targets", "manifest", "post_cleanup", "status"},
    "manifest": {"complete", "counts", "stage"},
    "manifest_item": {
        "actual_count",
        "inventory_class",
        "required_count",
    },
    "cleanup_item": {
        "affected_count",
        "bound_cardinality",
        "deletion_provenance",
        "inventory_class",
        "operation_status",
        "predicate_shape",
        "remaining_count",
        "resource",
        "selector",
        "target_tag",
    },
    "post_cleanup": {
        "artifact_bucket_private",
        "final_replica",
        "final_runtime",
        "marker_counts",
        "media_bucket_private",
        "sentinel_unchanged",
    },
    "marker_item": {"remaining_count", "scope"},
    "safety": {
        "cleanup_predicates",
        "dotenv_file_loading",
        "identifiers",
        "publisher",
    },
}


def _require_exact_keys(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _EXACT_KEYS[context]:
        raise EvidenceViolation(f"{context} structure is invalid")
    return value


def validate_evidence_structure(payload: object, *, kind: str) -> None:
    """Recursively enforce the exact typed evidence allowlist."""

    root = _require_exact_keys(payload, kind)
    gate = root.get("gates")
    if gate is not None:
        gate_value = _require_exact_keys(gate, "gates")
        _require_exact_keys(gate_value["replica"], "replica")
        for name in ("static", "runtime"):
            report = _require_exact_keys(gate_value[name], "preflight")
            checks = report["checks"]
            if not isinstance(checks, dict):
                raise EvidenceViolation("preflight checks are invalid")
            for check in checks.values():
                _require_exact_keys(check, "preflight_check")
    if kind == "blocked":
        return
    initial = root["initial"]
    if initial is not None:
        initial_value = _require_exact_keys(initial, "initial")
        _require_exact_keys(initial_value["analytics"], "analytics")
        _require_exact_keys(initial_value["auth_mapping"], "auth_mapping")
        for item in initial_value["http"]:
            _require_exact_keys(item, "http_item")
        for item in initial_value["postgrest_rls"]:
            _require_exact_keys(item, "rls_item")
        storage = _require_exact_keys(initial_value["storage"], "storage")
        for item in storage["authorized"]:
            _require_exact_keys(item, "storage_authorized_item")
        for item in storage["denials"]:
            _require_exact_keys(item, "storage_denial_item")
        _require_exact_keys(storage["privacy"], "privacy")
    boundary = root["restart_boundary"]
    if boundary is not None:
        _require_exact_keys(boundary, "boundary")
    restart = root["restart"]
    if restart is not None:
        restart_value = _require_exact_keys(restart, "restart")
        for phase in ("before", "after", "settled"):
            snapshot = _require_exact_keys(
                restart_value[phase],
                "restart_snapshot",
            )
            _require_exact_keys(snapshot["analytics"], "analytics")
            for fixture in ("queued", "retry", "fenced"):
                _require_exact_keys(snapshot[fixture], "fixture")
    finish = root["finish"]
    if finish is not None:
        finish_value = _require_exact_keys(finish, "finish")
        for item in finish_value["http"]:
            _require_exact_keys(item, "http_item")
    cleanup = _require_exact_keys(root["cleanup"], "cleanup")
    manifest = _require_exact_keys(cleanup["manifest"], "manifest")
    for item in manifest["counts"]:
        _require_exact_keys(item, "manifest_item")
    for item in cleanup["exact_targets"]:
        _require_exact_keys(item, "cleanup_item")
    post_cleanup = cleanup["post_cleanup"]
    if post_cleanup is not None:
        post = _require_exact_keys(post_cleanup, "post_cleanup")
        _require_exact_keys(post["final_replica"], "replica")
        final_runtime = _require_exact_keys(post["final_runtime"], "preflight")
        for check in final_runtime["checks"].values():
            _require_exact_keys(check, "preflight_check")
        for item in post["marker_counts"]:
            _require_exact_keys(item, "marker_item")
    _require_exact_keys(root["safety"], "safety")


def evidence_document(document: EvidencePayload) -> dict[str, object]:
    if not isinstance(document, (HarnessEvidence, BlockedEvidence)):
        raise TypeError("evidence_document accepts typed evidence only")
    if isinstance(document, HarnessEvidence):
        document.validate_semantics()
    payload = document.public_dict()
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    result = {
        **payload,
        "evidence_digest": hashlib.sha256(canonical).hexdigest()[:16],
    }
    kind = "harness" if isinstance(document, HarnessEvidence) else "blocked"
    validate_evidence_structure(result, kind=kind)
    validate_public_evidence(result)
    return result


def write_evidence(destination: str | Path, document: EvidencePayload) -> None:
    """Create one private typed evidence file without overwriting anything."""

    path = Path(destination)
    if path.name.casefold().startswith(".env"):
        raise EvidenceViolation("evidence destination is prohibited")
    payload = evidence_document(document)
    encoded = (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "ascii"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def plan_document() -> dict[str, object]:
    """Return a deterministic inert contract; never inspect runtime values."""

    return {
        "cleanup": {
            "inventory_counts": {
                item.value: MANDATORY_INVENTORY_COUNTS[item] for item in InventoryClass
            },
            "marker_scopes": [item.value for item in MarkerScope],
            "predicate_mode": "equality_only",
        },
        "gates": [
            "explicit_remote_mutation_opt_in",
            "dotenv_file_loading_disabled",
            "exact_static_process_environment_preflight",
            "probed_single_runtime_replica",
            "exact_loopback_runtime_preflight",
        ],
        "http_statuses": [item.public_dict() for item in HTTP_STATUS_MATRIX],
        "implementation_state": "live_driver_required",
        "mode": "plan_only",
        "remote_mutation": False,
        "restart": {
            "fixtures": ["queued", "retry", "fenced"],
            "identity_contract": "stable_job_and_idempotency_tags",
            "side_effect_contract": "queued_1_retry_1_fenced_0",
        },
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "storage_denials": {
            "status_contract": "observed_numeric_non_2xx",
            "steps": list(STORAGE_DENIAL_STEPS),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed QA-03B remote smoke harness",
    )
    subparsers = parser.add_subparsers(dest="action")
    subparsers.add_parser("plan", help="render the inert plan (default)")
    run_parser = subparsers.add_parser(
        "run",
        help="validate local gates; live driver remains intentionally unwired",
    )
    run_parser.add_argument("--allow-remote-mutation", action="store_true")
    run_parser.add_argument("--port", type=int, default=8010)
    run_parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    runtime_probe: RuntimeProbe | None = None,
    replica_probe: ReplicaProbe | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if args.action in {None, "plan"}:
        print(json.dumps(plan_document(), ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    selected_environment = os.environ if environment is None else environment
    selected_runtime_probe = runtime_probe or (
        lambda: runtime_preflight(port=args.port, timeout_s=args.timeout)
    )
    selected_replica_probe = replica_probe or (
        lambda: ReplicaGateObservation(
            source="unavailable",
            target_confirmed=False,
            replica_count=0,
        )
    )
    try:
        gate = evaluate_execution_gate(
            selected_environment,
            allow_remote_mutation=args.allow_remote_mutation,
            replica_probe=selected_replica_probe,
            runtime_probe=selected_runtime_probe,
        )
    except HarnessBlocked as exc:
        payload = BlockedEvidence(exc.code).public_dict()
        validate_public_evidence(payload)
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return 2

    payload = BlockedEvidence(HarnessCode.LIVE_DRIVER_UNAVAILABLE, gate).public_dict()
    validate_public_evidence(payload)
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
