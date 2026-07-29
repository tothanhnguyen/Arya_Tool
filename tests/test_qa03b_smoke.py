"""Offline false-positive and safety tests for the QA-03B harness contract."""

from __future__ import annotations

import io
import json
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

import pytest

from laplace.ops import qa03b_smoke as qa
from laplace.ops.preflight import CheckState, PreflightCheck, PreflightReport


def _valid_environment() -> dict[str, str]:
    return {
        "LAPLACE_DISABLE_ENV_FILE": "1",
        "LAPLACE_DB_URL": (
            "postgresql://postgres.project-ref:db-password@"
            "aws-0-region.pooler.supabase.com:5432/postgres"
        ),
        "LAPLACE_SUPABASE_URL": "https://project-ref.supabase.co",
        "LAPLACE_SUPABASE_SECRET_KEY": "sb_secret_server-sentinel",
        "LAPLACE_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_public-sentinel",
        "LAPLACE_SUPABASE_AUTH_ENABLED": "true",
        "LAPLACE_SOCIAL_MEDIA_BACKEND": "supabase",
        "LAPLACE_SOCIAL_PUBLISHER": "mock",
        "LAPLACE_SOCIAL_BROWSER_PUBLISHER": "false",
    }


def _report(
    codes: frozenset[str],
    *,
    source: str,
    remote_access: bool,
    state: CheckState = CheckState.VALID,
) -> PreflightReport:
    return PreflightReport(
        checks=tuple(PreflightCheck(code, state) for code in sorted(codes)),
        source=source,
        remote_access=remote_access,
    )


def _static_report() -> PreflightReport:
    return _report(
        qa._STATIC_CHECKS,
        source="process_environment_only",
        remote_access=False,
    )


def _runtime_report() -> PreflightReport:
    return _report(
        qa._RUNTIME_CHECKS,
        source="loopback_health_endpoints",
        remote_access=True,
    )


def _replica() -> qa.ReplicaGateObservation:
    return qa.ReplicaGateObservation(
        source="approved_runtime_inventory",
        target_confirmed=True,
        replica_count=1,
    )


def _gate() -> qa.ValidatedExecutionGate:
    return qa.ValidatedExecutionGate(
        static_report=_static_report(),
        runtime_report=_runtime_report(),
        replica=_replica(),
        remote_mutation_opt_in=True,
        dotenv_file_disabled=True,
    )


def _tag(number: int) -> str:
    return f"{number:016x}"


def _http_observations(
    phase: qa.FlowPhase,
) -> tuple[qa.HttpObservation, ...]:
    matrix = qa.INITIAL_HTTP_MATRIX if phase is qa.FlowPhase.INITIAL else qa.FINISH_HTTP_MATRIX
    return tuple(
        qa.HttpObservation(
            step=item.step,
            phase=phase,
            actual_status=item.expected_status,
            created_count=item.expected_created_count,
            no_sensitive_disclosure=True,
            owner_state_valid=True,
            payload_hash_matches=(True if item.payload_hash_required else None),
        )
        for item in matrix
    )


def _storage() -> qa.StorageObservations:
    authorized = tuple(
        qa.AuthorizedStorageObservation(
            step=step,
            actual_status=200,
            payload_hash_matches=(True if step.endswith("download") else None),
            signed_reference_persisted=False,
        )
        for step in qa.AUTHORIZED_STORAGE_STEPS
    )
    denials = tuple(
        qa.StorageDenialObservation(
            step=step,
            actual_status=401 + (index % 4),
            payload_disclosed=False,
            signed_reference_disclosed=False,
            owner_hash_before=_tag(100 + index),
            owner_hash_after=_tag(100 + index),
        )
        for index, step in enumerate(qa.STORAGE_DENIAL_STEPS)
    )
    return qa.StorageObservations(
        authorized=authorized,
        denials=denials,
        privacy=qa.BucketPrivacyObservation(True, True),
    )


def _rls() -> tuple[qa.RLSObservation, ...]:
    observations: list[qa.RLSObservation] = []
    index = 200
    for resource in qa.RLS_RESOURCES:
        for operation in qa.RLS_OPERATIONS:
            observations.append(
                qa.RLSObservation(
                    resource=resource,
                    operation=operation,
                    selector_shape=qa.RLS_SELECTOR_SHAPE,
                    actual_status=200,
                    affected_rows=0,
                    empty_result=True,
                    owner_hash_before=_tag(index),
                    owner_hash_after=_tag(index),
                )
            )
            index += 1
    return tuple(observations)


def _analytics() -> qa.AnalyticsObservation:
    return qa.AnalyticsObservation(
        event_tags=(_tag(301), _tag(302), _tag(303)),
        view_count=1,
        click_count=1,
        commission_count=1,
        expected_commission_amount=15000,
        observed_commission_amount=15000,
        expected_currency="VND",
        observed_currency="VND",
    )


def _auth_mapping() -> qa.AuthMappingObservation:
    return qa.AuthMappingObservation(
        auth_tags=(_tag(401), _tag(402)),
        owner_tags=(_tag(403), _tag(404)),
        preexisting_count=0,
        one_to_one=True,
    )


def _initial() -> qa.InitialFlowObservations:
    return qa.InitialFlowObservations(
        http=_http_observations(qa.FlowPhase.INITIAL),
        storage=_storage(),
        rls=_rls(),
        auth_mapping=_auth_mapping(),
        analytics=_analytics(),
    )


