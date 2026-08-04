import logging
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from sources.bigcommerce.bigcommerce_client import BigCommerceClient

logger = logging.getLogger("bigcommerce_export_orders")

FULFILLMENT_STATUS_MAP = {
    "Shipped": "FULFILLED",
    "Partially Shipped": "PARTIALLY_FULFILLED",
}

FINANCIAL_STATUS_MAP = {
    "Awaiting Payment": "PENDING",
    "Pending": "PENDING",
    "Awaiting Fulfillment": "PAID",
    "Awaiting Shipment": "PAID",
    "Awaiting Pickup": "PAID",
    "Shipped": "PAID",
    "Partially Shipped": "PAID",
    "Completed": "PAID",
    "Refunded": "REFUNDED",
    "Partially Refunded": "PARTIALLY_REFUNDED",
}


def _parse_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        logger.warning("Could not parse BigCommerce date %r", value)
        return None


def map_bc_address(addr: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not addr:
        return None
    return {
        "address1": addr.get("street_1") or None,
        "address2": addr.get("street_2") or None,
        "city": addr.get("city") or None,
        "company": addr.get("company") or None,
        "countryCodeV2": addr.get("country_iso2") or None,
        "firstName": addr.get("first_name") or None,
        "lastName": addr.get("last_name") or None,
        "phone": addr.get("phone") or None,
        "provinceCode": addr.get("state") or None,
        "zip": addr.get("zip") or None,
    }


def map_line_item(item: Dict[str, Any], currency: str) -> Dict[str, Any]:
    price = item.get("price_ex_tax")
    return {
        "sku": item.get("sku") or None,
        "title": item.get("name") or "",
        "variant_title": None,
        "vendor": None,
        "quantity": int(item.get("quantity") or 0),
        "taxable": bool(float(item.get("price_tax") or 0) > 0),
        "requires_shipping": not bool(item.get("is_free_shipping", False)),
        "price": str(price) if price is not None else "0.00",
        "currency": currency,
    }


def fetch_orders(client: BigCommerceClient):
    return client.get_paginated_v2("orders")


def fetch_line_items(client: BigCommerceClient, order_id: Any) -> List[Dict[str, Any]]:
    return list(client.get_paginated_v2(f"orders/{order_id}/products"))


def fetch_shipping_address(client: BigCommerceClient, order_id: Any) -> Optional[Dict[str, Any]]:
    addresses = list(client.get_paginated_v2(f"orders/{order_id}/shipping_addresses"))
    return addresses[0] if addresses else None


def map_order(
    order: Dict[str, Any],
    line_items_raw: List[Dict[str, Any]],
    shipping_address_raw: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    currency = order.get("currency_code") or "USD"
    billing = order.get("billing_address") or {}
    status = order.get("status")

    shipping_amount = order.get("shipping_cost_ex_tax")
    shipping_line = None
    if shipping_amount is not None:
        shipping_line = {
            "title": (shipping_address_raw or {}).get("shipping_method") or "Shipping",
            "amount": str(shipping_amount),
            "currency": currency,
        }

    return {
        "id": order.get("id"),
        "name": f"#{order.get('id')}",
        "email": billing.get("email") or None,
        "phone": billing.get("phone") or None,
        "note": order.get("customer_message") or None,
        "tags": [],
        "test": False,
        "processed_at": _parse_date(order.get("date_created")),
        "closed_at": None,
        "cancelled_at": _parse_date(order.get("date_modified")) if status == "Cancelled" else None,
        "cancel_reason": None,
        "po_number": None,
        "source_name": order.get("order_source"),
        "discount_codes": [],
        "custom_attributes": [],
        "currency": currency,
        "financial_status": FINANCIAL_STATUS_MAP.get(status),
        "fulfillment_status": FULFILLMENT_STATUS_MAP.get(status),
        "shipping_address": map_bc_address(shipping_address_raw),
        "billing_address": map_bc_address(billing),
        "shipping_line": shipping_line,
        "tax_lines": [],
        "line_items": [map_line_item(li, currency) for li in line_items_raw],
        "transactions": [],
        "metafields": [],
    }


def export_orders(client: BigCommerceClient, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    exported = []
    count = 0
    for order in fetch_orders(client):
        if limit is not None and count >= limit:
            break
        order_id = order.get("id")
        try:
            line_items_raw = fetch_line_items(client, order_id)
            shipping_address_raw = fetch_shipping_address(client, order_id)
            exported.append(map_order(order, line_items_raw, shipping_address_raw))
        except Exception:
            logger.exception("Failed to export order %s", order_id)
        count += 1

    logger.info("Exported %s order(s)", len(exported))
    return exported
