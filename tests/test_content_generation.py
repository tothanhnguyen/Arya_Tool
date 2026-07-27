"""Tests for style presets and deterministic AI draft generation."""

from laplace.llm.mock import MockLLM
from laplace.models import User
from laplace.social.content_generation import (
    ContentGenerationService,
    GenerateDraftRequest,
)
from laplace.social.models import (
    AffiliateProduct,
    ContentGeneration,
    MediaAsset,
    SocialPost,
)
from laplace.social.styles import STYLES, check_copy, get_style


def _seed_context(session):
    user = User()
    session.add(user)
    session.flush()
    media = MediaAsset(
        user_id=user.id,
        type="image",
        local_path="/tmp/product.png",
        original_name="product.png",
        mime_type="image/png",
        size_bytes=10,
        sha256="a" * 64,
    )
    product = AffiliateProduct(
        user_id=user.id,
        network="Demo Network",
        merchant="Demo Shop",
        product_name="Bình giữ nhiệt A",
        product_url="https://example.test/product",
        affiliate_url="https://example.test/go",
    )
    session.add_all([media, product])
    session.commit()
    return user, media, product


def _request(user, media, product, style_id="deal_ngan_gon"):
    return GenerateDraftRequest(
        user_id=user.id,
        media_asset_id=media.id,
        affiliate_product_id=product.id,
        style_id=style_id,
        platform="facebook",
        audience="người làm văn phòng",
        product_facts=(
            "Dung tích 500 ml. Chất liệu thép không gỉ theo thông tin của nhà bán. "
            "Có nắp vặn và ba màu lựa chọn."
        ),
    )


def _valid_copy():
    return {
        "title": "Bình 500 ml gọn cho bàn làm việc",
        "caption": (
            "Nếu bạn cần một chiếc bình gọn để đặt cạnh máy tính, mẫu 500 ml này "
            "là lựa chọn đáng tham khảo. Theo thông tin từ nhà bán, bình dùng thép "
            "không gỉ, có nắp vặn và ba màu để lựa chọn. Dung tích vừa phải phù hợp "
            "với người làm văn phòng, nhưng bạn vẫn nên kiểm tra kích thước thực tế "
            "và hướng dẫn vệ sinh trước khi quyết định. Xem thêm thông tin để đối "
            "chiếu với nhu cầu của bạn."
        ),
        "hashtags": ["#BinhGiuNhiet", "#DoDungVanPhong"],
    }


def test_built_in_styles_and_quality_rules():
    assert len(STYLES) == 6
    style = get_style("deal_ngan_gon")
    valid = _valid_copy()

    report = check_copy(
        title=valid["title"],
        caption=valid["caption"],
        hashtags=valid["hashtags"],
        style=style,
    )
    blocked = check_copy(
        title="Tốt nhất thị trường",
        caption=(
            "Cam kết 100% hiệu quả. Xem https://bad.example ngay hôm nay vì đây "
            "là lựa chọn chắc chắn phù hợp với tất cả mọi người."
        )
        * 3,
        hashtags=[],
        style=style,
    )

    assert report.valid
    assert not blocked.valid
    assert any("banned claim" in error for error in blocked.errors)
    assert any("URLs" in error for error in blocked.errors)


def test_generator_creates_draft_and_redacted_audit(session):
    user, media, product = _seed_context(session)
    provider = MockLLM([_valid_copy()])

    generated = ContentGenerationService(provider).generate_draft(
        _request(user, media, product)
    )

    session.expire_all()
    post = session.get(SocialPost, generated.post_id)
    audit = session.query(ContentGeneration).filter_by(
        social_post_id=generated.post_id
    ).one()
    assert post is not None
    assert post.status == "draft"
    assert post.hashtags_json == _valid_copy()["hashtags"]
    assert generated.model == "mock"
    assert audit.style_id == "deal_ngan_gon"
    assert len(audit.prompt_fingerprint) == 64
    assert audit.quality_json["valid"] is True
    assert "prompt" not in audit.quality_json
    assert provider.calls[0]["json_schema"]["properties"]["caption"]


def test_generator_retries_copy_that_fails_rules(session, monkeypatch):
    user, media, product = _seed_context(session)
    invalid = {
        "title": "Deal",
        "caption": "Quá ngắn",
        "hashtags": [],
    }
    provider = MockLLM([invalid, _valid_copy()])
    from laplace.config import get_settings

    monkeypatch.setattr(get_settings(), "social_content_max_retries", 1)

    generated = ContentGenerationService(provider).generate_draft(
        _request(user, media, product)
    )

    assert generated.retries == 1
    assert len(provider.calls) == 2
    assert "Draft trước không đạt" in provider.calls[1]["messages"][1]["content"]


def test_generator_rejects_cross_user_assets(session):
    _user, media, product = _seed_context(session)
    other = User()
    session.add(other)
    session.commit()

    request = _request(other, media, product)

    try:
        ContentGenerationService(MockLLM([_valid_copy()])).generate_draft(request)
    except ValueError as exc:
        assert "media asset not found" in str(exc)
    else:
        raise AssertionError("cross-user media was accepted")
