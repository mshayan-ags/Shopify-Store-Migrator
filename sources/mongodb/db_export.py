import logging
from typing import Any, Dict, List, Optional

from sources.mongodb.db_client import find_all

logger = logging.getLogger("mongodb_export")


def resolve_dotted_field(doc: Optional[Dict[str, Any]], dotted_path: str) -> Any:
    if not dotted_path:
        return None

    current: Any = doc
    for part in dotted_path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def apply_transform(value: Any, transforms: Optional[Dict[str, Any]]) -> Any:
    if not transforms or value is None:
        return value
    key = str(value).lower() if isinstance(value, bool) else str(value)
    if key in transforms:
        return transforms[key]
    return value


def map_fields(
    doc: Dict[str, Any],
    fields: Dict[str, str],
    transforms: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    transforms = transforms or {}
    result: Dict[str, Any] = {}
    for canonical_name, dotted_path in fields.items():
        value = resolve_dotted_field(doc, dotted_path)
        value = apply_transform(value, transforms.get(canonical_name))
        result[canonical_name] = value
    return result


def _resolve_nested_items(parent_doc: Dict[str, Any], nested_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    item_fields = nested_config.get("fields", {})
    item_transforms = nested_config.get("transforms", {})

    if nested_config.get("embedded_field"):
        raw_items = resolve_dotted_field(parent_doc, nested_config["embedded_field"]) or []
        if not isinstance(raw_items, list):
            logger.warning(
                "Embedded field '%s' on document %s is not a list -- skipping nested items",
                nested_config["embedded_field"], parent_doc.get("_id"),
            )
            return []
        return [map_fields(item, item_fields, item_transforms) for item in raw_items]

    if nested_config.get("collection") and nested_config.get("foreign_key"):
        parent_id = parent_doc.get("_id")
        raw_items = find_all(nested_config["collection"], {nested_config["foreign_key"]: parent_id})
        return [map_fields(item, item_fields, item_transforms) for item in raw_items]

    logger.warning("Nested items config %s has neither 'embedded_field' nor 'collection'+'foreign_key' -- skipping", nested_config)
    return []


def export_products(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    section = config.get("products")
    if not section:
        return []

    collection_name = section["collection"]
    fields = section["fields"]
    transforms = section.get("transforms", {})
    variants_config = section.get("variants")

    docs = find_all(collection_name)
    logger.info("Fetched %s document(s) from collection '%s' for resource 'products'", len(docs), collection_name)

    products: List[Dict[str, Any]] = []
    for doc in docs:
        mapped = map_fields(doc, fields, transforms)
        variants = _resolve_nested_items(doc, variants_config) if variants_config else []

        products.append({
            "id": doc.get("_id"),
            "handle": mapped.get("handle"),
            "title": mapped.get("title"),
            "body_html": mapped.get("body_html"),
            "vendor": mapped.get("vendor"),
            "product_type": mapped.get("product_type"),
            "tags": mapped.get("tags"),
            "status": mapped.get("status") or "active",
            "template_suffix": mapped.get("template_suffix"),
            "seo_title": mapped.get("seo_title"),
            "seo_description": mapped.get("seo_description"),
            "category_id": mapped.get("category_id"),
            "requires_selling_plan": bool(mapped.get("requires_selling_plan") or False),
            "options": mapped.get("options") or [],
            "images": mapped.get("images") or [],
            "variants": variants,
            "metafields": mapped.get("metafields") or [],
        })

    return products


def export_collections(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    section = config.get("collections")
    if not section:
        return []

    collection_name = section["collection"]
    fields = section["fields"]
    transforms = section.get("transforms", {})
    membership_config = section.get("product_membership")

    products_section = config.get("products")
    products_collection_name = products_section["collection"] if products_section else None
    product_handle_field = products_section["fields"].get("handle") if products_section else None

    docs = find_all(collection_name)
    logger.info("Fetched %s document(s) from collection '%s' for resource 'collections'", len(docs), collection_name)

    collections: List[Dict[str, Any]] = []
    for doc in docs:
        mapped = map_fields(doc, fields, transforms)

        member_products: List[Dict[str, Any]] = []
        if membership_config and products_collection_name:
            fk_field = membership_config["product_field_referencing_collection"]
            member_docs = find_all(products_collection_name, {fk_field: doc.get("_id")})
            for member_doc in member_docs:
                handle = resolve_dotted_field(member_doc, product_handle_field) if product_handle_field else None
                member_products.append({"id": member_doc.get("_id"), "handle": handle})
        elif membership_config and not products_collection_name:
            logger.warning(
                "Resource 'collections' has a 'product_membership' block but the mapping has no 'products' "
                "section to resolve member products from -- leaving 'products' empty for collection '%s'",
                doc.get("_id"),
            )

        collections.append({
            "type": "custom",
            "id": doc.get("_id"),
            "title": mapped.get("title"),
            "handle": mapped.get("handle"),
            "body_html": mapped.get("body_html"),
            "image": mapped.get("image"),
            "image_local": None,
            "products": member_products,
            "rules": None,
            "published": True if mapped.get("published") is None else bool(mapped.get("published")),
            "metafields": mapped.get("metafields") or [],
        })

    return collections


def export_customers(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    section = config.get("customers")
    if not section:
        return []

    collection_name = section["collection"]
    fields = section["fields"]
    transforms = section.get("transforms", {})

    docs = find_all(collection_name)
    logger.info("Fetched %s document(s) from collection '%s' for resource 'customers'", len(docs), collection_name)

    customers: List[Dict[str, Any]] = []
    for doc in docs:
        mapped = map_fields(doc, fields, transforms)
        if not mapped.get("email"):
            logger.warning("Skipping customer document %s -- no email resolved (email is required)", doc.get("_id"))
            continue

        customers.append({
            "id": doc.get("_id"),
            "email": mapped.get("email"),
            "first_name": mapped.get("first_name"),
            "last_name": mapped.get("last_name"),
            "phone": mapped.get("phone"),
            "note": mapped.get("note"),
            "tags": mapped.get("tags") or [],
            "tax_exempt": mapped.get("tax_exempt"),
            "tax_exemptions": mapped.get("tax_exemptions") or [],
            "locale": mapped.get("locale"),
            "email_marketing_consent": mapped.get("email_marketing_consent"),
            "sms_marketing_consent": mapped.get("sms_marketing_consent"),
            "default_address": mapped.get("default_address"),
            "addresses": mapped.get("addresses") or [],
            "metafields": mapped.get("metafields") or [],
        })

    return customers


def export_orders(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    section = config.get("orders")
    if not section:
        return []

    collection_name = section["collection"]
    fields = section["fields"]
    transforms = section.get("transforms", {})
    line_items_config = section.get("line_items")

    docs = find_all(collection_name)
    logger.info("Fetched %s document(s) from collection '%s' for resource 'orders'", len(docs), collection_name)

    orders: List[Dict[str, Any]] = []
    for doc in docs:
        mapped = map_fields(doc, fields, transforms)
        line_items = _resolve_nested_items(doc, line_items_config) if line_items_config else []

        orders.append({
            "id": doc.get("_id"),
            "name": mapped.get("name"),
            "email": mapped.get("email"),
            "phone": mapped.get("phone"),
            "note": mapped.get("note"),
            "tags": mapped.get("tags") or [],
            "test": bool(mapped.get("test") or False),
            "processed_at": mapped.get("processed_at"),
            "closed_at": mapped.get("closed_at"),
            "cancelled_at": mapped.get("cancelled_at"),
            "cancel_reason": mapped.get("cancel_reason"),
            "po_number": mapped.get("po_number"),
            "source_name": mapped.get("source_name"),
            "discount_codes": mapped.get("discount_codes") or [],
            "custom_attributes": mapped.get("custom_attributes") or [],
            "currency": mapped.get("currency"),
            "financial_status": mapped.get("financial_status"),
            "fulfillment_status": mapped.get("fulfillment_status"),
            "shipping_address": mapped.get("shipping_address"),
            "billing_address": mapped.get("billing_address"),
            "shipping_line": mapped.get("shipping_line"),
            "tax_lines": mapped.get("tax_lines") or [],
            "line_items": line_items,
            "transactions": mapped.get("transactions") or [],
            "metafields": mapped.get("metafields") or [],
        })

    return orders


EXPORTERS = {
    "products": export_products,
    "collections": export_collections,
    "customers": export_customers,
    "orders": export_orders,
}


def export_resource(resource: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    exporter = EXPORTERS.get(resource)
    if exporter is None:
        raise ValueError(f"Unknown resource '{resource}' -- expected one of {list(EXPORTERS.keys())}")
    return exporter(config)