def _fixture(
    fixture: qa.RestartFixture,
    *,
    job_tag: str,
    idempotency_tag: str,
    status: str,
    attempt_count: int,
    numbers: tuple[int, ...],
    statuses: tuple[str, ...],
    finished: int,
    fences: int,
    next_retry: qa.NextRetryState,
    due_after_restart: bool,
    stale: bool,
    error_code: str | None = None,
    manual: bool = False,
    side_effect_tags: tuple[str, ...] = (),
) -> qa.RestartFixtureSnapshot:
    return qa.RestartFixtureSnapshot(
        fixture=fixture,
        job_tag=job_tag,
        idempotency_tag=idempotency_tag,
        job_count=1,
        job_status=status,
        attempt_count=attempt_count,
        attempt_numbers=numbers,
        attempt_statuses=statuses,
        finished_attempt_count=finished,
        intent_fence_count=fences,
        next_retry_state=next_retry,
        scheduled_due_after_restart=due_after_restart,
        stale_before_cutoff=stale,
        error_code=error_code,
        manual_reconciliation=manual,
        side_effect_tags=side_effect_tags,
    )


def _before_restart(
    *,
    queued_job_tag: str = _tag(501),
    retry_job_tag: str = _tag(502),
    fenced_job_tag: str = _tag(503),
    queued_idempotency_tag: str = _tag(511),
    retry_idempotency_tag: str = _tag(512),
    fenced_idempotency_tag: str = _tag(513),
) -> qa.RestartSnapshot:
    return qa.RestartSnapshot(
        queued=_fixture(
            qa.RestartFixture.QUEUED,
            job_tag=queued_job_tag,
            idempotency_tag=queued_idempotency_tag,
            status="queued",
            attempt_count=0,
            numbers=(),
            statuses=(),
            finished=0,
            fences=0,
            next_retry=qa.NextRetryState.NOT_APPLICABLE,
            due_after_restart=True,
            stale=False,
        ),
        retry=_fixture(
            qa.RestartFixture.RETRY,
            job_tag=retry_job_tag,
            idempotency_tag=retry_idempotency_tag,
            status="retry",
            attempt_count=1,
            numbers=(1, 2),
            statuses=("retryable_error", "retryable_error"),
            finished=2,
            fences=0,
            next_retry=qa.NextRetryState.DUE_AFTER_RESTART,
            due_after_restart=True,
            stale=False,
        ),
        fenced=_fixture(
            qa.RestartFixture.FENCED,
            job_tag=fenced_job_tag,
            idempotency_tag=fenced_idempotency_tag,
            status="running",
            attempt_count=1,
            numbers=(1,),
            statuses=("running",),
            finished=0,
            fences=1,
            next_retry=qa.NextRetryState.NULL,
            due_after_restart=False,
            stale=True,
        ),
        analytics=_analytics(),
        media_object_tags=(_tag(520),),
        artifact_tags=(_tag(521),),
        media_bucket_private=True,
        artifact_bucket_private=True,
        attempt_unique_constraint_present=True,
    )


def _after_restart(
    before: qa.RestartSnapshot | None = None,
) -> qa.RestartSnapshot:
    source = before or _before_restart()
    return qa.RestartSnapshot(
        queued=_fixture(
            qa.RestartFixture.QUEUED,
            job_tag=source.queued.job_tag,
            idempotency_tag=source.queued.idempotency_tag,
            status="published",
            attempt_count=1,
            numbers=(1,),
            statuses=("published",),
            finished=1,
            fences=1,
            next_retry=qa.NextRetryState.NULL,
            due_after_restart=False,
            stale=False,
            side_effect_tags=(_tag(530),),
        ),
        retry=_fixture(
            qa.RestartFixture.RETRY,
            job_tag=source.retry.job_tag,
            idempotency_tag=source.retry.idempotency_tag,
            status="published",
            attempt_count=3,
            numbers=(1, 2, 3),
            statuses=("retryable_error", "retryable_error", "published"),
            finished=3,
            fences=1,
            next_retry=qa.NextRetryState.NULL,
            due_after_restart=False,
            stale=False,
            side_effect_tags=(_tag(531),),
        ),
        fenced=_fixture(
            qa.RestartFixture.FENCED,
            job_tag=source.fenced.job_tag,
            idempotency_tag=source.fenced.idempotency_tag,
            status="failed",
            attempt_count=1,
            numbers=(1,),
            statuses=("unknown",),
            finished=1,
            fences=1,
            next_retry=qa.NextRetryState.NULL,
            due_after_restart=False,
            stale=False,
            error_code="publish_outcome_unknown",
            manual=True,
        ),
        analytics=source.analytics,
        media_object_tags=source.media_object_tags,
        artifact_tags=source.artifact_tags,
        media_bucket_private=source.media_bucket_private,
        artifact_bucket_private=source.artifact_bucket_private,
        attempt_unique_constraint_present=True,
    )


def _boundary() -> qa.RestartBoundaryObservation:
    return qa.RestartBoundaryObservation(
        replica_count_before=1,
        fixture_transaction_committed=True,
        graceful_stop_exit=0,
        liveness_unavailable_while_stopped=True,
        start_exit=0,
        dotenv_file_disabled_after=True,
        replica_count_after=1,
        instance_tag_before=_tag(601),
        instance_tag_after=_tag(602),
    )


def _finish() -> qa.FinishFlowObservations:
    return qa.FinishFlowObservations(
        http=_http_observations(qa.FlowPhase.FINISH),
        artifact_metadata_deleted=True,
        artifact_object_absent=True,
        reconciliation_active_count=0,
        reconciliation_verified_count=0,
    )


_RESOURCE_BY_INVENTORY = {item: qa._INVENTORY_RESOURCE[item] for item in qa.InventoryClass}


