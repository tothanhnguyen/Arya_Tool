"""Offline API tests for owner-scoped private artifacts."""

from __future__ import annotations

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import laplace.models  # noqa: F401
from laplace.db import Base
from laplace.models import User
from laplace.services.artifacts import (
    Artifact,
    ArtifactError,
    ArtifactOwnershipError,
    ArtifactService,
    StoredArtifact,
    artifact_object_key,
    safe_artifact_filename,
)
from laplace.web.artifacts import get_artifact_api_service, router
from laplace.web.deps import require_api_key

_NO_OWNER = object()


class FakeArtifactStorage:
    bucket = "arya-artifacts"
    max_bytes = 64

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_upload = False
        self.fail_download = False
        self.fail_signed_url = False
        self.fail_delete = False

    def upload(self, *, user_id, original_name, payload, content_type):
        if self.fail_upload:
            raise ArtifactError(
                "upstream response contained server-secret and private URL"
            )
        safe_name = safe_artifact_filename(original_name)
        digest = hashlib.sha256(payload).hexdigest()
        key = artifact_object_key(user_id, digest, safe_name)
        created = key not in self.objects
        self.objects[key] = payload
        return StoredArtifact(
            user_id=user_id,
            original_name=safe_name,
            content_type=content_type,
            size_bytes=len(payload),
            sha256=digest,
            bucket=self.bucket,
            key=key,
            created=created,
        )

    def download(self, stored, *, user_id):
        if stored.user_id != user_id:
            raise ArtifactOwnershipError("wrong owner")
        if self.fail_download:
            raise ArtifactError("upstream download leaked server-secret")
        try:
            return self.objects[stored.key]
        except KeyError as exc:
            raise ArtifactError("object is missing") from exc

    def create_signed_url(self, stored, *, user_id, expires_in):
        if stored.user_id != user_id:
            raise ArtifactOwnershipError("wrong owner")
        if self.fail_signed_url:
            raise ArtifactError(
                "upstream signed URL https://private.invalid/?token=server-secret"
            )
        return (
            f"https://signed.example.test/{stored.sha256}"
            f"?expires={expires_in}&token=fake"
        )

    def delete(self, stored, *, user_id):
        if stored.user_id != user_id:
            raise ArtifactOwnershipError("wrong owner")
        if self.fail_delete:
            raise ArtifactError("upstream delete leaked server-secret")
        self.objects.pop(stored.key, None)


@pytest.fixture()
def artifact_runtime(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'artifact-api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all([User(id=1), User(id=2)])
        session.commit()
    storage = FakeArtifactStorage()
    service = ArtifactService(
        storage=storage,
        session_factory=factory,
        signed_url_ttl_seconds=300,
    )
    yield service, storage, factory
    engine.dispose()


def _client(
    service: ArtifactService,
    owner_id: object = _NO_OWNER,
    *,
    bearer: bool = True,
) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_artifact_api_service] = lambda: service
    app.dependency_overrides[require_api_key] = lambda: None
    if owner_id is not _NO_OWNER:

        @app.middleware("http")
        async def _authenticated_owner(request, call_next):
            request.state.user_id = owner_id
            return await call_next(request)

    headers = {"Authorization": "Bearer offline-test"} if bearer else None
    return TestClient(app, headers=headers)


def _upload(client: TestClient, *, user_id: int = 999):
    return client.post(
        "/api/artifacts",
        data={"kind": "report", "user_id": str(user_id)},
        files={
            "file": (
                "report.json",
                b'{"ready":true}',
                "application/json",
            )
        },
    )


def test_artifact_api_vertical_flow_uses_context_owner(artifact_runtime):
    service, storage, factory = artifact_runtime
    with _client(service, owner_id=1) as client:
        uploaded = _upload(client, user_id=2)

        assert uploaded.status_code == 201
        public = uploaded.json()
        assert public["kind"] == "report"
        assert public["original_name"] == "report.json"
        assert {
            "user_id",
            "storage_bucket",
            "storage_key",
            "url",
        }.isdisjoint(public)

        artifact_id = public["id"]
        downloaded = client.get(f"/api/artifacts/{artifact_id}/download")
        assert downloaded.status_code == 200
        assert downloaded.content == b'{"ready":true}'
        assert downloaded.headers["content-type"].startswith("application/json")
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert 'filename="report.json"' in downloaded.headers["content-disposition"]

        signed = client.post(
            f"/api/artifacts/{artifact_id}/signed-url",
            json={"expires_in": 60},
        )
        assert signed.status_code == 200
        assert signed.json()["expires_in"] == 60
        assert signed.json()["url"].startswith("https://signed.example.test/")

        reconciliation = client.get("/api/artifacts/reconciliation")
        assert reconciliation.json() == {
            "healthy": True,
            "active_metadata_rows": 1,
            "verified_objects": 1,
            "unavailable_artifact_ids": [],
        }

        deleted = client.delete(f"/api/artifacts/{artifact_id}")
        assert deleted.status_code == 204
        assert client.get("/api/artifacts/reconciliation").json()[
            "active_metadata_rows"
        ] == 0

    with factory() as session:
        row = session.scalar(select(Artifact).where(Artifact.id == artifact_id))
        assert row is not None
        assert row.user_id == 1
        assert row.status == "deleted"
    assert not storage.objects


