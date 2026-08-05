import logging
from typing import Any, Dict, List, Optional

from sources.wix.wix_client import WixClient

logger = logging.getLogger("wix_export_customers")

PAGE_LIMIT = 100


def primary_email(info: Dict[str, Any]) -> Optional[str]:
    items = (info.get("emails") or {}).get("items") or []
    for item in items:
        if item.get("primary"):
            return item.get("email")
    for item in items:
        if item.get("tag") == "MAIN":
            return item.get("email")
    return items[0].get("email") if items else None


def primary_phone(info: Dict[str, Any]) -> Optional[str]:
    items = (info.get("phones") or {}).get("items") or []
    for item in items:
        if item.get("primary"):
            return item.get("phone")
    return items[0].get("phone") if items else None


def map_address(address_item: Dict[str, Any], contact_info: Dict[str, Any]) -> Dict[str, Any]:
    addr = address_item.get("address") or {}
    subdivision = addr.get("subdivision")
    province_code = subdivision
    if subdivision and "-" in subdivision:
        province_code = subdivision.split("-", 1)[1]

    name = contact_info.get("name") or {}
    return {
        "address1": addr.get("addressLine1") or addr.get("addressLine") or None,
        "address2": addr.get("addressLine2") or None,
        "city": addr.get("city") or None,
        "company": address_item.get("company") or None,
        "countryCodeV2": addr.get("country") or None,
        "firstName": name.get("first") or None,
        "lastName": name.get("last") or None,
        "phone": primary_phone(contact_info),
        "provinceCode": province_code or None,
        "zip": addr.get("postalCode") or None,
    }


def map_customer(contact: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    info = contact.get("info") or {}
    email = primary_email(info)
    if not email:
        return None

    address_items = (info.get("addresses") or {}).get("items") or []
    addresses = [map_address(a, info) for a in address_items]
    name = info.get("name") or {}
    label_keys = [item.get("key") for item in (info.get("labelKeys") or {}).get("items") or [] if item.get("key")]

    return {
        "id": contact.get("id"),
        "email": email,
        "first_name": name.get("first") or None,
        "last_name": name.get("last") or None,
        "phone": primary_phone(info),
        "note": None,
        "tags": label_keys,
        "tax_exempt": None,
        "tax_exemptions": [],
        "locale": (contact.get("info") or {}).get("locale") or None,
        "email_marketing_consent": None,
        "sms_marketing_consent": None,
        "default_address": addresses[0] if addresses else None,
        "addresses": addresses,
        "metafields": [],
    }


def fetch_contacts(client: WixClient, page_limit: int = PAGE_LIMIT) -> List[Dict[str, Any]]:
    contacts: List[Dict[str, Any]] = []
    offset = 0

    while True:
        body = {"query": {"paging": {"limit": page_limit, "offset": offset}}}
        response = client.post("contacts/v4/contacts/query", json_body=body) or {}
        page = response.get("contacts") or []
        contacts.extend(page)

        if len(page) < page_limit:
            break
        offset += page_limit

    return contacts


def export_customers(client: WixClient) -> List[Dict[str, Any]]:
    contacts_raw = fetch_contacts(client)
    exported = []
    skipped_no_email = 0

    for contact in contacts_raw:
        try:
            mapped = map_customer(contact)
        except Exception:
            logger.exception("Failed to export contact %s", contact.get("id"))
            continue
        if mapped is None:
            skipped_no_email += 1
            continue
        exported.append(mapped)

    logger.info(
        "Exported %s customer(s) (%s skipped for missing email)",
        len(exported),
        skipped_no_email,
    )
    return exported
