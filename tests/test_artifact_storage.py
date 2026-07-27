"""Offline tests for private, owner-scoped Supabase artifact storage."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import laplace.models  # noqa: F401
from laplace.db import Base
from laplace.models import User
from laplace.services.artifacts import (
    Artifact,
    ArtifactError,
    ArtifactOwnershipError,
    ArtifactPersistenceError,
    ArtifactService,
    ArtifactValidationError,
    StoredArtifact,
    SupabaseArtifactStorage,
    artifact_object_key,
    safe_artifact_filename,
    upload_with_compensation,
)


def _stored_artifact(
    *,
    user_id: int = 3,
    payload: bytes = b"artifact-payload",
    original_name: str = "report.json",
    created: bool = True,
) -> StoredArtifact:
    digest = hashlib.sha256(payload).hexdigest()
    safe_name = safe_artifact_filename(original_name)
    return StoredArtifact(
        user_id=user_id,
        original_name=safe_name,
        content_type="application/json",
        size_bytes=len(payload),
        sha256=digest,
        bucket="arya-artifacts",
        key=artifact_object_key(user_id, digest, safe_name),
        created=created,
    )


def test_artifact_object_key_is_deterministic_and_path_safe():
    digest = "a" * 64

    key = artifact_object_key(7, digest, "../../Bao cao QUY III?.json")

    assert key == f"users/7/aa/{digest}-bao-cao-quy-iii-.json"
    assert ".." not in key
    assert "\\" not in key


def test_private_storage_upload_download_and_signed_url_are_owner_scoped():
    secret = "test-secret-never-logged"
    payload = b'{"ready": true}'
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/object/sign/" in request.url.path:
            return httpx.Response(200, json={"signedURL": "/object/sign/short"})
        if "/object/authenticated/" in request.url.path:
            return httpx.Response(200, content=payload)
        return httpx.Response(201, json={"Key": "stored"})

    storage = SupabaseArtifactStorage(
        project_url="https://project.example.test",
        secret_key=secret,
        bucket="arya-artifacts",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    stored = storage.upload(
        user_id=3,
        original_name="Report.JSON",
        payload=payload,
        content_type="application/json",
    )

    assert stored.key == artifact_object_key(3, stored.sha256, "report.json")
    assert storage.download(stored, user_id=3) == payload
    assert storage.create_signed_url(
        stored,
        user_id=3,
        expires_in=120,
    ) == "https://project.example.test/storage/v1/object/sign/short"
    assert all(request.headers["authorization"] == f"Bearer {secret}" for request in requests)
    assert requests[0].headers["x-upsert"] == "false"

    with pytest.raises(ArtifactOwnershipError):
        storage.create_signed_url(stored, user_id=4, expires_in=120)
    assert len(requests) == 3


def test_idempotent_upload_verifies_existing_object_hash():
    payload = b"same-object"
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(409)
        return httpx.Response(200, content=payload)

    storage = SupabaseArtifactStorage(
        project_url="https://project.example.test",
        secret_key="fake",
        bucket="arya-artifacts",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    stored = storage.upload(
        user_id=2,
        original_name="data.csv",
        payload=payload,
        content_type="text/csv",
    )

    assert not stored.created
    assert calls == ["POST", "GET"]


def test_signed_url_rejects_an_unexpected_origin():
    stored = _stored_artifact()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"signedURL": "https://untrusted.invalid/private-object"},
        )

    storage = SupabaseArtifactStorage(
        project_url="https://project.example.test",
        secret_key="fake",
        bucket="arya-artifacts",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ArtifactError, match="response is invalid"):
        storage.create_signed_url(stored, user_id=3, expires_in=120)


def test_storage_validation_and_errors_do_not_expose_secret_or_response():
    secret = "do-not-expose"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"upstream echoed {secret}")

    storage = SupabaseArtifactStorage(
        project_url="https://project.example.test",
        secret_key=secret,
        bucket="arya-artifacts",
        max_bytes=8,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ArtifactValidationError, match="size limit"):
        storage.upload(
            user_id=1,
            original_name="large.json",
            payload=b"123456789",
            content_type="application/json",
        )
    with pytest.raises(ArtifactValidationError, match="content type"):
        storage.upload(
            user_id=1,
            original_name="page.html",
            payload=b"ok",
            content_type="text/html",
        )
    with pytest.raises(ArtifactError) as caught:
        storage.upload(
            user_id=1,
            original_name="ok.json",
            payload=b"ok",
            content_type="application/json",
        )

    assert "status 500" in str(caught.value)
    assert secret not in str(caught.value)
    assert "upstream echoed" not in str(caught.value)


def test_persistence_failure_compensates_only_new_uploads():
    class FakeStorage:
        def __init__(self, created: bool):
            self.created = created
            self.deleted = []

        def upload(self, **_kwargs):
            return _stored_artifact(created=self.created)

        def delete(self, stored, *, user_id):
            self.deleted.append((stored.key, user_id))

    for created, expected_deletes in ((True, 1), (False, 0)):
        storage = FakeStorage(created)

        with pytest.raises(ArtifactPersistenceError):
            upload_with_compensation(
                storage,
                user_id=3,
                original_name="report.json",
                payload=b"artifact-payload",
                content_type="application/json",
                persist=lambda _stored: (_ for _ in ()).throw(RuntimeError("db")),
            )

        assert len(storage.deleted) == expected_deletes


class _FakeArtifactStorage:
    bucket = "arya-artifacts"

    def __init__(self) -> None:
        self.upload_calls = 0
        self.deleted: list[str] = []
        self.fail_delete = False
        self.payloads: dict[str, bytes] = {}

    def upload(self, *, user_id, original_name, payload, content_type):
        self.upload_calls += 1
        stored = _stored_artifact(
            user_id=user_id,
            payload=payload,
            original_name=original_name,
        )
        created = stored.key not in self.payloads
        self.payloads[stored.key] = payload
        return StoredArtifact(
            user_id=stored.user_id,
            original_name=stored.original_name,
            content_type=content_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            bucket=stored.bucket,
            key=stored.key,
            created=created,
        )

    def download(self, stored, *, user_id):
        if stored.user_id != user_id:
            raise ArtifactOwnershipError("wrong owner")
        try:
            return self.payloads[stored.key]
        except KeyError as exc:
            raise ArtifactError("missing object") from exc

    def create_signed_url(self, stored, *, user_id, expires_in):
        if stored.user_id != user_id:
            raise ArtifactOwnershipError("wrong owner")
        return f"https://signed.invalid/{expires_in}/{stored.sha256}"

    def delete(self, stored, *, user_id):
        if self.fail_delete:
            raise ArtifactError("safe delete failure")
        if stored.user_id != user_id:
            raise ArtifactOwnershipError("wrong owner")
        self.deleted.append(stored.key)
        self.payloads.pop(stored.key, None)


def _artifact_service(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'artifacts.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all([User(id=1), User(id=2)])
        session.commit()
    storage = _FakeArtifactStorage()
    return ArtifactService(storage=storage, session_factory=factory), storage, factory


def test_artifact_service_persists_deduplicates_and_enforces_owner(tmp_path):
    service, storage, factory = _artifact_service(tmp_path)
    payload = b'{"report": 1}'

    first = service.upload(
        user_id=1,
        kind="report",
        original_name="report.json",
        payload=payload,
        content_type="application/json",
    )
    second = service.upload(
        user_id=1,
        kind="report",
        original_name="report.json",
        payload=payload,
        content_type="application/json",
    )

    assert first == second
    assert storage.upload_calls == 2
    assert len(storage.payloads) == 1
    assert service.download(first.id, user_id=1) == payload
    assert "/120/" in service.create_signed_url(first.id, user_id=1, expires_in=120)
    assert service.reconcile_owner(user_id=1).healthy
    with pytest.raises(ArtifactOwnershipError):
        service.download(first.id, user_id=2)

    stored_payload = storage.payloads.pop(first.storage_key)
    reconciliation = service.reconcile_owner(user_id=1)
    assert not reconciliation.healthy
    assert reconciliation.unavailable_artifact_ids == (first.id,)
    storage.payloads[first.storage_key] = stored_payload

    service.delete(first.id, user_id=1)
    with factory() as session:
        row = session.scalar(select(Artifact).where(Artifact.id == first.id))
        assert row is not None
        assert row.status == "deleted"
        assert row.deleted_at is not None
        assert row.last_error is None


def test_artifact_delete_failure_keeps_auditable_metadata(tmp_path):
    service, storage, factory = _artifact_service(tmp_path)
    artifact = service.upload(
        user_id=1,
        kind="import",
        original_name="input.csv",
        payload=b"header\nvalue\n",
        content_type="text/csv",
    )
    storage.fail_delete = True

    with pytest.raises(ArtifactError, match="safe delete failure"):
        service.delete(artifact.id, user_id=1)

    with factory() as session:
        row = session.get(Artifact, artifact.id)
        assert row is not None
        assert row.status == "delete_failed"
        assert row.last_error == "storage_delete_failed"
        assert row.deleted_at is None
