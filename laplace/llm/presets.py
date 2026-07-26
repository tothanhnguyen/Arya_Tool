"""Preset registry cho cac hang AI co endpoint tuong thich OpenAI (T15).

Moi preset mo ta du thong tin de "dan key la chay": base_url, ten bien env
chua key, model mac dinh, URL trang lay key (in ra khi thieu key — phan
"de dung" cua plan), va bang gia USD/1M token cho cac model hay dung.

File nay TU DUNG MOT MINH (khong import module khac trong package) de wizard
setup/check va test import re. `get_provider()` (laplace/llm/base.py) doc
registry nay thay cho if/else cung; Anthropic native khong co o day vi API
format rieng (di duong OpenRouter — xem PLAN_PROVIDERS.md muc 3).
"""

from dataclasses import dataclass, field

# Bang gia: {model: (USD/1M token input, USD/1M token output)}
Pricing = dict[str, tuple[float, float]]


@dataclass(frozen=True)
class ProviderPreset:
    name: str  # ten dung trong LAPLACE_LLM_PROVIDER
    label: str  # ten hien thi cho nguoi dung (wizard/check)
    base_url: str | None  # None = mac dinh SDK OpenAI (api.openai.com)
    default_model: str
    key_url: str  # trang lay key (hoac trang cai dat voi ollama)
    free_tier: str  # mo ta ngan cho wizard + README
    pricing: Pricing = field(default_factory=dict)
    requires_key: bool = True
    local: bool = False  # chay local (ollama): moi model gia 0, khong warning

    @property
    def env_key(self) -> str | None:
        """Ten bien env chua API key, vd LAPLACE_GROQ_API_KEY."""
        if not self.requires_key:
            return None
        return f"LAPLACE_{self.name.upper()}_API_KEY"

    @property
    def settings_field(self) -> str:
        """Ten field tuong ung trong laplace.config.Settings."""
        return f"{self.name}_api_key"


# Base URL tra theo tai lieu chinh thuc tung hang (OpenAI-compatible endpoint).
PRESETS: dict[str, ProviderPreset] = {
    p.name: p
    for p in [
        ProviderPreset(
            name="gemini",
            label="Google Gemini (AI Studio)",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            default_model="gemini-3.1-flash-lite",
            key_url="https://aistudio.google.com/apikey",
            free_tier="Co — free tier rong (ban lite ~500 req/ngay)",
            pricing={
                "gemini-3.1-flash-lite": (0.10, 0.40),
                "gemini-2.5-flash": (0.30, 2.50),
                "gemini-2.5-flash-lite": (0.10, 0.40),
                "gemini-2.5-pro": (1.25, 10.00),
            },
        ),
        ProviderPreset(
            name="openai",
            label="OpenAI",
            base_url=None,  # dung mac dinh SDK: https://api.openai.com/v1
            default_model="gpt-4o-mini",
            key_url="https://platform.openai.com/api-keys",
            free_tier="Khong — tra phi theo token",
            pricing={
                "gpt-4o": (2.50, 10.00),
                "gpt-4o-mini": (0.15, 0.60),
                "gpt-4.1": (2.00, 8.00),
                "gpt-4.1-mini": (0.40, 1.60),
                "gpt-4.1-nano": (0.10, 0.40),
                "o4-mini": (1.10, 4.40),
            },
        ),
        ProviderPreset(
            name="groq",
            label="Groq (inference sieu nhanh)",
            base_url="https://api.groq.com/openai/v1",
            default_model="llama-3.3-70b-versatile",
            key_url="https://console.groq.com/keys",
            free_tier="Co — free tier nhanh, hop chay eval",
            pricing={
                "llama-3.3-70b-versatile": (0.59, 0.79),
                "llama-3.1-8b-instant": (0.05, 0.08),
                "openai/gpt-oss-120b": (0.15, 0.60),
            },
        ),
        ProviderPreset(
            name="openrouter",
            label="OpenRouter (1 key -> tram model, ke ca Claude)",
            base_url="https://openrouter.ai/api/v1",
            default_model="openrouter/auto",
            key_url="https://openrouter.ai/settings/keys",
            free_tier="Co — nhieu model :free; con lai tra phi theo model",
            pricing={
                # Gia tham khao cua vai model hay dung qua OpenRouter
                "openai/gpt-4o-mini": (0.15, 0.60),
                "anthropic/claude-sonnet-4": (3.00, 15.00),
                "google/gemini-2.5-flash": (0.30, 2.50),
                "deepseek/deepseek-chat": (0.27, 1.10),
            },
        ),
        ProviderPreset(
            name="deepseek",
            label="DeepSeek",
            base_url="https://api.deepseek.com",
            default_model="deepseek-chat",
            key_url="https://platform.deepseek.com/api_keys",
            free_tier="Khong — nhung gia re",
            pricing={
                "deepseek-chat": (0.27, 1.10),
                "deepseek-reasoner": (0.55, 2.19),
            },
        ),
        ProviderPreset(
            name="xai",
            label="xAI (Grok)",
            base_url="https://api.x.ai/v1",
            default_model="grok-3-mini",
            key_url="https://console.x.ai/",
            free_tier="Khong (thinh thoang co credit dung thu)",
            pricing={
                "grok-3-mini": (0.30, 0.50),
                "grok-3": (3.00, 15.00),
            },
        ),
        ProviderPreset(
            name="mistral",
            label="Mistral AI",
            base_url="https://api.mistral.ai/v1",
            default_model="mistral-small-latest",
            key_url="https://console.mistral.ai/api-keys",
            free_tier="Co — free tier (La Plateforme, gioi han rate)",
            pricing={
                "mistral-small-latest": (0.10, 0.30),
                "mistral-medium-latest": (0.40, 2.00),
                "mistral-large-latest": (2.00, 6.00),
            },
        ),
        ProviderPreset(
            name="ollama",
            label="Ollama (chay LOCAL, khong can mang)",
            base_url="http://localhost:11434/v1",
            default_model="llama3.2",
            key_url="https://ollama.com/download",
            free_tier="Mien phi — model chay tren may ban",
            pricing={},  # local: moi model gia 0
            requires_key=False,
            local=True,
        ),
    ]
}


def mask_key(key: str | None) -> str:
    """Mask API key khi in ra man hinh/log: 6 ky tu dau + "..." — KHONG BAO GIO in full."""
    if not key:
        return "(chua dat)"
    prefix = key[:6] if len(key) > 8 else key[:2]
    return prefix + "..."


def missing_key_message(preset: ProviderPreset) -> str:
    """Thong bao thieu key: chi dung bien env + URL trang lay key cua DUNG hang do."""
    return (
        f"Thieu API key cho provider '{preset.name}' ({preset.label}): "
        f"dat {preset.env_key} trong .env.\n"
        f"  -> Lay key tai: {preset.key_url}\n"
        f"  -> Hoac chay wizard: python -m laplace.llm.setup\n"
        f"  (hoac chuyen LAPLACE_LLM_PROVIDER=mock de chay khong can key)"
    )
