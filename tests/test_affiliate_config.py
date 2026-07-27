"""Product-level safety defaults for Arya_Tool."""

from laplace.config import Settings


def test_affiliate_defaults_are_local_and_mock_only():
    settings = Settings(_env_file=None)

    assert settings.app_name == "arya-tool"
    assert settings.db_url.endswith("arya-tool.db")
    assert settings.web_host == "127.0.0.1"
    assert settings.web_port == 8010
    assert settings.social_publisher == "mock"
    assert settings.social_browser_publisher is False
    assert settings.social_daily_post_limit == 2