class _TargetFactory:
    def __init__(self) -> None:
        self.next_id = 1000

    def make(self, inventory_class: qa.InventoryClass) -> qa.ExactCleanupTarget:
        resource = _RESOURCE_BY_INVENTORY[inventory_class]
        self.next_id += 1
        if resource is qa.CleanupResource.AUTH_USER:
            selector = qa.CleanupSelector.AUTH_USER_ID_EQ
            values: tuple[object, ...] = (str(UUID(int=self.next_id)),)
        elif resource in {
            qa.CleanupResource.MEDIA_OBJECT,
            qa.CleanupResource.ARTIFACT_OBJECT,
        }:
            selector = qa.CleanupSelector.BUCKET_AND_OBJECT_EQ
            bucket = (
                "arya-media" if resource is qa.CleanupResource.MEDIA_OBJECT else "arya-artifacts"
            )
            values = (
                bucket,
                f"users/{self.next_id}/aa/complete-object-{self.next_id}.bin",
            )
        else:
            selector = qa.CleanupSelector.ID_EQ
            values = (self.next_id,)
        provenance = (
            qa.DeletionProvenance.ARTIFACT_API_204
            if resource is qa.CleanupResource.ARTIFACT_OBJECT
            else qa.DeletionProvenance.CLEANUP_EXACT
        )
        return qa.ExactCleanupTarget(
            inventory_class=inventory_class,
            resource=resource,
            selector=selector,
            values=values,
            deletion_provenance=provenance,
        )


def _register_counts(
    ledger: qa.FixtureLedger,
    counts: Mapping[qa.InventoryClass, int],
    factory: _TargetFactory,
) -> tuple[qa.ExactCleanupTarget, ...]:
    targets: list[qa.ExactCleanupTarget] = []
    for inventory_class in qa.InventoryClass:
        for _index in range(counts.get(inventory_class, 0)):
            target = factory.make(inventory_class)
            ledger.register(target)
            targets.append(target)
    return tuple(targets)


_FIXTURE_COUNTS = {
    inventory_class: (
        qa.MANDATORY_INVENTORY_COUNTS[inventory_class]
        - qa.INITIAL_INVENTORY_COUNTS.get(inventory_class, 0)
    )
    for inventory_class in qa.InventoryClass
    if qa.MANDATORY_INVENTORY_COUNTS[inventory_class]
    - qa.INITIAL_INVENTORY_COUNTS.get(inventory_class, 0)
    > 0
}


def _post_cleanup(
    *,
    omit_scope: qa.MarkerScope | None = None,
    sentinel_unchanged: bool = True,
    buckets_private: bool = True,
    runtime_report: PreflightReport | None = None,
    replica: qa.ReplicaGateObservation | None = None,
) -> qa.PostCleanupVerification:
    return qa.PostCleanupVerification(
        marker_counts=tuple(
            qa.MarkerZeroObservation(scope, 0)
            for scope in qa.MarkerScope
            if scope is not omit_scope
        ),
        sentinel_unchanged=sentinel_unchanged,
        media_bucket_private=buckets_private,
        artifact_bucket_private=buckets_private,
        final_runtime_report=runtime_report or _runtime_report(),
        final_replica=replica or _replica(),
    )


class _Checkpoint:
    def __init__(self) -> None:
        self.calls = 0

    def await_restart(self) -> qa.RestartBoundaryObservation:
        self.calls += 1
        return _boundary()


class _FakeDriver:
    def __init__(
        self,
        *,
        initial: qa.InitialFlowObservations | None = None,
        finish: qa.FinishFlowObservations | None = None,
    ) -> None:
        self.initial_result = initial or _initial()
        self.finish_result = finish or _finish()
        self.factory = _TargetFactory()
        self.remaining: dict[qa.ExactCleanupTarget, int] = {}
        self.calls = Counter()
        self.before: qa.RestartSnapshot | None = None

    def _track(self, targets: tuple[qa.ExactCleanupTarget, ...]) -> None:
        for target in targets:
            self.remaining[target] = (
                0 if target.deletion_provenance is qa.DeletionProvenance.ARTIFACT_API_204 else 1
            )

    def run_initial_flow(
        self,
        *,
        marker: str,
        ledger: qa.FixtureLedger,
        tagger: qa.OpaqueTagger,
    ) -> qa.InitialFlowObservations:
        del marker, tagger
        self.calls["initial"] += 1
        self._track(
            _register_counts(
                ledger,
                qa.INITIAL_INVENTORY_COUNTS,
                self.factory,
            )
        )
        return self.initial_result

    def prepare_restart_fixtures(
        self,
        *,
        ledger: qa.FixtureLedger,
        tagger: qa.OpaqueTagger,
    ) -> qa.RestartSnapshot:
        del tagger
        self.calls["prepare"] += 1
        self._track(_register_counts(ledger, _FIXTURE_COUNTS, self.factory))
        self.before = _before_restart()
        return self.before

    def resume_after_restart(
        self,
        *,
        tagger: qa.OpaqueTagger,
    ) -> qa.RestartSnapshot:
        del tagger
        self.calls["resume"] += 1
        assert self.before is not None
        return _after_restart(self.before)

    def settled_snapshot(
        self,
        *,
        tagger: qa.OpaqueTagger,
    ) -> qa.RestartSnapshot:
        del tagger
        self.calls["settled"] += 1
        assert self.before is not None
        return _after_restart(self.before)

    def finish_flow(self) -> qa.FinishFlowObservations:
        self.calls["finish"] += 1
        return self.finish_result

    def delete_exact(self, target: qa.ExactCleanupTarget) -> int:
        self.calls["delete"] += 1
        affected = self.remaining[target]
        self.remaining[target] = 0
        return affected

    def remaining_exact(self, target: qa.ExactCleanupTarget) -> int:
        self.calls["verify"] += 1
        return self.remaining[target]

    def post_cleanup_verification(
        self,
        marker: str,
    ) -> qa.PostCleanupVerification:
        del marker
        self.calls["post_cleanup"] += 1
        return _post_cleanup()


