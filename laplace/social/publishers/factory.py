"""Fail-closed factory for social publisher adapters."""

from __future__ import annotations

from threading import Lock

from laplace.social.publishers.base import SocialPublisher
from laplace.social.publishers.mock import MockPublisher


class PublisherNotConfiguredError(RuntimeError):
    """A known publisher was requested before its adapter was configured."""


_PUBLISHER_CACHE: dict[str, SocialPublisher] = {}
_CACHE_LOCK = Lock()
_KNOWN_UNCONFIGURED = {"meta_graph", "browser_profile"}


def get_publisher(name: str = "mock", *, use_cache: bool = True) -> SocialPublisher:
    """Return a configured publisher.

    Only the network-free mock is enabled at this stage.  Reserved production
    adapter names deliberately fail closed: merely selecting a name or setting
    an environment variable must never activate browser automation.
    """

    normalized = _normalize_name(name)
    if normalized in _KNOWN_UNCONFIGURED:
        raise PublisherNotConfiguredError(_not_configured_message(normalized))
    if normalized != "mock":
        raise ValueError(
            f"unknown publisher {name!r}; enabled publishers: mock"
        )

    if not use_cache:
        return MockPublisher()

    with _CACHE_LOCK:
        publisher = _PUBLISHER_CACHE.get(normalized)
        if publisher is None:
            publisher = MockPublisher()
            _PUBLISHER_CACHE[normalized] = publisher
        return publisher


def reset_publisher_cache() -> None:
    """Clear cached adapters, primarily for isolated tests and local reloads."""

    with _CACHE_LOCK:
        _PUBLISHER_CACHE.clear()


def _normalize_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("publisher name must be a non-empty string")
    return name.strip().lower().replace("-", "_")


def _not_configured_message(name: str) -> str:
    if name == "browser_profile":
        return (
            "browser_profile publisher is disabled and not configured; "
            "browser automation must never be enabled implicitly"
        )
    return (
        "meta_graph publisher adapter is not configured; "
        "configure the official API adapter before selecting it"
    )
