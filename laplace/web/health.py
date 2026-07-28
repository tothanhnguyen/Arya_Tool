"""Public liveness and dependency-aware readiness endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from laplace.ops.health import (
    ReadinessService,
    build_default_readiness_service,
    liveness_report,
)

router = APIRouter(tags=["operations"])


def get_readiness_service() -> ReadinessService:
    return build_default_readiness_service()


ReadinessDep = Annotated[ReadinessService, Depends(get_readiness_service)]


@router.get("/health/live", include_in_schema=False)
def live() -> dict[str, str]:
    return liveness_report()


@router.get("/health/ready", include_in_schema=False)
def ready(service: ReadinessDep) -> JSONResponse:
    report = service.check()
    return JSONResponse(
        status_code=200 if report.ready else 503,
        content=report.public_dict(),
    )