class _InterruptingCleanupDriver(_FakeDriver):
    def __init__(self, interruption: BaseException) -> None:
        super().__init__()
        self.interruption = interruption
        self.verified_targets: list[qa.ExactCleanupTarget] = []

    def delete_exact(self, target: qa.ExactCleanupTarget) -> int:
        self.calls["delete"] += 1
        if self.calls["delete"] == 1:
            raise self.interruption
        affected = self.remaining[target]
        self.remaining[target] = 0
        return affected

    def remaining_exact(self, target: qa.ExactCleanupTarget) -> int:
        self.calls["verify"] += 1
        self.verified_targets.append(target)
        return self.remaining[target]


def _run(
    driver: _FakeDriver,
    *,
    checkpoint: _Checkpoint | None = None,
    key_factory: qa.KeyFactory | None = None,
) -> qa.HarnessEvidence:
    kwargs: dict[str, object] = {}
    if key_factory is not None:
        kwargs["key_factory"] = key_factory
    return qa.run_smoke(
        driver=driver,
        gate=_gate(),
        checkpoint=checkpoint or _Checkpoint(),
        post_restart_runtime_probe=_runtime_report,
        marker="opaque-marker-123456789",
        **kwargs,
    )


def test_default_cli_is_deterministic_plan_only(capsys):
    assert qa.main([]) == 0
    first = json.loads(capsys.readouterr().out)
    assert qa.main(["plan"]) == 0
    second = json.loads(capsys.readouterr().out)

    assert first == second == qa.plan_document()
    assert first["mode"] == "plan_only"
    assert first["remote_mutation"] is False
    assert first["implementation_state"] == "live_driver_required"


def test_gate_requires_opt_in_and_exact_dotenv_guard_before_probes():
    calls = Counter()

    def replica_probe() -> qa.ReplicaGateObservation:
        calls["replica"] += 1
        return _replica()

    def runtime_probe() -> PreflightReport:
        calls["runtime"] += 1
        return _runtime_report()

    with pytest.raises(qa.HarnessBlocked) as opt_in:
        qa.evaluate_execution_gate(
            _valid_environment(),
            allow_remote_mutation=False,
            replica_probe=replica_probe,
            runtime_probe=runtime_probe,
        )
    assert opt_in.value.code is qa.HarnessCode.OPT_IN_REQUIRED
    assert not calls

    environment = _valid_environment()
    environment["LAPLACE_DISABLE_ENV_FILE"] = "true"
    with pytest.raises(qa.HarnessBlocked) as dotenv:
        qa.evaluate_execution_gate(
            environment,
            allow_remote_mutation=True,
            replica_probe=replica_probe,
            runtime_probe=runtime_probe,
        )
    assert dotenv.value.code is qa.HarnessCode.DOTENV_GUARD_REQUIRED
    assert not calls


def test_gate_requires_probed_replica_and_exact_report_sets_and_sources():
    with pytest.raises(ValueError):
        qa.ValidatedExecutionGate(
            static_report=_report(
                frozenset({"one_check"}),
                source="process_environment_only",
                remote_access=False,
            ),
            runtime_report=_runtime_report(),
            replica=_replica(),
            remote_mutation_opt_in=True,
            dotenv_file_disabled=True,
        )

    with pytest.raises(qa.HarnessBlocked) as replica:
        qa.evaluate_execution_gate(
            _valid_environment(),
            allow_remote_mutation=True,
            replica_probe=lambda: qa.ReplicaGateObservation(
                "caller_assertion",
                True,
                1,
            ),
            runtime_probe=_runtime_report,
        )
    assert replica.value.code is qa.HarnessCode.REPLICA_GATE_BLOCKED

    wrong_runtime = _report(
        qa._RUNTIME_CHECKS - {"runtime_scheduler"},
        source="loopback_health_endpoints",
        remote_access=True,
    )
    with pytest.raises(qa.HarnessBlocked) as runtime:
        qa.evaluate_execution_gate(
            _valid_environment(),
            allow_remote_mutation=True,
            replica_probe=_replica,
            runtime_probe=lambda: wrong_runtime,
        )
    assert runtime.value.code is qa.HarnessCode.RUNTIME_PREFLIGHT_BLOCKED


def test_gate_accepts_only_full_typed_observations_without_exposing_values():
    gate = qa.evaluate_execution_gate(
        _valid_environment(),
        allow_remote_mutation=True,
        replica_probe=_replica,
        runtime_probe=_runtime_report,
    )
    serialized = json.dumps(gate.public_dict())

    assert isinstance(gate, qa.ValidatedExecutionGate)
    assert "project-ref" not in serialized
    assert "db-password" not in serialized
    assert "sentinel" not in serialized


def test_initial_http_is_exact_and_invariants_are_typed():
    accepted = qa.assert_http_phase(
        _http_observations(qa.FlowPhase.INITIAL),
        qa.FlowPhase.INITIAL,
    )
    assert len(accepted) == len(qa.INITIAL_HTTP_MATRIX)

    mismatch = list(accepted)
    mismatch[0] = replace(mismatch[0], actual_status=403)
    with pytest.raises(qa.HarnessBlocked):
        qa.assert_http_phase(mismatch, qa.FlowPhase.INITIAL)

    disclosed = list(accepted)
    disclosed[-1] = replace(
        disclosed[-1],
        no_sensitive_disclosure=False,
    )
    with pytest.raises(qa.HarnessBlocked):
        qa.assert_http_phase(disclosed, qa.FlowPhase.INITIAL)

    mutated = list(accepted)
    cross_index = next(
        index for index, item in enumerate(mutated) if item.step == "cross_owner_product_override"
    )
    mutated[cross_index] = replace(
        mutated[cross_index],
        created_count=1,
        owner_state_valid=False,
    )
    with pytest.raises(qa.HarnessBlocked):
        qa.assert_http_phase(mutated, qa.FlowPhase.INITIAL)


