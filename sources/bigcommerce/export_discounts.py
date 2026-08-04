import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sources.bigcommerce.bigcommerce_client import BigCommerceClient

logger = logging.getLogger("bigcommerce_export_discounts")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_basic_action(rule: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    action = rule.get("action") or {}
    cart_value = action.get("cart_value")
    if not cart_value:
        return None

    discount = cart_value.get("discount") or {}
    discount_type = str(discount.get("type") or "").strip().lower()

    if discount_type in ("percentage", "percentage_discount", "percentage_amount"):
        pct = discount.get("amount")
        if pct is None:
            pct = discount.get("percentage_amount")
        if pct is None:
            return None
        try:
            return {"kind": "percentage", "percentage": float(pct) / 100.0}
        except (TypeError, ValueError):
            return None

    if discount_type in ("fixed_amount", "fixed_amount_off", "price_discount", "money"):
        amt = discount.get("amount")
        if amt is None:
            return None
        return {"kind": "amount", "amount": str(amt), "currency": discount.get("currency_code")}

    if discount_type in ("free_shipping", "shipping"):
        return {"kind": "free_shipping"}

    return None


def fetch_promotions(client: BigCommerceClient):
    return client.get_paginated_v3("promotions")


def fetch_promotion_code(client: BigCommerceClient, promotion_id: Any) -> Optional[str]:
    try:
        codes = list(client.get_paginated_v3(f"promotions/{promotion_id}/codes"))
    except Exception:
        logger.warning("Could not fetch codes for promotion %s", promotion_id, exc_info=True)
        return None
    for c in codes:
        code = c.get("code")
        if code:
            return code
    return None


def compute_status(promotion: Dict[str, Any]) -> str:
    bc_status = str(promotion.get("status") or "").upper()
    if bc_status not in ("ENABLED",):
        return "EXPIRED"

    now = datetime.now(timezone.utc)
    starts_at = _parse_iso(promotion.get("start_date"))
    ends_at = _parse_iso(promotion.get("end_date"))
    if starts_at and starts_at > now:
        return "SCHEDULED"
    if ends_at and ends_at < now:
        return "EXPIRED"
    return "ACTIVE"


def map_promotion(client: BigCommerceClient, promotion: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rules = promotion.get("rules") or []
    action = None
    for rule in rules:
        action = extract_basic_action(rule)
        if action is not None:
            break

    if action is None:
        return None

    redemption_type = str(promotion.get("redemption_type") or "").upper()
    is_code = redemption_type == "CODE"

    if action["kind"] == "free_shipping":
        typename = "DiscountCodeFreeShipping" if is_code else "DiscountAutomaticFreeShipping"
        customer_gets = None
        maximum_shipping_price = None
    else:
        typename = "DiscountCodeBasic" if is_code else "DiscountAutomaticBasic"
        if action["kind"] == "percentage":
            value = {"kind": "percentage", "percentage": action["percentage"]}
        else:
            value = {
                "kind": "amount",
                "amount": action["amount"],
                "currency": action.get("currency"),
                "applies_on_each_item": False,
            }
        customer_gets = {
            "value": value,
            "items": {"kind": "all"},
            "applies_on_one_time_purchase": True,
            "applies_on_subscription": False,
        }
        maximum_shipping_price = None

    code = fetch_promotion_code(client, promotion.get("id")) if is_code else None
    max_uses_per_customer = promotion.get("max_uses_per_customer")

    return {
        "typename": typename,
        "title": promotion.get("name"),
        "status": compute_status(promotion),
        "starts_at": promotion.get("start_date"),
        "ends_at": promotion.get("end_date"),
        "code": code,
        "combines_with": {
            "orderDiscounts": False,
            "productDiscounts": False,
            "shippingDiscounts": False,
        },
        "applies_once_per_customer": (max_uses_per_customer == 1) if max_uses_per_customer is not None else None,
        "usage_limit": promotion.get("max_uses"),
        "customer_gets": customer_gets,
        "customer_buys": None,
        "customer_gets_bxgy": None,
        "minimum_requirement": None,
        "maximum_shipping_price": maximum_shipping_price,
        "uses_per_order_limit": None,
        "metafields": [],
    }


def export_discounts(client: BigCommerceClient) -> Dict[str, Any]:
    exported: List[Dict[str, Any]] = []
    skipped_complex = 0

    for promotion in fetch_promotions(client):
        try:
            mapped = map_promotion(client, promotion)
        except Exception:
            logger.exception("Failed to export promotion %s", promotion.get("id"))
            continue

        if mapped is None:
            skipped_complex += 1
            logger.info(
                "Skipping promotion %s (%s): no simple Cart Value action found "
                "(likely a tiered/BOGO/Cart Item rule) -- not decoded into a Bxgy shape",
                promotion.get("id"),
                promotion.get("name"),
            )
            continue

        exported.append(mapped)

    logger.info(
        "Exported %s discount(s) (%s skipped as too complex to map cleanly)",
        len(exported),
        skipped_complex,
    )
    return {"discounts": exported}
