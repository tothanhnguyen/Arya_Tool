from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env nam o goc repo, doc theo duong dan tuyet doi de khong phu thuoc cwd
# (eval harness chdir vao thu muc ket qua, Docker chay tu /app...)
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_prefix="LAPLACE_", extra="ignore")

    app_name: str = "laplace-demon"
    db_url: str = "sqlite:///./laplace.db"

    # LLM — provider theo preset registry (laplace/llm/presets.py):
    # mock | gemini | openai | groq | openrouter | deepseek | xai | mistral | ollama
    llm_provider: str = "mock"
    # Override model chung cho MOI provider (trong = dung model mac dinh cua preset)
    llm_model: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    gemini_api_key: str | None = None
    # Mac dinh ban lite: quota free tier rong; gemini-2.5-flash da dong voi key moi
    gemini_model: str = "gemini-3.1-flash-lite"
    # Key cac hang khac (T15) — chi can dien hang minh dung
    groq_api_key: str | None = None
    openrouter_api_key: str | None = None
    deepseek_api_key: str | None = None
    xai_api_key: str | None = None
    mistral_api_key: str | None = None

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