def test_initial_http_mismatch_cleans_immediately_before_restart_mutations():
    bad_http = list(_http_observations(qa.FlowPhase.INITIAL))
    bad_http[0] = replace(bad_http[0], actual_status=403)
    driver = _FakeDriver(initial=replace(_initial(), http=tuple(bad_http)))
    checkpoint = _Checkpoint()

    evidence = _run(driver, checkpoint=checkpoint)

    assert evidence.result == "blocked"
    assert evidence.failure_code == qa.HarnessCode.INITIAL_CONTRACT_BLOCKED
    assert driver.calls["initial"] == 1
    assert driver.calls["prepare"] == 0
    assert checkpoint.calls == 0
    assert driver.calls["resume"] == 0
    assert driver.calls["finish"] == 0
    assert driver.calls["delete"] == sum(qa.INITIAL_INVENTORY_COUNTS.values()) - 1
    assert evidence.cleanup.ready


def test_driver_exception_with_complete_initial_cleanup_is_consistent_failure():
    class FailingInitialDriver(_FakeDriver):
        def run_initial_flow(
            self,
            *,
            marker: str,
            ledger: qa.FixtureLedger,
            tagger: qa.OpaqueTagger,
        ) -> qa.InitialFlowObservations:
            super().run_initial_flow(marker=marker, ledger=ledger, tagger=tagger)
            raise RuntimeError("sanitized driver failure")

    evidence = _run(FailingInitialDriver())

    assert evidence.result == "failed"
    assert evidence.failure_code == qa.HarnessCode.DRIVER_FAILED
    assert evidence.cleanup.ready


def test_storage_contract_records_exact_dynamic_denials_and_owner_proofs():
    storage = _storage()
    assert storage.accepted
    assert {item.actual_status for item in storage.denials} == {
        401,
        402,
        403,
        404,
    }

    success_denial = list(storage.denials)
    success_denial[0] = replace(success_denial[0], actual_status=200)
    assert not replace(storage, denials=tuple(success_denial)).accepted

    changed_owner = list(storage.denials)
    changed_owner[0] = replace(
        changed_owner[0],
        owner_hash_after=_tag(999),
    )
    assert not replace(storage, denials=tuple(changed_owner)).accepted

    bad_authorized = list(storage.authorized)
    bad_authorized[0] = replace(
        bad_authorized[0],
        payload_hash_matches=False,
    )
    assert not replace(storage, authorized=tuple(bad_authorized)).accepted
    assert not replace(
        storage,
        privacy=replace(
            storage.privacy,
            artifact_private=False,
        ),
    ).accepted


def test_rls_contract_requires_exact_equality_zero_rows_and_stable_hash():
    observations = _rls()
    assert len(qa.assert_rls_observations(observations)) == 21

    broad = list(observations)
    broad[0] = replace(broad[0], selector_shape="owner_id = :owner")
    with pytest.raises(qa.HarnessBlocked):
        qa.assert_rls_observations(broad)

    affected = list(observations)
    affected[0] = replace(affected[0], affected_rows=1)
    with pytest.raises(qa.HarnessBlocked):
        qa.assert_rls_observations(affected)

    changed = list(observations)
    changed[0] = replace(changed[0], owner_hash_after=_tag(999))
    with pytest.raises(qa.HarnessBlocked):
        qa.assert_rls_observations(changed)


def test_analytics_requires_seeded_totals_currency_and_event_identity():
    analytics = _analytics()
    assert analytics.accepted
    assert not replace(
        analytics,
        observed_commission_amount=14999,
    ).accepted
    assert not replace(analytics, observed_currency="USD").accepted
    assert not replace(
        analytics,
        event_tags=(analytics.event_tags[0],) * 3,
    ).accepted


def test_manifest_fails_for_each_missing_inventory_class_and_cardinality():
    for missing_class in qa.InventoryClass:
        ledger = qa.FixtureLedger()
        factory = _TargetFactory()
        reduced = dict(qa.MANDATORY_INVENTORY_COUNTS)
        reduced[missing_class] -= 1
        _register_counts(ledger, reduced, factory)
        assert not ledger.manifest().complete, missing_class

    ledger = qa.FixtureLedger()
    factory = _TargetFactory()
    _register_counts(ledger, qa.MANDATORY_INVENTORY_COUNTS, factory)
    ledger.register(factory.make(qa.InventoryClass.AUTH_USER))
    assert not ledger.manifest().complete


def test_manifest_accepts_empty_initial_and_full_stages_only():
    empty = qa.FixtureLedger()
    assert empty.manifest().complete
    assert empty.manifest().stage == "empty"
    assert not empty.manifest("initial").complete

    initial = qa.FixtureLedger()
    _register_counts(
        initial,
        qa.INITIAL_INVENTORY_COUNTS,
        _TargetFactory(),
    )
    assert initial.manifest().complete
    assert initial.manifest().stage == "initial"
    assert not initial.manifest("complete").complete

    full = qa.FixtureLedger()
    _register_counts(
        full,
        qa.MANDATORY_INVENTORY_COUNTS,
        _TargetFactory(),
    )
    assert full.manifest().complete
    assert full.manifest().stage == "complete"


def test_auth_cleanup_target_requires_a_canonical_uuid():
    with pytest.raises(ValueError):
        qa.ExactCleanupTarget(
            inventory_class=qa.InventoryClass.AUTH_USER,
            resource=qa.CleanupResource.AUTH_USER,
            selector=qa.CleanupSelector.AUTH_USER_ID_EQ,
            values=("auth-user-123",),
        )

    canonical = str(UUID(int=123))
    target = qa.ExactCleanupTarget(
        inventory_class=qa.InventoryClass.AUTH_USER,
        resource=qa.CleanupResource.AUTH_USER,
        selector=qa.CleanupSelector.AUTH_USER_ID_EQ,
        values=(canonical,),
    )
    assert target.values == (canonical,)


