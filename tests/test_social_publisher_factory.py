"""Tests for the fail-closed publisher factory."""

import pytest

from laplace.social.publishers import (
    MockPublisher,
    PublisherNotConfiguredError,
    get_publisher,
    reset_publisher_cache,
)


@pytest.fixture(autouse=True)
def _isolated_publisher_cache():
    reset_publisher_cache()
    yield
    reset_publisher_cache()


def test_mock_is_the_only_default_enabled_publisher():
    publisher = get_publisher()

    assert isinstance(publisher, MockPublisher)
    assert publisher.name == "mock"


def test_mock_is_cached_by_default_and_cache_can_be_reset():
    first = get_publisher("mock")
    second = get_publisher(" MOCK ")

    assert second is first

    reset_publisher_cache()

    assert get_publisher("mock") is not first


def test_cache_can_be_bypassed_for_isolated_instances():
    cached = get_publisher("mock")
    isolated_a = get_publisher("mock", use_cache=False)
    isolated_b = get_publisher("mock", use_cache=False)

    assert isolated_a is not cached
    assert isolated_b is not isolated_a


@pytest.mark.parametrize("name", ["meta_graph", "meta-graph"])
def test_meta_graph_fails_closed_until_adapter_is_configured(name):
    with pytest.raises(PublisherNotConfiguredError, match="not configured"):
        get_publisher(name)


@pytest.mark.parametrize("name", ["browser_profile", "browser-profile"])
def test_browser_profile_fails_closed_even_when_explicitly_selected(name):
    with pytest.raises(
        PublisherNotConfiguredError,
        match="disabled and not configured",
    ):
        get_publisher(name)


def test_environment_cannot_implicitly_enable_browser_publisher(monkeypatch):
    monkeypatch.setenv("LAPLACE_SOCIAL_BROWSER_PUBLISHER", "true")
    monkeypatch.setenv("LAPLACE_SOCIAL_PUBLISHER", "browser_profile")

    assert isinstance(get_publisher(), MockPublisher)
    with pytest.raises(PublisherNotConfiguredError):
        get_publisher("browser_profile")


@pytest.mark.parametrize("name", ["facebook", "", "   ", None])
def test_unknown_or_empty_publisher_names_are_rejected(name):
    with pytest.raises(ValueError):
        get_publisher(name)
