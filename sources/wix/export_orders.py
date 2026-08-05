import logging
from typing import Any, Dict, List, Optional

from sources.wix.wix_client import WixClient

logger = logging.getLogger("wix_export_orders")

PAGE_LIMIT = 100

PAYMENT_STATUS_MAP = {
    "PAID": "PAID",
    "PARTIALLY_PAID": "PARTIALLY_PAID",
    "NOT_PAID": "PENDING",
    "PENDING": "PENDING",
    "PARTIALLY_REFUNDED": "PARTIALLY_REFUNDED",
    "FULLY_REFUNDED": "REFUNDED",
}

FULFILLMENT_STATUS_MAP = {
    "FULFILLED": "FULFILLED",
    "PARTIALLY_FULFILLED": "PARTIALLY_FULFILLED",
}


def fetch_orders(client: WixClient, page_limit: int = PAGE_LIMIT) -> List[Dict[str, Any]]:
    orders: List[Dict[str, Any]] = []
    offset = 0

    while True:
        body = {"query": {"paging": {"limit": page_limit, "offset": offset}}}
        response = client.post("ecom/v1/orders/query", json_body=body) or {}
        page = response.get("orders") or []
        orders.extend(page)

        if len(page) < page_limit:
            break
        offset += page_limit

    return orders


def map_address(address: Optional[Dict[str, Any]], contact_details: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not address and not contact_details:
        return None

    addr = address or {}
    contact = contact_details or {}
    subdivision = addr.get("subdivision")
    province_code = subdivision
    if subdivision and "-" in subdivision:
        province_code = subdivision.split("-", 1)[1]

    return {
        "address1": addr.get("addressLine1") or addr.get("addressLine") or None,
        "address2": addr.get("addressLine2") or None,
        "city": addr.get("city") or None,
        "company": contact.get("company") or None,
        "countryCodeV2": addr.get("country") or None,
        "firstName": contact.get("firstName") or None,
        "lastName": contact.get("lastName") or None,
        "phone": contact.get("phone") or None,
        "provinceCode": province_code or None,
        "zip": addr.get("postalCode") or None,
    }


def map_line_item(item: Dict[str, Any], currency: str) -> Dict[str, Any]:
    physical = item.get("physicalProperties") or {}
    catalog_ref = item.get("catalogReference") or {}
    product_name = item.get("productName") or {}
    title = product_name.get("original") if isinstance(product_name, dict) else product_name
    price = (item.get("price") or {}).get("amount")
    item_type = (item.get("itemType") or {}).get("preset")

    return {
        "sku": physical.get("sku") or catalog_ref.get("sku") or None,
        "title": title or "",
        "variant_title": None,
        "vendor": None,
        "quantity": int(item.get("quantity") or 0),
        "taxable": True,
        "requires_shipping": item_type != "DIGITAL",
        "price": str(price) if price is not None else "0.00",
        "currency": currency,
    }


def extract_discount_codes(order: Dict[str, Any]) -> List[str]:
    codes: List[str] = []
    for discount in order.get("appliedDiscounts") or []:
        coupon = discount.get("coupon") or {}
        code = coupon.get("code") or coupon.get("name")
        if code:
            codes.append(code)
    return codes


def build_shipping_line(order: Dict[str, Any], currency: str) -> Optional[Dict[str, Any]]:
    shipping_info = order.get("shippingInfo") or {}
    cost = (shipping_info.get("cost") or {}).get("price") or {}
    amount = cost.get("amount")
    if amount is None:
        amount = ((order.get("priceSummary") or {}).get("shipping") or {}).get("amount")
    if amount is None:
        return None

    return {"title": shipping_info.get("title") or "Shipping", "amount": str(amount), "currency": currency}


def map_order(order: Dict[str, Any]) -> Dict[str, Any]:
    currency = order.get("currency") or "USD"
    buyer_info = order.get("buyerInfo") or {}
    billing_info = order.get("billingInfo") or {}
    shipping_destination = ((order.get("shippingInfo") or {}).get("logistics") or {}).get("shippingDestination") or {}
    line_items_raw = order.get("lineItems") or []
    status = str(order.get("status") or "").upper()
    number = order.get("number")

    return {
        "id": order.get("id"),
        "name": f"#{number}" if number else f"#{order.get('id')}",
        "email": buyer_info.get("email") or None,
        "phone": (billing_info.get("contactDetails") or {}).get("phone") or None,
        "note": order.get("buyerNote") or None,
        "tags": [],
        "test": False,
        "processed_at": order.get("createdDate") or None,
        "closed_at": None,
        "cancelled_at": order.get("updatedDate") if status == "CANCELED" else None,
        "cancel_reason": None,
        "po_number": None,
        "source_name": (order.get("channelInfo") or {}).get("type"),
        "discount_codes": extract_discount_codes(order),
        "custom_attributes": [],
        "currency": currency,
        "financial_status": PAYMENT_STATUS_MAP.get(order.get("paymentStatus")),
        "fulfillment_status": FULFILLMENT_STATUS_MAP.get(order.get("fulfillmentStatus")),
        "shipping_address": map_address(shipping_destination.get("address"), shipping_destination.get("contactDetails")),
        "billing_address": map_address(billing_info.get("address"), billing_info.get("contactDetails")),
        "shipping_line": build_shipping_line(order, currency),
        "tax_lines": [],
        "line_items": [map_line_item(li, currency) for li in line_items_raw],
        "transactions": [],
        "metafields": [],
    }


def export_orders(client: WixClient, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    orders_raw = fetch_orders(client)
    exported = []
    count = 0

    for order in orders_raw:
        if limit is not None and count >= limit:
            break
        count += 1
        try:
            exported.append(map_order(order))
        except Exception:
            logger.exception("Failed to export order %s", order.get("id"))

    logger.info("Exported %s order(s)", len(exported))
    return exported
