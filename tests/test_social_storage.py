"""Storage backend tests that never require real Supabase credentials."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from laplace.config import get_settings
from laplace.social.models import MediaAsset
from laplace.social.storage import (
    LocalMediaStorage,
    StorageConfigurationError,
    StorageError,
    StoredMedia,
    SupabaseMediaStorage,
    get_media_storage,
    materialize_media,
)


def test_local_storage_round_trip_is_content_addressed(tmp_path):
    payload = b"safe-image-payload"
    digest = hashlib.sha256(payload).hexdigest()
    storage = LocalMediaStorage(tmp_path)

    stored = storage.store(
        user_id=7,
        digest=digest,
        suffix=".png",
        payload=payload,
        content_type="image/png",
    )

    assert stored.backend == "local"
    assert Path(stored.location) == tmp_path / "7" / f"{digest}.png"
    assert storage.download(stored) == payload
    storage.delete(stored)
    assert not Path(stored.location).exists()


def test_supabase_storage_uses_private_object_api_without_leaking_key():
    secret = "server-secret-never-log"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/object/sign/" in request.url.path:
            return httpx.Response(200, json={"signedURL": "/object/sign/example"})
        return httpx.Response(201, json={"Key": "users/3/aa/example.png"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    storage = SupabaseMediaStorage(
        project_url="https://project.supabase.co",
        secret_key=secret,
        bucket="arya-media",
        client=client,
    )

    stored = storage.store(
        user_id=3,
        digest="a" * 64,
        suffix=".png",
        payload=b"payload",
        content_type="image/png",
    )
    signed_url = storage.create_signed_url(stored, 300)

    assert stored == StoredMedia(
        backend="supabase",
        location=f"supabase://arya-media/users/3/aa/{'a' * 64}.png",
        bucket="arya-media",
        key=f"users/3/aa/{'a' * 64}.png",
        created=True,
    )
    assert signed_url == (
        "https://project.supabase.co/storage/v1/object/sign/example"
    )
    assert seen[0].headers["authorization"] == f"Bearer {secret}"
    assert seen[0].headers["x-upsert"] == "false"


def test_supabase_error_does_not_include_response_or_secret():
    secret = "do-not-expose"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            text=f"upstream accidentally echoed {secret}",
        )

    storage = SupabaseMediaStorage(
        project_url="https://project.supabase.co",
        secret_key=secret,
        bucket="arya-media",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(StorageError) as caught:
        storage.store(
            user_id=1,
            digest="b" * 64,
            suffix=".jpg",
            payload=b"payload",
            content_type="image/jpeg",
        )

    assert "status 500" in str(caught.value)
    assert secret not in str(caught.value)
    assert "accidentally echoed" not in str(caught.value)


def test_supabase_backend_fails_closed_without_credentials(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "social_media_backend", "supabase")
    monkeypatch.setattr(settings, "supabase_url", None)
    monkeypatch.setattr(settings, "supabase_secret_key", None)

    with pytest.raises(StorageConfigurationError):
        get_media_storage()


def test_materialize_supabase_media_checks_hash_and_cleans_temp(
    monkeypatch,
):
    payload = b"downloaded-private-media"
    asset = MediaAsset(
        user_id=1,
        type="image",
        local_path="supabase://arya-media/users/1/test.png",
        storage_backend="supabase",
        storage_bucket="arya-media",
        storage_key="users/1/test.png",
        original_name="test.png",
        mime_type="image/png",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    class FakeStorage:
        def download(self, _stored):
            return payload

    monkeypatch.setattr(
        "laplace.social.storage.get_media_storage",
        lambda: FakeStorage(),
    )

    with materialize_media(asset) as path:
        temporary_path = path
        assert path.read_bytes() == payload

    assert not temporary_path.exists()