def test_artifact_api_provenance_is_mandatory_both_directions_and_residue_blocks():
    with pytest.raises(ValueError):
        qa.ExactCleanupTarget(
            inventory_class=qa.InventoryClass.MEDIA_OBJECT,
            resource=qa.CleanupResource.MEDIA_OBJECT,
            selector=qa.CleanupSelector.BUCKET_AND_OBJECT_EQ,
            values=("arya-media", "users/1/aa/object.png"),
            deletion_provenance=qa.DeletionProvenance.ARTIFACT_API_204,
        )

    with pytest.raises(ValueError):
        qa.ExactCleanupTarget(
            inventory_class=qa.InventoryClass.ARTIFACT_OBJECT,
            resource=qa.CleanupResource.ARTIFACT_OBJECT,
            selector=qa.CleanupSelector.BUCKET_AND_OBJECT_EQ,
            values=("arya-artifacts", "users/1/aa/object.bin"),
        )

    ledger = qa.FixtureLedger()
    factory = _TargetFactory()
    targets = _register_counts(
        ledger,
        qa.INITIAL_INVENTORY_COUNTS,
        factory,
    )
    remaining = {target: 1 for target in targets}

    class Executor:
        def delete_exact(self, target: qa.ExactCleanupTarget) -> int:
            affected = remaining[target]
            remaining[target] = 0
            return affected

        def remaining_exact(self, target: qa.ExactCleanupTarget) -> int:
            return remaining[target]

        def post_cleanup_verification(
            self,
            marker: str,
        ) -> qa.PostCleanupVerification:
            del marker
            return _post_cleanup()

    key = bytearray(b"k" * 32)
    tagger = qa.OpaqueTagger(key)
    report = qa.execute_cleanup(
        ledger,
        Executor(),
        marker="opaque-marker-123456",
        tagger=tagger,
    )
    assert not report.ready
    artifact = next(
        item
        for item in report.observations
        if item.inventory_class is qa.InventoryClass.ARTIFACT_OBJECT
    )
    assert artifact.operation_status == "api_delete_residue_removed"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"omit_scope": qa.MarkerScope.AUTH_USERS}, False),
        ({"sentinel_unchanged": False}, False),
        ({"buckets_private": False}, False),
        (
            {
                "runtime_report": _report(
                    qa._RUNTIME_CHECKS - {"runtime_scheduler"},
                    source="loopback_health_endpoints",
                    remote_access=True,
                )
            },
            False,
        ),
        (
            {
                "replica": qa.ReplicaGateObservation(
                    "approved_runtime_inventory",
                    True,
                    2,
                )
            },
            False,
        ),
        ({}, True),
    ],
)
def test_post_cleanup_requires_per_scope_zero_sentinel_buckets_and_health(
    change,
    expected,
):
    assert _post_cleanup(**change).accepted is expected


def test_restart_requires_stable_identity_idempotency_due_and_side_effects():
    before = _before_restart()
    after = _after_restart(before)
    result = qa.validate_restart_transition(before, after, after)
    assert result.after.queued.side_effect_tags == (_tag(530),)
    assert result.after.retry.side_effect_tags == (_tag(531),)
    assert result.after.fenced.side_effect_tags == ()

    wrong_identity = replace(
        after,
        queued=replace(after.queued, job_tag=_tag(999)),
    )
    with pytest.raises(qa.HarnessBlocked):
        qa.validate_restart_transition(before, wrong_identity, wrong_identity)

    wrong_idempotency = replace(
        after,
        retry=replace(after.retry, idempotency_tag=_tag(998)),
    )
    with pytest.raises(qa.HarnessBlocked):
        qa.validate_restart_transition(
            before,
            wrong_idempotency,
            wrong_idempotency,
        )

    not_due = replace(
        before,
        retry=replace(
            before.retry,
            next_retry_state=qa.NextRetryState.NULL,
        ),
    )
    with pytest.raises(qa.HarnessBlocked):
        qa.validate_restart_transition(not_due, after, after)

    duplicate_side_effect = replace(
        after,
        queued=replace(
            after.queued,
            side_effect_tags=(_tag(530), _tag(532)),
        ),
    )
    with pytest.raises(qa.HarnessBlocked):
        qa.validate_restart_transition(
            before,
            duplicate_side_effect,
            duplicate_side_effect,
        )

    repeated_side_effect = replace(
        after,
        queued=replace(
            after.queued,
            side_effect_tags=(_tag(530), _tag(530)),
        ),
    )
    with pytest.raises(qa.HarnessBlocked):
        qa.validate_restart_transition(
            before,
            repeated_side_effect,
            repeated_side_effect,
        )

    replayed_fence = replace(
        after,
        fenced=replace(
            after.fenced,
            error_code=None,
            manual_reconciliation=False,
            side_effect_tags=(_tag(533),),
        ),
    )
    with pytest.raises(qa.HarnessBlocked):
        qa.validate_restart_transition(
            before,
            replayed_fence,
            replayed_fence,
        )


