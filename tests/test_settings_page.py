"""Test trang /settings (T16): render form 8 hang + mask key, bao mat loopback,
POST validate truoc khi ghi .env. KHONG goi mang — validate_key duoc mock.

Loopback duoc test qua tham so `client=` cua starlette TestClient: host cua
request.client chinh la gia tri nay, nen gia lap duoc ca may LAN (1.2.3.4)
lan localhost (127.0.0.1) ma khong can dependency override.
"""

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from laplace.llm import setup as llm_setup
from laplace.llm.presets import PRESETS
from laplace.web.app import create_app
from laplace.web.settings_page import (
    CSRF_TOKEN,
    SOCIAL_CONTENT_MODEL,
    SOCIAL_CONTENT_PROVIDER,
)

LOOPBACK = ("127.0.0.1", 50000)
LAN_HOST = ("1.2.3.4", 123)

FULL_KEY = "sk-secret-full-key-1234567890abcdef"


@pytest.fixture()
def env_path(tmp_path, monkeypatch):
    """Tro ENV_PATH cua wizard sang file tam — khong dung .env that cua repo.
    Tat luon seed tu .env.example de noi dung file ghi ra hoan toan xac dinh."""
    path = tmp_path / ".env"
    monkeypatch.setattr(llm_setup, "ENV_PATH", path)
    monkeypatch.setattr(llm_setup, "ENV_EXAMPLE_PATH", tmp_path / ".env.example")
    return path


@pytest.fixture()
def client(session, env_path):
    """TestClient tu loopback (mac dinh cua starlette la host 'testclient' —
    se bi 403, nen phai chi dinh ro 127.0.0.1)."""
    with TestClient(create_app(), client=LOOPBACK, base_url="http://127.0.0.1") as c:
        yield c


# ------------------------------------------------------------------ GET form


