"""Owner-scoped HTTP API for private artifact storage."""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from laplace.config import get_settings
from laplace.db import session_scope
from laplace.models import User
from laplace.services.artifacts import (
    ALLOWED_ARTIFACT_CONTENT_TYPES,
    ArtifactConfigurationError,
    ArtifactError,
    ArtifactMetadata,
    ArtifactOwnershipError,
    ArtifactPersistenceError,
    ArtifactService,
    ArtifactStateError,
    ArtifactValidationError,
    get_artifact_service,
    safe_artifact_filename,
    validate_artifact_upload,
)
from laplace.web.deps import require_api_key

router = APIRouter(
    prefix="/api/artifacts",
    tags=["artifacts"],
    dependencies=[Depends(require_api_key)],
)
_MISSING_OWNER = object()


def get_artifact_api_service() -> ArtifactService:
    """Dependency boundary for runtime configuration and offline API tests."""

    return get_artifact_service()


def require_artifact_owner(request: Request) -> int:
    owner_id = getattr(request.state, "user_id", _MISSING_OWNER)
    if owner_id is _MISSING_OWNER:
        if get_settings().supabase_auth_enabled:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authenticated owner context is required.",
            )
        with session_scope() as session:
            local_owner_ids = tuple(
                session.scalars(select(User.id).order_by(User.id).limit(2))
            )
        if not local_owner_ids:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A local owner must exist before using artifacts.",
            )
        if len(local_owner_ids) != 1:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Local artifact owner is ambiguous; enable user Auth.",
            )
        owner_id = local_owner_ids[0]
        request.state.user_id = owner_id
    if type(owner_id) is not int or owner_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authenticated owner context is invalid.",
        )
    return owner_id


def require_artifact_mutation_bearer(request: Request) -> None:
    """Reject ambient-cookie Auth for mutation endpoints."""

    owner_id = getattr(request.state, "user_id", _MISSING_OWNER)
    if owner_id is _MISSING_OWNER or request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    scheme, separator, credentials = request.headers.get(
        "authorization",
        "",
    ).partition(" ")
    if not separator or scheme.casefold() != "bearer" or not credentials.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authenticated API mutations require a Bearer token.",
        )


ArtifactServiceDep = Annotated[
    ArtifactService,
    Depends(get_artifact_api_service),
]
ArtifactOwnerDep = Annotated[int, Depends(require_artifact_owner)]


class SignedUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_in: int | None = Field(default=None, ge=1, le=3600)


def _public_metadata(metadata: ArtifactMetadata) -> dict[str, object]:
    return {
        "id": metadata.id,
        "kind": metadata.kind,
        "original_name": metadata.original_name,
        "content_type": metadata.content_type,
        "size_bytes": metadata.size_bytes,
        "sha256": metadata.sha256,
        "status": metadata.status,
    }


def _raise_safe_artifact_error(exc: Exception) -> NoReturn:
    if isinstance(exc, ArtifactOwnershipError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact was not found.",
        ) from exc
    if isinstance(exc, ArtifactValidationError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Artifact input is invalid.",
        ) from exc
    if isinstance(exc, ArtifactStateError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Artifact is not available in its current state.",
        ) from exc
    if isinstance(exc, ArtifactConfigurationError | ArtifactPersistenceError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Artifact service is unavailable.",
        ) from exc
    if isinstance(exc, ArtifactError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Artifact storage operation failed.",
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Artifact service is unavailable.",
    ) from exc


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_artifact(
    _mutation_auth: Annotated[None, Depends(require_artifact_mutation_bearer)],
    kind: Annotated[str, Form(min_length=1, max_length=32)],
    file: Annotated[UploadFile, File()],
    owner_id: ArtifactOwnerDep,
    service: ArtifactServiceDep,
) -> dict[str, object]:
    content_type = (file.content_type or "").strip().lower()
    original_name = file.filename or ""
    try:
        if content_type not in ALLOWED_ARTIFACT_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Artifact content type is not supported.",
            )
        max_bytes = service.max_upload_bytes
        payload = await file.read(max_bytes + 1)
    finally:
        await file.close()

    if len(payload) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Artifact exceeds the upload size limit.",
        )
    try:
        validate_artifact_upload(
            original_name=original_name,
            payload=payload,
            content_type=content_type,
            max_bytes=max_bytes,
            inspect_content=True,
        )
    except ArtifactValidationError as exc:
        _raise_safe_artifact_error(exc)

    try:
        metadata = await run_in_threadpool(
            service.upload,
            user_id=owner_id,
            kind=kind,
            original_name=original_name,
            payload=payload,
            content_type=content_type,
        )
    except Exception as exc:
        _raise_safe_artifact_error(exc)
    return _public_metadata(metadata)


@router.get("/{artifact_id}/download")
async def download_artifact(
    artifact_id: int,
    owner_id: ArtifactOwnerDep,
    service: ArtifactServiceDep,
) -> Response:
    try:
        metadata = await run_in_threadpool(
            service.get_metadata,
            artifact_id,
            user_id=owner_id,
        )
        payload = await run_in_threadpool(
            service.download,
            artifact_id,
            user_id=owner_id,
        )
    except Exception as exc:
        _raise_safe_artifact_error(exc)
    download_name = safe_artifact_filename(metadata.original_name)
    media_type = (
        metadata.content_type
        if metadata.content_type in ALLOWED_ARTIFACT_CONTENT_TYPES
        else "application/octet-stream"
    )
    return Response(
        content=payload,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{download_name}"'
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{artifact_id}/signed-url")
async def create_artifact_signed_url(
    _mutation_auth: Annotated[None, Depends(require_artifact_mutation_bearer)],
    artifact_id: int,
    body: SignedUrlRequest,
    owner_id: ArtifactOwnerDep,
    service: ArtifactServiceDep,
) -> dict[str, object]:
    expires_in = body.expires_in or service.signed_url_ttl_seconds
    try:
        url = await run_in_threadpool(
            service.create_signed_url,
            artifact_id,
            user_id=owner_id,
            expires_in=expires_in,
        )
    except Exception as exc:
        _raise_safe_artifact_error(exc)
    return {"url": url, "expires_in": expires_in}


@router.delete("/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_artifact(
    _mutation_auth: Annotated[None, Depends(require_artifact_mutation_bearer)],
    artifact_id: int,
    owner_id: ArtifactOwnerDep,
    service: ArtifactServiceDep,
) -> Response:
    try:
        await run_in_threadpool(
            service.delete,
            artifact_id,
            user_id=owner_id,
        )
    except Exception as exc:
        _raise_safe_artifact_error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/reconciliation")
async def reconcile_artifacts(
    owner_id: ArtifactOwnerDep,
    service: ArtifactServiceDep,
) -> dict[str, object]:
    try:
        report = await run_in_threadpool(
            service.reconcile_owner,
            user_id=owner_id,
        )
    except Exception as exc:
        _raise_safe_artifact_error(exc)
    return {
        "healthy": report.healthy,
        "active_metadata_rows": report.active_metadata_rows,
        "verified_objects": report.verified_objects,
        "unavailable_artifact_ids": list(report.unavailable_artifact_ids),
    }
