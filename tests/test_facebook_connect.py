"""Security and behavior tests for the local Facebook connection flow."""

from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from laplace.config import get_settings
from laplace.models import User
from laplace.social.browser_profile import BrowserProfileInfo
from laplace.social.models import SocialAccount
from laplace.web.settings_page import CSRF_TOKEN
from laplace.web.social.facebook_connect import (
    api_router,
    get_browser_profile_manager,
    router,
)

LOOPBACK = ("127.0.0.1", 50000)
LAN = ("192.0.2.20", 50000)
BASE_URL = "http://127.0.0.1"


class FakeProfileManager:
    def __init__(self) -> None:
        self.info = BrowserProfileInfo(
            account_id="1",
            user_id="101",
            profile_dir="/private/secret-profile-path",
            login_url="https://www.facebook.com/",
            status="missing",
        )
        self.calls: list[tuple[str, str, str]] = []

    def _owned(self, account_id: str, user_id: str) -> BrowserProfileInfo:
        if self.info.status != "missing" and user_id != self.info.user_id:
            raise PermissionError("owner mismatch with private path")
        return replace(self.info, account_id=account_id, user_id=user_id)

    def prepare(self, account_id: str, user_id: str) -> BrowserProfileInfo:
        self.calls.append(("prepare", account_id, user_id))
        self.info = replace(
            self._owned(account_id, user_id),
            account_id=account_id,
            user_id=user_id,
            status="prepared",
        )
        return self.info

    def launch_login(
        self,
        account_id: str,
        user_id: str,
        launcher=None,
    ) -> BrowserProfileInfo:
        del launcher
        self.calls.append(("launch", account_id, user_id))
        info = self._owned(account_id, user_id)
        if info.status == "missing":
            raise FileNotFoundError("profile not prepared")
        self.info = replace(info, status="login_pending")
        return self.info

    def mark_ready(self, account_id: str, user_id: str) -> BrowserProfileInfo:
        self.calls.append(("ready", account_id, user_id))
        info = self._owned(account_id, user_id)
        if info.status != "login_pending":
            raise RuntimeError("login not pending")
        self.info = replace(info, status="ready")
        return self.info

    def status(self, account_id: str, user_id: str) -> BrowserProfileInfo:
        self.calls.append(("status", account_id, user_id))
        return self._owned(account_id, user_id)


@pytest.fixture()
def fake_manager() -> FakeProfileManager:
    return FakeProfileManager()


@pytest.fixture()
def app(fake_manager: FakeProfileManager, session) -> FastAPI:
    session.add(User(id=101))
    session.commit()
    instance = FastAPI()
    instance.include_router(router)
    instance.include_router(api_router)
    instance.dependency_overrides[get_browser_profile_manager] = lambda: fake_manager
    return instance


@pytest.fixture()
def client(app: FastAPI):
    with TestClient(app, client=LOOPBACK, base_url=BASE_URL) as test_client:
        yield test_client


def test_connect_page_has_no_credential_inputs(client):
    response = client.get("/social/facebook/connect")

    assert response.status_code == 200
    assert "Kết nối Facebook" in response.text
    assert 'name="user_id"' in response.text
    assert 'name="display_name"' in response.text
    assert 'name="external_id"' in response.text
    assert 'name="cookie"' not in response.text.lower()
    assert 'name="token"' not in response.text.lower()
    assert "không đăng bài tự động" in response.text
    assert '<input type="hidden" name="user_id" value="1">' in response.text


