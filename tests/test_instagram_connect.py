"""Kiểm thử luồng web kết nối Instagram bằng browser profile an toàn."""

from dataclasses import replace
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from laplace.config import get_settings
from laplace.models import User
from laplace.social.instagram_profile import InstagramProfileInfo
from laplace.social.models import SocialAccount
from laplace.web.settings_page import CSRF_TOKEN
from laplace.web.social.instagram_connect import (
    get_instagram_profile_manager,
    router,
)

LOOPBACK = ("127.0.0.1", 50000)
LAN = ("192.0.2.20", 50000)
BASE_URL = "http://127.0.0.1"


class FakeInstagramProfileManager:
    def __init__(self) -> None:
        self.info = InstagramProfileInfo(
            account_id="0",
            user_id="0",
            profile_dir="/private/instagram-profile",
            login_url="https://www.instagram.com/accounts/login/",
            status="missing",
        )
        self.calls: list[tuple[str, str, str]] = []

    def _owned(self, account_id: str, user_id: str) -> InstagramProfileInfo:
        if self.info.status != "missing" and (
            account_id != self.info.account_id or user_id != self.info.user_id
        ):
            raise PermissionError("owner mismatch")
        return replace(self.info, account_id=account_id, user_id=user_id)

    def prepare(self, account_id: str, user_id: str) -> InstagramProfileInfo:
        self.calls.append(("prepare", account_id, user_id))
        self.info = replace(self._owned(account_id, user_id), status="prepared")
        return self.info

    def launch_login(
        self,
        account_id: str,
        user_id: str,
        launcher=None,
    ) -> InstagramProfileInfo:
        del launcher
        self.calls.append(("launch", account_id, user_id))
        info = self._owned(account_id, user_id)
        if info.status == "missing":
            raise FileNotFoundError("profile chưa chuẩn bị")
        self.info = replace(info, status="login_pending")
        return self.info

    def mark_ready(self, account_id: str, user_id: str) -> InstagramProfileInfo:
        self.calls.append(("ready", account_id, user_id))
        info = self._owned(account_id, user_id)
        if info.status != "login_pending":
            raise RuntimeError("chưa mở đăng nhập")
        self.info = replace(info, status="ready")
        return self.info

    def status(self, account_id: str, user_id: str) -> InstagramProfileInfo:
        self.calls.append(("status", account_id, user_id))
        return self._owned(account_id, user_id)


@pytest.fixture()
def fake_manager() -> FakeInstagramProfileManager:
    return FakeInstagramProfileManager()


@pytest.fixture()
def app(fake_manager: FakeInstagramProfileManager) -> FastAPI:
    instance = FastAPI()
    instance.include_router(router)
    instance.dependency_overrides[get_instagram_profile_manager] = lambda: fake_manager
    return instance


@pytest.fixture()
def client(app: FastAPI):
    with TestClient(app, client=LOOPBACK, base_url=BASE_URL) as test_client:
        yield test_client


def _seed_user(session, user_id: int = 101) -> None:
    session.add(User(id=user_id))
    session.commit()


def test_connect_page_has_only_public_account_fields(client) -> None:
    response = client.get("/social/instagram/connect")

    assert response.status_code == 200
    assert "Kết nối Instagram" in response.text
    for field in ("user_id", "display_name", "external_id"):
        assert f'name="{field}"' in response.text
    for forbidden in ("cookie", "password", "access_token"):
        assert f'name="{forbidden}"' not in response.text.lower()
    assert "không tự đăng bài" in response.text
    assert "không vượt qua 2FA hoặc checkpoint" in response.text
    assert '<input type="hidden" name="user_id" value="1">' in response.text


