"""Kiểm thử browser profile Instagram tách biệt và không lưu bí mật."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laplace.social.instagram_profile import (
    INSTAGRAM_LOGIN_URL,
    InstagramBrowserUnavailableError,
    InstagramProfileError,
    InstagramProfileManager,
    InstagramProfileOwnershipError,
)


def test_prepare_creates_private_owner_isolated_profile(tmp_path: Path) -> None:
    manager = InstagramProfileManager(tmp_path)

    first = manager.prepare("11", "101")
    second = manager.prepare("11", "202")

    assert first.profile_dir == str(tmp_path / "101" / "11")
    assert second.profile_dir == str(tmp_path / "202" / "11")
    assert first.status == second.status == "prepared"
    assert first.login_url == INSTAGRAM_LOGIN_URL
    assert Path(first.profile_dir).stat().st_mode & 0o777 == 0o700

    metadata_path = Path(first.profile_dir) / ".arya-instagram-profile.json"
    assert json.loads(metadata_path.read_text()) == {
        "account_id": "11",
        "platform": "instagram",
        "schema_version": 1,
        "status": "prepared",
        "user_id": "101",
    }
    assert metadata_path.stat().st_mode & 0o777 == 0o600


def test_launch_uses_exact_profile_and_official_instagram_login_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = InstagramProfileManager(tmp_path)
    launched: list[list[str]] = []
    monkeypatch.setattr(manager, "_find_system_browser", lambda: "/system/chromium")

    info = manager.launch_login("11", "101", launcher=launched.append)

    expected_dir = str(tmp_path / "101" / "11")
    assert launched == [
        [
            "/system/chromium",
            f"--user-data-dir={expected_dir}",
            INSTAGRAM_LOGIN_URL,
        ]
    ]
    assert info.status == "login_pending"
    assert manager.status("11", "101").status == "login_pending"


def test_mark_ready_requires_a_successful_launch(tmp_path: Path) -> None:
    manager = InstagramProfileManager(tmp_path)
    manager.prepare("11", "101")

    with pytest.raises(InstagramProfileError, match="chờ xác nhận"):
        manager.mark_ready("11", "101")


def test_launch_failure_does_not_claim_login_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = InstagramProfileManager(tmp_path)
    monkeypatch.setattr(manager, "_find_system_browser", lambda: "/system/chromium")

    def fail(_command: list[str]) -> None:
        raise OSError("không mở được")

    with pytest.raises(OSError, match="không mở được"):
        manager.launch_login("11", "101", launcher=fail)

    assert manager.status("11", "101").status == "prepared"


def test_launch_fails_closed_without_supported_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = InstagramProfileManager(tmp_path)
    monkeypatch.setattr(manager, "_find_system_browser", lambda: None)

    with pytest.raises(InstagramBrowserUnavailableError, match="Chrome"):
        manager.launch_login("11", "101", launcher=lambda _command: None)

    assert manager.status("11", "101").status == "prepared"


def test_ready_metadata_contains_no_login_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = InstagramProfileManager(tmp_path)
    monkeypatch.setattr(manager, "_find_system_browser", lambda: "/system/chromium")
    info = manager.launch_login("11", "101", launcher=lambda _command: None)

    manager.mark_ready("11", "101")

    serialized = (
        Path(info.profile_dir) / ".arya-instagram-profile.json"
    ).read_text().lower()
    for secret_field in ("password", "cookie", "access_token", "secret"):
        assert secret_field not in serialized


@pytest.mark.parametrize(
    ("account_id", "user_id"),
    [
        ("../11", "101"),
        ("11", "../101"),
        ("/11", "101"),
        ("11/12", "101"),
        ("", "101"),
        ("11", ".."),
        ("11", "user name"),
    ],
)
def test_identifiers_cannot_escape_profile_root(
    tmp_path: Path,
    account_id: str,
    user_id: str,
) -> None:
    manager = InstagramProfileManager(tmp_path)

    with pytest.raises(ValueError):
        manager.prepare(account_id, user_id)


def test_tampered_owner_or_platform_metadata_is_rejected(tmp_path: Path) -> None:
    manager = InstagramProfileManager(tmp_path)
    info = manager.prepare("11", "101")
    metadata_path = Path(info.profile_dir) / ".arya-instagram-profile.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["user_id"] = "202"
    metadata_path.write_text(json.dumps(metadata))

    with pytest.raises(InstagramProfileOwnershipError, match="owner khác"):
        manager.status("11", "101")


def test_metadata_symlink_is_never_read(tmp_path: Path) -> None:
    manager = InstagramProfileManager(tmp_path)
    info = manager.prepare("11", "101")
    metadata_path = Path(info.profile_dir) / ".arya-instagram-profile.json"
    metadata_path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text(
        '{"account_id":"11","user_id":"101","platform":"instagram","status":"ready"}'
    )
    metadata_path.symlink_to(outside)

    with pytest.raises(InstagramProfileOwnershipError, match="symbolic link"):
        manager.status("11", "101")
