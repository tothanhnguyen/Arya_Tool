"""Private, owner-scoped artifact storage backed by Supabase Storage.

Artifact objects are content addressed. Metadata stays in the application
database so a service-role Storage client can still enforce ownership before
issuing a download or signed URL.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeVar
from urllib.parse import quote

import httpx
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from laplace.config import get_settings
from laplace.db import Base

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_SAFE_KIND_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/octet-stream",
        "application/zip",
        "text/csv",
        "text/markdown",
    }
)
_MAX_SIGNED_URL_TTL_SECONDS = 3600


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ArtifactError(RuntimeError):
    """An artifact operation failed without exposing credentials or payloads."""


class ArtifactConfigurationError(ArtifactError):
    """Supabase artifact storage is not configured."""


class ArtifactValidationError(ArtifactError):
    """Artifact metadata or content is invalid."""


class ArtifactOwnershipError(ArtifactError):
    """The caller does not own the requested artifact."""


class ArtifactPersistenceError(ArtifactError):
    """Artifact metadata could not be persisted or compensated safely."""


class Artifact(Base):
    """Durable metadata for one private artifact object."""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint(
            "storage_bucket",
            "storage_key",
            name="uq_artifact_storage_object",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_artifact_size"),
        CheckConstraint(
            "status IN ('active', 'deleting', 'delete_failed', 'deleted')",
            name="ck_artifact_status",
        ),
        Index("ix_artifacts_user_created", "user_id", "created_at"),
        Index("ix_artifacts_user_kind", "user_id", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    original_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_bucket: Mapped[str] = mapped_column(String(100))
    storage_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    user_id: int
    original_name: str
    content_type: str
    size_bytes: int
    sha256: str
    bucket: str
    key: str
    created: bool

    @property
    def location(self) -> str:
        return f"supabase://{self.bucket}/{self.key}"


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    id: int
    user_id: int
    kind: str
    original_name: str
    content_type: str
    size_bytes: int
    sha256: str
    storage_bucket: str
    storage_key: str
    status: str


@dataclass(frozen=True, slots=True)
class ArtifactReconciliation:
    user_id: int
    active_metadata_rows: int
    verified_objects: int
    unavailable_artifact_ids: tuple[int, ...]

    @property
    def healthy(self) -> bool:
        return (
            self.verified_objects == self.active_metadata_rows
            and not self.unavailable_artifact_ids
        )


class ArtifactStorage(Protocol):
    bucket: str

    def upload(
        self,
        *,
        user_id: int,
        original_name: str,
        payload: bytes,
        content_type: str,
    ) -> StoredArtifact: ...

    def download(self, stored: StoredArtifact, *, user_id: int) -> bytes: ...

    def create_signed_url(
        self,
        stored: StoredArtifact,
        *,
        user_id: int,
        expires_in: int,
    ) -> str: ...

    def delete(self, stored: StoredArtifact, *, user_id: int) -> None: ...


def safe_artifact_filename(original_name: str) -> str:
    """Return a deterministic ASCII filename without path components."""

    leaf = original_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    normalized = (
        unicodedata.normalize("NFKD", leaf).encode("ascii", "ignore").decode()
    )
    safe = _SAFE_FILENAME_RE.sub("-", normalized).strip(".-_").lower()
    if not safe:
        safe = "artifact.bin"
    return safe[:120]


def artifact_object_key(user_id: int, digest: str, original_name: str) -> str:
    if user_id < 1:
        raise ArtifactValidationError("artifact owner must be a positive integer")
    if not _SHA256_RE.fullmatch(digest):
        raise ArtifactValidationError("artifact SHA-256 is invalid")
    filename = safe_artifact_filename(original_name)
    return f"users/{user_id}/{digest[:2]}/{digest}-{filename}"


def _validate_kind(kind: str) -> str:
    normalized = kind.strip().lower()
    if not _SAFE_KIND_RE.fullmatch(normalized):
        raise ArtifactValidationError("artifact kind is invalid")
    return normalized


def _validate_content(
    *,
    payload: bytes,
    content_type: str,
    max_bytes: int,
) -> None:
    if not payload:
        raise ArtifactValidationError("artifact payload is empty")
    if len(payload) > max_bytes:
        raise ArtifactValidationError("artifact payload exceeds the size limit")
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ArtifactValidationError("artifact content type is not allowed")


class SupabaseArtifactStorage:
    """Minimal private Storage HTTP client with explicit owner checks."""

    def __init__(
        self,
        *,
        project_url: str,
        secret_key: str,
        bucket: str,
        max_bytes: int = 50 * 1024 * 1024,
        client: httpx.Client | None = None,
    ) -> None:
        if not project_url or not secret_key or not bucket:
            raise ArtifactConfigurationError(
                "Supabase artifact storage configuration is incomplete"
            )
        if max_bytes < 1:
            raise ArtifactConfigurationError("artifact size limit is invalid")
        self._project_url = project_url.rstrip("/")
        self._secret_key = secret_key
        self.bucket = bucket
        self.max_bytes = max_bytes
        self._client = client or httpx.Client(timeout=30.0)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._secret_key,
            "authorization": f"Bearer {self._secret_key}",
        }

    def _object_url(self, endpoint: str, key: str = "") -> str:
        encoded_bucket = quote(self.bucket, safe="")
        encoded_key = quote(key, safe="/")
        suffix = f"/{encoded_key}" if encoded_key else ""
        return (
            f"{self._project_url}/storage/v1/{endpoint}/"
            f"{encoded_bucket}{suffix}"
        )

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ArtifactError("Supabase artifact request failed") from exc

    def _validate_reference(
        self,
        stored: StoredArtifact,
        *,
        user_id: int,
    ) -> None:
        if stored.user_id != user_id:
            raise ArtifactOwnershipError("artifact is owned by another user")
        expected_key = artifact_object_key(
            stored.user_id,
            stored.sha256,
            stored.original_name,
        )
        if stored.bucket != self.bucket or stored.key != expected_key:
            raise ArtifactValidationError("artifact storage reference is invalid")

    def upload(
        self,
        *,
        user_id: int,
        original_name: str,
        payload: bytes,
        content_type: str,
    ) -> StoredArtifact:
        _validate_content(
            payload=payload,
            content_type=content_type,
            max_bytes=self.max_bytes,
        )
        digest = hashlib.sha256(payload).hexdigest()
        safe_name = safe_artifact_filename(original_name)
        key = artifact_object_key(user_id, digest, safe_name)
        stored = StoredArtifact(
            user_id=user_id,
            original_name=safe_name,
            content_type=content_type,
            size_bytes=len(payload),
            sha256=digest,
            bucket=self.bucket,
            key=key,
            created=True,
        )
        response = self._request(
            "POST",
            self._object_url("object", key),
            headers={
                **self._headers,
                "content-type": content_type,
                "x-upsert": "false",
            },
            content=payload,
        )
        if response.status_code not in {200, 201, 409}:
            raise ArtifactError(
                f"Supabase artifact upload failed with status {response.status_code}"
            )
        if response.status_code != 409:
            return stored

        existing_payload = self.download(stored, user_id=user_id)
        if hashlib.sha256(existing_payload).hexdigest() != digest:
            raise ArtifactError("Supabase artifact object conflict failed validation")
        return StoredArtifact(
            user_id=stored.user_id,
            original_name=stored.original_name,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            bucket=stored.bucket,
            key=stored.key,
            created=False,
        )

    def download(self, stored: StoredArtifact, *, user_id: int) -> bytes:
        self._validate_reference(stored, user_id=user_id)
        response = self._request(
            "GET",
            self._object_url("object/authenticated", stored.key),
            headers=self._headers,
        )
        if response.status_code != 200:
            raise ArtifactError(
                f"Supabase artifact download failed with status {response.status_code}"
            )
        payload = response.content
        if len(payload) != stored.size_bytes:
            raise ArtifactError("downloaded artifact size does not match metadata")
        if hashlib.sha256(payload).hexdigest() != stored.sha256:
            raise ArtifactError("downloaded artifact hash does not match metadata")
        return payload

    def create_signed_url(
        self,
        stored: StoredArtifact,
        *,
        user_id: int,
        expires_in: int,
    ) -> str:
        self._validate_reference(stored, user_id=user_id)
        if not 1 <= expires_in <= _MAX_SIGNED_URL_TTL_SECONDS:
            raise ArtifactValidationError("signed URL TTL must be between 1 and 3600")
        response = self._request(
            "POST",
            self._object_url("object/sign", stored.key),
            headers={**self._headers, "content-type": "application/json"},
            json={"expiresIn": expires_in},
        )
        if response.status_code != 200:
            raise ArtifactError(
                f"Supabase artifact signed URL failed with status {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ArtifactError(
                "Supabase artifact signed URL response is invalid"
            ) from exc
        value = body.get("signedURL") or body.get("signedUrl")
        if not isinstance(value, str) or not value:
            raise ArtifactError("Supabase artifact signed URL response is invalid")
        if value.startswith(("http://", "https://")):
            expected_prefix = f"{self._project_url}/storage/v1/"
            if not value.startswith(expected_prefix):
                raise ArtifactError(
                    "Supabase artifact signed URL response is invalid"
                )
            return value
        return f"{self._project_url}/storage/v1{value}"

    def delete(self, stored: StoredArtifact, *, user_id: int) -> None:
        self._validate_reference(stored, user_id=user_id)
        response = self._request(
            "DELETE",
            self._object_url("object"),
            headers={**self._headers, "content-type": "application/json"},
            json={"prefixes": [stored.key]},
        )
        if response.status_code not in {200, 204, 404}:
            raise ArtifactError(
                f"Supabase artifact delete failed with status {response.status_code}"
            )


_T = TypeVar("_T")


def upload_with_compensation(
    storage: ArtifactStorage,
    *,
    user_id: int,
    original_name: str,
    payload: bytes,
    content_type: str,
    persist: Callable[[StoredArtifact], _T],
) -> _T:
    """Persist metadata after upload and remove newly created orphan objects."""

    stored = storage.upload(
        user_id=user_id,
        original_name=original_name,
        payload=payload,
        content_type=content_type,
    )
    try:
        return persist(stored)
    except Exception as exc:
        if stored.created:
            try:
                storage.delete(stored, user_id=user_id)
            except ArtifactError as compensation_exc:
                raise ArtifactPersistenceError(
                    "artifact persistence and upload compensation both failed"
                ) from compensation_exc
        raise ArtifactPersistenceError("artifact metadata persistence failed") from exc


def _metadata_from_row(row: Artifact) -> ArtifactMetadata:
    return ArtifactMetadata(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        original_name=row.original_name,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        storage_bucket=row.storage_bucket,
        storage_key=row.storage_key,
        status=row.status,
    )


class ArtifactService:
    """Own metadata transactions and compensate cross-system partial failures."""

    def __init__(
        self,
        *,
        storage: ArtifactStorage,
        session_factory: Callable[[], Session],
        signed_url_ttl_seconds: int = 300,
    ) -> None:
        if not 1 <= signed_url_ttl_seconds <= _MAX_SIGNED_URL_TTL_SECONDS:
            raise ArtifactConfigurationError(
                "artifact signed URL TTL must be between 1 and 3600"
            )
        self.storage = storage
        self.session_factory = session_factory
        self.signed_url_ttl_seconds = signed_url_ttl_seconds

    def _find_by_key(
        self,
        session: Session,
        *,
        user_id: int,
        bucket: str,
        key: str,
    ) -> Artifact | None:
        return session.scalar(
            select(Artifact).where(
                Artifact.user_id == user_id,
                Artifact.storage_bucket == bucket,
                Artifact.storage_key == key,
            )
        )

    def upload(
        self,
        *,
        user_id: int,
        kind: str,
        original_name: str,
        payload: bytes,
        content_type: str,
    ) -> ArtifactMetadata:
        normalized_kind = _validate_kind(kind)
        safe_name = safe_artifact_filename(original_name)

        def persist(stored: StoredArtifact) -> ArtifactMetadata:
            with self.session_factory() as session:
                existing = self._find_by_key(
                    session,
                    user_id=user_id,
                    bucket=stored.bucket,
                    key=stored.key,
                )
                if existing is None:
                    existing = Artifact(
                        user_id=user_id,
                        kind=normalized_kind,
                        original_name=stored.original_name,
                        content_type=stored.content_type,
                        size_bytes=stored.size_bytes,
                        sha256=stored.sha256,
                        storage_bucket=stored.bucket,
                        storage_key=stored.key,
                    )
                    session.add(existing)
                else:
                    if (
                        existing.kind != normalized_kind
                        or existing.original_name != stored.original_name
                        or existing.content_type != stored.content_type
                        or existing.size_bytes != stored.size_bytes
                        or existing.sha256 != stored.sha256
                    ):
                        raise ArtifactValidationError(
                            "artifact metadata conflicts with an existing object"
                        )
                    existing.kind = normalized_kind
                    existing.content_type = stored.content_type
                    existing.size_bytes = stored.size_bytes
                    existing.sha256 = stored.sha256
                    existing.status = "active"
                    existing.last_error = None
                    existing.deleted_at = None
                try:
                    session.flush()
                    metadata = _metadata_from_row(existing)
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    concurrent = self._find_by_key(
                        session,
                        user_id=user_id,
                        bucket=stored.bucket,
                        key=stored.key,
                    )
                    if concurrent is None:
                        raise
                    return _metadata_from_row(concurrent)
                return metadata

        return upload_with_compensation(
            self.storage,
            user_id=user_id,
            original_name=safe_name,
            payload=payload,
            content_type=content_type,
            persist=persist,
        )

    def download(self, artifact_id: int, *, user_id: int) -> bytes:
        stored = self._active_stored_artifact(artifact_id, user_id=user_id)
        return self.storage.download(stored, user_id=user_id)

    def create_signed_url(
        self,
        artifact_id: int,
        *,
        user_id: int,
        expires_in: int | None = None,
    ) -> str:
        stored = self._active_stored_artifact(artifact_id, user_id=user_id)
        ttl = self.signed_url_ttl_seconds if expires_in is None else expires_in
        return self.storage.create_signed_url(
            stored,
            user_id=user_id,
            expires_in=ttl,
        )

    def _active_stored_artifact(
        self,
        artifact_id: int,
        *,
        user_id: int,
    ) -> StoredArtifact:
        with self.session_factory() as session:
            artifact = session.get(Artifact, artifact_id)
            if artifact is None or artifact.user_id != user_id:
                raise ArtifactOwnershipError("artifact is not available to this user")
            if artifact.status != "active":
                raise ArtifactError("artifact is not active")
            return StoredArtifact(
                user_id=artifact.user_id,
                original_name=artifact.original_name,
                content_type=artifact.content_type,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
                bucket=artifact.storage_bucket,
                key=artifact.storage_key,
                created=False,
            )

    def delete(self, artifact_id: int, *, user_id: int) -> None:
        stored = self._mark_deleting(artifact_id, user_id=user_id)
        try:
            self.storage.delete(stored, user_id=user_id)
        except ArtifactError:
            self._finish_delete(
                artifact_id,
                user_id=user_id,
                status="delete_failed",
                error_code="storage_delete_failed",
            )
            raise
        self._finish_delete(
            artifact_id,
            user_id=user_id,
            status="deleted",
            error_code=None,
        )

    def reconcile_owner(self, *, user_id: int) -> ArtifactReconciliation:
        """Verify active metadata points to objects with matching bytes."""

        with self.session_factory() as session:
            artifacts = list(
                session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.user_id == user_id,
                        Artifact.status == "active",
                    )
                    .order_by(Artifact.id)
                )
            )
            references = [
                (
                    artifact.id,
                    StoredArtifact(
                        user_id=artifact.user_id,
                        original_name=artifact.original_name,
                        content_type=artifact.content_type,
                        size_bytes=artifact.size_bytes,
                        sha256=artifact.sha256,
                        bucket=artifact.storage_bucket,
                        key=artifact.storage_key,
                        created=False,
                    ),
                )
                for artifact in artifacts
            ]

        unavailable: list[int] = []
        verified = 0
        for artifact_id, stored in references:
            try:
                self.storage.download(stored, user_id=user_id)
            except ArtifactError:
                unavailable.append(artifact_id)
            else:
                verified += 1
        return ArtifactReconciliation(
            user_id=user_id,
            active_metadata_rows=len(references),
            verified_objects=verified,
            unavailable_artifact_ids=tuple(unavailable),
        )

    def _mark_deleting(
        self,
        artifact_id: int,
        *,
        user_id: int,
    ) -> StoredArtifact:
        try:
            with self.session_factory() as session:
                artifact = session.get(Artifact, artifact_id)
                if artifact is None or artifact.user_id != user_id:
                    raise ArtifactOwnershipError(
                        "artifact is not available to this user"
                    )
                if artifact.status == "deleted":
                    raise ArtifactError("artifact is already deleted")
                artifact.status = "deleting"
                artifact.last_error = None
                stored = StoredArtifact(
                    user_id=artifact.user_id,
                    original_name=artifact.original_name,
                    content_type=artifact.content_type,
                    size_bytes=artifact.size_bytes,
                    sha256=artifact.sha256,
                    bucket=artifact.storage_bucket,
                    key=artifact.storage_key,
                    created=False,
                )
                session.commit()
                return stored
        except SQLAlchemyError as exc:
            raise ArtifactPersistenceError(
                "artifact deletion audit could not be started"
            ) from exc

    def _finish_delete(
        self,
        artifact_id: int,
        *,
        user_id: int,
        status: str,
        error_code: str | None,
    ) -> None:
        try:
            with self.session_factory() as session:
                artifact = session.get(Artifact, artifact_id)
                if artifact is None or artifact.user_id != user_id:
                    raise ArtifactOwnershipError(
                        "artifact is not available to this user"
                    )
                artifact.status = status
                artifact.last_error = error_code
                artifact.deleted_at = _utcnow() if status == "deleted" else None
                session.commit()
        except SQLAlchemyError as exc:
            raise ArtifactPersistenceError(
                "artifact deletion audit could not be finalized"
            ) from exc


def get_artifact_storage() -> SupabaseArtifactStorage:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise ArtifactConfigurationError(
            "Set Supabase server configuration before using artifact storage"
        )
    return SupabaseArtifactStorage(
        project_url=settings.supabase_url,
        secret_key=settings.supabase_secret_key,
        bucket=settings.supabase_artifact_bucket,
        max_bytes=settings.supabase_artifact_max_bytes,
    )


def get_artifact_service(
    *,
    session_factory: Callable[[], Session] | None = None,
) -> ArtifactService:
    settings = get_settings()
    if session_factory is None:
        from laplace.db import session_scope

        session_factory = session_scope
    return ArtifactService(
        storage=get_artifact_storage(),
        session_factory=session_factory,
        signed_url_ttl_seconds=settings.supabase_signed_url_ttl_s,
    )
