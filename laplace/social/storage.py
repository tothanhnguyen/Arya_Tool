"""Media storage backends for local files and private Supabase Storage.

The Supabase implementation talks to the official Storage HTTP API through the
existing ``httpx`` dependency. This keeps Arya_Tool compatible with Python 3.14
without requiring the optional Supabase SDK/cryptography dependency tree.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from laplace.config import get_settings
from laplace.social.models import MediaAsset


class StorageError(RuntimeError):
    """A storage operation failed without exposing credentials."""


class StorageConfigurationError(StorageError):
    """The selected storage backend is missing required local configuration."""


@dataclass(frozen=True, slots=True)
class StoredMedia:
    backend: str
    location: str
    bucket: str | None = None
    key: str | None = None
    created: bool = True


class MediaStorage(Protocol):
    def store(
        self,
        *,
        user_id: int,
        digest: str,
        suffix: str,
        payload: bytes,
        content_type: str,
    ) -> StoredMedia: ...

    def delete(self, stored: StoredMedia) -> None: ...

    def download(self, stored: StoredMedia) -> bytes: ...


class LocalMediaStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def store(
        self,
        *,
        user_id: int,
        digest: str,
        suffix: str,
        payload: bytes,
        content_type: str,
    ) -> StoredMedia:
        del content_type
        destination = self.root / str(user_id) / f"{digest}{suffix}"
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{digest[:12]}-",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, destination)
        finally:
            if fd >= 0:
                os.close(fd)
            temporary_path.unlink(missing_ok=True)
        return StoredMedia(backend="local", location=str(destination))

    def delete(self, stored: StoredMedia) -> None:
        if stored.backend != "local":
            raise StorageError("local storage cannot delete a non-local object")
        Path(stored.location).unlink(missing_ok=True)

    def download(self, stored: StoredMedia) -> bytes:
        if stored.backend != "local":
            raise StorageError("local storage cannot read a non-local object")
        try:
            return Path(stored.location).read_bytes()
        except OSError as exc:
            raise StorageError("local media object is unavailable") from exc


class SupabaseMediaStorage:
    def __init__(
        self,
        *,
        project_url: str,
        secret_key: str,
        bucket: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not project_url or not secret_key or not bucket:
            raise StorageConfigurationError("Supabase Storage configuration is incomplete")
        self.project_url = project_url.rstrip("/")
        self.secret_key = secret_key
        self.bucket = bucket
        self.client = client or httpx.Client(timeout=30.0)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.secret_key,
            "authorization": f"Bearer {self.secret_key}",
        }

    def _object_url(self, endpoint: str, key: str = "") -> str:
        bucket = quote(self.bucket, safe="")
        encoded_key = quote(key, safe="/")
        suffix = f"/{encoded_key}" if encoded_key else ""
        return f"{self.project_url}/storage/v1/{endpoint}/{bucket}{suffix}"

    def store(
        self,
        *,
        user_id: int,
        digest: str,
        suffix: str,
        payload: bytes,
        content_type: str,
    ) -> StoredMedia:
        key = f"users/{user_id}/{digest[:2]}/{digest}{suffix}"
        response = self.client.post(
            self._object_url("object", key),
            headers={
                **self._headers,
                "content-type": content_type,
                "x-upsert": "false",
            },
            content=payload,
        )
        if response.status_code not in {200, 201, 409}:
            raise StorageError(
                f"Supabase media upload failed with status {response.status_code}"
            )
        return StoredMedia(
            backend="supabase",
            location=f"supabase://{self.bucket}/{key}",
            bucket=self.bucket,
            key=key,
            created=response.status_code != 409,
        )

    def delete(self, stored: StoredMedia) -> None:
        bucket, key = self._validate_reference(stored)
        response = self.client.request(
            "DELETE",
            f"{self.project_url}/storage/v1/object/{quote(bucket, safe='')}",
            headers={**self._headers, "content-type": "application/json"},
            json={"prefixes": [key]},
        )
        if response.status_code not in {200, 204, 404}:
            raise StorageError(
                f"Supabase media delete failed with status {response.status_code}"
            )

    def download(self, stored: StoredMedia) -> bytes:
        bucket, key = self._validate_reference(stored)
        response = self.client.get(
            (
                f"{self.project_url}/storage/v1/object/authenticated/"
                f"{quote(bucket, safe='')}/{quote(key, safe='/')}"
            ),
            headers=self._headers,
        )
        if response.status_code != 200:
            raise StorageError(
                f"Supabase media download failed with status {response.status_code}"
            )
        return response.content

    def create_signed_url(self, stored: StoredMedia, expires_in: int) -> str:
        bucket, key = self._validate_reference(stored)
        response = self.client.post(
            (
                f"{self.project_url}/storage/v1/object/sign/"
                f"{quote(bucket, safe='')}/{quote(key, safe='/')}"
            ),
            headers={**self._headers, "content-type": "application/json"},
            json={"expiresIn": expires_in},
        )
        if response.status_code != 200:
            raise StorageError(
                f"Supabase signed URL failed with status {response.status_code}"
            )
        value = response.json().get("signedURL") or response.json().get("signedUrl")
        if not isinstance(value, str) or not value:
            raise StorageError("Supabase signed URL response is invalid")
        if value.startswith(("http://", "https://")):
            return value
        return f"{self.project_url}/storage/v1{value}"

    @staticmethod
    def _validate_reference(stored: StoredMedia) -> tuple[str, str]:
        if stored.backend != "supabase" or not stored.bucket or not stored.key:
            raise StorageError("Supabase storage reference is incomplete")
        return stored.bucket, stored.key


def get_media_storage() -> MediaStorage:
    settings = get_settings()
    if settings.social_media_backend == "local":
        return LocalMediaStorage(settings.social_media_dir)
    if settings.social_media_backend == "supabase":
        if not settings.supabase_url or not settings.supabase_secret_key:
            raise StorageConfigurationError(
                "Set LAPLACE_SUPABASE_URL and LAPLACE_SUPABASE_SECRET_KEY "
                "before enabling Supabase media storage"
            )
        return SupabaseMediaStorage(
            project_url=settings.supabase_url,
            secret_key=settings.supabase_secret_key,
            bucket=settings.supabase_media_bucket,
        )
    raise StorageConfigurationError(
        f"Unsupported social media backend: {settings.social_media_backend}"
    )


def stored_media_from_asset(asset: MediaAsset) -> StoredMedia:
    return StoredMedia(
        backend=asset.storage_backend,
        location=asset.local_path,
        bucket=asset.storage_bucket,
        key=asset.storage_key,
    )


@contextmanager
def materialize_media(asset: MediaAsset) -> Iterator[Path]:
    """Yield a local path for a local or private Supabase media asset."""

    stored = stored_media_from_asset(asset)
    if stored.backend == "local":
        yield Path(stored.location)
        return

    storage = get_media_storage()
    payload = storage.download(stored)
    if len(payload) != asset.size_bytes:
        raise StorageError("downloaded media size does not match database metadata")
    if hashlib.sha256(payload).hexdigest() != asset.sha256:
        raise StorageError("downloaded media hash does not match database metadata")

    suffix = Path(asset.original_name).suffix
    fd, temporary_name = tempfile.mkstemp(prefix="arya-media-", suffix=suffix)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
        yield temporary_path
    finally:
        if fd >= 0:
            os.close(fd)
        temporary_path.unlink(missing_ok=True)
