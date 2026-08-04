import logging
from typing import Any, Dict, List, Optional

from sources.bigcommerce.bigcommerce_client import BigCommerceClient

logger = logging.getLogger("bigcommerce_export_customers")

ADDRESS_BATCH_SIZE = 50


def map_address(addr: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "address1": addr.get("address1") or None,
        "address2": addr.get("address2") or None,
        "city": addr.get("city") or None,
        "company": addr.get("company") or None,
        "countryCodeV2": addr.get("country_code") or None,
        "firstName": addr.get("first_name") or None,
        "lastName": addr.get("last_name") or None,
        "phone": addr.get("phone") or None,
        "provinceCode": addr.get("state_or_province") or None,
        "zip": addr.get("postal_code") or None,
    }


def _chunk(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def fetch_customers(client: BigCommerceClient):
    return client.get_paginated_v3("customers")


def fetch_addresses_by_customer(
    client: BigCommerceClient, customer_ids: List[Any]
) -> Dict[Any, List[Dict[str, Any]]]:
    by_customer: Dict[Any, List[Dict[str, Any]]] = {}
    for batch in _chunk(list(customer_ids), ADDRESS_BATCH_SIZE):
        if not batch:
            continue
        params = {"customer_id:in": ",".join(str(cid) for cid in batch)}
        for addr in client.get_paginated_v3("customers/addresses", params=params):
            cid = addr.get("customer_id")
            by_customer.setdefault(cid, []).append(addr)
    return by_customer


def map_customer(customer: Dict[str, Any], addresses_raw: List[Dict[str, Any]]) -> Dict[str, Any]:
    addresses = [map_address(a) for a in addresses_raw]
    tax_exempt_category = customer.get("tax_exempt_category")

    return {
        "id": customer.get("id"),
        "email": customer.get("email"),
        "first_name": customer.get("first_name") or None,
        "last_name": customer.get("last_name") or None,
        "phone": customer.get("phone") or None,
        "note": customer.get("notes") or None,
        "tags": [],
        "tax_exempt": bool(tax_exempt_category) if tax_exempt_category is not None else None,
        "tax_exemptions": [],
        "locale": None,
        "email_marketing_consent": None,
        "sms_marketing_consent": None,
        "default_address": addresses[0] if addresses else None,
        "addresses": addresses,
        "metafields": [],
    }


def export_customers(client: BigCommerceClient) -> List[Dict[str, Any]]:
    customers_raw = list(fetch_customers(client))
    customer_ids = [c.get("id") for c in customers_raw if c.get("id") is not None]
    addresses_by_customer = fetch_addresses_by_customer(client, customer_ids)

    exported = []
    skipped_no_email = 0
    for customer in customers_raw:
        if not customer.get("email"):
            skipped_no_email += 1
            continue
        try:
            exported.append(
                map_customer(customer, addresses_by_customer.get(customer.get("id"), []))
            )
        except Exception:
            logger.exception("Failed to export customer %s", customer.get("id"))

    logger.info(
        "Exported %s customer(s) (%s skipped for missing email)",
        len(exported),
        skipped_no_email,
    )
    return exported