def test_restart_rejects_changed_analytics_or_duplicate_artifacts():
    before = _before_restart()
    after = _after_restart(before)
    changed_analytics = replace(
        after,
        analytics=replace(
            after.analytics,
            observed_commission_amount=16000,
        ),
    )
    with pytest.raises(qa.HarnessBlocked):
        qa.validate_restart_transition(
            before,
            changed_analytics,
            changed_analytics,
        )

    duplicate_artifact = replace(
        after,
        artifact_tags=after.artifact_tags + (_tag(540),),
    )
    with pytest.raises(qa.HarnessBlocked):
        qa.validate_restart_transition(
            before,
            duplicate_artifact,
            duplicate_artifact,
        )

    non_private_bucket = replace(
        after,
        artifact_bucket_private=False,
    )
    with pytest.raises(qa.HarnessBlocked):
        qa.validate_restart_transition(
            before,
            non_private_bucket,
            non_private_bucket,
        )


class _TTYStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_interactive_checkpoint_requires_real_typed_boundary():
    output = io.StringIO()
    checkpoint = qa.InteractiveRestartCheckpoint(
        boundary_probe=_boundary,
        input_stream=_TTYStringIO("RESUME\n"),
        output_stream=output,
    )
    assert checkpoint.await_restart().accepted
    assert output.getvalue() == "qa03b_restart_checkpoint=ready\n"

    invalid = qa.InteractiveRestartCheckpoint(
        boundary_probe=lambda: replace(
            _boundary(),
            instance_tag_after=_boundary().instance_tag_before,
        ),
        input_stream=_TTYStringIO("RESUME\n"),
        output_stream=io.StringIO(),
    )
    with pytest.raises(qa.HarnessBlocked) as exc:
        invalid.await_restart()
    assert exc.value.code is qa.HarnessCode.RESTART_BOUNDARY_INVALID


def test_full_fake_driver_registers_inventory_and_passes_typed_contract():
    driver = _FakeDriver()
    checkpoint = _Checkpoint()
    key = bytearray(b"a" * 32)

    evidence = _run(
        driver,
        checkpoint=checkpoint,
        key_factory=lambda: key,
    )

    assert evidence.result == "passed"
    assert evidence.failure_code is None
    assert evidence.cleanup.ready
    assert evidence.cleanup.manifest.complete
    assert evidence.cleanup.manifest.stage == "complete"
    assert len(evidence.cleanup.observations) == sum(qa.MANDATORY_INVENTORY_COUNTS.values())
    assert checkpoint.calls == 1
    assert driver.calls["prepare"] == 1
    assert driver.calls["resume"] == 1
    assert driver.calls["settled"] == 1
    assert driver.calls["finish"] == 1
    assert all(value == 0 for value in key)


def test_full_flow_with_zero_registered_targets_blocks_complete_manifest():
    class EmptyLedgerDriver(_FakeDriver):
        def run_initial_flow(
            self,
            *,
            marker: str,
            ledger: qa.FixtureLedger,
            tagger: qa.OpaqueTagger,
        ) -> qa.InitialFlowObservations:
            del marker, ledger, tagger
            self.calls["initial"] += 1
            return self.initial_result

        def prepare_restart_fixtures(
            self,
            *,
            ledger: qa.FixtureLedger,
            tagger: qa.OpaqueTagger,
        ) -> qa.RestartSnapshot:
            del ledger, tagger
            self.calls["prepare"] += 1
            self.before = _before_restart()
            return self.before

    evidence = _run(EmptyLedgerDriver())

    assert evidence.result == "blocked"
    assert evidence.failure_code == qa.HarnessCode.CLEANUP_INCOMPLETE
    assert evidence.cleanup.manifest.stage == "complete"
    assert not evidence.cleanup.manifest.complete
    assert evidence.cleanup.observations == ()


def test_full_flow_rejects_artifact_target_without_api_delete_provenance():
    class DefaultArtifactProvenanceDriver(_FakeDriver):
        def run_initial_flow(
            self,
            *,
            marker: str,
            ledger: qa.FixtureLedger,
            tagger: qa.OpaqueTagger,
        ) -> qa.InitialFlowObservations:
            del marker, tagger
            self.calls["initial"] += 1
            for inventory_class in qa.InventoryClass:
                for _index in range(qa.INITIAL_INVENTORY_COUNTS.get(inventory_class, 0)):
                    target = self.factory.make(inventory_class)
                    if inventory_class is qa.InventoryClass.ARTIFACT_OBJECT:
                        target = replace(
                            target,
                            deletion_provenance=qa.DeletionProvenance.CLEANUP_EXACT,
                        )
                    ledger.register(target)
                    self._track((target,))
            return self.initial_result

    driver = DefaultArtifactProvenanceDriver()
    evidence = _run(driver)

    assert evidence.result == "blocked"
    assert evidence.failure_code == qa.HarnessCode.CLEANUP_INCOMPLETE
    assert not evidence.cleanup.ready
    assert driver.calls["prepare"] == 0


def test_keyboard_interrupt_during_cleanup_runs_every_verification_and_blocks():
    driver = _InterruptingCleanupDriver(KeyboardInterrupt())
    evidence = _run(driver)

    assert evidence.result == "blocked"
    assert evidence.failure_code == qa.HarnessCode.CLEANUP_INCOMPLETE
    assert len(evidence.cleanup.observations) == sum(qa.MANDATORY_INVENTORY_COUNTS.values())
    assert evidence.cleanup.observations[0].operation_status == "interrupted"
    assert set(driver.verified_targets) == set(driver.remaining)
    assert driver.calls["post_cleanup"] == 1


@pytest.mark.parametrize("fatal_interrupt", [SystemExit(17), GeneratorExit()])
def test_fatal_cleanup_interrupt_runs_verification_then_is_reraised(fatal_interrupt):
    key = bytearray(b"e" * 32)
    driver = _InterruptingCleanupDriver(fatal_interrupt)

    with pytest.raises(type(fatal_interrupt)):
        _run(driver, key_factory=lambda: key)

    assert set(driver.verified_targets) == set(driver.remaining)
    assert driver.calls["post_cleanup"] == 1
    assert all(value == 0 for value in key)