def test_local_form_bootstraps_internal_owner(client, fake_manager, session) -> None:
    response = client.post(
        "/social/instagram/prepare",
        data={
            "csrf": CSRF_TOKEN,
            "user_id": "1",
            "display_name": "Local Instagram",
            "external_id": "local-instagram-public-id",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert session.get(User, 1) is not None
    account = session.scalar(
        select(SocialAccount).where(
            SocialAccount.platform == "instagram",
            SocialAccount.external_id == "local-instagram-public-id",
        )
    )
    assert account is not None
    assert account.user_id == 1
    assert fake_manager.calls == [("prepare", str(account.id), "1")]


def test_prepare_persists_safe_social_account(
    client,
    fake_manager,
    session,
) -> None:
    _seed_user(session)

    response = client.post(
        "/social/instagram/prepare",
        data={
            "csrf": CSRF_TOKEN,
            "user_id": "101",
            "display_name": "Arya Deals",
            "external_id": "@arya.deals",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    session.expire_all()
    account = session.scalar(select(SocialAccount))
    assert account is not None
    assert response.headers["location"].endswith(
        f"user_id=101&account_id={account.id}"
    )
    assert account.user_id == 101
    assert account.platform == "instagram"
    assert account.display_name == "Arya Deals"
    assert account.external_id == "@arya.deals"
    assert account.auth_type == "browser_profile"
    assert account.auth_ref == f"browser-profile://instagram/101/{account.id}"
    assert account.status == "login_required"
    assert fake_manager.calls == [("prepare", str(account.id), "101")]


def test_launch_and_ready_require_exact_owner_and_activate_account(
    client,
    fake_manager,
    session,
) -> None:
    _seed_user(session)
    prepared = client.post(
        "/social/instagram/prepare",
        data={
            "csrf": CSRF_TOKEN,
            "user_id": "101",
            "display_name": "Arya Deals",
            "external_id": "arya.deals",
        },
        follow_redirects=False,
    )
    account_id = int(prepared.headers["location"].split("account_id=")[1])

    launched = client.post(
        f"/social/instagram/{account_id}/launch",
        data={"csrf": CSRF_TOKEN, "user_id": "101"},
        follow_redirects=False,
    )
    ready = client.post(
        f"/social/instagram/{account_id}/ready",
        data={"csrf": CSRF_TOKEN, "user_id": "101"},
        follow_redirects=False,
    )

    assert launched.status_code == 303
    assert ready.status_code == 303
    session.expire_all()
    account = session.get(SocialAccount, account_id)
    assert account is not None
    assert account.status == "active"
    assert isinstance(account.last_checked_at, datetime)
    assert fake_manager.calls == [
        ("prepare", str(account_id), "101"),
        ("launch", str(account_id), "101"),
        ("ready", str(account_id), "101"),
    ]


def test_wrong_owner_and_wrong_account_type_fail_closed(
    client,
    fake_manager,
    session,
) -> None:
    _seed_user(session)
    session.add(User(id=202))
    session.commit()
    prepared = client.post(
        "/social/instagram/prepare",
        data={
            "csrf": CSRF_TOKEN,
            "user_id": "101",
            "display_name": "Arya Deals",
            "external_id": "arya.deals",
        },
        follow_redirects=False,
    )
    account_id = int(prepared.headers["location"].split("account_id=")[1])

    wrong_owner = client.post(
        f"/social/instagram/{account_id}/launch",
        data={"csrf": CSRF_TOKEN, "user_id": "202"},
    )
    session.expire_all()
    account = session.get(SocialAccount, account_id)
    assert account is not None
    account.auth_type = "mock"
    session.commit()
    wrong_type = client.post(
        f"/social/instagram/{account_id}/launch",
        data={"csrf": CSRF_TOKEN, "user_id": "101"},
    )

    assert wrong_owner.status_code == 404
    assert wrong_type.status_code == 404
    assert fake_manager.calls == [("prepare", str(account_id), "101")]


def test_duplicate_external_id_is_rejected_without_new_profile(
    client,
    fake_manager,
    session,
) -> None:
    _seed_user(session)
    payload = {
        "csrf": CSRF_TOKEN,
        "user_id": "101",
        "display_name": "Arya Deals",
        "external_id": "arya.deals",
    }
    assert client.post("/social/instagram/prepare", data=payload).status_code == 200
    calls_after_first = list(fake_manager.calls)

    duplicate = client.post("/social/instagram/prepare", data=payload)

    assert duplicate.status_code == 409
    assert fake_manager.calls == calls_after_first
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(SocialAccount)) == 1


def test_mutations_require_valid_csrf(client, fake_manager, session) -> None:
    _seed_user(session)

    response = client.post(
        "/social/instagram/prepare",
        data={
            "csrf": "sai",
            "user_id": "101",
            "display_name": "Arya Deals",
            "external_id": "arya.deals",
        },
    )

    assert response.status_code == 403
    assert fake_manager.calls == []
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(SocialAccount)) == 0


def test_router_requires_api_key_and_loopback(app, client, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "api_key", "dashboard-secret")

    assert client.get("/social/instagram/connect").status_code == 401
    assert (
        client.get(
            "/social/instagram/connect",
            headers={"X-API-Key": "dashboard-secret"},
        ).status_code
        == 200
    )
    with TestClient(app, client=LAN, base_url=BASE_URL) as remote:
        blocked = remote.get(
            "/social/instagram/connect",
            headers={"X-API-Key": "dashboard-secret"},
        )
    assert blocked.status_code == 403


def test_tampered_auth_reference_blocks_profile_access(
    client,
    fake_manager,
    session,
) -> None:
    _seed_user(session)
    account = SocialAccount(
        user_id=101,
        platform="instagram",
        display_name="Arya Deals",
        external_id="arya.deals",
        auth_type="browser_profile",
        auth_ref="browser-profile://instagram/202/999",
        status="login_required",
    )
    session.add(account)
    session.commit()

    response = client.post(
        f"/social/instagram/{account.id}/launch",
        data={"csrf": CSRF_TOKEN, "user_id": "101"},
    )

    assert response.status_code == 409
    assert fake_manager.calls == []
