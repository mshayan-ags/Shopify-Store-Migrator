import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from sources.wix.wix_client import WixClient
from transfer.transfer_collections import download_image

logger = logging.getLogger("wix_export_products")

PAGE_LIMIT = 100


def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "product"


def fetch_products(client: WixClient, page_limit: int = PAGE_LIMIT) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    offset = 0

    while True:
        body = {"query": {"paging": {"limit": page_limit, "offset": offset}}}
        response = client.post("stores/v1/products/query", json_body=body) or {}
        page = response.get("products") or []
        products.extend(page)

        if len(page) < page_limit:
            break
        offset += page_limit

    return products


def build_images(items: Optional[List[Dict[str, Any]]], handle: str, out_dir: Optional[Path]) -> List[Dict[str, Any]]:
    images_export = []
    for i, item in enumerate(items or []):
        image = item.get("image") or item
        url = image.get("url")
        if not url:
            continue

        local_path = None
        if out_dir is not None:
            try:
                img_dir = out_dir / "wix_product_images" / handle
                img_dir.mkdir(parents=True, exist_ok=True)
                filename = f"image_{item.get('id') or i}.jpg"
                local_path = str(img_dir / filename)
                download_image(url, Path(local_path))
            except Exception:
                logger.exception("Failed to download image %s for product '%s'", url, handle)
                local_path = None

        images_export.append(
            {
                "id": item.get("id"),
                "position": i + 1,
                "alt": image.get("altText") or None,
                "src": url,
                "local_path": local_path,
                "variant_ids": [],
                "metafields": [],
            }
        )
    return images_export


def build_options(product_options: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    options_export = []
    for i, opt in enumerate(product_options or []):
        values = [
            (c.get("description") or c.get("value"))
            for c in (opt.get("choices") or [])
            if (c.get("description") or c.get("value"))
        ]
        if not values:
            continue
        options_export.append(
            {
                "name": opt.get("name") or f"Option {i + 1}",
                "position": i + 1,
                "values": values,
            }
        )
    return options_export


def match_image_position(variant_choices: Dict[str, Any], images_export: List[Dict[str, Any]]) -> Optional[int]:
    return None


def build_variants(product: Dict[str, Any], option_names: List[str], images_export: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    raw_variants = product.get("variants") or []

    def to_canonical(price_data: Dict[str, Any], sku: Optional[str], weight: Optional[float],
                      choices: Dict[str, Any], stock: Dict[str, Any], position: int, title: str) -> Dict[str, Any]:
        option1 = option2 = option3 = None
        chosen = [choices.get(name) for name in option_names if choices.get(name)]
        if len(chosen) > 0:
            option1 = chosen[0]
        if len(chosen) > 1:
            option2 = chosen[1]
        if len(chosen) > 2:
            option3 = chosen[2]

        track_quantity = bool(stock.get("trackQuantity"))
        price = price_data.get("price")

        return {
            "id": None,
            "title": title,
            "sku": sku,
            "barcode": None,
            "price": str(price) if price is not None else "0.00",
            "compare_at_price": str(price_data["price"]) if price_data.get("salePrice") and price_data.get("price") else None,
            "position": position,
            "option1": option1,
            "option2": option2,
            "option3": option3,
            "taxable": True,
            "weight": float(weight) if weight not in (None, "") else None,
            "weight_unit": "kg",
            "requires_shipping": product.get("productType") != "digital",
            "inventory_policy": "deny",
            "inventory_management": "shopify" if track_quantity else None,
            "inventory_quantity": stock.get("quantity"),
            "inventory_by_location": [],
            "unit_cost": None,
            "country_code_of_origin": None,
            "harmonized_system_code": None,
            "province_code_of_origin": None,
            "image_position": match_image_position(choices, images_export),
            "metafields": [],
        }

    variants_export = []
    if product.get("manageVariants") and raw_variants:
        for i, v in enumerate(raw_variants):
            inner = v.get("variant") or {}
            choices = v.get("choices") or {}
            title = " / ".join(str(val) for val in choices.values()) if choices else "Default Title"
            variants_export.append(
                to_canonical(
                    price_data=inner.get("priceData") or {},
                    sku=inner.get("sku"),
                    weight=inner.get("weight"),
                    choices=choices,
                    stock=v.get("stock") or {},
                    position=i + 1,
                    title=title,
                )
            )
            variants_export[-1]["id"] = v.get("id")
    else:
        variants_export.append(
            to_canonical(
                price_data=product.get("priceData") or {},
                sku=product.get("sku"),
                weight=product.get("weight"),
                choices={},
                stock=product.get("stock") or {},
                position=1,
                title="Default Title",
            )
        )

    return variants_export


def map_product(product: Dict[str, Any], out_dir: Optional[Path] = None) -> Dict[str, Any]:
    handle = product.get("slug") or slugify(product.get("name"))
    images_export = build_images((product.get("media") or {}).get("items"), handle, out_dir)
    option_names = [o.get("name") for o in (product.get("productOptions") or []) if o.get("name")]
    variants_export = build_variants(product, option_names, images_export)

    exported = {
        "id": product.get("id"),
        "handle": handle,
        "title": product.get("name"),
        "body_html": product.get("description") or None,
        "vendor": None,
        "product_type": None,
        "tags": None,
        "status": "active" if product.get("visible", True) else "draft",
        "template_suffix": None,
        "seo_title": None,
        "seo_description": None,
        "category_id": None,
        "requires_selling_plan": False,
        "options": build_options(product.get("productOptions")),
        "images": images_export,
        "variants": variants_export,
        "metafields": [],
        "_wix_collection_ids": product.get("collectionIds") or [],
    }
    return exported


def export_products(client: WixClient, out_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    products_raw = fetch_products(client)
    exported = []
    for product in products_raw:
        try:
            exported.append(map_product(product, out_dir))
        except Exception:
            logger.exception("Failed to export product %s", product.get("id"))
    logger.info("Exported %s product(s)", len(exported))
    return exported
