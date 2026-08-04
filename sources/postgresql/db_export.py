import logging
import re
from typing import Any, Dict, List, Optional

from sources.postgresql.db_client import fetch_all

logger = logging.getLogger("postgresql_export")

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: Any, where: str) -> str:
    if not isinstance(name, str) or not IDENTIFIER_RE.match(name):
        raise ValueError(
            f"Invalid identifier {name!r} for '{where}' -- must be a plain table/column name matching "
            f"^[A-Za-z_][A-Za-z0-9_]*$ (no dots, quotes, spaces, or SQL). Use a 'query:' key instead if "
            f"you need a join or expression."
        )
    return name


def _select_query(section: Dict[str, Any]) -> str:
    query = section.get("query")
    if query:
        return query
    table = _validate_identifier(section.get("table"), "table")
    return f"SELECT * FROM {table}"


def _resolve_id(row: Dict[str, Any], columns: Optional[Dict[str, Any]]) -> Any:
    id_column = (columns or {}).get("id")
    if id_column:
        _validate_identifier(id_column, "columns.id")
        return row.get(id_column)
    return row.get("id")


def _apply_columns(
    row: Dict[str, Any],
    columns: Optional[Dict[str, Any]],
    transforms: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    transforms = transforms or {}
    result: Dict[str, Any] = {}
    for canonical_field, source_column in (columns or {}).items():
        if not source_column:
            continue
        _validate_identifier(source_column, f"columns.{canonical_field}")
        value = row.get(source_column)

        field_transforms = transforms.get(canonical_field)
        if field_transforms:
            lookup_key = str(value) if value is not None else value
            if lookup_key in field_transforms:
                value = field_transforms[lookup_key]

        result[canonical_field] = value
    return result


def _stringify_fields(d: Dict[str, Any], fields: List[str]) -> None:
    for field in fields:
        if d.get(field) is not None:
            d[field] = str(d[field])


PRODUCT_DEFAULTS: Dict[str, Any] = {
    "id": None,
    "handle": None,
    "title": None,
    "body_html": None,
    "vendor": None,
    "product_type": None,
    "tags": None,
    "status": "active",
    "template_suffix": None,
    "seo_title": None,
    "seo_description": None,
    "category_id": None,
    "requires_selling_plan": False,
    "options": [],
    "images": [],
    "variants": [],
    "metafields": [],
}

VARIANT_DEFAULTS: Dict[str, Any] = {
    "id": None,
    "title": "Default Title",
    "sku": None,
    "barcode": None,
    "price": "0.00",
    "compare_at_price": None,
    "position": 1,
    "option1": None,
    "option2": None,
    "option3": None,
    "taxable": True,
    "weight": None,
    "weight_unit": None,
    "requires_shipping": True,
    "inventory_policy": "deny",
    "inventory_management": "shopify",
    "inventory_quantity": None,
    "inventory_by_location": [],
    "unit_cost": None,
    "country_code_of_origin": None,
    "harmonized_system_code": None,
    "province_code_of_origin": None,
    "image_position": None,
    "metafields": [],
}

COLLECTION_DEFAULTS: Dict[str, Any] = {
    "type": "custom",
    "id": None,
    "title": None,
    "handle": None,
    "body_html": None,
    "image": None,
    "image_local": None,
    "products": [],
    "rules": None,
    "published": True,
    "metafields": [],
}

CUSTOMER_DEFAULTS: Dict[str, Any] = {
    "id": None,
    "email": None,
    "first_name": None,
    "last_name": None,
    "phone": None,
    "note": None,
    "tags": [],
    "tax_exempt": None,
    "tax_exemptions": [],
    "locale": None,
    "email_marketing_consent": None,
    "sms_marketing_consent": None,
    "default_address": None,
    "addresses": [],
    "metafields": [],
}

ORDER_DEFAULTS: Dict[str, Any] = {
    "id": None,
    "name": None,
    "email": None,
    "phone": None,
    "note": None,
    "tags": [],
    "test": False,
    "processed_at": None,
    "closed_at": None,
    "cancelled_at": None,
    "cancel_reason": None,
    "po_number": None,
    "source_name": None,
    "discount_codes": [],
    "custom_attributes": [],
    "currency": "USD",
    "financial_status": None,
    "fulfillment_status": None,
    "shipping_address": None,
    "billing_address": None,
    "shipping_line": None,
    "tax_lines": [],
    "line_items": [],
    "transactions": [],
    "metafields": [],
}

LINE_ITEM_DEFAULTS: Dict[str, Any] = {
    "sku": None,
    "title": None,
    "variant_title": None,
    "vendor": None,
    "quantity": 1,
    "taxable": True,
    "requires_shipping": True,
    "price": "0.00",
    "currency": "USD",
}


def export_products(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    cfg = mapping["products"]
    columns = cfg.get("columns")
    rows = fetch_all(_select_query(cfg))

    variants_cfg = cfg.get("variants")
    variants_by_parent: Dict[Any, List[Dict[str, Any]]] = {}
    variants_columns = None
    if variants_cfg:
        foreign_key = _validate_identifier(variants_cfg.get("foreign_key"), "products.variants.foreign_key")
        variants_columns = variants_cfg.get("columns")
        for vrow in fetch_all(_select_query(variants_cfg)):
            parent_id = vrow.get(foreign_key)
            variants_by_parent.setdefault(parent_id, []).append(vrow)

    products = []
    for row in rows:
        product = dict(PRODUCT_DEFAULTS)
        product.update(_apply_columns(row, columns, cfg.get("transforms")))
        product["id"] = _resolve_id(row, columns)

        if variants_cfg:
            position_mapped = "position" in (variants_columns or {})
            variants = []
            for i, vrow in enumerate(variants_by_parent.get(product["id"], []), start=1):
                variant = dict(VARIANT_DEFAULTS)
                variant.update(_apply_columns(vrow, variants_columns, variants_cfg.get("transforms")))
                variant["id"] = _resolve_id(vrow, variants_columns)
                if not position_mapped:
                    variant["position"] = i
                _stringify_fields(variant, ["price", "compare_at_price", "unit_cost"])
                variants.append(variant)
            product["variants"] = variants

        products.append(product)

    return products


def _product_id_to_handle_map(mapping: Dict[str, Any]) -> Dict[Any, Any]:
    products_cfg = mapping.get("products")
    if not products_cfg:
        return {}
    columns = products_cfg.get("columns") or {}
    handle_column = columns.get("handle")
    if not handle_column:
        return {}
    _validate_identifier(handle_column, "products.columns.handle")

    lookup: Dict[Any, Any] = {}
    for row in fetch_all(_select_query(products_cfg)):
        lookup[_resolve_id(row, columns)] = row.get(handle_column)
    return lookup


def export_collections(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    cfg = mapping["collections"]
    columns = cfg.get("columns")
    rows = fetch_all(_select_query(cfg))

    membership_cfg = cfg.get("product_membership")
    products_by_collection: Dict[Any, List[Dict[str, Any]]] = {}
    if membership_cfg:
        product_id_column = _validate_identifier(
            membership_cfg.get("product_id_column"), "collections.product_membership.product_id_column"
        )
        collection_id_column = _validate_identifier(
            membership_cfg.get("collection_id_column"), "collections.product_membership.collection_id_column"
        )
        id_to_handle = _product_id_to_handle_map(mapping)
        for mrow in fetch_all(_select_query(membership_cfg)):
            collection_id = mrow.get(collection_id_column)
            product_id = mrow.get(product_id_column)
            products_by_collection.setdefault(collection_id, []).append(
                {"id": product_id, "handle": id_to_handle.get(product_id)}
            )

    collections = []
    for row in rows:
        collection = dict(COLLECTION_DEFAULTS)
        collection.update(_apply_columns(row, columns, cfg.get("transforms")))
        collection_id = _resolve_id(row, columns)
        collection["id"] = collection_id
        if membership_cfg:
            collection["products"] = products_by_collection.get(collection_id, [])
        collections.append(collection)

    return collections


def export_customers(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    cfg = mapping["customers"]
    columns = cfg.get("columns")
    rows = fetch_all(_select_query(cfg))

    customers = []
    for row in rows:
        customer = dict(CUSTOMER_DEFAULTS)
        customer.update(_apply_columns(row, columns, cfg.get("transforms")))
        customer["id"] = _resolve_id(row, columns)
        if not customer.get("email"):
            logger.warning(
                "Customer row (source id=%s) has no email mapped/populated -- it will fail canonical "
                "validation downstream since email is the required cross-store matching key",
                customer["id"],
            )
        customers.append(customer)

    return customers


def export_orders(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    cfg = mapping["orders"]
    columns = cfg.get("columns")
    rows = fetch_all(_select_query(cfg))

    line_items_cfg = cfg.get("line_items")
    items_by_parent: Dict[Any, List[Dict[str, Any]]] = {}
    line_items_columns = None
    if line_items_cfg:
        foreign_key = _validate_identifier(line_items_cfg.get("foreign_key"), "orders.line_items.foreign_key")
        line_items_columns = line_items_cfg.get("columns")
        for irow in fetch_all(_select_query(line_items_cfg)):
            parent_id = irow.get(foreign_key)
            items_by_parent.setdefault(parent_id, []).append(irow)

    orders = []
    for row in rows:
        order = dict(ORDER_DEFAULTS)
        order.update(_apply_columns(row, columns, cfg.get("transforms")))
        order_id = _resolve_id(row, columns)
        order["id"] = order_id

        if line_items_cfg:
            line_items = []
            for irow in items_by_parent.get(order_id, []):
                line_item = dict(LINE_ITEM_DEFAULTS)
                line_item.update(_apply_columns(irow, line_items_columns, line_items_cfg.get("transforms")))
                _stringify_fields(line_item, ["price"])
                line_items.append(line_item)
            order["line_items"] = line_items

        orders.append(order)

    return orders


RESOURCE_EXPORTERS = {
    "products": export_products,
    "collections": export_collections,
    "customers": export_customers,
    "orders": export_orders,
}


def export_resource(mapping: Dict[str, Any], resource: str) -> List[Dict[str, Any]]:
    if resource not in RESOURCE_EXPORTERS:
        raise ValueError(f"Unknown resource '{resource}' -- expected one of {list(RESOURCE_EXPORTERS)}")
    if resource not in mapping:
        raise ValueError(f"Mapping has no '{resource}' section configured -- nothing to export")

    logger.info("Exporting '%s' from PostgreSQL", resource)
    rows = RESOURCE_EXPORTERS[resource](mapping)
    logger.info("Exported %s '%s' record(s)", len(rows), resource)
    return rows
