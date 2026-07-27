from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env nam o goc repo, doc theo duong dan tuyet doi de khong phu thuoc cwd
# (eval harness chdir vao thu muc ket qua, Docker chay tu /app...)
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_prefix="LAPLACE_", extra="ignore")

    app_name: str = "arya-tool"
    db_url: str = "sqlite:///./arya-tool.db"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_connect_timeout_s: int = 10
    db_ssl_mode: str = "require"

    # Supabase values remain empty until a project is linked. The secret key is
    # server-side only and must never be rendered, logged, or sent to an LLM.
    supabase_url: str | None = None
    supabase_secret_key: str | None = None
    supabase_media_bucket: str = "arya-media"
    supabase_artifact_bucket: str = "arya-artifacts"
    supabase_signed_url_ttl_s: int = 300

    # Social publishing stays local-first. Real adapters are opt-in; the
    # deterministic mock is the safe default for development and tests.
    social_publisher: str = "mock"
    social_timezone: str = "Asia/Ho_Chi_Minh"
    social_daily_post_limit: int = 2
    social_browser_publisher: bool = False
    social_poll_seconds: int = 15
    social_media_dir: str = "media"
    social_media_backend: str = "local"
    social_media_max_bytes: int = 50 * 1024 * 1024
    social_currency: str = "VND"
    social_content_provider: str = "openrouter"
    social_content_model: str = "openrouter/free"
    social_content_max_retries: int = 1

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
    web_host: str = "127.0.0.1"
    web_port: int = 8010
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
