import argparse
import base64
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from utils.shopify_client import ShopifyClient
from transfer.transfer_collections import admin_base_for_host, download_image
from transfer.transfer_store_metafields import (
    retry_with_backoff,
    set_metafields,
    product_gid,
    product_variant_gid,
    product_image_gid,
    gql_quote,
)
from utils.shopify_graphql_utils import run_concurrently, DEFAULT_WORKERS
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_product")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def shop_host(shop_name: str) -> str:
    return f"{shop_name}.myshopify.com"


def make_client(shop_name: str, token: str) -> ShopifyClient:
    host = shop_host(shop_name)
    client = ShopifyClient(access_token=token)
    client.set_shop(admin_base_for_host(host), f"https://{host}/admin/oauth/access_token")
    return client


def fetch_all_product_handles(client: ShopifyClient) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    after_clause = ""

    while True:
        query = f"""
        {{
          products(first: 250{after_clause}) {{
            edges {{ node {{ id handle }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """
        data = retry_with_backoff(lambda: client.query(query))
        connection = data["products"]

        for edge in connection["edges"]:
            node = edge["node"]
            products.append({"id": int(node["id"].rsplit("/", 1)[-1]), "handle": node["handle"]})

        if not connection["pageInfo"]["hasNextPage"]:
            break
        after_clause = f', after: {gql_quote(connection["pageInfo"]["endCursor"])}'

    return products


def find_product_by_handle(client: ShopifyClient, handle: str) -> Optional[Dict[str, Any]]:
    handle_key = (handle or "").strip().lower()
    if not handle_key:
        return None

    data = retry_with_backoff(
        lambda: client.query(f"{{ productByHandle(handle: {gql_quote(handle_key)}) {{ legacyResourceId }} }}")
    )
    product_ref = data.get("productByHandle")
    if not product_ref:
        return None

    result = retry_with_backoff(lambda: client.rest_get(f"products/{product_ref['legacyResourceId']}"))
    return result.get("product")


def find_source_product(client: ShopifyClient, identifier: str) -> Dict[str, Any]:
    if identifier.isdigit():
        data = retry_with_backoff(lambda: client.rest_get(f"products/{identifier}"))
        product = data.get("product")
        if not product:
            raise RuntimeError(f"Product {identifier} not found in source store")
        return product

    product = find_product_by_handle(client, identifier)
    if not product:
        raise RuntimeError(f"Product with handle '{identifier}' not found in source store")
    return product


def fetch_owner_metafields(client: ShopifyClient, owner_resource: str, owner_id: int) -> List[Dict[str, Any]]:
    metafields: List[Dict[str, Any]] = []
    since_id = 0

    while True:
        params = {
            "metafield[owner_resource]": owner_resource,
            "metafield[owner_id]": owner_id,
            "limit": 250,
        }
        if since_id:
            params["since_id"] = since_id

        page = retry_with_backoff(lambda: client.rest_get("metafields", params=params))
        page_mfs = page.get("metafields", [])
        if not page_mfs:
            break

        metafields.extend(page_mfs)
        since_id = page_mfs[-1].get("id") or 0
        if len(page_mfs) < 250 or not since_id:
            break

    return metafields


def fetch_variant_inventory_details(src_client: ShopifyClient, product_id: int) -> Dict[int, Dict[str, Any]]:
    try:
        data = retry_with_backoff(
            lambda: src_client.query(
                f"""
                {{ product(id: "gid://shopify/Product/{product_id}") {{
                  variants(first: 250) {{
                    edges {{ node {{
                      legacyResourceId
                      inventoryItem {{
                        unitCost {{ amount }}
                        countryCodeOfOrigin
                        harmonizedSystemCode
                        provinceCodeOfOrigin
                        inventoryLevels(first: 50) {{
                          edges {{ node {{
                            location {{ name }}
                            quantities(names: ["available"]) {{ name quantity }}
                          }} }}
                        }}
                      }}
                    }} }}
                  }}
                }} }}
                """
            )
        )
    except Exception:
        logger.exception("Failed to fetch variant inventory details for product %s", product_id)
        return {}

    details: Dict[int, Dict[str, Any]] = {}
    for edge in ((data.get("product") or {}).get("variants") or {}).get("edges", []):
        node = edge["node"]
        item = node.get("inventoryItem") or {}

        by_location = []
        for level_edge in (item.get("inventoryLevels") or {}).get("edges", []):
            level = level_edge["node"]
            location_name = (level.get("location") or {}).get("name")
            available = next(
                (q["quantity"] for q in level.get("quantities", []) if q.get("name") == "available"),
                None,
            )
            if location_name and available is not None:
                by_location.append({"location_name": location_name, "available": available})

        details[int(node["legacyResourceId"])] = {
            "unit_cost": (item.get("unitCost") or {}).get("amount"),
            "country_code_of_origin": item.get("countryCodeOfOrigin"),
            "harmonized_system_code": item.get("harmonizedSystemCode"),
            "province_code_of_origin": item.get("provinceCodeOfOrigin"),
            "inventory_by_location": by_location,
        }
    return details


