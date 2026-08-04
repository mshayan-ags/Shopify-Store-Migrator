import logging
import re
from typing import Any, Dict, List, Optional

from sources.bigcommerce.bigcommerce_client import BigCommerceClient
from sources.bigcommerce.export_products import handle_from_product, slugify

logger = logging.getLogger("bigcommerce_export_collections")


def category_handle(category: Dict[str, Any]) -> str:
    custom_url = (category.get("custom_url") or {}).get("url")
    if custom_url:
        return custom_url.lstrip("/") or slugify(category.get("name"))
    return slugify(category.get("name"))


def build_membership_from_products(products: List[Dict[str, Any]]) -> Dict[Any, List[Dict[str, Any]]]:
    membership: Dict[Any, List[Dict[str, Any]]] = {}
    for product in products:
        pid = product.get("id")
        handle = product.get("handle")
        for cat_id in product.get("_bigcommerce_category_ids") or []:
            membership.setdefault(cat_id, []).append({"id": pid, "handle": handle})
    return membership


def fetch_category_product_members(client: BigCommerceClient, category_id: Any) -> List[Dict[str, Any]]:
    members = []
    for product in client.get_paginated_v3(
        "catalog/products", params={"categories:in": category_id}
    ):
        members.append({"id": product.get("id"), "handle": handle_from_product(product)})
    return members


def map_category(
    category: Dict[str, Any],
    membership: Optional[Dict[Any, List[Dict[str, Any]]]],
    client: Optional[BigCommerceClient],
) -> Dict[str, Any]:
    cat_id = category.get("id")

    if membership is not None:
        products = membership.get(cat_id, [])
    elif client is not None:
        products = fetch_category_product_members(client, cat_id)
    else:
        products = []

    return {
        "type": "custom",
        "id": cat_id,
        "title": category.get("name"),
        "handle": category_handle(category),
        "body_html": category.get("description") or None,
        "image": category.get("image_url") or None,
        "image_local": None,
        "products": products,
        "rules": None,
        "published": bool(category.get("is_visible", True)),
        "metafields": [],
    }


def fetch_categories(client: BigCommerceClient):
    return client.get_paginated_v3("catalog/categories")


def export_collections(
    client: BigCommerceClient, products: Optional[List[Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    membership = build_membership_from_products(products) if products is not None else None

    exported = []
    for category in fetch_categories(client):
        try:
            exported.append(map_category(category, membership, client))
        except Exception:
            logger.exception("Failed to export category %s", category.get("id"))
    logger.info("Exported %s collection(s)", len(exported))
    return exported