def test_artifact_api_hides_cross_owner_resources(artifact_runtime):
    service, storage, _factory = artifact_runtime
    with _client(service, owner_id=1) as owner_one:
        artifact_id = _upload(owner_one).json()["id"]

    with _client(service, owner_id=2) as owner_two:
        assert (
            owner_two.get(f"/api/artifacts/{artifact_id}/download").status_code
            == 404
        )
        assert (
            owner_two.post(
                f"/api/artifacts/{artifact_id}/signed-url",
                json={},
            ).status_code
            == 404
        )
        assert owner_two.delete(f"/api/artifacts/{artifact_id}").status_code == 404
        assert owner_two.get("/api/artifacts/reconciliation").json() == {
            "healthy": True,
            "active_metadata_rows": 0,
            "verified_objects": 0,
            "unavailable_artifact_ids": [],
        }
    assert len(storage.objects) == 1


@pytest.mark.parametrize("owner_id", [True, 0, -1, "1", None])
def test_artifact_api_rejects_invalid_owner_context(artifact_runtime, owner_id):
    service, _storage, _factory = artifact_runtime
    with _client(service, owner_id=owner_id) as client:
        response = client.get("/api/artifacts/reconciliation")

    assert response.status_code == 403


def test_artifact_api_requires_authenticated_owner_context(
    artifact_runtime,
    monkeypatch,
):
    from laplace.config import get_settings

    monkeypatch.setattr(get_settings(), "supabase_auth_enabled", True)
    service, _storage, _factory = artifact_runtime
    with _client(service) as client:
        response = client.get("/api/artifacts/reconciliation")

    assert response.status_code == 401


def test_artifact_api_uses_unambiguous_local_owner(
    session,
    artifact_runtime,
):
    service, _storage, _factory = artifact_runtime
    session.add(User(id=1))
    session.commit()

    with _client(service) as client:
        response = _upload(client)

    assert response.status_code == 201


def test_artifact_api_rejects_ambiguous_local_owner(
    session,
    artifact_runtime,
):
    service, _storage, _factory = artifact_runtime
    session.add_all([User(id=1), User(id=2)])
    session.commit()

    with _client(service) as client:
        response = client.get("/api/artifacts/reconciliation")

    assert response.status_code == 403


def test_artifact_mutations_reject_cookie_only_owner_context(artifact_runtime):
    service, _storage, _factory = artifact_runtime
    with _client(service, owner_id=1) as bearer_client:
        artifact_id = _upload(bearer_client).json()["id"]
    with _client(service, owner_id=1, bearer=False) as client:
        upload = _upload(client)
        signed_url = client.post(
            f"/api/artifacts/{artifact_id}/signed-url",
            json={},
        )
        deleted = client.delete(f"/api/artifacts/{artifact_id}")

    for response in (upload, signed_url, deleted):
        assert response.status_code == 403
        assert "Bearer token" in response.json()["detail"]


def test_artifact_api_validates_upload_and_signed_url_input(artifact_runtime):
    service, storage, _factory = artifact_runtime
    with _client(service, owner_id=1) as client:
        unsupported = client.post(
            "/api/artifacts",
            data={"kind": "report"},
            files={"file": ("page.html", b"<b>x</b>", "text/html")},
        )
        wrong_extension = client.post(
            "/api/artifacts",
            data={"kind": "report"},
            files={"file": ("report.txt", b"{}", "application/json")},
        )
        invalid_json = client.post(
            "/api/artifacts",
            data={"kind": "report"},
            files={"file": ("report.json", b"not-json", "application/json")},
        )
        path_filename = client.post(
            "/api/artifacts",
            data={"kind": "report"},
            files={"file": ("../report.json", b"{}", "application/json")},
        )
        too_large = client.post(
            "/api/artifacts",
            data={"kind": "report"},
            files={
                "file": (
                    "report.json",
                    b'{"value":"' + (b"x" * 80) + b'"}',
                    "application/json",
                )
            },
        )
        valid = _upload(client)
        artifact_id = valid.json()["id"]
        owner_override = client.post(
            f"/api/artifacts/{artifact_id}/signed-url",
            json={"user_id": 2},
        )
        invalid_ttl = client.post(
            f"/api/artifacts/{artifact_id}/signed-url",
            json={"expires_in": 3601},
        )

    assert unsupported.status_code == 415
    assert wrong_extension.status_code == 400
    assert invalid_json.status_code == 400
    assert path_filename.status_code == 400
    assert too_large.status_code == 413
    assert owner_override.status_code == 422
    assert invalid_ttl.status_code == 422
    assert len(storage.objects) == 1


def test_artifact_api_sanitizes_storage_errors(artifact_runtime):
    service, storage, _factory = artifact_runtime
    with _client(service, owner_id=1) as client:
        storage.fail_upload = True
        failed_upload = _upload(client)
        storage.fail_upload = False

        uploaded = _upload(client)
        artifact_id = uploaded.json()["id"]
        storage.fail_signed_url = True
        failed_signed_url = client.post(
            f"/api/artifacts/{artifact_id}/signed-url",
            json={},
        )
        storage.fail_signed_url = False
        storage.fail_delete = True
        failed_delete = client.delete(f"/api/artifacts/{artifact_id}")

    for response in (failed_upload, failed_signed_url, failed_delete):
        assert response.status_code == 502
        assert "server-secret" not in response.text
        assert "private.invalid" not in response.text
        assert "upstream" not in response.text