def export_product(src_client: ShopifyClient, product: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    pid = product["id"]
    handle = product.get("handle") or f"product-{pid}"
    logger.info("Exporting product %s (%s)", pid, handle)

    img_dir = out_dir / "product_images" / handle
    images_export = []
    for img in product.get("images", []):
        img_src = img.get("src")
        local_path = None
        if img_src:
            img_dir.mkdir(parents=True, exist_ok=True)
            filename = f"image_{img.get('id')}.jpg"
            local_path = str(img_dir / filename)
            download_image(img_src, Path(local_path))

        metafields = []
        try:
            metafields = fetch_owner_metafields(src_client, "product_image", img.get("id"))
        except Exception:
            logger.exception("Failed to fetch metafields for image %s", img.get("id"))

        images_export.append(
            {
                "id": img.get("id"),
                "position": img.get("position"),
                "alt": img.get("alt"),
                "src": img_src,
                "local_path": local_path,
                "variant_ids": img.get("variant_ids", []),
                "metafields": metafields,
            }
        )

    inventory_details = fetch_variant_inventory_details(src_client, pid)

    variants_export = []
    for v in product.get("variants", []):
        metafields = []
        try:
            metafields = fetch_owner_metafields(src_client, "variant", v.get("id"))
        except Exception:
            logger.exception("Failed to fetch metafields for variant %s", v.get("id"))

        image_position = None
        for img in images_export:
            if v.get("image_id") and v.get("image_id") == img.get("id"):
                image_position = img.get("position")
                break

        details = inventory_details.get(v.get("id"), {})

        variants_export.append(
            {
                "id": v.get("id"),
                "title": v.get("title"),
                "sku": v.get("sku"),
                "barcode": v.get("barcode"),
                "price": v.get("price"),
                "compare_at_price": v.get("compare_at_price"),
                "position": v.get("position"),
                "option1": v.get("option1"),
                "option2": v.get("option2"),
                "option3": v.get("option3"),
                "taxable": v.get("taxable"),
                "weight": v.get("weight"),
                "weight_unit": v.get("weight_unit"),
                "requires_shipping": v.get("requires_shipping"),
                "inventory_policy": v.get("inventory_policy"),
                "inventory_management": v.get("inventory_management"),
                "inventory_quantity": v.get("inventory_quantity"),
                "inventory_by_location": details.get("inventory_by_location", []),
                "unit_cost": details.get("unit_cost"),
                "country_code_of_origin": details.get("country_code_of_origin"),
                "harmonized_system_code": details.get("harmonized_system_code"),
                "province_code_of_origin": details.get("province_code_of_origin"),
                "image_position": image_position,
                "metafields": metafields,
            }
        )

    product_metafields = []
    try:
        product_metafields = fetch_owner_metafields(src_client, "product", pid)
    except Exception:
        logger.exception("Failed to fetch metafields for product %s", pid)

    seo = None
    category_id = None
    requires_selling_plan = None
    try:
        gql_data = retry_with_backoff(
            lambda: src_client.query(
                f"""
                {{ product(id: "gid://shopify/Product/{pid}") {{
                  seo {{ title description }}
                  category {{ id }}
                  requiresSellingPlan
                }} }}
                """
            )
        )
        gql_product = gql_data.get("product") or {}
        seo = gql_product.get("seo")
        category_id = (gql_product.get("category") or {}).get("id")
        requires_selling_plan = gql_product.get("requiresSellingPlan")
    except Exception:
        logger.exception("Failed to fetch GraphQL-only fields (seo/category) for product %s", pid)

    return {
        "id": pid,
        "handle": handle,
        "title": product.get("title"),
        "body_html": product.get("body_html"),
        "vendor": product.get("vendor"),
        "product_type": product.get("product_type"),
        "tags": product.get("tags"),
        "status": product.get("status"),
        "template_suffix": product.get("template_suffix"),
        "seo_title": (seo or {}).get("title"),
        "seo_description": (seo or {}).get("description"),
        "category_id": category_id,
        "requires_selling_plan": requires_selling_plan,
        "options": [
            {"name": o.get("name"), "position": o.get("position"), "values": o.get("values")}
            for o in product.get("options", [])
        ],
        "images": images_export,
        "variants": variants_export,
        "metafields": product_metafields,
    }


def build_import_payload(exported: Dict[str, Any]) -> Dict[str, Any]:
    images_payload = []
    for img in exported.get("images", []):
        local_path = img.get("local_path")
        if not local_path or not os.path.exists(local_path):
            continue
        with open(local_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        images_payload.append(
            {
                "attachment": b64,
                "position": img.get("position"),
                "alt": img.get("alt"),
            }
        )

    variants_payload = []
    for v in exported.get("variants", []):
        variants_payload.append(
            {
                "option1": v.get("option1"),
                "option2": v.get("option2"),
                "option3": v.get("option3"),
                "sku": v.get("sku"),
                "barcode": v.get("barcode"),
                "price": v.get("price"),
                "compare_at_price": v.get("compare_at_price"),
                "position": v.get("position"),
                "taxable": v.get("taxable"),
                "weight": v.get("weight"),
                "weight_unit": v.get("weight_unit"),
                "requires_shipping": v.get("requires_shipping"),
                "inventory_policy": v.get("inventory_policy"),
                "inventory_management": v.get("inventory_management"),
            }
        )

    options_payload = [
        {"name": o.get("name"), "values": o.get("values")}
        for o in exported.get("options", [])
        if o.get("name")
    ]

    product_payload: Dict[str, Any] = {
        "title": exported.get("title"),
        "body_html": exported.get("body_html"),
        "vendor": exported.get("vendor"),
        "product_type": exported.get("product_type"),
        "tags": exported.get("tags"),
        "status": exported.get("status") or "draft",
        "handle": exported.get("handle"),
    }
    if exported.get("template_suffix"):
        product_payload["template_suffix"] = exported["template_suffix"]
    if options_payload:
        product_payload["options"] = options_payload
    if variants_payload:
        product_payload["variants"] = variants_payload
    if images_payload:
        product_payload["images"] = images_payload

    return {"product": product_payload}


def find_existing_destination_product(client: ShopifyClient, handle: str) -> Optional[Dict[str, Any]]:
    return find_product_by_handle(client, handle)


def normalize_tags(tags: Any) -> set:
    if not tags:
        return set()
    return {t.strip().lower() for t in str(tags).split(",") if t.strip()}


def normalize_options(options: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"name": o.get("name"), "values": list(o.get("values") or [])}
        for o in options or []
    ]


def diff_product_fields(existing: Dict[str, Any], exported: Dict[str, Any]) -> Dict[str, Any]:
    updates: Dict[str, Any] = {}

    for field in ("title", "body_html", "vendor", "product_type"):
        src_val = exported.get(field)
        if (existing.get(field) or None) != (src_val or None):
            updates[field] = src_val

    if normalize_tags(existing.get("tags")) != normalize_tags(exported.get("tags")):
        updates["tags"] = exported.get("tags")

    src_status = exported.get("status") or "draft"
    if existing.get("status") != src_status:
        updates["status"] = src_status

    src_template = exported.get("template_suffix")
    if src_template and existing.get("template_suffix") != src_template:
        updates["template_suffix"] = src_template

    src_options = normalize_options(exported.get("options"))
    if src_options and normalize_options(existing.get("options")) != src_options:
        updates["options"] = src_options

    return updates


def images_match(existing_images: List[Dict[str, Any]], exported_images: List[Dict[str, Any]]) -> bool:
    if len(existing_images) != len(exported_images):
        return False

    existing_sorted = sorted(existing_images, key=lambda i: i.get("position") or 0)
    exported_sorted = sorted(exported_images, key=lambda i: i.get("position") or 0)
    for dest_img, src_img in zip(existing_sorted, exported_sorted):
        if dest_img.get("position") != src_img.get("position"):
            return False
        if (dest_img.get("alt") or "") != (src_img.get("alt") or ""):
            return False

    return True


VARIANT_SYNC_FIELDS = [
    "price",
    "compare_at_price",
    "barcode",
    "weight",
    "weight_unit",
    "taxable",
    "requires_shipping",
    "inventory_policy",
    "inventory_management",
    "option1",
    "option2",
    "option3",
]


def diff_variant_fields(dest_variant: Dict[str, Any], src_variant: Dict[str, Any]) -> Dict[str, Any]:
    diff: Dict[str, Any] = {}
    for field in VARIANT_SYNC_FIELDS:
        dest_val = dest_variant.get(field)
        src_val = src_variant.get(field)
        if (dest_val if dest_val not in (None, "") else None) != (src_val if src_val not in (None, "") else None):
            diff[field] = src_val
    return diff


def match_variant(
    dest_by_sku: Dict[str, Dict[str, Any]],
    dest_by_options: Dict[Any, Dict[str, Any]],
    src_variant: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    sku_key = (src_variant.get("sku") or "").strip().lower()
    option_key = (src_variant.get("option1"), src_variant.get("option2"), src_variant.get("option3"))
    return (dest_by_sku.get(sku_key) if sku_key else None) or dest_by_options.get(option_key)


def update_existing_product(dest_client: ShopifyClient, existing: Dict[str, Any], exported: Dict[str, Any]) -> Dict[str, Any]:
    pid = existing["id"]
    title = exported.get("title")

    field_updates = diff_product_fields(existing, exported)
    if field_updates:
        logger.info("Updating mismatched fields for '%s' (id %s): %s", title, pid, sorted(field_updates.keys()))
        retry_with_backoff(
            lambda: dest_client.rest_put(f"products/{pid}", {"product": {"id": pid, **field_updates}})
        )
    else:
        logger.info("Product fields for '%s' already match source", title)

    if not images_match(existing.get("images", []), exported.get("images", [])):
        images_payload = []
        for img in exported.get("images", []):
            local_path = img.get("local_path")
            if not local_path or not os.path.exists(local_path):
                continue
            with open(local_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            images_payload.append({"attachment": b64, "position": img.get("position"), "alt": img.get("alt")})

        logger.info(
            "Replacing images for '%s' (%s -> %s) to match source",
            title,
            len(existing.get("images", [])),
            len(images_payload),
        )
        retry_with_backoff(
            lambda: dest_client.rest_put(f"products/{pid}", {"product": {"id": pid, "images": images_payload}})
        )
    else:
        logger.info("Images for '%s' already match source", title)

    dest_variants = existing.get("variants", [])
    dest_by_sku = {(v.get("sku") or "").strip().lower(): v for v in dest_variants if v.get("sku")}
    dest_by_options = {(v.get("option1"), v.get("option2"), v.get("option3")): v for v in dest_variants}

    for src_variant in exported.get("variants", []):
        match = match_variant(dest_by_sku, dest_by_options, src_variant)
        if match:
            diff = diff_variant_fields(match, src_variant)
            if diff:
                logger.info("Updating variant %s for '%s': %s", match.get("id"), title, sorted(diff.keys()))
                try:
                    retry_with_backoff(
                        lambda: dest_client.rest_put(
                            f"variants/{match['id']}", {"variant": {"id": match["id"], **diff}}
                        )
                    )
                except Exception as e:
                    logger.warning("Failed to update variant %s for '%s': %s", match.get("id"), title, e)
        else:
            logger.info("Creating missing variant (sku=%s) for '%s'", src_variant.get("sku"), title)
            variant_payload = {
                field: src_variant.get(field)
                for field in [
                    "option1",
                    "option2",
                    "option3",
                    "sku",
                    "barcode",
                    "price",
                    "compare_at_price",
                    "position",
                    "taxable",
                    "weight",
                    "weight_unit",
                    "requires_shipping",
                    "inventory_policy",
                    "inventory_management",
                ]
            }
            try:
                retry_with_backoff(
                    lambda: dest_client.rest_post(f"products/{pid}/variants", {"variant": variant_payload})
                )
            except Exception as e:
                logger.warning("Failed to create variant for '%s': %s", title, e)

    src_skus = {(v.get("sku") or "").strip().lower() for v in exported.get("variants", []) if v.get("sku")}
    extra_skus = set(dest_by_sku) - src_skus
    if extra_skus:
        logger.warning(
            "Destination '%s' has variant(s) not present in source (left untouched, not deleted): %s",
            title,
            sorted(extra_skus),
        )

    refreshed = retry_with_backoff(lambda: dest_client.rest_get(f"products/{pid}")).get("product")

    variants_by_sku = {(v.get("sku") or "").strip().lower(): v for v in refreshed.get("variants", []) if v.get("sku")}
    variants_by_position = {v.get("position"): v for v in refreshed.get("variants", [])}
    images_by_position = {img.get("position"): img for img in refreshed.get("images", [])}

    for src_variant in exported.get("variants", []):
        image_position = src_variant.get("image_position")
        if image_position is None:
            continue
        dest_image = images_by_position.get(image_position)
        if not dest_image:
            continue
        dest_variant = match_variant(variants_by_sku, {}, src_variant) or variants_by_position.get(
            src_variant.get("position")
        )
        if not dest_variant or dest_variant.get("image_id") == dest_image["id"]:
            continue
        try:
            retry_with_backoff(
                lambda: dest_client.rest_put(
                    f"variants/{dest_variant['id']}",
                    {"variant": {"id": dest_variant["id"], "image_id": dest_image["id"]}},
                )
            )
        except Exception as e:
            logger.warning("Failed to assign image to variant %s: %s", dest_variant.get("id"), e)

    sync_inventory(dest_client, exported, variants_by_sku, variants_by_position)

    return retry_with_backoff(lambda: dest_client.rest_get(f"products/{pid}")).get("product")


_locations_cache: Dict[str, Any] = {"checked": False, "location_id": None, "by_name": {}}


def _get_cached_dest_locations(dest_client: ShopifyClient) -> Dict[str, Any]:
    if _locations_cache["checked"]:
        return _locations_cache

    _locations_cache["checked"] = True
    try:
        locations = retry_with_backoff(lambda: dest_client.rest_get("locations")).get("locations", [])
    except Exception as e:
        logger.warning("Destination locations unavailable (%s) -- inventory sync will be skipped for this run", e)
        return _locations_cache

    if not locations:
        logger.warning("No destination locations found -- inventory sync will be skipped for this run")
        return _locations_cache

    _locations_cache["location_id"] = locations[0]["id"]
    _locations_cache["by_name"] = {
        (loc.get("name") or "").strip().lower(): loc["id"] for loc in locations if loc.get("name")
    }
    return _locations_cache


def sync_variant_inventory_item(dest_client: ShopifyClient, inventory_item_id: Optional[int], src_variant: Dict[str, Any]) -> None:
    if not inventory_item_id:
        return

    fields = []
    if src_variant.get("unit_cost") is not None:
        fields.append(f"cost: {gql_quote(src_variant['unit_cost'])}")
    if src_variant.get("country_code_of_origin"):
        fields.append(f"countryCodeOfOrigin: {src_variant['country_code_of_origin']}")
    if src_variant.get("harmonized_system_code"):
        fields.append(f"harmonizedSystemCode: {gql_quote(src_variant['harmonized_system_code'])}")
    if src_variant.get("province_code_of_origin"):
        fields.append(f"provinceCodeOfOrigin: {gql_quote(src_variant['province_code_of_origin'])}")

    if not fields:
        return

    mutation = f"""
    mutation {{
      inventoryItemUpdate(
        id: "gid://shopify/InventoryItem/{inventory_item_id}"
        input: {{ {", ".join(fields)} }}
      ) {{
        inventoryItem {{ id }}
        userErrors {{ field message }}
      }}
    }}
    """
    try:
        result = retry_with_backoff(lambda: dest_client.mutation(mutation))
    except Exception as e:
        logger.warning("Failed to sync inventory item %s: %s", inventory_item_id, e)
        return

    errors = (result.get("inventoryItemUpdate") or {}).get("userErrors")
    if errors:
        logger.warning("Failed to sync inventory item %s: %s", inventory_item_id, errors)


def sync_inventory(dest_client: ShopifyClient, exported: Dict[str, Any], variants_by_sku: Dict[str, Any], variants_by_position: Dict[Any, Any]) -> None:
    dest_locations = _get_cached_dest_locations(dest_client)
    location_id = dest_locations.get("location_id")
    if not location_id:
        return
    by_name = dest_locations.get("by_name", {})

    unmapped_names = set()

    for src_variant in exported.get("variants", []):
        if src_variant.get("inventory_management") != "shopify":
            continue

        dest_variant = (
            variants_by_sku.get((src_variant.get("sku") or "").strip().lower())
            or variants_by_position.get(src_variant.get("position"))
        )
        inventory_item_id = dest_variant.get("inventory_item_id") if dest_variant else None
        if not inventory_item_id:
            continue

        by_location = src_variant.get("inventory_by_location") or []
        if not by_location:
            quantity = src_variant.get("inventory_quantity")
            if quantity is None:
                continue
            by_location = [{"location_name": None, "available": quantity}]

        for entry in by_location:
            name_key = (entry.get("location_name") or "").strip().lower()
            target_location_id = by_name.get(name_key, location_id) if name_key else location_id
            if name_key and name_key not in by_name:
                unmapped_names.add(entry["location_name"])

            try:
                retry_with_backoff(
                    lambda: dest_client.rest_post(
                        "inventory_levels/set",
                        {
                            "location_id": target_location_id,
                            "inventory_item_id": inventory_item_id,
                            "available": entry.get("available"),
                        },
                    )
                )
            except Exception as e:
                logger.warning("Failed to set inventory for variant %s at location %s: %s", dest_variant.get("id"), entry.get("location_name"), e)

    if unmapped_names:
        logger.warning(
            "%s source location name(s) have no destination match -- their inventory was applied to the "
            "destination's first location instead: %s. Run transfer_locations.py --execute first to fix this.",
            len(unmapped_names), sorted(unmapped_names),
        )


def sync_graphql_only_fields(dest_client: ShopifyClient, product_id: int, exported: Dict[str, Any]) -> None:
    fields = []
    if exported.get("seo_title") or exported.get("seo_description"):
        fields.append(
            "seo: {"
            f" title: {gql_quote_or_null(exported.get('seo_title'))},"
            f" description: {gql_quote_or_null(exported.get('seo_description'))}"
            " }"
        )
    if exported.get("category_id"):
        fields.append(f"category: {json.dumps(exported['category_id'])}")
    if exported.get("requires_selling_plan") is not None:
        fields.append(f"requiresSellingPlan: {'true' if exported['requires_selling_plan'] else 'false'}")

    if not fields:
        return

    mutation = f"""
    mutation {{
      productUpdate(product: {{ id: {json.dumps(f"gid://shopify/Product/{product_id}")}, {", ".join(fields)} }}) {{
        product {{ id }}
        userErrors {{ field message }}
      }}
    }}
    """
    try:
        result = retry_with_backoff(lambda: dest_client.mutation(mutation))
    except Exception as e:
        logger.warning("Failed to sync seo/category for product %s: %s", product_id, e)
        return

    errors = (result.get("productUpdate") or {}).get("userErrors")
    if errors:
        logger.warning("Failed to sync seo/category for product %s: %s", product_id, errors)


def gql_quote_or_null(value: Optional[str]) -> str:
    return json.dumps(value, ensure_ascii=False) if value else "null"


REFERENCE_METAFIELD_TYPES = {
    "product_reference", "list.product_reference",
    "collection_reference", "list.collection_reference",
    "variant_reference", "list.variant_reference",
    "page_reference", "list.page_reference",
    "metaobject_reference", "list.metaobject_reference",
    "file_reference", "list.file_reference",
}


def parse_metafield_gid_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    value = value.strip()
    if value.startswith("["):
        try:
            parsed = json.loads(value)
            return [v for v in parsed if isinstance(v, str) and v.startswith("gid://")]
        except Exception:
            logger.warning(
                "Reference metafield value looks like a JSON list but failed to parse -- "
                "treating as unresolvable, source GID(s) will NOT be remapped: %r", value[:200],
            )
            return []
    if value.startswith("gid://"):
        return [value]
    return []


def resolve_source_reference_nodes(src_client: ShopifyClient, gids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not gids:
        return {}
    ids_str = ", ".join(gql_quote(g) for g in gids)
    query = f"""
    {{ nodes(ids: [{ids_str}]) {{
        id
        __typename
        ... on Product {{ handle }}
        ... on ProductVariant {{ sku }}
        ... on Collection {{ handle }}
        ... on Page {{ handle }}
        ... on Metaobject {{ handle type }}
        ... on MediaImage {{ image {{ url }} }}
        ... on GenericFile {{ url }}
        ... on Video {{ sources {{ url }} }}
    }} }}
    """
    try:
        data = retry_with_backoff(lambda: src_client.query(query))
    except Exception:
        logger.exception("Failed to resolve reference metafield source GIDs")
        return {}

    resolved: Dict[str, Dict[str, Any]] = {}
    for node in data.get("nodes") or []:
        if not node:
            continue
        info: Dict[str, Any] = {"typename": node.get("__typename")}
        if node.get("handle"):
            info["handle"] = node.get("handle")
        if node.get("sku"):
            info["sku"] = node.get("sku")
        if node.get("type"):
            info["metaobject_type"] = node.get("type")
        if node.get("image"):
            info["url"] = (node.get("image") or {}).get("url")
        if node.get("url"):
            info["url"] = node.get("url")
        if node.get("sources"):
            sources = node.get("sources") or []
            if sources:
                info["url"] = sources[0].get("url")
        resolved[node.get("id")] = info

    return resolved


_dest_reference_gid_cache: Dict[Tuple[str, str], Optional[str]] = {}


def find_destination_reference_gid(dest_client: ShopifyClient, info: Dict[str, Any]) -> Optional[str]:
    typename = info.get("typename")
    cache_key = (typename or "", info.get("handle") or info.get("sku") or "")
    if cache_key in _dest_reference_gid_cache:
        return _dest_reference_gid_cache[cache_key]

    gid: Optional[str] = None
    try:
        if typename == "Product" and info.get("handle"):
            data = retry_with_backoff(
                lambda: dest_client.query(f'{{ productByHandle(handle: {gql_quote(info["handle"])}) {{ id }} }}')
            )
            gid = (data.get("productByHandle") or {}).get("id")
        elif typename == "Collection" and info.get("handle"):
            data = retry_with_backoff(
                lambda: dest_client.query(f'{{ collectionByHandle(handle: {gql_quote(info["handle"])}) {{ id }} }}')
            )
            gid = (data.get("collectionByHandle") or {}).get("id")
        elif typename == "ProductVariant" and info.get("sku"):
            search = gql_quote(f'sku:{json.dumps(info["sku"])}')
            data = retry_with_backoff(
                lambda: dest_client.query(
                    f"{{ productVariants(first: 1, query: {search}) {{ edges {{ node {{ id }} }} }} }}"
                )
            )
            edges = (data.get("productVariants") or {}).get("edges") or []
            gid = edges[0]["node"]["id"] if edges else None
        elif typename == "Page" and info.get("handle"):
            search = gql_quote(f'handle:{json.dumps(info["handle"])}')
            data = retry_with_backoff(
                lambda: dest_client.query(f"{{ pages(first: 1, query: {search}) {{ edges {{ node {{ id }} }} }} }}")
            )
            edges = (data.get("pages") or {}).get("edges") or []
            gid = edges[0]["node"]["id"] if edges else None
        elif typename == "Metaobject" and info.get("handle") and info.get("metaobject_type"):
            data = retry_with_backoff(
                lambda: dest_client.query(
                    f'{{ metaobjectByHandle(handle: {{ type: {gql_quote(info["metaobject_type"])}, '
                    f'handle: {gql_quote(info["handle"])} }}) {{ id }} }}'
                )
            )
            gid = (data.get("metaobjectByHandle") or {}).get("id")
    except Exception:
        logger.exception("Failed to look up destination reference for %s", info)
        gid = None

    _dest_reference_gid_cache[cache_key] = gid
    return gid


_uploaded_reference_file_cache: Dict[str, Optional[str]] = {}


def upload_reference_file_to_destination(dest_client: ShopifyClient, typename: str, url: str) -> Optional[str]:
    if url in _uploaded_reference_file_cache:
        return _uploaded_reference_file_cache[url]

    content_type = {"MediaImage": "IMAGE", "GenericFile": "FILE", "Video": "VIDEO"}.get(typename, "FILE")
    mutation = f"""
    mutation {{
      fileCreate(files: [{{ originalSource: {gql_quote(url)}, contentType: {content_type} }}]) {{
        files {{ id fileStatus }}
        userErrors {{ field message }}
      }}
    }}
    """
    gid: Optional[str] = None
    try:
        result = retry_with_backoff(lambda: dest_client.mutation(mutation))
        payload = result.get("fileCreate") or {}
        errors = payload.get("userErrors")
        if errors:
            logger.warning("fileCreate failed for %s: %s", url, errors)
        else:
            files = payload.get("files") or []
            if files:
                gid = files[0].get("id")
    except Exception:
        logger.exception("fileCreate raised for %s", url)

    _uploaded_reference_file_cache[url] = gid
    return gid


def remap_metafield_for_destination(
    src_client: Optional[ShopifyClient], dest_client: ShopifyClient, mf: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    mf_type = mf.get("type")
    if mf_type not in REFERENCE_METAFIELD_TYPES:
        return mf

    src_gids = parse_metafield_gid_list(mf.get("value"))
    if not src_gids:
        return mf

    if src_client is None:
        logger.warning(
            "Dropping reference metafield %s.%s -- no live source connection to resolve it (import-from mode)",
            mf.get("namespace"), mf.get("key"),
        )
        return None

    node_info = resolve_source_reference_nodes(src_client, src_gids)

    dest_gids: List[str] = []
    for gid in src_gids:
        info = node_info.get(gid)
        if not info:
            continue
        if info.get("typename") in ("MediaImage", "GenericFile", "Video") and info.get("url"):
            dest_gid = upload_reference_file_to_destination(dest_client, info["typename"], info["url"])
        else:
            dest_gid = find_destination_reference_gid(dest_client, info)

        if dest_gid:
            dest_gids.append(dest_gid)
        else:
            logger.warning(
                "Could not remap %s.%s reference %s to destination -- dropping from value",
                mf.get("namespace"), mf.get("key"), gid,
            )

    if not dest_gids:
        return None

    new_value = json.dumps(dest_gids) if mf_type.startswith("list.") else dest_gids[0]
    return {**mf, "value": new_value}


def prepare_metafields_for_destination(
    src_client: Optional[ShopifyClient], dest_client: ShopifyClient, metafields: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    prepared = []
    for mf in metafields:
        remapped = remap_metafield_for_destination(src_client, dest_client, mf)
        if remapped is not None:
            prepared.append(remapped)
    return prepared


_dest_collection_cache: Dict[str, Optional[Tuple[int, bool]]] = {}


def resolve_destination_collection(dest_client: ShopifyClient, handle: str) -> Optional[Tuple[int, bool]]:
    if handle in _dest_collection_cache:
        return _dest_collection_cache[handle]

    result: Optional[Tuple[int, bool]] = None
    try:
        data = retry_with_backoff(
            lambda: dest_client.query(
                f'{{ collectionByHandle(handle: {gql_quote(handle)}) {{ id ruleSet {{ rules {{ column }} }} }} }}'
            )
        )
        coll = data.get("collectionByHandle")
        if coll:
            gid = coll["id"]
            result = (int(gid.rsplit("/", 1)[-1]), coll.get("ruleSet") is not None)
    except Exception:
        logger.exception("Failed to look up destination collection '%s'", handle)

    _dest_collection_cache[handle] = result
    return result


def fetch_source_product_collection_handles(src_client: ShopifyClient, product_id: int) -> List[str]:
    handles: List[str] = []
    after_clause = ""
    while True:
        query = f"""
        {{ product(id: "gid://shopify/Product/{product_id}") {{
            collections(first: 100{after_clause}) {{
              edges {{ node {{ handle }} }}
              pageInfo {{ hasNextPage endCursor }}
            }}
        }} }}
        """
        data = retry_with_backoff(lambda: src_client.query(query))
        connection = (data.get("product") or {}).get("collections") or {}
        handles.extend(edge["node"]["handle"] for edge in connection.get("edges", []))
        if not connection.get("pageInfo", {}).get("hasNextPage"):
            break
        after_clause = f', after: {gql_quote(connection["pageInfo"]["endCursor"])}'
    return handles


def sync_product_collections(
    src_client: ShopifyClient, dest_client: ShopifyClient, src_product_id: int, dest_product_id: int
) -> None:
    handles = fetch_source_product_collection_handles(src_client, src_product_id)
    added = already_member = smart_skipped = not_found = failed = 0

    for handle in handles:
        resolved = resolve_destination_collection(dest_client, handle)
        if not resolved:
            logger.warning("Collection '%s' not found on destination -- can't sync membership for it", handle)
            not_found += 1
            continue

        dest_collection_id, is_smart = resolved
        if is_smart:
            smart_skipped += 1
            continue

        try:
            retry_with_backoff(
                lambda: dest_client.rest_post(
                    "collects",
                    {"collect": {"product_id": dest_product_id, "collection_id": dest_collection_id}},
                )
            )
            added += 1
        except Exception as e:
            if "unprocessable entity" in str(e).lower():
                already_member += 1
            else:
                logger.warning("Failed to add product to collection '%s': %s", handle, e)
                failed += 1

    logger.info(
        "Collection membership: %s added, %s already member, %s smart (auto-managed, left alone), "
        "%s not found on destination, %s failed",
        added, already_member, smart_skipped, not_found, failed,
    )


def import_product(src_client: Optional[ShopifyClient], dest_client: ShopifyClient, exported: Dict[str, Any]) -> Dict[str, Any]:
    handle = exported.get("handle")
    existing = find_existing_destination_product(dest_client, handle)

    if existing:
        logger.info(
            "Product with handle '%s' already exists in destination (id %s); syncing to match source",
            handle,
            existing.get("id"),
        )
        new_product = update_existing_product(dest_client, existing, exported)
    else:
        payload = build_import_payload(exported)
        logger.info("Creating product '%s' on destination", exported.get("title"))
        result = retry_with_backoff(lambda: dest_client.rest_post("products", payload))
        new_product = result.get("product")
        if not new_product:
            raise RuntimeError(f"Failed to create product: {result}")
        logger.info("Created product %s (id %s)", new_product.get("title"), new_product.get("id"))

    new_pid = new_product["id"]
    sync_graphql_only_fields(dest_client, new_pid, exported)

    new_images = new_product.get("images", [])
    new_variants = new_product.get("variants", [])

    images_by_position = {img.get("position"): img for img in new_images}
    variants_by_sku = {(v.get("sku") or "").strip().lower(): v for v in new_variants if v.get("sku")}
    variants_by_position = {v.get("position"): v for v in new_variants}

    if not existing:
        for src_variant in exported.get("variants", []):
            image_position = src_variant.get("image_position")
            if image_position is None:
                continue
            dest_image = images_by_position.get(image_position)
            if not dest_image:
                continue
            dest_variant = (
                variants_by_sku.get((src_variant.get("sku") or "").strip().lower())
                or variants_by_position.get(src_variant.get("position"))
            )
            if not dest_variant:
                continue
            try:
                retry_with_backoff(
                    lambda: dest_client.rest_put(
                        f"variants/{dest_variant['id']}",
                        {"variant": {"id": dest_variant["id"], "image_id": dest_image["id"]}},
                    )
                )
            except Exception as e:
                logger.warning("Failed to assign image to variant %s: %s", dest_variant.get("id"), e)

        sync_inventory(dest_client, exported, variants_by_sku, variants_by_position)

    if exported.get("metafields"):
        prepared = prepare_metafields_for_destination(src_client, dest_client, exported["metafields"])
        result = set_metafields(dest_client, product_gid(new_pid), prepared)
        logger.info(
            "Product metafields: %s updated, %s skipped", result["updated_count"], result["skipped_count"]
        )

    for src_variant in exported.get("variants", []):
        dest_variant = (
            variants_by_sku.get((src_variant.get("sku") or "").strip().lower())
            or variants_by_position.get(src_variant.get("position"))
        )
        if not dest_variant:
            if src_variant.get("metafields"):
                logger.warning("Could not match destination variant for metafields (sku=%s)", src_variant.get("sku"))
            continue

        if src_variant.get("metafields"):
            prepared = prepare_metafields_for_destination(src_client, dest_client, src_variant["metafields"])
            result = set_metafields(dest_client, product_variant_gid(dest_variant["id"]), prepared)
            logger.info(
                "Variant %s metafields: %s updated, %s skipped",
                dest_variant.get("id"),
                result["updated_count"],
                result["skipped_count"],
            )

        sync_variant_inventory_item(dest_client, dest_variant.get("inventory_item_id"), src_variant)

    for src_image in exported.get("images", []):
        if not src_image.get("metafields"):
            continue
        dest_image = images_by_position.get(src_image.get("position"))
        if not dest_image:
            logger.warning("Could not match destination image for metafields (position=%s)", src_image.get("position"))
            continue
        prepared = prepare_metafields_for_destination(src_client, dest_client, src_image["metafields"])
        result = set_metafields(dest_client, product_image_gid(dest_image["id"]), prepared)
        logger.info(
            "Image %s metafields: %s updated, %s skipped",
            dest_image.get("id"),
            result["updated_count"],
            result["skipped_count"],
        )

    if exported.get("id") and src_client is not None:
        sync_product_collections(src_client, dest_client, exported["id"], new_pid)
    elif exported.get("id"):
        logger.warning(
            "Skipping collection-membership sync for '%s' -- no live source connection (import-from mode)",
            exported.get("title"),
        )

    return new_product


def transfer_one(
    src_client: ShopifyClient,
    dest_client: ShopifyClient,
    out_dir: Path,
    identifier: str,
    execute: bool,
    write_xlsx: bool = False,
) -> Dict[str, Any]:
    product = find_source_product(src_client, identifier)
    exported = export_product(src_client, product, out_dir)

    ts = int(time.time())
    out_file = out_dir / f"product_export_{exported['handle']}_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(exported, f, indent=2, ensure_ascii=False)

    if write_xlsx:
        from utils.tabular_io import export_to_xlsx
        export_to_xlsx(exported, out_dir / f"product_export_{exported['handle']}_{ts}.xlsx")

    logger.info(
        "Product '%s': %s image(s), %s variant(s), %s product metafield(s). Export: %s",
        exported.get("title"),
        len(exported.get("images", [])),
        len(exported.get("variants", [])),
        len(exported.get("metafields", [])),
        out_file,
    )

    if execute:
        new_product = import_product(src_client, dest_client, exported)
        logger.info(
            "Transfer complete for '%s'. Destination product id: %s, handle: %s",
            exported.get("title"),
            new_product.get("id"),
            new_product.get("handle"),
        )
    else:
        logger.info("Dry-run only for '%s'. Re-run with --execute to write it to the destination store.", exported.get("title"))

    return exported


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transfer complete product(s) - details, images, variants, metafields - from Src to dest"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--product", help="Source product ID or handle to transfer")
    group.add_argument("--all", action="store_true", help="Transfer every product in the source store")
    group.add_argument(
        "--import-from",
        help=(
            "Skip the source export step and import product(s) from this previously-saved canonical "
            "JSON file instead (a single product dict, or a list of them -- see docs/CANONICAL_SCHEMA.md). "
            "Lets you import from a non-Shopify source connector (Wix/BigCommerce/a database), or replay a "
            "prior dry-run export, without a live source Shopify store. No SRC_SHOPIFY_* credentials needed "
            "in this mode, but reference-type metafields and collection-membership sync are skipped since "
            "those require a live source connection."
        ),
    )
    parser.add_argument("--execute", action="store_true", help="Perform the real import into the destination store")
    parser.add_argument("--out", default="Results", help="Output directory for export JSON and downloaded images")
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
    parser.add_argument("--limit", type=int, default=None, help="With --all, only transfer the first N products")
    parser.add_argument(
        "--start-at",
        type=int,
        default=0,
        help="With --all, skip the first N products and resume from there (0-indexed)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"With --all, number of products to transfer concurrently (default {DEFAULT_WORKERS}; 1 = sequential)",
    )
    args = parser.parse_args()

    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")
    require_env(DEST_SHOPIFY_SHOP=dest_shop, DEST_SHOPIFY_ACCESS_TOKEN=dest_token)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    dest_client = make_client(dest_shop, dest_token)

    if args.import_from:
        logger.info("Loading product export(s) from %s (skipping source fetch)", args.import_from)
        if args.import_from.lower().endswith(".xlsx"):
            from utils.tabular_io import import_from_xlsx
            loaded = import_from_xlsx(args.import_from)
        else:
            with open(args.import_from, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        products = loaded if isinstance(loaded, list) else [loaded]
        logger.info("Loaded %s product(s) to import", len(products))

        for exported in products:
            try:
                if args.execute:
                    new_product = import_product(None, dest_client, exported)
                    logger.info(
                        "Transfer complete for '%s'. Destination product id: %s, handle: %s",
                        exported.get("title"), new_product.get("id"), new_product.get("handle"),
                    )
                else:
                    logger.info(
                        "Dry-run: would import '%s'. Re-run with --execute to write it to the destination store.",
                        exported.get("title"),
                    )
            except Exception:
                logger.exception("Failed to import product '%s'", exported.get("title"))
        return

    src_shop = os.getenv("SRC_SHOPIFY_SHOP")
    src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
    require_env(SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token)

    src_client = make_client(src_shop, src_token)

    if args.all:
        entries = fetch_all_product_handles(src_client)
        total_found = len(entries)
        if args.start_at:
            entries = entries[args.start_at :]
        if args.limit:
            entries = entries[: args.limit]
        logger.info(
            "Transferring %s product(s) from %s to %s (starting at #%s of %s found)",
            len(entries),
            src_shop,
            dest_shop,
            args.start_at + 1,
            total_found,
        )
        progress_lock = threading.Lock()
        completed = 0

        def process(entry: Dict[str, Any]) -> None:
            nonlocal completed
            identifier = entry.get("handle") or str(entry.get("id"))
            with progress_lock:
                completed += 1
                position = completed
            logger.info("[%s/%s] %s", position, len(entries), identifier)
            try:
                transfer_one(src_client, dest_client, out_dir, identifier, args.execute, write_xlsx=args.xlsx)
            except Exception:
                logger.exception("Failed to transfer product %s", identifier)

        logger.info("Using %s worker(s)", args.workers)
        run_concurrently(entries, process, max_workers=args.workers, label="product")
    else:
        transfer_one(src_client, dest_client, out_dir, args.product, args.execute, write_xlsx=args.xlsx)


if __name__ == "__main__":
    main()
