"""Owner-scoped affiliate metrics for dashboards and reports."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from laplace.social.models import (
    AffiliateEvent,
    AffiliateProduct,
    SocialAccount,
    SocialPost,
)

DEFAULT_MAX_DAYS = 14
DEFAULT_TOP_LIMIT = 10


def _money(value: Decimal, currency: str) -> str:
    if currency == "VND":
        return f"{value:,.0f} ₫"
    return f"{value:,.2f} {currency}"


def _integer(value: int) -> str:
    return f"{value:,}"


def _percentage(numerator: int, denominator: int) -> float:
    return numerator * 100.0 / denominator if denominator else 0.0


def _optional_percentage(numerator: int, denominator: int) -> float | None:
    return numerator * 100.0 / denominator if denominator else None


def _optional_epc(commission: Decimal, clicks: int) -> Decimal | None:
    return commission / clicks if clicks else None


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "Hôm nay"
    if (today - day).days == 1:
        return "Hôm qua"
    return day.strftime("%d/%m/%Y")


def _empty_bucket(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "views": 0,
        "clicks": 0,
        "commission_count": 0,
        "commission": Decimal(0),
    }


def _add_event(bucket: dict[str, Any], event: Any, currency: str) -> None:
    if event.event_type == "view":
        bucket["views"] += 1
    elif event.event_type == "click":
        bucket["clicks"] += 1
    elif event.event_type == "commission" and event.currency == currency:
        bucket["commission_count"] += 1
        bucket["commission"] += Decimal(str(event.amount or 0))


def _present_bucket(bucket: dict[str, Any], currency: str) -> dict[str, Any]:
    views = int(bucket["views"])
    clicks = int(bucket["clicks"])
    commission_count = int(bucket["commission_count"])
    commission = Decimal(bucket["commission"])
    epc = _optional_epc(commission, clicks)
    conversion = _optional_percentage(commission_count, clicks)
    return {
        **bucket,
        "commission": float(commission),
        "ctr": _percentage(clicks, views),
        "conversion_rate": conversion,
        "commission_display": _money(commission, currency),
        "epc": float(epc) if epc is not None else None,
        "epc_display": _money(epc, currency) if epc is not None else "—",
        "conversion_display": f"{conversion:.1f}%" if conversion is not None else "—",
    }


def _top_rows(
    buckets: dict[Any, dict[str, Any]],
    *,
    currency: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = [_present_bucket(bucket, currency) for bucket in buckets.values()]
    return sorted(
        rows,
        key=lambda item: (
            float(item["commission"]),
            int(item["clicks"]),
            int(item["views"]),
            str(item["label"]),
        ),
        reverse=True,
    )[:limit]


def _names_for_ids(
    session: Session,
    model: type,
    label_column: Any,
    ids: set[int],
    user_id: int,
) -> dict[int, str]:
    if not ids:
        return {}
    rows = session.execute(
        select(model.id, label_column).where(
            model.id.in_(ids),
            model.user_id == user_id,
        )
    ).all()
    return {int(row[0]): str(row[1]) for row in rows}


def _empty_report(
    *,
    user_id: int | None,
    currency: str,
    timezone_name: str,
) -> dict[str, Any]:
    return {
        "empty": True,
        "owner_user_id": user_id,
        "currency": currency,
        "timezone": timezone_name,
        "kpis": (
            {"k": "Lượt xem", "v": "0", "hint": "đã ghi nhận"},
            {"k": "Lượt bấm", "v": "0", "hint": "affiliate click"},
            {"k": "CTR", "v": "0.0%", "hint": "click / view"},
            {"k": "Hoa hồng", "v": _money(Decimal(0), currency), "hint": "0 lượt"},
            {"k": "Hoa hồng / click", "v": "—", "hint": "chưa có click"},
            {"k": "Tỷ lệ chuyển đổi", "v": "—", "hint": "chưa có click"},
        ),
        "daily_rows": [],
        "content_rows": [],
        "product_rows": [],
        "account_rows": [],
        "time_rows": [],
    }


def collect_affiliate_analytics(
    session: Session,
    *,
    user_id: int | None,
    currency: str,
    timezone_name: str,
    max_days: int = DEFAULT_MAX_DAYS,
    top_limit: int = DEFAULT_TOP_LIMIT,
) -> dict[str, Any]:
    """Return one owner's normalized funnel and breakdowns.

    ``None`` EPC is intentional when no click denominator exists. Readers must
    not interpret missing traffic as a measured zero EPC.
    """
    currency = currency.strip().upper()
    if not 3 <= len(currency) <= 8:
        raise ValueError("currency must contain 3-8 characters")
    if max_days < 1 or top_limit < 1:
        raise ValueError("max_days and top_limit must be positive")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {timezone_name}") from exc
    if user_id is None:
        return _empty_report(
            user_id=None,
            currency=currency,
            timezone_name=timezone_name,
        )

    events = session.execute(
        select(
            AffiliateEvent.event_type,
            AffiliateEvent.amount,
            AffiliateEvent.currency,
            AffiliateEvent.social_post_id,
            AffiliateEvent.affiliate_product_id,
            AffiliateEvent.social_account_id,
            AffiliateEvent.occurred_at,
        )
        .where(AffiliateEvent.user_id == user_id)
        .order_by(AffiliateEvent.occurred_at, AffiliateEvent.id)
    ).all()
    if not events:
        return _empty_report(
            user_id=user_id,
            currency=currency,
            timezone_name=timezone_name,
        )

    post_names = _names_for_ids(
        session,
        SocialPost,
        SocialPost.title,
        {event.social_post_id for event in events if event.social_post_id is not None},
        user_id,
    )
    product_names = _names_for_ids(
        session,
        AffiliateProduct,
        AffiliateProduct.product_name,
        {
            event.affiliate_product_id
            for event in events
            if event.affiliate_product_id is not None
        },
        user_id,
    )
    account_names = _names_for_ids(
        session,
        SocialAccount,
        SocialAccount.display_name,
        {
            event.social_account_id
            for event in events
            if event.social_account_id is not None
        },
        user_id,
    )

    total = _empty_bucket("Tổng")
    daily: dict[date, dict[str, Any]] = {}
    by_content: dict[int | None, dict[str, Any]] = {}
    by_product: dict[int | None, dict[str, Any]] = {}
    by_account: dict[int | None, dict[str, Any]] = {}
    by_hour: dict[int, dict[str, Any]] = {}

    for event in events:
        _add_event(total, event, currency)
        occurred_at = event.occurred_at
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        local = occurred_at.astimezone(timezone)

        day_bucket = daily.setdefault(local.date(), _empty_bucket(""))
        _add_event(day_bucket, event, currency)

        content_bucket = by_content.setdefault(
            event.social_post_id,
            _empty_bucket(post_names.get(event.social_post_id, "Không gắn nội dung")),
        )
        _add_event(content_bucket, event, currency)

        product_bucket = by_product.setdefault(
            event.affiliate_product_id,
            _empty_bucket(
                product_names.get(event.affiliate_product_id, "Không gắn sản phẩm")
            ),
        )
        _add_event(product_bucket, event, currency)

        account_bucket = by_account.setdefault(
            event.social_account_id,
            _empty_bucket(account_names.get(event.social_account_id, "Không gắn tài khoản")),
        )
        _add_event(account_bucket, event, currency)

        hour_bucket = by_hour.setdefault(
            local.hour,
            _empty_bucket(f"{local.hour:02d}:00–{local.hour:02d}:59"),
        )
        _add_event(hour_bucket, event, currency)

    total_presented = _present_bucket(total, currency)
    today = datetime.now(timezone).date()
    days = sorted(daily)[-max_days:]
    max_traffic = max(
        (max(int(daily[day]["views"]), int(daily[day]["clicks"])) for day in days),
        default=1,
    )
    max_traffic = max(max_traffic, 1)
    max_commission = max(
        (Decimal(daily[day]["commission"]) for day in days),
        default=Decimal(1),
    )
    max_commission = max(max_commission, Decimal(1))
    daily_rows: list[dict[str, Any]] = []
    for day in days:
        row = _present_bucket(daily[day], currency)
        commission = Decimal(str(row["commission"]))
        daily_rows.append(
            {
                **row,
                "day": _day_label(day, today),
                "view_width": round(int(row["views"]) * 100 / max_traffic, 1),
                "click_width": round(int(row["clicks"]) * 100 / max_traffic, 1),
                "commission_width": round(float(commission * 100 / max_commission), 1),
            }
        )

    clicks = int(total_presented["clicks"])
    commission_count = int(total_presented["commission_count"])
    epc = total_presented["epc"]
    conversion = total_presented["conversion_rate"]
    return {
        "empty": False,
        "owner_user_id": user_id,
        "currency": currency,
        "timezone": timezone_name,
        "kpis": (
            {
                "k": "Lượt xem",
                "v": _integer(int(total_presented["views"])),
                "hint": "đã ghi nhận",
            },
            {
                "k": "Lượt bấm",
                "v": _integer(clicks),
                "hint": "affiliate click",
            },
            {
                "k": "CTR",
                "v": f"{float(total_presented['ctr']):.1f}%",
                "hint": "click / view",
            },
            {
                "k": "Hoa hồng",
                "v": total_presented["commission_display"],
                "hint": f"{commission_count} lượt ghi nhận",
            },
            {
                "k": "Hoa hồng / click",
                "v": total_presented["epc_display"],
                "hint": "EPC" if epc is not None else "chưa có click",
            },
            {
                "k": "Tỷ lệ chuyển đổi",
                "v": f"{conversion:.1f}%" if conversion is not None else "—",
                "hint": "hoa hồng / click" if clicks else "chưa có click",
            },
        ),
        "daily_rows": daily_rows,
        "content_rows": _top_rows(by_content, currency=currency, limit=top_limit),
        "product_rows": _top_rows(by_product, currency=currency, limit=top_limit),
        "account_rows": _top_rows(by_account, currency=currency, limit=top_limit),
        "time_rows": _top_rows(by_hour, currency=currency, limit=top_limit),
    }
