"""FastAPI dependencies dung chung cho API va trace viewer."""

from fastapi import Header, HTTPException

from laplace.config import get_settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Chan IDOR o muc MVP: khi LAPLACE_API_KEY duoc dat, moi request phai kem
    header X-API-Key trung khop. Khong dat key = mo (dev local)."""
    key = get_settings().api_key
    if key and x_api_key != key:
        raise HTTPException(status_code=401, detail="Thieu hoac sai X-API-Key")
