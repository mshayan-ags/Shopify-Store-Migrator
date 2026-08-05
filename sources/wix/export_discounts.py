import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sources.wix.wix_client import WixClient

logger = logging.getLogger("wix_export_discounts")

PAGE_LIMIT = 100


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def compute_status(active: Optional[bool], starts_at: Optional[str], ends_at: Optional[str]) -> str:
    if active is False:
        return "EXPIRED"

    now = datetime.now(timezone.utc)
    starts = _parse_iso(starts_at)
    ends = _parse_iso(ends_at)
    if starts and starts > now:
        return "SCHEDULED"
    if ends and ends < now:
        return "EXPIRED"
    return "ACTIVE"


def extract_coupon_value(coupon: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    discount_type = coupon.get("discountType")

    if discount_type == "PercentOffRate":
        rate = coupon.get("percentOffRate")
        if rate is None:
            return None
        try:
            return {"kind": "percentage", "percentage": float(rate) / 100.0}
        except (TypeError, ValueError):
            return None

    if discount_type in ("MoneyOffAmount", "FixedPriceAmount"):
        amount = coupon.get("amount")
        if amount is None:
            return None
        return {"kind": "amount", "amount": str(amount), "currency": None}

    if discount_type == "FreeShipping":
        return {"kind": "free_shipping"}

    return None


def fetch_coupons(client: WixClient, page_limit: int = PAGE_LIMIT) -> List[Dict[str, Any]]:
    coupons: List[Dict[str, Any]] = []
    offset = 0

    while True:
        body = {"query": {"paging": {"limit": page_limit, "offset": offset}}}
        response = client.post("coupons/v1/coupons/query", json_body=body) or {}
        page = response.get("coupons") or []
        coupons.extend(page)

        if len(page) < page_limit:
            break
        offset += page_limit

    return coupons


def map_coupon(coupon: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = extract_coupon_value(coupon)
    if value is None:
        return None

    specification = coupon.get("specification") or {}
    usage_limit_info = coupon.get("usageLimit") or {}
    min_subtotal = specification.get("minSubtotal")

    if value["kind"] == "free_shipping":
        typename = "DiscountCodeFreeShipping"
        customer_gets = None
    else:
        typename = "DiscountCodeBasic"
        customer_gets = {
            "value": value,
            "items": {"kind": "all"},
            "applies_on_one_time_purchase": True,
            "applies_on_subscription": False,
        }

    minimum_requirement = None
    if min_subtotal is not None:
        minimum_requirement = {"kind": "subtotal", "amount": str(min_subtotal), "currency": None}

    return {
        "typename": typename,
        "title": coupon.get("name"),
        "status": compute_status(coupon.get("active"), coupon.get("startTime"), coupon.get("expirationTime")),
        "starts_at": coupon.get("startTime"),
        "ends_at": coupon.get("expirationTime"),
        "code": coupon.get("code"),
        "combines_with": {"orderDiscounts": False, "productDiscounts": False, "shippingDiscounts": False},
        "applies_once_per_customer": bool(usage_limit_info["limitedPerCustomer"]) if "limitedPerCustomer" in usage_limit_info else None,
        "usage_limit": usage_limit_info.get("maxUsagePerCoupon") if usage_limit_info.get("limitedToXUses") else None,
        "customer_gets": customer_gets,
        "customer_buys": None,
        "customer_gets_bxgy": None,
        "minimum_requirement": minimum_requirement,
        "maximum_shipping_price": None,
        "uses_per_order_limit": None,
        "metafields": [],
    }


def export_discounts(client: WixClient) -> Dict[str, Any]:
    coupons_raw = fetch_coupons(client)
    exported: List[Dict[str, Any]] = []
    skipped = 0

    for coupon in coupons_raw:
        try:
            mapped = map_coupon(coupon)
        except Exception:
            logger.exception("Failed to export coupon %s", coupon.get("id"))
            continue
        if mapped is None:
            skipped += 1
            continue
        exported.append(mapped)

    logger.info("Exported %s coupon discount(s) (%s skipped as unmapped discount type)", len(exported), skipped)
    return {"discounts": exported}


def fetch_discount_rules(client: WixClient, page_limit: int = PAGE_LIMIT) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    offset = 0

    while True:
        body = {"query": {"paging": {"limit": page_limit, "offset": offset}}}
        response = client.post("discount-rules/v1/discount-rules/query", json_body=body) or {}
        page = response.get("discountRules") or []
        rules.extend(page)

        if len(page) < page_limit:
            break
        offset += page_limit

    return rules


def extract_rule_value(rule: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    discount = rule.get("discount") or {}
    discount_type = discount.get("type")

    if discount_type == "PERCENT":
        rate = discount.get("value")
        if rate is None:
            return None
        try:
            return {"kind": "percentage", "percentage": float(rate) / 100.0}
        except (TypeError, ValueError):
            return None

    if discount_type in ("AMOUNT", "FIXED_PRICE"):
        amount = discount.get("value")
        if amount is None:
            return None
        return {"kind": "amount", "amount": str(amount), "currency": None}

    return None


def map_discount_rule(rule: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = extract_rule_value(rule)
    if value is None:
        return None

    return {
        "typename": "DiscountAutomaticBasic",
        "title": rule.get("name"),
        "status": compute_status(rule.get("active"), rule.get("startDate"), rule.get("endDate")),
        "starts_at": rule.get("startDate"),
        "ends_at": rule.get("endDate"),
        "code": None,
        "combines_with": {"orderDiscounts": False, "productDiscounts": False, "shippingDiscounts": False},
        "applies_once_per_customer": None,
        "usage_limit": None,
        "customer_gets": {
            "value": value,
            "items": {"kind": "all"},
            "applies_on_one_time_purchase": True,
            "applies_on_subscription": False,
        },
        "customer_buys": None,
        "customer_gets_bxgy": None,
        "minimum_requirement": None,
        "maximum_shipping_price": None,
        "uses_per_order_limit": None,
        "metafields": [],
    }


def export_discount_rules(client: WixClient) -> Dict[str, Any]:
    rules_raw = fetch_discount_rules(client)
    exported: List[Dict[str, Any]] = []
    skipped = 0

    for rule in rules_raw:
        try:
            mapped = map_discount_rule(rule)
        except Exception:
            logger.exception("Failed to export discount rule %s", rule.get("id"))
            continue
        if mapped is None:
            skipped += 1
            continue
        exported.append(mapped)

    logger.info("Exported %s discount rule(s) as automatic discounts (%s skipped as unmapped)", len(exported), skipped)
    return {"discounts": exported}