def test_get_form_renders_all_8_providers(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    html = resp.text
    assert len(PRESETS) == 8
    for preset in PRESETS.values():
        assert preset.name in html
        assert str(escape(preset.label)) in html  # Jinja autoescape (vd "->" -> "-&gt;")
        assert preset.default_model in html
        assert preset.key_url in html
    # Ollama khong can key — co nut chon lam provider, khong doi key
    assert "không cần key" in html
    assert "Chọn làm provider" in html
    # Dong nhac restart
    assert "restart" in html
    # Copywriter co config rieng, khong phu thuoc provider cua agent.
    assert f"LAPLACE_SOCIAL_CONTENT_PROVIDER={SOCIAL_CONTENT_PROVIDER}" in html
    assert f"LAPLACE_SOCIAL_CONTENT_MODEL={SOCIAL_CONTENT_MODEL}" in html
    assert "50 request/ngày" in html
    assert "chất lượng, độ trễ" in html


def test_get_form_masks_existing_key(client, env_path):
    env_path.write_text(f"LAPLACE_GROQ_API_KEY={FULL_KEY}\nLAPLACE_LLM_PROVIDER=groq\n")
    resp = client.get("/settings")
    assert resp.status_code == 200
    # Chi hien mask 6 ky tu dau + "..." — key day du KHONG bao gio xuat hien
    assert "sk-sec..." in resp.text
    assert FULL_KEY not in resp.text
    # groq dang la provider hien tai
    assert "đang chọn" in resp.text


# ------------------------------------------------------------ loopback only


def test_get_from_non_loopback_returns_403(session, env_path):
    with TestClient(create_app(), client=LAN_HOST, base_url="http://127.0.0.1") as c:
        resp = c.get("/settings")
    assert resp.status_code == 403


def test_post_from_non_loopback_returns_403_and_writes_nothing(session, env_path):
    with TestClient(create_app(), client=LAN_HOST, base_url="http://127.0.0.1") as c:
        resp = c.post(
            "/settings", data={"csrf": CSRF_TOKEN, "provider": "groq", "api_key": FULL_KEY}
        )
    assert resp.status_code == 403
    assert not env_path.exists()
    assert FULL_KEY not in resp.text


def test_default_testclient_host_is_blocked(session, env_path):
    """Host mac dinh 'testclient' khong phai loopback -> cung phai 403."""
    with TestClient(create_app()) as c:
        assert c.get("/settings").status_code == 403


def test_other_pages_still_open_for_lan(session, env_path):
    """Chi /settings bi chan loopback — trace viewer van mo cho LAN nhu cu."""
    with TestClient(create_app(), client=LAN_HOST, base_url="http://127.0.0.1") as c:
        assert c.get("/").status_code == 200


# ------------------------------------------------------------------ POST


def test_post_live_key_writes_env_600(client, env_path, monkeypatch):
    monkeypatch.setattr(llm_setup, "validate_key", lambda *a, **k: (123, "llama-3.3"))
    resp = client.post(
        "/settings",
        data={
            "csrf": CSRF_TOKEN,
            "provider": "groq",
            "api_key": FULL_KEY,
            "model": "llama-3.1-8b-instant",
        },
    )
    assert resp.status_code == 200
    # Ket qua validate hien tren trang
    assert "123 ms" in resp.text
    assert "llama-3.3" in resp.text
    # .env duoc ghi dung dong + chmod 600
    content = env_path.read_text()
    assert f"LAPLACE_GROQ_API_KEY={FULL_KEY}" in content
    assert "LAPLACE_LLM_PROVIDER=groq" in content
    assert "LAPLACE_LLM_MODEL=llama-3.1-8b-instant" in content
    assert (env_path.stat().st_mode & 0o777) == 0o600
    # Key day du KHONG xuat hien trong HTML tra ve (chi mask)
    assert FULL_KEY not in resp.text
    assert "sk-sec..." in resp.text


def test_post_content_writer_writes_separate_free_config(client, env_path, monkeypatch):
    env_path.write_text("LAPLACE_LLM_PROVIDER=groq\nLAPLACE_LLM_MODEL=llama-3.1-8b-instant\n")

    def _validate(preset, api_key, model):
        assert preset.name == SOCIAL_CONTENT_PROVIDER
        assert api_key == FULL_KEY
        assert model == SOCIAL_CONTENT_MODEL
        return 42, SOCIAL_CONTENT_MODEL

    monkeypatch.setattr(llm_setup, "validate_key", _validate)
    resp = client.post(
        "/settings",
        data={
            "csrf": CSRF_TOKEN,
            "target": "social_content",
            "api_key": FULL_KEY,
        },
    )

    assert resp.status_code == 200
    assert "Copywriter sẵn sàng" in resp.text
    assert FULL_KEY not in resp.text
    assert "sk-sec..." in resp.text
    content = env_path.read_text()
    assert f"LAPLACE_OPENROUTER_API_KEY={FULL_KEY}" in content
    assert f"LAPLACE_SOCIAL_CONTENT_PROVIDER={SOCIAL_CONTENT_PROVIDER}" in content
    assert f"LAPLACE_SOCIAL_CONTENT_MODEL={SOCIAL_CONTENT_MODEL}" in content
    # Cau hinh copywriter khong thay provider/model cua agent chinh.
    assert "LAPLACE_LLM_PROVIDER=groq" in content
    assert "LAPLACE_LLM_MODEL=llama-3.1-8b-instant" in content


def test_post_content_writer_reuses_masked_existing_key(client, env_path, monkeypatch):
    env_path.write_text(f"LAPLACE_OPENROUTER_API_KEY={FULL_KEY}\n")
    observed = {}

    def _validate(preset, api_key, model):
        observed.update(provider=preset.name, api_key=api_key, model=model)
        return 42, SOCIAL_CONTENT_MODEL

    monkeypatch.setattr(llm_setup, "validate_key", _validate)
    resp = client.post(
        "/settings",
        data={"csrf": CSRF_TOKEN, "target": "social_content"},
    )

    assert resp.status_code == 200
    assert observed == {
        "provider": SOCIAL_CONTENT_PROVIDER,
        "api_key": FULL_KEY,
        "model": SOCIAL_CONTENT_MODEL,
    }
    assert FULL_KEY not in resp.text


def test_content_writer_error_never_leaks_existing_key(client, env_path, monkeypatch):
    env_path.write_text(f"LAPLACE_OPENROUTER_API_KEY={FULL_KEY}\n")

    def _boom(*args, **kwargs):
        raise RuntimeError(f"provider rejected {FULL_KEY}")

    monkeypatch.setattr(llm_setup, "validate_key", _boom)
    resp = client.post(
        "/settings",
        data={"csrf": CSRF_TOKEN, "target": "social_content"},
    )

    assert resp.status_code == 400
    assert FULL_KEY not in resp.text
    assert "sk-sec..." in resp.text


def test_post_dead_key_writes_nothing(client, env_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("Error code: 401 - invalid api key")

    monkeypatch.setattr(llm_setup, "validate_key", _boom)
    resp = client.post("/settings", data={"csrf": CSRF_TOKEN, "provider": "gemini", "api_key": FULL_KEY})
    assert resp.status_code == 400
    assert "chua ghi gi vao .env" in resp.text
    assert not env_path.exists()
    assert FULL_KEY not in resp.text


def test_post_error_message_never_leaks_key(client, env_path, monkeypatch):
    """SDK nao do nhet nguyen key vao message loi -> trang van phai mask."""

    def _boom(*a, **k):
        raise RuntimeError(f"bad key: {FULL_KEY}")

    monkeypatch.setattr(llm_setup, "validate_key", _boom)
    resp = client.post("/settings", data={"csrf": CSRF_TOKEN, "provider": "xai", "api_key": FULL_KEY})
    assert resp.status_code == 400
    assert FULL_KEY not in resp.text
    assert not env_path.exists()


def test_post_missing_key_rejected_before_validate(client, env_path, monkeypatch):
    def _never(*a, **k):  # pragma: no cover - khong duoc goi
        raise AssertionError("validate_key khong duoc goi khi thieu key")

    monkeypatch.setattr(llm_setup, "validate_key", _never)
    resp = client.post("/settings", data={"csrf": CSRF_TOKEN, "provider": "openai", "api_key": "  "})
    assert resp.status_code == 400
    assert not env_path.exists()


def test_post_unknown_provider_rejected(client, env_path):
    resp = client.post("/settings", data={"csrf": CSRF_TOKEN, "provider": "hax0r", "api_key": "x"})
    assert resp.status_code == 400
    assert not env_path.exists()


def test_post_ollama_needs_no_key(client, env_path, monkeypatch):
    monkeypatch.setattr(llm_setup, "validate_key", lambda *a, **k: (5, "llama3.2"))
    resp = client.post("/settings", data={"csrf": CSRF_TOKEN, "provider": "ollama"})
    assert resp.status_code == 200
    content = env_path.read_text()
    assert "LAPLACE_LLM_PROVIDER=ollama" in content
    assert "API_KEY" not in content  # khong ghi dong key nao
    assert (env_path.stat().st_mode & 0o777) == 0o600


def test_post_preserves_other_env_lines(client, env_path, monkeypatch):
    """Ghi qua set_env_line: giu nguyen comment + cac dong khac, backup .env.bak."""
    env_path.write_text(
        "# comment giu nguyen\nLAPLACE_TELEGRAM_BOT_TOKEN=tok123\nLAPLACE_LLM_PROVIDER=mock\n"
    )
    monkeypatch.setattr(llm_setup, "validate_key", lambda *a, **k: (9, "deepseek-chat"))
    resp = client.post("/settings", data={"csrf": CSRF_TOKEN, "provider": "deepseek", "api_key": FULL_KEY})
    assert resp.status_code == 200
    content = env_path.read_text()
    assert "# comment giu nguyen" in content
    assert "LAPLACE_TELEGRAM_BOT_TOKEN=tok123" in content
    assert "LAPLACE_LLM_PROVIDER=deepseek" in content
    bak = env_path.with_name(".env.bak")
    assert bak.exists()
    assert (bak.stat().st_mode & 0o777) == 0o600


# ------------------------------------------------------------------ CSRF (3 lop)


def test_post_without_csrf_token_403(client, env_path):
    resp = client.post("/settings", data={"provider": "ollama"})
    assert resp.status_code == 403
    assert not env_path.exists()


def test_post_wrong_csrf_token_403(client, env_path):
    resp = client.post("/settings", data={"csrf": "sai-token", "provider": "ollama"})
    assert resp.status_code == 403
    assert not env_path.exists()


def test_post_cross_site_origin_403_even_with_token(client, env_path):
    """Form an tren trang web ngoai: IP loopback (browser chu may) + du token
    van phai bi chan vi Origin la domain la."""
    resp = client.post(
        "/settings",
        data={"csrf": CSRF_TOKEN, "provider": "ollama"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    assert not env_path.exists()


def test_post_cross_site_referer_403(client, env_path):
    resp = client.post(
        "/settings",
        data={"csrf": CSRF_TOKEN, "provider": "ollama"},
        headers={"Referer": "https://evil.example/attack.html"},
    )
    assert resp.status_code == 403


def test_non_loopback_host_header_403(session, env_path):
    """DNS rebinding: client loopback nhung Host la domain la -> chan."""
    with TestClient(
        create_app(), client=LOOPBACK, base_url="http://evil.example"
    ) as c:
        assert c.get("/settings").status_code == 403


def test_same_origin_post_with_loopback_origin_ok(client, env_path, monkeypatch):
    monkeypatch.setattr(llm_setup, "validate_key", lambda *a, **k: (5, "llama3.2"))
    resp = client.post(
        "/settings",
        data={"csrf": CSRF_TOKEN, "provider": "ollama"},
        headers={"Origin": "http://127.0.0.1:8010"},
    )
    assert resp.status_code == 200