def test_hmac_tags_are_per_run_distinct_and_key_is_not_retained():
    first_key = bytearray(b"b" * 32)
    second_key = bytearray(b"c" * 32)
    first = _run(_FakeDriver(), key_factory=lambda: first_key)
    second = _run(_FakeDriver(), key_factory=lambda: second_key)

    first_tag = first.cleanup.observations[0].target_tag
    second_tag = second.cleanup.observations[0].target_tag
    assert first_tag != second_tag
    assert all(value == 0 for value in first_key)
    assert all(value == 0 for value in second_key)
    assert "OpaqueTagger" not in repr(first)


def test_hmac_key_is_erased_when_driver_is_interrupted():
    class InterruptingDriver(_FakeDriver):
        def run_initial_flow(
            self,
            *,
            marker: str,
            ledger: qa.FixtureLedger,
            tagger: qa.OpaqueTagger,
        ) -> qa.InitialFlowObservations:
            super().run_initial_flow(marker=marker, ledger=ledger, tagger=tagger)
            raise KeyboardInterrupt

    key = bytearray(b"d" * 32)
    driver = InterruptingDriver()

    with pytest.raises(KeyboardInterrupt):
        _run(driver, key_factory=lambda: key)

    assert all(value == 0 for value in key)
    assert all(remaining == 0 for remaining in driver.remaining.values())


def test_finish_requires_api_delete_absence_and_post_delete_reconciliation():
    driver = _FakeDriver(
        finish=replace(
            _finish(),
            artifact_object_absent=False,
        )
    )
    evidence = _run(driver)

    assert evidence.result == "blocked"
    assert evidence.failure_code == qa.HarnessCode.FINISH_CONTRACT_BLOCKED
    artifact = next(
        item
        for item in evidence.cleanup.observations
        if item.inventory_class is qa.InventoryClass.ARTIFACT_OBJECT
    )
    assert artifact.operation_status == "verified_api_delete"


def test_evidence_writer_accepts_only_typed_schema_and_is_private(tmp_path):
    evidence = _run(_FakeDriver())
    destination = tmp_path / "evidence.json"

    qa.write_evidence(destination, evidence)
    persisted = json.loads(destination.read_text(encoding="ascii"))

    assert persisted["result"] == "passed"
    assert len(persisted["evidence_digest"]) == 16
    assert os.stat(destination).st_mode & 0o777 == 0o600
    with pytest.raises(TypeError):
        qa.write_evidence(tmp_path / "mapping.json", {"status": "passed"})
    with pytest.raises(FileExistsError):
        qa.write_evidence(destination, evidence)
    with pytest.raises(qa.EvidenceViolation):
        qa.write_evidence(tmp_path / ".env", evidence)


def test_harness_evidence_rejects_forged_result_semantics_at_every_boundary(
    tmp_path,
):
    evidence = _run(_FakeDriver())

    with pytest.raises(qa.EvidenceViolation):
        replace(
            evidence,
            result="passed",
            failure_code=None,
            initial=None,
            restart_boundary=None,
            restart=None,
            finish=None,
        )
    with pytest.raises(qa.EvidenceViolation):
        replace(evidence, result="blocked", failure_code=None)
    with pytest.raises(qa.EvidenceViolation):
        replace(
            evidence,
            result="blocked",
            failure_code=qa.HarnessCode.INITIAL_CONTRACT_BLOCKED.value,
        )
    with pytest.raises(qa.EvidenceViolation):
        replace(
            evidence,
            result="failed",
            failure_code=qa.HarnessCode.CLEANUP_INCOMPLETE.value,
        )
    with pytest.raises(qa.EvidenceViolation):
        replace(
            evidence,
            result="failed",
            failure_code=qa.HarnessCode.DRIVER_FAILED.value,
        )

    forged = _run(_FakeDriver())
    object.__setattr__(forged, "initial", None)
    with pytest.raises(qa.EvidenceViolation):
        qa.evidence_document(forged)
    with pytest.raises(qa.EvidenceViolation):
        qa.write_evidence(tmp_path / "forged.json", forged)
    assert not (tmp_path / "forged.json").exists()


def test_structural_allowlist_rejects_unknown_nested_fields():
    document = qa.evidence_document(_run(_FakeDriver()))
    document["initial"]["storage"]["privacy"]["unexpected"] = True

    with pytest.raises(qa.EvidenceViolation):
        qa.validate_evidence_structure(document, kind="harness")


@pytest.mark.parametrize(
    "payload",
    [
        {"value": "postgresql://user:password@host/database"},
        {"value": "db.project.supabase.co"},
        {"value": "users/12/aa/complete-object.png"},
        {"value": "https://project.invalid"},
        {"id": 12},
        {"server_secret_key": "raw-value"},
    ],
)
def test_redaction_rejects_connections_hosts_objects_and_identifiers(payload):
    with pytest.raises(qa.EvidenceViolation):
        qa.validate_public_evidence(payload)


def test_cli_run_remains_truthfully_unwired_and_non_mutating(capsys):
    assert (
        qa.main(
            ["run"],
            environment=_valid_environment(),
            runtime_probe=_runtime_report,
            replica_probe=_replica,
        )
        == 2
    )
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["code"] == qa.HarnessCode.OPT_IN_REQUIRED

    assert (
        qa.main(
            ["run", "--allow-remote-mutation"],
            environment=_valid_environment(),
            runtime_probe=_runtime_report,
            replica_probe=_replica,
        )
        == 2
    )
    unavailable = json.loads(capsys.readouterr().out)
    assert unavailable["code"] == qa.HarnessCode.LIVE_DRIVER_UNAVAILABLE
    assert unavailable["remote_mutation"] is False
