"""Built-in Vietnamese writing styles and deterministic copy checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class WritingStyle:
    id: str
    name: str
    description: str
    structure: tuple[str, ...]
    tone: str
    min_chars: int
    max_chars: int
    max_hashtags: int
    max_emojis: int
    cta: str

    def public_dict(self) -> dict:
        return asdict(self)


STYLES: dict[str, WritingStyle] = {
    style.id: style
    for style in (
        WritingStyle(
            id="review_that_tha",
            name="Review thật thà",
            description="Nói rõ ưu, nhược điểm và người phù hợp; không giả trải nghiệm.",
            structure=("Hook", "Điểm đáng chú ý", "Điểm cần cân nhắc", "Phù hợp với ai", "CTA"),
            tone="gần gũi, cân bằng, cụ thể, không phóng đại",
            min_chars=350,
            max_chars=900,
            max_hashtags=5,
            max_emojis=3,
            cta="mời người đọc xem thông tin chi tiết, không gây áp lực",
        ),
        WritingStyle(
            id="deal_ngan_gon",
            name="Deal ngắn gọn",
            description="Đi thẳng lợi ích chính và CTA, phù hợp bài deal nhanh.",
            structure=("Hook", "Lợi ích chính", "Điều kiện/giới hạn", "CTA"),
            tone="nhanh, rõ, thân thiện",
            min_chars=180,
            max_chars=500,
            max_hashtags=4,
            max_emojis=3,
            cta="khuyến khích kiểm tra giá và điều kiện hiện tại",
        ),
        WritingStyle(
            id="ke_chuyen_trai_nghiem",
            name="Kể chuyện tình huống",
            description="Dùng tình huống giả định, tuyệt đối không bịa đã mua hoặc đã dùng.",
            structure=("Tình huống", "Vấn đề", "Cách sản phẩm hỗ trợ", "Lưu ý", "CTA"),
            tone="tự nhiên, có nhịp kể chuyện, minh bạch",
            min_chars=400,
            max_chars=950,
            max_hashtags=5,
            max_emojis=3,
            cta="mời người đọc tự đối chiếu nhu cầu",
        ),
        WritingStyle(
            id="so_sanh_chon_mua",
            name="So sánh để chọn mua",
            description="So sánh theo tiêu chí và kết luận từng nhóm nhu cầu.",
            structure=("Nhu cầu", "Các tiêu chí", "Khác biệt", "Ai nên chọn gì", "CTA"),
            tone="phân tích, dễ hiểu, không thiên vị quá mức",
            min_chars=450,
            max_chars=1_000,
            max_hashtags=5,
            max_emojis=2,
            cta="mời xem thông số và giá cập nhật",
        ),
        WritingStyle(
            id="huong_dan_checklist",
            name="Hướng dẫn checklist",
            description="Biến thông tin sản phẩm thành checklist kiểm tra trước khi mua.",
            structure=("Mục tiêu", "Checklist", "Lưu ý", "CTA"),
            tone="hữu ích, mạch lạc, ưu tiên bullet ngắn",
            min_chars=350,
            max_chars=900,
            max_hashtags=5,
            max_emojis=2,
            cta="mời lưu bài hoặc xem thông tin chi tiết",
        ),
        WritingStyle(
            id="video_ngan",
            name="Script video ngắn",
            description="Hook nhanh, ba ý chính và CTA cho video ngắn.",
            structure=("Hook 2 giây", "Ba ý chính", "Lưu ý", "CTA"),
            tone="nói tự nhiên, câu ngắn, dễ đọc thành tiếng",
            min_chars=180,
            max_chars=450,
            max_hashtags=4,
            max_emojis=2,
            cta="CTA một câu, không thúc ép",
        ),
    )
}

_BANNED_PHRASES = (
    "cam kết 100%",
    "đảm bảo 100%",
    "tốt nhất thị trường",
    "số 1 thị trường",
    "chắc chắn hiệu quả",
    "chữa khỏi",
    "kiếm tiền chắc chắn",
)


@dataclass(frozen=True, slots=True)
class CopyQuality:
    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def get_style(style_id: str) -> WritingStyle:
    try:
        return STYLES[style_id]
    except KeyError as exc:
        raise ValueError(f"unknown writing style: {style_id}") from exc


def _emoji_count(text: str) -> int:
    return sum(
        1
        for char in text
        if (
            "\U0001f300" <= char <= "\U0001faff"
            or "\u2600" <= char <= "\u27bf"
        )
    )


def _contains_url(text: str) -> bool:
    for token in text.replace("\n", " ").split():
        candidate = token.strip("()[]{}<>.,;!?\"'")
        parsed = urlsplit(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return True
    return False


def check_copy(
    *,
    title: str,
    caption: str,
    hashtags: list[str],
    style: WritingStyle,
) -> CopyQuality:
    errors: list[str] = []
    warnings: list[str] = []
    cleaned_title = title.strip()
    cleaned_caption = caption.strip()
    folded = f"{cleaned_title}\n{cleaned_caption}".casefold()

    if not cleaned_title:
        errors.append("title is blank")
    if len(cleaned_title) > 120:
        errors.append("title exceeds 120 characters")
    if len(cleaned_caption) < style.min_chars:
        errors.append(f"caption is shorter than {style.min_chars} characters")
    if len(cleaned_caption) > style.max_chars:
        errors.append(f"caption exceeds {style.max_chars} characters")
    if len(hashtags) > style.max_hashtags:
        errors.append(f"hashtags exceed style limit of {style.max_hashtags}")
    if _emoji_count(f"{cleaned_title}\n{cleaned_caption}") > style.max_emojis:
        errors.append(f"emoji exceed style limit of {style.max_emojis}")
    if _contains_url(f"{cleaned_title}\n{cleaned_caption}"):
        errors.append("copy must not contain URLs; Arya_Tool attaches the affiliate link")
    for phrase in _BANNED_PHRASES:
        if phrase in folded:
            errors.append(f"banned claim: {phrase}")
    if not any(term in folded for term in ("phù hợp", "cân nhắc", "kiểm tra", "tham khảo")):
        warnings.append("copy could include a clearer buyer-fit or verification note")

    return CopyQuality(valid=not errors, errors=tuple(errors), warnings=tuple(warnings))
