"""Tests cho T15 dot 1: preset registry 8 hang + get_provider theo preset
+ bang gia per-preset + wizard setup (.env) + check. KHONG goi mang."""

import stat

import pytest

from laplace.config import get_settings
from laplace.llm.base import (
    MissingAPIKeyError,
    get_provider,
    resolve_provider_config,
)
from laplace.llm.budget import ENV_MAX_COST
from laplace.llm.mock import MockLLM
from laplace.llm.openai_provider import (
    _WARNED_UNKNOWN_MODELS,
    GEMINI_BASE_URL,
    OpenAIProvider,
    _compute_cost,
)
from laplace.llm.presets import PRESETS, mask_key, missing_key_message
from laplace.llm.setup import (
    get_env_value,
    read_env_lines,
    set_env_line,
    validate_key,
    write_env,
)

EXPECTED_PRESETS = {
    "gemini", "openai", "groq", "openrouter", "deepseek", "xai", "mistral", "ollama",
}


@pytest.fixture()
def _no_budget(monkeypatch):
    """Tat budget wrap de soi truc tiep OpenAIProvider ben trong."""
    monkeypatch.setenv(ENV_MAX_COST, "0")


@pytest.fixture()
def _clean_llm_model(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_model", None)


# ------------------------------------------------------------------ presets

def test_presets_du_8_hang():
    assert set(PRESETS) == EXPECTED_PRESETS


def test_preset_schema_hop_le():
    for name, p in PRESETS.items():
        assert p.name == name
        assert p.label and p.default_model and p.key_url.startswith("http")
        if name == "openai":
            assert p.base_url is None  # dung mac dinh SDK
        else:
            assert p.base_url.startswith("http")
        if p.requires_key:
            assert p.env_key == f"LAPLACE_{name.upper()}_API_KEY"
        else:
            assert p.env_key is None
        for model, price in p.pricing.items():
            assert isinstance(model, str) and len(price) == 2
            assert all(isinstance(x, float) and x >= 0 for x in price)


def test_preset_gemini_khop_hang_so_cu():
    assert PRESETS["gemini"].base_url == GEMINI_BASE_URL


def test_ollama_khong_can_key_va_local():
    p = PRESETS["ollama"]
    assert not p.requires_key and p.local
    assert "11434" in p.base_url


def test_default_model_cua_preset_co_gia_tru_openrouter_ollama():
    # openrouter/auto va model local ollama gia tuy model -> cho phep khong co gia
    for name, p in PRESETS.items():
        if name in ("openrouter", "ollama"):
            continue
        assert p.default_model in p.pricing, f"{name}: default model chua co gia"


# ------------------------------------------------------------- get_provider

def test_get_provider_dung_preset_groq(monkeypatch, _no_budget, _clean_llm_model):
    monkeypatch.setattr(get_settings(), "groq_api_key", "gsk_fake_khong_goi_that")
    provider = get_provider("groq")
    assert isinstance(provider, OpenAIProvider)
    assert provider.name == "groq"
    assert provider.model == "llama-3.3-70b-versatile"
    assert "api.groq.com/openai/v1" in str(provider._client.base_url)


def test_get_provider_openai_giu_hanh_vi_cu(monkeypatch, _no_budget, _clean_llm_model):
    monkeypatch.setattr(get_settings(), "openai_api_key", "sk-fake")
    provider = get_provider("openai")
    assert provider.name == "openai"
    assert provider.model == "gpt-4o-mini"  # tu settings.openai_model nhu cu
    assert "api.openai.com" in str(provider._client.base_url)


def test_get_provider_gemini_giu_hanh_vi_cu(monkeypatch, _no_budget, _clean_llm_model):
    monkeypatch.setattr(get_settings(), "gemini_api_key", "AIzafake")
    provider = get_provider("gemini")
    assert provider.name == "gemini"
    assert provider.model == get_settings().gemini_model
    assert "generativelanguage.googleapis.com" in str(provider._client.base_url)


def test_get_provider_llm_model_override(monkeypatch, _no_budget):
    monkeypatch.setattr(get_settings(), "deepseek_api_key", "sk-fake")
    monkeypatch.setattr(get_settings(), "llm_model", "model-tuy-chon")
    provider = get_provider("deepseek")
    assert provider.model == "model-tuy-chon"


def test_get_provider_ollama_khong_can_key(monkeypatch, _no_budget, _clean_llm_model):
    provider = get_provider("ollama")
    assert provider.name == "ollama"
    assert provider.model == "llama3.2"
    assert "localhost:11434" in str(provider._client.base_url)


def test_get_provider_mock_va_ten_la_van_ve_mock():
    assert isinstance(get_provider("mock"), MockLLM)
    assert isinstance(get_provider("khong-ton-tai"), MockLLM)
    assert isinstance(get_provider(), MockLLM)  # conftest ep llm_provider=mock


def test_thieu_key_bao_kem_url_lay_key_dung_hang(monkeypatch):
    monkeypatch.setattr(get_settings(), "groq_api_key", None)
    with pytest.raises(MissingAPIKeyError) as exc:
        get_provider("groq")
    msg = str(exc.value)
    assert "console.groq.com/keys" in msg  # URL trang lay key dung hang
    assert "LAPLACE_GROQ_API_KEY" in msg
    # message cua tung hang phai tro dung URL hang do
    assert "aistudio.google.com" in missing_key_message(PRESETS["gemini"])
    assert "platform.deepseek.com" in missing_key_message(PRESETS["deepseek"])


def test_resolve_provider_config(monkeypatch, _clean_llm_model):
    monkeypatch.setattr(get_settings(), "mistral_api_key", "mk-fake")
    preset, api_key, model = resolve_provider_config("mistral")
    assert preset.name == "mistral"
    assert api_key == "mk-fake"
    assert model == "mistral-small-latest"


# ------------------------------------------------------------------ pricing

def test_compute_cost_dung_bang_gia_preset():
    pricing = PRESETS["deepseek"].pricing
    cost = _compute_cost("deepseek-chat", 1_000_000, 1_000_000, pricing)
    assert cost == pytest.approx(0.27 + 1.10)


def test_model_la_gia_0_va_warning_1_lan(caplog):
    _WARNED_UNKNOWN_MODELS.discard("model-bi-an")
    with caplog.at_level("WARNING", logger="laplace.llm.openai_provider"):
        assert _compute_cost("model-bi-an", 1000, 1000, {}, warn_unknown=True) == 0.0
        assert _compute_cost("model-bi-an", 1000, 1000, {}, warn_unknown=True) == 0.0
    warnings = [r for r in caplog.records if "model-bi-an" in r.message]
    assert len(warnings) == 1  # chi warning DUY NHAT 1 lan


def test_model_local_khong_warning(caplog):
    _WARNED_UNKNOWN_MODELS.discard("llama3.2")
    with caplog.at_level("WARNING", logger="laplace.llm.openai_provider"):
        assert _compute_cost("llama3.2", 1000, 1000, {}, warn_unknown=False) == 0.0
    assert not caplog.records


# ----------------------------------------------------------------- mask key

def test_mask_key():
    assert mask_key("gsk_abcdef123456789") == "gsk_ab..."
    assert mask_key(None) == "(chua dat)"
    assert mask_key("") == "(chua dat)"
    # key ngan: khong duoc lo toan bo
    assert mask_key("abc123") == "ab..."
    for key in ("gsk_abcdef123456789", "abc123"):
        assert key not in mask_key(key)


# ------------------------------------------------------------ ghi file .env

SAMPLE_ENV = """# Comment dau file phai giu nguyen
LAPLACE_APP_NAME=laplace-demon

# LLM provider
LAPLACE_LLM_PROVIDER=mock

# Key Groq (comment nay cung phai giu)
LAPLACE_GROQ_API_KEY=
LAPLACE_TELEGRAM_BOT_TOKEN=abc:xyz
"""


def _write_sample(tmp_path):
    env = tmp_path / ".env"
    env.write_text(SAMPLE_ENV, encoding="utf-8")
    return env


def test_set_env_line_sua_dung_dong_giu_comment(tmp_path):
    env = _write_sample(tmp_path)
    lines = read_env_lines(env)
    lines = set_env_line(lines, "LAPLACE_GROQ_API_KEY", "gsk_moi")
    lines = set_env_line(lines, "LAPLACE_LLM_PROVIDER", "groq")
    assert get_env_value(lines, "LAPLACE_GROQ_API_KEY") == "gsk_moi"
    assert get_env_value(lines, "LAPLACE_LLM_PROVIDER") == "groq"
    # comment + dong khac giu nguyen, khong nhan doi dong
    assert lines.count("# Comment dau file phai giu nguyen") == 1
    assert "# Key Groq (comment nay cung phai giu)" in lines
    assert get_env_value(lines, "LAPLACE_TELEGRAM_BOT_TOKEN") == "abc:xyz"
    assert sum(1 for line in lines if line.startswith("LAPLACE_GROQ_API_KEY=")) == 1


def test_set_env_line_append_khi_chua_co():
    lines = set_env_line(["A=1"], "LAPLACE_XAI_API_KEY", "xai-key")
    assert get_env_value(lines, "LAPLACE_XAI_API_KEY") == "xai-key"
    assert lines[0] == "A=1"


def test_write_env_backup_va_chmod_600(tmp_path):
    env = _write_sample(tmp_path)
    lines = set_env_line(read_env_lines(env), "LAPLACE_GROQ_API_KEY", "gsk_moi")
    write_env(env, lines, backup=True)
    bak = tmp_path / ".env.bak"
    assert bak.exists()
    assert "LAPLACE_GROQ_API_KEY=" in bak.read_text()  # backup la ban CU
    assert "gsk_moi" not in bak.read_text()
    assert "LAPLACE_GROQ_API_KEY=gsk_moi" in env.read_text()
    for f in (env, bak):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_read_env_lines_file_chua_ton_tai(tmp_path):
    assert read_env_lines(tmp_path / "khong-co.env") == []


def test_get_env_value_phat_hien_key_da_co(tmp_path):
    env = _write_sample(tmp_path)
    lines = read_env_lines(env)
    assert get_env_value(lines, "LAPLACE_GROQ_API_KEY") == ""  # dong co nhung rong
    assert get_env_value(lines, "LAPLACE_TELEGRAM_BOT_TOKEN") == "abc:xyz"  # da co -> phai hoi
    assert get_env_value(lines, "LAPLACE_KHONG_CO") is None


# ------------------------------------------- validate/check (mock, khong mang)

@pytest.fixture()
def _fake_complete(monkeypatch):
    from laplace.llm.base import LLMResult

    def fake(self, messages, *, json_schema=None):
        return LLMResult(content="OK", model="fake-model-tra-loi")

    monkeypatch.setattr(OpenAIProvider, "complete", fake)


def test_validate_key_tra_latency_va_model(_fake_complete):
    latency_ms, model = validate_key(PRESETS["groq"], "gsk_fake")
    assert model == "fake-model-tra-loi"
    assert latency_ms >= 0


def test_check_preset_song_in_mask_khong_lo_key(monkeypatch, _fake_complete, capsys):
    from laplace.llm import check as check_mod

    monkeypatch.setattr(get_settings(), "groq_api_key", "gsk_bi_mat_tuyet_doi")
    monkeypatch.setattr(get_settings(), "llm_model", None)
    assert check_mod.check_preset(PRESETS["groq"]) is True
    out = capsys.readouterr().out
    assert "SONG" in out and "fake-model-tra-loi" in out
    assert "gsk_bi_mat_tuyet_doi" not in out  # key that khong bao gio duoc in
    assert "gsk_bi..." in out


def test_check_preset_chet_bao_url_lay_key(monkeypatch, capsys):
    from laplace.llm import check as check_mod

    def boom(preset, api_key, model=None):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(check_mod, "validate_key", boom)
    monkeypatch.setattr(get_settings(), "groq_api_key", "gsk_sai")
    assert check_mod.check_preset(PRESETS["groq"]) is False
    out = capsys.readouterr().out
    assert "CHET" in out and "console.groq.com/keys" in out
