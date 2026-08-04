import logging
import re
from typing import Any, Dict, List, Optional

from sources.bigcommerce.bigcommerce_client import BigCommerceClient

logger = logging.getLogger("bigcommerce_export_products")

PRODUCT_INCLUDES = "images,variants,custom_fields,options"

AVAILABILITY_TO_STATUS = {
    "available": "active",
    "preorder": "active",
    "disabled": "draft",
}


def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "product"


def handle_from_product(product: Dict[str, Any]) -> str:
    custom_url = (product.get("custom_url") or {}).get("url")
    if custom_url:
        return custom_url.lstrip("/") or slugify(product.get("name"))
    return slugify(product.get("name"))


def sanitize_metafield_key(name: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9_]+", "_", (name or "").strip()).strip("_").lower()
    return (key or "field")[:64]


def build_metafields(custom_fields: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    metafields = []
    for cf in custom_fields or []:
        name = cf.get("name")
        value = cf.get("value")
        if not name:
            continue
        metafields.append(
            {
                "namespace": "custom",
                "key": sanitize_metafield_key(name),
                "value": "" if value is None else str(value),
                "type": "single_line_text_field",
            }
        )
    return metafields


def build_images(images_raw: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    images_export = []
    for img in images_raw or []:
        images_export.append(
            {
                "id": img.get("id"),
                "position": img.get("sort_order", 0),
                "alt": img.get("description") or None,
                "src": img.get("url_standard"),
                "local_path": None,
                "variant_ids": [],
                "metafields": [],
            }
        )
    images_export.sort(key=lambda i: (i["position"] if i["position"] is not None else 0))
    return images_export


def match_image_position(variant_image_url: Optional[str], images_export: List[Dict[str, Any]]) -> Optional[int]:
    if not variant_image_url:
        return None
    for img in images_export:
        if img["src"] == variant_image_url:
            return img["position"]
    return None


def build_options(options_raw: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    options_export = []
    for i, opt in enumerate(options_raw or []):
        values = [ov.get("label") for ov in opt.get("option_values") or [] if ov.get("label")]
        if not values:
            continue
        options_export.append(
            {
                "name": opt.get("display_name") or f"Option {i + 1}",
                "position": i + 1,
                "values": values,
            }
        )
    return options_export


def build_variants(product: Dict[str, Any], images_export: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    raw_variants = product.get("variants") or []
    inventory_tracking = product.get("inventory_tracking", "none")
    inventory_management = None if inventory_tracking == "none" else "shopify"

    def to_canonical(v: Dict[str, Any], position: int, option_values: List[Dict[str, Any]]) -> Dict[str, Any]:
        option1 = option2 = option3 = None
        labels = [ov.get("label") for ov in option_values if ov.get("label")]
        if len(labels) > 0:
            option1 = labels[0]
        if len(labels) > 1:
            option2 = labels[1]
        if len(labels) > 2:
            option3 = labels[2]

        price = v.get("price") if v.get("price") not in (None, "") else product.get("price")
        weight = v.get("weight") if v.get("weight") is not None else product.get("weight")
        sku = v.get("sku") or product.get("sku")

        title = " / ".join(labels) if labels else "Default Title"

        return {
            "id": v.get("id"),
            "title": title,
            "sku": sku,
            "barcode": v.get("upc") or product.get("upc"),
            "price": str(price) if price is not None else "0.00",
            "compare_at_price": str(product["retail_price"]) if product.get("retail_price") else None,
            "position": position,
            "option1": option1,
            "option2": option2,
            "option3": option3,
            "taxable": True,
            "weight": float(weight) if weight not in (None, "") else None,
            "weight_unit": None,
            "requires_shipping": product.get("type") != "digital",
            "inventory_policy": "deny",
            "inventory_management": inventory_management,
            "inventory_quantity": v.get("inventory_level", product.get("inventory_level")),
            "inventory_by_location": [],
            "unit_cost": str(v["cost_price"]) if v.get("cost_price") else (
                str(product["cost_price"]) if product.get("cost_price") else None
            ),
            "country_code_of_origin": None,
            "harmonized_system_code": None,
            "province_code_of_origin": None,
            "image_position": match_image_position(v.get("image_url"), images_export),
            "metafields": [],
        }

    variants_export = []
    if raw_variants:
        for i, v in enumerate(raw_variants):
            variants_export.append(to_canonical(v, i + 1, v.get("option_values") or []))
    else:
        variants_export.append(to_canonical(product, 1, []))

    return variants_export


def map_product(product: Dict[str, Any]) -> Dict[str, Any]:
    images_export = build_images(product.get("images"))
    variants_export = build_variants(product, images_export)

    exported = {
        "id": product.get("id"),
        "handle": handle_from_product(product),
        "title": product.get("name"),
        "body_html": product.get("description") or None,
        "vendor": None,
        "product_type": None,
        "tags": None,
        "status": AVAILABILITY_TO_STATUS.get(product.get("availability"), "draft"),
        "template_suffix": None,
        "seo_title": product.get("page_title") or None,
        "seo_description": product.get("meta_description") or None,
        "category_id": None,
        "requires_selling_plan": False,
        "options": build_options(product.get("options")),
        "images": images_export,
        "variants": variants_export,
        "metafields": build_metafields(product.get("custom_fields")),
        "_bigcommerce_category_ids": product.get("categories") or [],
    }
    return exported


def fetch_products(client: BigCommerceClient):
    return client.get_paginated_v3(
        "catalog/products", params={"include": PRODUCT_INCLUDES}
    )


def export_products(client: BigCommerceClient) -> List[Dict[str, Any]]:
    exported = []
    for product in fetch_products(client):
        try:
            exported.append(map_product(product))
        except Exception:
            logger.exception("Failed to export product %s", product.get("id"))
    logger.info("Exported %s product(s)", len(exported))
    return exported
