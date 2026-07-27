"""Content Studio web/API flow with an injected offline writer."""

from fastapi.testclient import TestClient

from laplace.llm.mock import MockLLM
from laplace.models import User
from laplace.social.content_generation import ContentGenerationService
from laplace.social.models import AffiliateProduct, MediaAsset, SocialPost
from laplace.web.app import create_app
from laplace.web.settings_page import CSRF_TOKEN


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


def _seed(session):
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


def _loopback_client():
    return TestClient(
        create_app(),
        client=("127.0.0.1", 50000),
        base_url="http://127.0.0.1:8010",
        follow_redirects=False,
    )


def _form(user, media, product):
    return {
        "csrf": CSRF_TOKEN,
        "user_id": str(user.id),
        "platform": "facebook",
        "affiliate_product_id": str(product.id),
        "media_asset_id": str(media.id),
        "style_id": "deal_ngan_gon",
        "audience": "người làm văn phòng",
        "product_facts": (
            "Dung tích 500 ml. Chất liệu thép không gỉ theo thông tin nhà bán. "
            "Có nắp vặn và ba màu."
        ),
    }


def test_content_studio_lists_six_styles_and_free_model(session):
    _seed(session)

    with _loopback_client() as client:
        response = client.get("/social/content")

    assert response.status_code == 200
    assert "Content Studio" in response.text
    assert "review_that_tha" in response.text
    assert "Review thật thà" in response.text
    assert "Script video ngắn" in response.text
    assert "openrouter/free" in response.text
    assert 'href="/evals"' not in response.text


def test_content_studio_generates_then_approves_draft(session, monkeypatch):
    user, media, product = _seed(session)
    service = ContentGenerationService(MockLLM([_valid_copy()]))
    from laplace.web.social import views

    monkeypatch.setattr(views, "get_content_generation_service", lambda: service)

    with _loopback_client() as client:
        generated = client.post("/social/content/generate", data=_form(user, media, product))
        assert generated.status_code == 303
        location = generated.headers["location"]
        preview = client.get(location)
        assert preview.status_code == 200
        assert "draft mới" in preview.text
        assert "Bình 500 ml gọn cho bàn làm việc" in preview.text
        post_id = int(location.rsplit("=", 1)[1])

        approved = client.post(
            f"/social/content/{post_id}/approve",
            data={"csrf": CSRF_TOKEN, "user_id": str(user.id)},
        )

    assert approved.status_code == 303
    session.expire_all()
    assert session.get(SocialPost, post_id).status == "approved"


def test_content_studio_rejects_missing_csrf(session):
    user, media, product = _seed(session)
    form = _form(user, media, product)
    form["csrf"] = "wrong"

    with _loopback_client() as client:
        response = client.post("/social/content/generate", data=form)

    assert response.status_code == 403
    assert "CSRF" in response.text


def test_content_generation_api_and_style_catalog(session, monkeypatch):
    user, media, product = _seed(session)
    service = ContentGenerationService(MockLLM([_valid_copy()]))
    from laplace.web.social import api

    monkeypatch.setattr(api, "get_content_generation_service", lambda: service)
    payload = {
        "user_id": user.id,
        "platform": "facebook",
        "affiliate_product_id": product.id,
        "media_asset_id": media.id,
        "style_id": "deal_ngan_gon",
        "audience": "người làm văn phòng",
        "product_facts": (
            "Dung tích 500 ml. Chất liệu thép không gỉ theo thông tin nhà bán. "
            "Có nắp vặn và ba màu."
        ),
    }

    with TestClient(create_app()) as client:
        styles = client.get("/api/social/content/styles")
        generated = client.post("/api/social/content/generate", json=payload)

    assert styles.status_code == 200
    assert len(styles.json()["styles"]) == 6
    assert generated.status_code == 201
    assert generated.json()["status"] == "draft"
    assert generated.json()["model"] == "mock"
