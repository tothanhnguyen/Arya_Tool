"""Isolated browser profiles for user-driven Facebook authentication.

This module only prepares and opens a dedicated browser profile.  Authentication,
including any checkpoint or second-factor prompt, remains entirely in the browser
and under the user's control.
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

FACEBOOK_LOGIN_URL = "https://www.facebook.com/"

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_METADATA_FILE = ".arya-profile.json"
_VALID_STATUSES = frozenset({"prepared", "login_pending", "ready"})


class BrowserProfileError(RuntimeError):
    """Base error for an unusable or untrusted browser profile."""


class BrowserUnavailableError(FileNotFoundError, BrowserProfileError):
    """Raised when no supported system browser can be found."""


class BrowserProfileOwnershipError(BrowserProfileError):
    """Raised when profile metadata does not match its requested owner."""


@dataclass(frozen=True, slots=True)
class BrowserProfileInfo:
    """Public, non-secret state for one isolated browser profile."""

    account_id: str
    user_id: str
    profile_dir: str
    login_url: str
    status: str


Launcher = Callable[[list[str]], object]


class BrowserProfileManager:
    """Prepare and launch owner-isolated Chrome/Chromium profiles."""

    def __init__(self, base_dir: str | Path) -> None:
        raw_base_dir = Path(base_dir).expanduser()
        if raw_base_dir.exists() and not raw_base_dir.is_dir():
            raise ValueError("base_dir must be a directory")
        self.base_dir = raw_base_dir.resolve(strict=False)

    def prepare(self, account_id: str, user_id: str) -> BrowserProfileInfo:
        """Create an isolated profile directory and non-secret ownership metadata."""

        profile_dir = self._profile_path(account_id, user_id)
        owner_dir = profile_dir.parent
        self.base_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._reject_symlink(self.base_dir)
        owner_dir.mkdir(mode=0o700, exist_ok=True)
        self._reject_symlink(owner_dir)
        profile_dir.mkdir(mode=0o700, exist_ok=True)
        self._reject_symlink(profile_dir)
        if not profile_dir.is_dir():
            raise BrowserProfileError("profile path is not a directory")

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
    ) -> BrowserProfileInfo:
        """Open Facebook in the profile and leave authentication to the user."""

        info = self.prepare(account_id, user_id)
        browser = self._find_system_browser()
        if browser is None:
            raise BrowserUnavailableError("Chrome or Chromium is not installed")

        command = [
            browser,
            f"--user-data-dir={info.profile_dir}",
            FACEBOOK_LOGIN_URL,
        ]
        if launcher is None:
            self._launch_process(command)
        else:
            launcher(command)

        profile_dir = Path(info.profile_dir)
        self._write_metadata(profile_dir, account_id, user_id, "login_pending")
        return self._info(account_id, user_id, profile_dir, "login_pending")

    def mark_ready(self, account_id: str, user_id: str) -> BrowserProfileInfo:
        """Mark a profile ready after the user confirms login in the browser."""

        profile_dir = self._profile_path(account_id, user_id)
        self._read_metadata(profile_dir, account_id, user_id)
        self._write_metadata(profile_dir, account_id, user_id, "ready")
        return self._info(account_id, user_id, profile_dir, "ready")

    def status(self, account_id: str, user_id: str) -> BrowserProfileInfo:
        """Return public state without opening or modifying the browser profile."""

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
            resolved = candidate.resolve(strict=False)
            try:
                resolved.relative_to(self.base_dir)
            except ValueError as exc:
                raise ValueError("profile path escapes base_dir") from exc
        return profile_dir

    @staticmethod
    def _validate_identifier(field_name: str, value: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError(
                f"{field_name} must use only letters, numbers, dots, underscores, or hyphens"
            )
        if value in {".", ".."}:
            raise ValueError(f"{field_name} is not a valid path component")
        return value

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink():
            raise BrowserProfileOwnershipError("symbolic links are not allowed in profile paths")

    @staticmethod
    def _set_private_permissions(path: Path) -> None:
        try:
            path.chmod(0o700)
        except OSError as exc:
            raise BrowserProfileError("could not secure browser profile directory") from exc

    def _read_metadata(
        self,
        profile_dir: Path,
        account_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        self._reject_symlink(profile_dir)
        if not profile_dir.is_dir():
            raise BrowserProfileError("browser profile has not been prepared")
        metadata_path = profile_dir / _METADATA_FILE
        self._reject_symlink(metadata_path)
        if not metadata_path.is_file():
            raise BrowserProfileError("browser profile metadata is missing")

        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BrowserProfileError("browser profile metadata is invalid") from exc
        if not isinstance(metadata, dict):
            raise BrowserProfileError("browser profile metadata is invalid")
        if metadata.get("account_id") != account_id or metadata.get("user_id") != user_id:
            raise BrowserProfileOwnershipError("browser profile belongs to another owner")
        if metadata.get("status") not in _VALID_STATUSES:
            raise BrowserProfileError("browser profile status is invalid")
        return metadata

    @staticmethod
    def _write_metadata(
        profile_dir: Path,
        account_id: str,
        user_id: str,
        profile_status: str,
    ) -> None:
        if profile_status not in _VALID_STATUSES:
            raise ValueError("invalid browser profile status")
        metadata = {
            "schema_version": 1,
            "account_id": account_id,
            "user_id": user_id,
            "status": profile_status,
        }
        fd, temporary_name = tempfile.mkstemp(
            dir=profile_dir,
            prefix=".arya-profile-",
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
    ) -> BrowserProfileInfo:
        return BrowserProfileInfo(
            account_id=account_id,
            user_id=user_id,
            profile_dir=str(profile_dir),
            login_url=FACEBOOK_LOGIN_URL,
            status=profile_status,
        )

    @staticmethod
    def _find_system_browser() -> str | None:
        browser_names = (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "chrome",
        )
        for browser_name in browser_names:
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
        return subprocess.Popen(
            command,
            close_fds=True,
            start_new_session=True,
        )
