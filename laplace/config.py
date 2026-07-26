from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="LAPLACE_", extra="ignore")

    app_name: str = "laplace-demon"
    db_url: str = "sqlite:///./laplace.db"

    # LLM
    llm_provider: str = "mock"  # mock | openai
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    # Interfaces
    telegram_bot_token: str | None = None
    # Neu dat, moi request toi REST API va trace viewer phai kem header
    # X-API-Key trung khop. De trong = mo (chi nen dung khi dev local).
    api_key: str | None = None

    # Tools
    search_api_key: str | None = None  # Tavily API key; khong co thi web_search chay che do stub

    # Safety limits
    max_steps: int = 8
    tool_timeout_s: int = 15
    task_timeout_s: int = 180
    tool_max_retries: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
