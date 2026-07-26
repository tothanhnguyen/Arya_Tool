"""Trang Settings: set API key hang AI ngay tren trace viewer (T16).

Dung lai toan bo logic cua wizard CLI (laplace/llm/setup.py): validate key
bang 1 request that truoc, song moi ghi .env qua write_env (backup + chmod 600).
Ghi xong chi co tac dung sau khi RESTART process — trang co dong nhac ro.

Bao mat:
- CHI chap nhan request tu loopback (127.0.0.1 / ::1) — server bind 0.0.0.0
  nen may khac trong LAN vao duoc cac trang khac, rieng /settings tra 403.
- Key di qua POST body (form), KHONG bao gio nam trong URL/query param.
- Key KHONG bao gio duoc render/log day du — moi cho hien thi deu qua
  mask_key(); thong bao loi cung duoc loc de khong lot key.
"""

import secrets
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from laplace.llm import setup as llm_setup
from laplace.llm.presets import PRESETS, mask_key
from laplace.web.deps import require_api_key
from laplace.web.traceview import templates

# Chi cho phep truy cap tu chinh may dang chay server.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


# Token per-process: form phai gui lai dung token nay (chong CSRF, lop 3).
# Sinh moi lan khoi dong — trang web ngoai khong doc duoc (SOP), form that thi co.
CSRF_TOKEN = secrets.token_urlsafe(32)


def _hostname_of(value: str | None) -> str | None:
    """Lay hostname (bo port) tu header Host/Origin/Referer; None neu khong parse duoc."""
    if not value:
        return None
    try:
        # Host header khong co scheme -> them // de urlsplit hieu la netloc
        parsed = urlsplit(value if "//" in value else f"//{value}")
        return parsed.hostname
    except ValueError:
        return None


def require_loopback(request: Request) -> None:
    """403 moi request khong den tu loopback — ke ca khi server bind 0.0.0.0.

    Chong ca CSRF/DNS-rebinding (lop 1+2):
    - Host header phai la loopback: trang web ngoai dung DNS rebinding se mang
      Host la domain cua no -> chan.
    - Origin/Referer (neu trinh duyet gui — form POST cross-site luon gui Origin)
      cung phai la loopback: form an tren trang web la tu POST vao day se bi chan
      du request xuat phat tu trinh duyet cua chinh chu may (IP loopback).
    """
    client = request.client
    if client is None or client.host not in LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="Trang Settings chi truy cap duoc tu chinh may chay server (loopback).",
        )
    if _hostname_of(request.headers.get("host")) not in LOOPBACK_HOSTS:
        raise HTTPException(status_code=403, detail="Host header khong phai loopback.")
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value and _hostname_of(value) not in LOOPBACK_HOSTS:
            raise HTTPException(
                status_code=403,
                detail=f"{header.capitalize()} khong phai loopback — tu choi (chong CSRF).",
            )


router = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_api_key), Depends(require_loopback)],
)


def _scrub(text: str, secret: str | None) -> str:
    """Phong thu: neu message loi (tu SDK) lo dinh key thi thay bang dang mask."""
    if secret and secret in text:
        text = text.replace(secret, mask_key(secret))
    return text


def _page_context(
    *,
    error: str | None = None,
    success: dict | None = None,
    selected: str | None = None,
) -> dict:
    """Ngu canh render: trang thai .env hien tai + danh sach 8 preset.

    Doc truc tiep tu .env (qua llm_setup) thay vi Settings da cache — de sau
    khi POST ghi xong, reload trang thay ngay gia tri moi (du process chua
    restart thi config dang chay van la gia tri cu).
    """
    lines = llm_setup.read_env_lines(llm_setup.ENV_PATH)
    current_provider = llm_setup.get_env_value(lines, "LAPLACE_LLM_PROVIDER") or "mock"
    current_model = llm_setup.get_env_value(lines, "LAPLACE_LLM_MODEL")
    providers = []
    for p in PRESETS.values():
        raw = llm_setup.get_env_value(lines, p.env_key) if p.env_key else None
        providers.append(
            {
                "name": p.name,
                "label": p.label,
                "default_model": p.default_model,
                "key_url": p.key_url,
                "free_tier": p.free_tier,
                "requires_key": p.requires_key,
                "has_key": bool(raw),
                "masked_key": mask_key(raw) if raw else None,
                "is_current": p.name == current_provider,
            }
        )
    return {
        "providers": providers,
        "current_provider": current_provider,
        "current_model": current_model,
        "selected": selected or current_provider,
        "error": error,
        "success": success,
        "active_nav": "settings",
        "csrf_token": CSRF_TOKEN,
    }


@router.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request):
    return templates.TemplateResponse(request, "settings.html", _page_context())


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    """Nhan provider + key tu POST body (khong bao gio qua query param):
    validate bang 1 request that -> song moi ghi .env, chet thi khong ghi gi.

    Form urlencoded duoc parse bang urllib.parse.parse_qs tren body (khong dung
    fastapi.Form/request.form() de khoi keo them dependency python-multipart —
    form nay khong upload file).
    """
    body = (await request.body()).decode("utf-8", errors="replace")
    form = {k: v[0] for k, v in parse_qs(body).items()}
    # Lop 3 chong CSRF: form phai mang dung token per-process (trang ngoai
    # khong doc duoc token nay do same-origin policy).
    if not secrets.compare_digest(form.get("csrf", ""), CSRF_TOKEN):
        raise HTTPException(status_code=403, detail="CSRF token sai hoac thieu.")
    provider = form.get("provider", "")
    api_key = form.get("api_key", "").strip()
    model = form.get("model", "").strip()
    preset = PRESETS.get(provider)
    if preset is None:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _page_context(error=f"Provider '{provider}' khong ton tai."),
            status_code=400,
        )
    if preset.requires_key and not api_key:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _page_context(
                error=f"{preset.label} can API key — dan key vao o ben duoi.",
                selected=preset.name,
            ),
            status_code=400,
        )

    try:
        # validate_key la call mang dong bo -> day sang threadpool cho khoi
        # chan event loop cua uvicorn
        latency_ms, live_model = await run_in_threadpool(
            llm_setup.validate_key, preset, api_key or None, model or None
        )
    except Exception as e:  # key sai / het quota / khong co mang...
        msg = _scrub(f"{type(e).__name__}: {e}", api_key)
        return templates.TemplateResponse(
            request,
            "settings.html",
            _page_context(
                error=(
                    f"Key KHONG dung duoc — chua ghi gi vao .env. ({msg}) "
                    f"Kiem tra lai key tai: {preset.key_url}"
                ),
                selected=preset.name,
            ),
            status_code=400,
        )

    # Key song -> ghi .env qua dung logic cua wizard CLI (backup + chmod 600)
    lines = llm_setup.read_env_lines(llm_setup.ENV_PATH)
    if preset.env_key:
        lines = llm_setup.set_env_line(lines, preset.env_key, api_key)
    lines = llm_setup.set_env_line(lines, "LAPLACE_LLM_PROVIDER", preset.name)
    if model:
        lines = llm_setup.set_env_line(lines, "LAPLACE_LLM_MODEL", model)
    llm_setup.write_env(llm_setup.ENV_PATH, lines, backup=True)

    return templates.TemplateResponse(
        request,
        "settings.html",
        _page_context(
            success={
                "provider": preset.name,
                "label": preset.label,
                "latency_ms": latency_ms,
                "model": live_model,
                "masked_key": mask_key(api_key) if preset.requires_key else None,
                "override_model": model or None,
            },
            selected=preset.name,
        ),
    )