def test_local_form_bootstraps_internal_owner(client, fake_manager, session):
    response = client.post(
        "/social/facebook/connect/prepare",
        data={
            "csrf": CSRF_TOKEN,
            "user_id": "1",
            "display_name": "Local Facebook",
            "external_id": "local-facebook-public-id",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert session.get(User, 1) is not None
    account = session.scalar(
        select(SocialAccount).where(
            SocialAccount.platform == "facebook",
            SocialAccount.external_id == "local-facebook-public-id",
        )
    )
    assert account is not None
    assert account.user_id == 1
    assert fake_manager.calls == [("prepare", str(account.id), "1")]


def test_web_flow_prepares_launches_and_marks_profile_ready(
    client,
    fake_manager,
    session,
):
    prepared = client.post(
        "/social/facebook/connect/prepare",
        data={
            "csrf": CSRF_TOKEN,
            "user_id": "101",
            "display_name": "Shop VN",
            "external_id": "facebook-public-101",
        },
        follow_redirects=False,
    )
    assert prepared.status_code == 303
    assert prepared.headers["location"].endswith("user_id=101&social_account_id=1")

    launched = client.post(
        "/social/facebook/connect/1/launch",
        data={"csrf": CSRF_TOKEN, "user_id": "101"},
        follow_redirects=False,
    )
    assert launched.status_code == 303

    ready = client.post(
        "/social/facebook/connect/1/ready",
        data={"csrf": CSRF_TOKEN, "user_id": "101"},
        follow_redirects=False,
    )
    assert ready.status_code == 303
    assert fake_manager.info.status == "ready"
    assert fake_manager.calls == [
        ("prepare", "1", "101"),
        ("launch", "1", "101"),
        ("status", "1", "101"),
        ("ready", "1", "101"),
    ]
    session.expire_all()
    account = session.get(SocialAccount, 1)
    assert account is not None
    assert account.status == "active"
    assert account.auth_type == "browser_profile"
    assert account.auth_ref == "browser-profile://101/1"
    assert account.last_checked_at is not None


def test_web_mutations_require_valid_csrf(client, fake_manager):
    response = client.post(
        "/social/facebook/connect/prepare",
        data={
            "csrf": "wrong",
            "user_id": "101",
            "display_name": "Shop VN",
            "external_id": "facebook-public-101",
        },
    )

    assert response.status_code == 403
    assert fake_manager.calls == []


def test_router_requires_api_key_for_pages_and_api(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_key", "dashboard-secret")

    assert client.get("/social/facebook/connect").status_code == 401
    assert (
        client.get(
            "/social/facebook/connect",
            headers={"X-API-Key": "dashboard-secret"},
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/social/facebook/connect/1/status?user_id=101",
        ).status_code
        == 401
    )


def test_router_rejects_non_loopback_requests(app):
    with TestClient(app, client=LAN, base_url=BASE_URL) as remote:
        page = remote.get("/social/facebook/connect")
        api = remote.get("/api/social/facebook/connect/1/status?user_id=101")

    assert page.status_code == 403
    assert api.status_code == 403


def test_json_api_requires_csrf_and_returns_only_public_metadata(client, fake_manager):
    missing_csrf = client.post(
        "/api/social/facebook/connect/prepare",
        json={
            "user_id": 101,
            "display_name": "Shop VN",
            "external_id": "facebook-public-101",
        },
    )
    assert missing_csrf.status_code == 403
    assert fake_manager.calls == []

    prepared = client.post(
        "/api/social/facebook/connect/prepare",
        json={
            "user_id": 101,
            "display_name": "Shop VN",
            "external_id": "facebook-public-101",
        },
        headers={"X-CSRF-Token": CSRF_TOKEN},
    )

    assert prepared.status_code == 201
    assert prepared.json() == {
        "account_id": "1",
        "user_id": "101",
        "status": "prepared",
        "login_url": "https://www.facebook.com/",
        "social_account_id": 1,
        "display_name": "Shop VN",
        "external_id": "facebook-public-101",
        "account_status": "login_required",
    }
    assert "profile_dir" not in prepared.text
    assert "/private/secret-profile-path" not in prepared.text
    assert "auth_ref" not in prepared.text


def test_json_api_enforces_profile_owner(client, session):
    csrf = {"X-CSRF-Token": CSRF_TOKEN}
    client.post(
        "/api/social/facebook/connect/prepare",
        json={
            "user_id": 101,
            "display_name": "Shop VN",
            "external_id": "facebook-public-101",
        },
        headers=csrf,
    )
    session.add(User(id=202))
    session.commit()

    response = client.post(
        "/api/social/facebook/connect/1/launch",
        json={"user_id": 202},
        headers=csrf,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Không tìm thấy Facebook account thuộc user đã chọn."
    assert "/private/secret-profile-path" not in response.text


def test_invalid_user_id_is_rejected_before_manager_call(client, fake_manager):
    response = client.post(
        "/social/facebook/connect/prepare",
        data={
            "csrf": CSRF_TOKEN,
            "user_id": "../escape",
            "display_name": "Shop VN",
            "external_id": "facebook-public-101",
        },
    )

    assert response.status_code == 400
    assert fake_manager.calls == []


def test_prepare_fails_closed_when_external_id_belongs_to_another_user(
    client,
    fake_manager,
    session,
):
    session.add(User(id=202))
    session.add(
        SocialAccount(
            user_id=202,
            platform="facebook",
            display_name="Other owner",
            external_id="facebook-public-101",
            auth_type="browser_profile",
            auth_ref="browser-profile://202/99",
        )
    )
    session.commit()

    response = client.post(
        "/api/social/facebook/connect/prepare",
        json={
            "user_id": 101,
            "display_name": "Shop VN",
            "external_id": "facebook-public-101",
        },
        headers={"X-CSRF-Token": CSRF_TOKEN},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Facebook account này đã tồn tại."
    assert fake_manager.calls == []
    session.expire_all()
    rows = session.scalars(select(SocialAccount)).all()
    assert len(rows) == 1
    assert rows[0].user_id == 202
