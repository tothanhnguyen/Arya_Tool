"""Hồ sơ trình duyệt tách biệt cho luồng đăng nhập Instagram do người dùng thực hiện.

Module này chỉ tạo thư mục profile và mở trang đăng nhập chính thức. Nó không
đọc, nhập, xuất hoặc ghi log cookie; mật khẩu, 2FA và checkpoint luôn được xử lý
trực tiếp giữa người dùng và Instagram trong trình duyệt.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INSTAGRAM_LOGIN_URL = "https://www.instagram.com/accounts/login/"

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_METADATA_FILE = ".arya-instagram-profile.json"
_VALID_STATUSES = frozenset({"prepared", "login_pending", "ready"})


class InstagramProfileError(RuntimeError):
    """Lỗi profile Instagram không an toàn hoặc không dùng được."""


class InstagramBrowserUnavailableError(FileNotFoundError, InstagramProfileError):
    """Không tìm thấy Chrome/Chromium cài trên máy."""


class InstagramProfileOwnershipError(InstagramProfileError):
    """Metadata của profile không khớp tài khoản và user được yêu cầu."""


@dataclass(frozen=True, slots=True)
class InstagramProfileInfo:
    """Thông tin công khai, không chứa bí mật của một browser profile."""

    account_id: str
    user_id: str
    profile_dir: str
    login_url: str
    status: str


Launcher = Callable[[list[str]], object]


class InstagramProfileManager:
    """Chuẩn bị và mở Chrome/Chromium profile riêng theo từng owner."""

    def __init__(self, base_dir: str | Path) -> None:
        raw_base_dir = Path(base_dir).expanduser()
        if raw_base_dir.is_symlink():
            raise InstagramProfileOwnershipError(
                "không cho phép symbolic link ở thư mục browser profile"
            )
        if raw_base_dir.exists() and not raw_base_dir.is_dir():
            raise ValueError("base_dir phải là một thư mục")
        self.base_dir = raw_base_dir.resolve(strict=False)

    def prepare(self, account_id: str, user_id: str) -> InstagramProfileInfo:
        """Tạo profile riêng và metadata ownership không chứa thông tin đăng nhập."""

        profile_dir = self._profile_path(account_id, user_id)
        owner_dir = profile_dir.parent
        self.base_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._reject_symlink(self.base_dir)
        owner_dir.mkdir(mode=0o700, exist_ok=True)
        self._reject_symlink(owner_dir)
        profile_dir.mkdir(mode=0o700, exist_ok=True)
        self._reject_symlink(profile_dir)
        if not profile_dir.is_dir():
            raise InstagramProfileError("đường dẫn browser profile không phải thư mục")

        self._set_private_permissions(owner_dir)
        self._set_private_permissions(profile_dir)

        metadata_path = profile_dir / _METADATA_FILE
        if metadata_path.exists() or metadata_path.is_symlink():
            metadata = self._read_metadata(profile_dir, account_id, user_id)
            profile_status = str(metadata["status"])
        else:
            profile_status = "prepared"
            self._write_metadata(profile_dir, account_id, user_id, profile_status)
        return self._info(account_id, user_id, profile_dir, profile_status)

    def launch_login(
        self,
        account_id: str,
        user_id: str,
        launcher: Launcher | None = None,
    ) -> InstagramProfileInfo:
        """Mở đúng URL Instagram trong profile riêng; người dùng tự đăng nhập."""

        info = self.prepare(account_id, user_id)
        browser = self._find_system_browser()
        if browser is None:
            raise InstagramBrowserUnavailableError(
                "Không tìm thấy Chrome hoặc Chromium trên máy"
            )

        command = [
            browser,
            f"--user-data-dir={info.profile_dir}",
            INSTAGRAM_LOGIN_URL,
        ]
        if launcher is None:
            self._launch_process(command)
        else:
            launcher(command)

        profile_dir = Path(info.profile_dir)
        self._write_metadata(profile_dir, account_id, user_id, "login_pending")
        return self._info(account_id, user_id, profile_dir, "login_pending")

    def mark_ready(self, account_id: str, user_id: str) -> InstagramProfileInfo:
        """Đánh dấu sẵn sàng sau khi người dùng xác nhận đã đăng nhập đúng tài khoản."""

        profile_dir = self._profile_path(account_id, user_id)
        metadata = self._read_metadata(profile_dir, account_id, user_id)
        if metadata["status"] != "login_pending":
            raise InstagramProfileError("browser profile chưa ở bước chờ xác nhận đăng nhập")
        self._write_metadata(profile_dir, account_id, user_id, "ready")
        return self._info(account_id, user_id, profile_dir, "ready")

    def status(self, account_id: str, user_id: str) -> InstagramProfileInfo:
        """Đọc trạng thái công khai mà không mở hoặc thay đổi browser profile."""

        profile_dir = self._profile_path(account_id, user_id)
        if not profile_dir.exists():
            return self._info(account_id, user_id, profile_dir, "missing")
        metadata = self._read_metadata(profile_dir, account_id, user_id)
        return self._info(account_id, user_id, profile_dir, str(metadata["status"]))

    def _profile_path(self, account_id: str, user_id: str) -> Path:
        account_id = self._validate_identifier("account_id", account_id)
        user_id = self._validate_identifier("user_id", user_id)
        owner_dir = self.base_dir / user_id
        profile_dir = owner_dir / account_id

        for candidate in (owner_dir, profile_dir):
            self._reject_symlink(candidate)
            try:
                candidate.resolve(strict=False).relative_to(self.base_dir)
            except ValueError as exc:
                raise ValueError("đường dẫn browser profile thoát khỏi base_dir") from exc
        return profile_dir

    @staticmethod
    def _validate_identifier(field_name: str, value: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError(
                f"{field_name} chỉ được chứa chữ, số, dấu chấm, gạch dưới hoặc gạch ngang"
            )
        if value in {".", ".."}:
            raise ValueError(f"{field_name} không phải thành phần đường dẫn hợp lệ")
        return value

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink():
            raise InstagramProfileOwnershipError(
                "không cho phép symbolic link trong đường dẫn browser profile"
            )

    @staticmethod
    def _set_private_permissions(path: Path) -> None:
        try:
            path.chmod(0o700)
        except OSError as exc:
            raise InstagramProfileError("không thể bảo vệ thư mục browser profile") from exc

    def _read_metadata(
        self,
        profile_dir: Path,
        account_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        self._reject_symlink(profile_dir)
        if not profile_dir.is_dir():
            raise InstagramProfileError("browser profile chưa được chuẩn bị")
        metadata_path = profile_dir / _METADATA_FILE
        self._reject_symlink(metadata_path)
        if not metadata_path.is_file():
            raise InstagramProfileError("thiếu metadata của browser profile")

        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InstagramProfileError("metadata của browser profile không hợp lệ") from exc
        if not isinstance(metadata, dict):
            raise InstagramProfileError("metadata của browser profile không hợp lệ")
        if metadata.get("account_id") != account_id or metadata.get("user_id") != user_id:
            raise InstagramProfileOwnershipError("browser profile thuộc owner khác")
        if metadata.get("platform") != "instagram":
            raise InstagramProfileOwnershipError("browser profile không dành cho Instagram")
        if metadata.get("status") not in _VALID_STATUSES:
            raise InstagramProfileError("trạng thái browser profile không hợp lệ")
        return metadata

    @staticmethod
    def _write_metadata(
        profile_dir: Path,
        account_id: str,
        user_id: str,
        profile_status: str,
    ) -> None:
        if profile_status not in _VALID_STATUSES:
            raise ValueError("trạng thái browser profile không hợp lệ")
        metadata = {
            "schema_version": 1,
            "platform": "instagram",
            "account_id": account_id,
            "user_id": user_id,
            "status": profile_status,
        }
        fd, temporary_name = tempfile.mkstemp(
            dir=profile_dir,
            prefix=".arya-instagram-profile-",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            os.chmod(temporary_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                json.dump(metadata, stream, ensure_ascii=True, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, profile_dir / _METADATA_FILE)
        finally:
            if fd >= 0:
                os.close(fd)
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _info(
        account_id: str,
        user_id: str,
        profile_dir: Path,
        profile_status: str,
    ) -> InstagramProfileInfo:
        return InstagramProfileInfo(
            account_id=account_id,
            user_id=user_id,
            profile_dir=str(profile_dir),
            login_url=INSTAGRAM_LOGIN_URL,
            status=profile_status,
        )

    @staticmethod
    def _find_system_browser() -> str | None:
        for browser_name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "chrome",
        ):
            executable = shutil.which(browser_name)
            if executable:
                return executable

        candidates: list[Path] = []
        if sys.platform == "darwin":
            candidates.extend(
                [
                    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
                    Path.home()
                    / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    Path.home() / "Applications/Chromium.app/Contents/MacOS/Chromium",
                ]
            )
        elif os.name == "nt":
            for environment_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                root = os.environ.get(environment_name)
                if root:
                    candidates.extend(
                        [
                            Path(root) / "Google/Chrome/Application/chrome.exe",
                            Path(root) / "Chromium/Application/chrome.exe",
                        ]
                    )
        return next(
            (str(candidate) for candidate in candidates if candidate.is_file()),
            None,
        )

    @staticmethod
    def _launch_process(command: list[str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(command, close_fds=True, start_new_session=True)
