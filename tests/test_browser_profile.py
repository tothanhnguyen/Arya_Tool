"""Tests for isolated, user-driven Facebook browser login profiles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laplace.social.browser_profile import (
    FACEBOOK_LOGIN_URL,
    BrowserProfileError,
    BrowserProfileManager,
    BrowserProfileOwnershipError,
    BrowserUnavailableError,
)


def test_prepare_creates_private_owner_isolated_profile(tmp_path: Path) -> None:
    manager = BrowserProfileManager(tmp_path)

    first = manager.prepare("account-1", "user-1")
    second = manager.prepare("account-1", "user-2")

    assert first.profile_dir == str(tmp_path / "user-1" / "account-1")
    assert second.profile_dir == str(tmp_path / "user-2" / "account-1")
    assert first.status == second.status == "prepared"
    assert first.login_url == FACEBOOK_LOGIN_URL
    assert Path(first.profile_dir).stat().st_mode & 0o777 == 0o700

    metadata_path = Path(first.profile_dir) / ".arya-profile.json"
    metadata = json.loads(metadata_path.read_text())
    assert metadata == {
        "account_id": "account-1",
        "schema_version": 1,
        "status": "prepared",
        "user_id": "user-1",
    }
    assert metadata_path.stat().st_mode & 0o777 == 0o600


def test_prepare_is_idempotent_and_preserves_ready_status(tmp_path: Path) -> None:
    manager = BrowserProfileManager(tmp_path)
    manager.prepare("account-1", "user-1")
    manager.mark_ready("account-1", "user-1")

    assert manager.prepare("account-1", "user-1").status == "ready"


def test_launch_uses_exact_profile_directory_and_facebook_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserProfileManager(tmp_path)
    launched: list[list[str]] = []
    monkeypatch.setattr(manager, "_find_system_browser", lambda: "/system/chromium")

    info = manager.launch_login("page.1", "owner_1", launcher=launched.append)

    profile_dir = str(tmp_path / "owner_1" / "page.1")
    assert launched == [
        [
            "/system/chromium",
            f"--user-data-dir={profile_dir}",
            FACEBOOK_LOGIN_URL,
        ]
    ]
    assert info.status == "login_pending"
    assert manager.status("page.1", "owner_1").status == "login_pending"


def test_launch_failure_does_not_claim_login_is_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserProfileManager(tmp_path)
    monkeypatch.setattr(manager, "_find_system_browser", lambda: "/system/chromium")

    def fail_to_launch(_command: list[str]) -> None:
        raise OSError("launch failed")

    with pytest.raises(OSError, match="launch failed"):
        manager.launch_login("account-1", "user-1", launcher=fail_to_launch)

    assert manager.status("account-1", "user-1").status == "prepared"


def test_launch_fails_closed_when_browser_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BrowserProfileManager(tmp_path)
    called = False
    monkeypatch.setattr(manager, "_find_system_browser", lambda: None)

    def launcher(_command: list[str]) -> None:
        nonlocal called
        called = True

    with pytest.raises(BrowserUnavailableError, match="Chrome or Chromium"):
        manager.launch_login("account-1", "user-1", launcher=launcher)

    assert not called
    assert manager.status("account-1", "user-1").status == "prepared"


def test_status_missing_and_mark_ready_requires_prepared_profile(tmp_path: Path) -> None:
    manager = BrowserProfileManager(tmp_path)

    assert manager.status("account-1", "user-1").status == "missing"
    with pytest.raises(BrowserProfileError, match="has not been prepared"):
        manager.mark_ready("account-1", "user-1")


def test_mark_ready_persists_only_public_metadata(tmp_path: Path) -> None:
    manager = BrowserProfileManager(tmp_path)
    info = manager.prepare("account-1", "user-1")

    ready = manager.mark_ready("account-1", "user-1")

    assert ready.status == "ready"
    assert manager.status("account-1", "user-1").status == "ready"
    serialized = (Path(info.profile_dir) / ".arya-profile.json").read_text().lower()
    assert "password" not in serialized
    assert "token" not in serialized
    assert "secret" not in serialized


@pytest.mark.parametrize(
    ("account_id", "user_id"),
    [
        ("../account", "user"),
        ("account", "../user"),
        ("/absolute", "user"),
        ("account/name", "user"),
        ("account", ""),
        ("account", ".."),
        ("account", "user name"),
    ],
)
def test_identifiers_cannot_escape_or_alias_profile_paths(
    tmp_path: Path,
    account_id: str,
    user_id: str,
) -> None:
    manager = BrowserProfileManager(tmp_path)

    with pytest.raises(ValueError):
        manager.prepare(account_id, user_id)


def test_symlink_cannot_redirect_profile_to_another_owner(tmp_path: Path) -> None:
    manager = BrowserProfileManager(tmp_path)
    target = tmp_path / "user-2"
    target.mkdir(parents=True)
    (tmp_path / "user-1").symlink_to(target, target_is_directory=True)

    with pytest.raises(BrowserProfileOwnershipError, match="symbolic links"):
        manager.prepare("account-1", "user-1")


def test_tampered_owner_metadata_is_rejected(tmp_path: Path) -> None:
    manager = BrowserProfileManager(tmp_path)
    info = manager.prepare("account-1", "user-1")
    metadata_path = Path(info.profile_dir) / ".arya-profile.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["user_id"] = "user-2"
    metadata_path.write_text(json.dumps(metadata))

    with pytest.raises(BrowserProfileOwnershipError, match="another owner"):
        manager.status("account-1", "user-1")


def test_metadata_symlink_is_not_read(tmp_path: Path) -> None:
    manager = BrowserProfileManager(tmp_path)
    info = manager.prepare("account-1", "user-1")
    metadata_path = Path(info.profile_dir) / ".arya-profile.json"
    metadata_path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text('{"account_id":"account-1","user_id":"user-1","status":"ready"}')
    metadata_path.symlink_to(outside)

    with pytest.raises(BrowserProfileOwnershipError, match="symbolic links"):
        manager.status("account-1", "user-1")
