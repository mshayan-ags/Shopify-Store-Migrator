import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from dotenv import load_dotenv

from utils.shopify_client import ShopifyClient
from transfer.transfer_collections import (
    admin_base_for_host,
    build_collection_lookup,
    fetch_all_collections,
)
from utils.concurrency_utils import retry_with_backoff, gql_quote
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_store_metafields")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

T = TypeVar("T")


SUPPORTED_OWNER_TYPES = {
    "shop",
    "collection",
    "product",
    "variant",
    "product_image",
    "blog",
    "article",
    "page",
    "customer",
    "location",
    "order",
    "draft_order",
}


def shop_host(shop_name: str) -> str:
    return f"{shop_name}.myshopify.com"


def chunk_items(items: List[Dict[str, Any]], chunk_size: int = 25) -> List[List[Dict[str, Any]]]:
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


def fetch_all_products_with_title(client: ShopifyClient) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    after_clause = ""

    while True:
        query = f"""
        {{
          products(first: 250{after_clause}) {{
            edges {{ node {{ id handle title }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """
        data = retry_with_backoff(lambda: client.query(query))
        connection = data["products"]

        for edge in connection["edges"]:
            node = edge["node"]
            products.append(
                {"id": int(node["id"].rsplit("/", 1)[-1]), "handle": node["handle"], "title": node["title"]}
            )

        if not connection["pageInfo"]["hasNextPage"]:
            break
        after_clause = f', after: {gql_quote(connection["pageInfo"]["endCursor"])}'

    return products


def fetch_paginated_resource(client: ShopifyClient, path: str, response_key: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    since_id = 0

    while True:
        params = {"limit": 250}
        if since_id:
            params["since_id"] = since_id

        def fetch_page():
            return client.rest_get(path, params=params)

        page = retry_with_backoff(fetch_page)
        page_items = page.get(response_key, [])
        if not page_items:
            break

        items.extend(page_items)
        since_id = page_items[-1].get("id") or 0
        if len(page_items) < 250 or not since_id:
            break

    return items


def fetch_owner_metafields(client: ShopifyClient, owner_resource: str, owner_id: int) -> List[Dict[str, Any]]:
    metafields: List[Dict[str, Any]] = []
    since_id = 0

    while True:
        if owner_resource == "shop":
            params = {"limit": 250}
        else:
            params = {
                "metafield[owner_resource]": owner_resource,
                "metafield[owner_id]": owner_id,
                "limit": 250,
            }
        if since_id:
            params["since_id"] = since_id

        def fetch_page():
            return client.rest_get("metafields", params=params)

        page = retry_with_backoff(fetch_page)
        page_metafields = page.get("metafields", [])
        if not page_metafields:
            break

        metafields.extend(page_metafields)
        since_id = page_metafields[-1].get("id") or 0
        if len(page_metafields) < 250 or not since_id:
            break

    return metafields


def _build_metafields_set_mutation(owner_gid: str, batch: List[Dict[str, Any]]) -> str:
    metafield_inputs = [
        "{"
        f"ownerId: {gql_quote(owner_gid)}, "
        f"namespace: {gql_quote(mf.get('namespace'))}, "
        f"key: {gql_quote(mf.get('key'))}, "
        f"value: {gql_quote(mf.get('value'))}, "
        f"type: {gql_quote(mf.get('type'))}"
        "}"
        for mf in batch
    ]
    return f"""
    mutation {{
      metafieldsSet(metafields: [{', '.join(metafield_inputs)}]) {{
        metafields {{
          id
          namespace
          key
          value
          type
        }}
        userErrors {{
          field
          message
        }}
      }}
    }}
    """


def set_metafields(dest_client: ShopifyClient, owner_gid: str, metafields: List[Dict[str, Any]]) -> Dict[str, Any]:
    updated: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for original_batch in chunk_items(metafields, 25):
        batch = list(original_batch)

        while batch:
            mutation = _build_metafields_set_mutation(owner_gid, batch)

            try:
                result = retry_with_backoff(lambda: dest_client.mutation(mutation))
            except Exception as e:
                logger.warning("MetafieldsSet failed: %s", e)
                for mf in batch:
                    skipped.append(
                        {
                            "namespace": mf.get("namespace"),
                            "key": mf.get("key"),
                            "reason": str(e),
                        }
                    )
                batch = []
                break

            payload = result.get("metafieldsSet", {})
            errors = payload.get("userErrors")

            if errors:
                bad_indices: Dict[int, str] = {}
                for err in errors:
                    field_path = err.get("field") or []
                    if len(field_path) >= 2 and field_path[0] == "metafields":
                        try:
                            bad_indices[int(field_path[1])] = err.get("message", "")
                        except ValueError:
                            continue

                if not bad_indices:
                    error_text = "; ".join(f"{err.get('field')}: {err.get('message')}" for err in errors)
                    logger.warning("MetafieldsSet returned userErrors: %s", error_text)
                    for mf in batch:
                        skipped.append(
                            {
                                "namespace": mf.get("namespace"),
                                "key": mf.get("key"),
                                "reason": error_text or "userErrors returned by metafieldsSet",
                            }
                        )
                    batch = []
                    break

                for idx, message in bad_indices.items():
                    if idx < len(batch):
                        mf = batch[idx]
                        logger.warning(
                            "Skipping metafield %s.%s: %s", mf.get("namespace"), mf.get("key"), message
                        )
                        skipped.append({"namespace": mf.get("namespace"), "key": mf.get("key"), "reason": message})

                batch = [mf for i, mf in enumerate(batch) if i not in bad_indices]
                continue

            for metafield in payload.get("metafields", []):
                updated.append(
                    {
                        "namespace": metafield.get("namespace"),
                        "key": metafield.get("key"),
                        "value": metafield.get("value"),
                        "type": metafield.get("type"),
                    }
                )
            break

    return {
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "metafields": updated,
        "skipped": skipped,
    }


METAFIELD_DEFINITION_OWNER_TYPES = [
    "SHOP",
    "COLLECTION",
    "PRODUCT",
    "PRODUCTVARIANT",
    "BLOG",
    "ARTICLE",
    "PAGE",
    "CUSTOMER",
    "LOCATION",
    "ORDER",
    "DRAFTORDER",
]


def fetch_metafield_definitions(client: ShopifyClient, owner_type: str) -> List[Dict[str, Any]]:
    definitions: List[Dict[str, Any]] = []
    after_clause = ""

    while True:
        query = f"""
        {{
          metafieldDefinitions(ownerType: {owner_type}, first: 100{after_clause}) {{
            edges {{
              node {{
                id
                namespace
                key
                name
                description
                pinnedPosition
                type {{ name }}
                validations {{ name value }}
              }}
            }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """
        data = retry_with_backoff(lambda: client.query(query))
        connection = data["metafieldDefinitions"]
        definitions.extend(edge["node"] for edge in connection["edges"])

        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after_clause = f', after: {gql_quote(page_info["endCursor"])}'

    return definitions


def export_metafield_definitions(src_client: ShopifyClient) -> List[Dict[str, Any]]:
    exported = []
    for owner_type in METAFIELD_DEFINITION_OWNER_TYPES:
        for d in fetch_metafield_definitions(src_client, owner_type):
            exported.append(
                {
                    "owner_type": owner_type,
                    "namespace": d["namespace"],
                    "key": d["key"],
                    "name": d["name"],
                    "description": d.get("description"),
                    "type": d["type"]["name"],
                    "pinned": d.get("pinnedPosition") is not None,
                    "validations": [{"name": v["name"], "value": v["value"]} for v in d.get("validations", [])],
                }
            )
    logger.info("Exported %s metafield definition(s)", len(exported))
    return exported


def import_metafield_definitions(
    dest_client: ShopifyClient,
    exported: List[Dict[str, Any]],
    metaobject_gid_map: Optional[Dict[str, str]] = None,
) -> None:
    metaobject_gid_map = metaobject_gid_map or {}
    existing_by_owner: Dict[str, Dict[Any, Dict[str, Any]]] = {
        owner_type: {(d["namespace"], d["key"]): d for d in fetch_metafield_definitions(dest_client, owner_type)}
        for owner_type in METAFIELD_DEFINITION_OWNER_TYPES
    }

    created = 0
    already_existed = 0
    failed = 0
    skipped_unmapped_metaobject_ref = 0

    for definition in exported:
        owner_type = definition["owner_type"]
        dedupe_key = (definition["namespace"], definition["key"])
        if dedupe_key in existing_by_owner.get(owner_type, {}):
            already_existed += 1
            continue

        validations = []
        unmapped = False
        for v in definition.get("validations", []):
            value = v["value"]
            if v["name"] == "metaobject_definition_id":
                mapped = metaobject_gid_map.get(value)
                if not mapped:
                    logger.warning(
                        "Skipping metafield definition %s.%s (%s): references metaobject definition %s "
                        "with no known destination mapping -- transfer metaobjects first",
                        definition["namespace"],
                        definition["key"],
                        owner_type,
                        value,
                    )
                    unmapped = True
                    break
                value = mapped
            validations.append({"name": v["name"], "value": value})

        if unmapped:
            skipped_unmapped_metaobject_ref += 1
            continue

        validations_literal = (
            "["
            + ", ".join(f'{{ name: {gql_quote(v["name"])}, value: {gql_quote(v["value"])} }}' for v in validations)
            + "]"
        )

        mutation = f"""
        mutation {{
          metafieldDefinitionCreate(definition: {{
            namespace: {gql_quote(definition["namespace"])}
            key: {gql_quote(definition["key"])}
            name: {gql_quote(definition["name"])}
            description: {gql_quote(definition.get("description"))}
            ownerType: {owner_type}
            type: {gql_quote(definition["type"])}
            pin: {"true" if definition.get("pinned") else "false"}
            validations: {validations_literal}
          }}) {{
            createdDefinition {{ id }}
            userErrors {{ field message }}
          }}
        }}
        """
        create_error: Optional[str] = None
        try:
            result = retry_with_backoff(lambda: dest_client.mutation(mutation))
            errors = (result.get("metafieldDefinitionCreate") or {}).get("userErrors")
            if errors:
                create_error = str(errors)
        except Exception as e:
            create_error = str(e)

        if create_error is None:
            created += 1
            continue

        enable_mutation = f"""
        mutation {{
          standardMetafieldDefinitionEnable(
            ownerType: {owner_type}
            namespace: {gql_quote(definition["namespace"])}
            key: {gql_quote(definition["key"])}
            pin: {"true" if definition.get("pinned") else "false"}
          ) {{
            createdDefinition {{ id }}
            userErrors {{ field message }}
          }}
        }}
        """
        try:
            enable_result = retry_with_backoff(lambda: dest_client.mutation(enable_mutation))
            enable_errors = (enable_result.get("standardMetafieldDefinitionEnable") or {}).get("userErrors")
        except Exception as e:
            enable_errors = str(e)

        if enable_errors:
            logger.warning(
                "Failed to create metafield definition %s.%s (%s): %s (standard-enable fallback: %s)",
                definition["namespace"],
                definition["key"],
                owner_type,
                create_error,
                enable_errors,
            )
            failed += 1
            continue

        created += 1

    logger.info(
        "Metafield definitions import complete: %s created, %s already existed, %s failed, %s skipped (unmapped metaobject reference)",
        created,
        already_existed,
        failed,
        skipped_unmapped_metaobject_ref,
    )


def shop_gid(shop_id: int) -> str:
    return f"gid://shopify/Shop/{shop_id}"


def collection_gid(collection_id: int) -> str:
    return f"gid://shopify/Collection/{collection_id}"


def product_gid(product_id: int) -> str:
    return f"gid://shopify/Product/{product_id}"


def product_variant_gid(variant_id: int) -> str:
    return f"gid://shopify/ProductVariant/{variant_id}"


def product_image_gid(image_id: int) -> str:
    return f"gid://shopify/ProductImage/{image_id}"


def blog_gid(blog_id: int) -> str:
    return f"gid://shopify/Blog/{blog_id}"


def page_gid(page_id: int) -> str:
    return f"gid://shopify/Page/{page_id}"


def customer_gid(customer_id: int) -> str:
    return f"gid://shopify/Customer/{customer_id}"


def location_gid(location_id: int) -> str:
    return f"gid://shopify/Location/{location_id}"


def order_gid(order_id: int) -> str:
    return f"gid://shopify/Order/{order_id}"


def draft_order_gid(draft_order_id: int) -> str:
    return f"gid://shopify/DraftOrder/{draft_order_id}"


def safe_fetch(label: str, func: Callable[[], List[Any]]) -> List[Any]:
    try:
        return func()
    except Exception as e:
        logger.warning(
            "Skipping %s: %s (likely missing an Admin API scope for this app -- "
            "grant it in Shopify Admin > Apps > [app] > Configuration and reinstall)",
            label,
            e,
        )
        return []


def fetch_all_source_owners(src_client: ShopifyClient) -> Dict[str, List[Dict[str, Any]]]:
    owners: Dict[str, List[Dict[str, Any]]] = {owner_type: [] for owner_type in SUPPORTED_OWNER_TYPES}

    shop = safe_fetch("shop", lambda: [retry_with_backoff(lambda: src_client.rest_get("shop")).get("shop") or {}])
    shop = shop[0] if shop else {}
    if shop.get("id"):
        owners["shop"].append(
            {
                "id": shop.get("id"),
                "name": shop.get("name"),
            }
        )

    for collection in safe_fetch("custom collections", lambda: fetch_all_collections(src_client, "custom_collections")):
        owners["collection"].append(
            {
                "id": collection.get("id"),
                "handle": collection.get("handle"),
                "title": collection.get("title"),
                "source_type": "custom",
            }
        )

    for collection in safe_fetch("smart collections", lambda: fetch_all_collections(src_client, "smart_collections")):
        owners["collection"].append(
            {
                "id": collection.get("id"),
                "handle": collection.get("handle"),
                "title": collection.get("title"),
                "source_type": "smart",
            }
        )

    products = safe_fetch("products", lambda: fetch_all_products_with_title(src_client))
    for product in products:
        owners["product"].append(
            {
                "id": product.get("id"),
                "handle": product.get("handle"),
                "title": product.get("title"),
            }
        )

        product_id = product.get("id")
        if not product_id:
            continue

        label = product.get("handle") or product_id
        variants = safe_fetch(
            f"variants for product {label}",
            lambda: fetch_paginated_resource(src_client, f"products/{product_id}/variants", "variants"),
        )
        for variant in variants:
            owners["variant"].append(
                {
                    "id": variant.get("id"),
                    "product_id": product_id,
                    "product_handle": product.get("handle"),
                    "product_title": product.get("title"),
                    "sku": variant.get("sku"),
                    "title": variant.get("title"),
                    "position": variant.get("position"),
                }
            )

        images = safe_fetch(
            f"images for product {label}",
            lambda: fetch_paginated_resource(src_client, f"products/{product_id}/images", "images"),
        )
        for image in images:
            owners["product_image"].append(
                {
                    "id": image.get("id"),
                    "product_id": product_id,
                    "product_handle": product.get("handle"),
                    "product_title": product.get("title"),
                    "position": image.get("position"),
                    "alt": image.get("alt"),
                    "src": image.get("src"),
                }
            )

    for blog in safe_fetch("blogs", lambda: fetch_paginated_resource(src_client, "blogs", "blogs")):
        owners["blog"].append(
            {
                "id": blog.get("id"),
                "handle": blog.get("handle"),
                "title": blog.get("title"),
            }
        )

        blog_id = blog.get("id")
        articles = safe_fetch(
            f"articles for blog {blog.get('handle') or blog_id}",
            lambda: fetch_paginated_resource(src_client, f"blogs/{blog_id}/articles", "articles"),
        )
        for article in articles:
            owners["article"].append(
                {
                    "id": article.get("id"),
                    "handle": article.get("handle"),
                    "title": article.get("title"),
                    "blog_id": blog.get("id"),
                    "blog_handle": blog.get("handle"),
                    "blog_title": blog.get("title"),
                }
            )

    for page in safe_fetch("pages", lambda: fetch_paginated_resource(src_client, "pages", "pages")):
        owners["page"].append(
            {
                "id": page.get("id"),
                "handle": page.get("handle"),
                "title": page.get("title"),
            }
        )

    for customer in safe_fetch("customers", lambda: fetch_paginated_resource(src_client, "customers", "customers")):
        owners["customer"].append(
            {
                "id": customer.get("id"),
                "email": customer.get("email"),
                "first_name": customer.get("first_name"),
                "last_name": customer.get("last_name"),
            }
        )

    for location in safe_fetch("locations", lambda: fetch_paginated_resource(src_client, "locations", "locations")):
        owners["location"].append(
            {
                "id": location.get("id"),
                "name": location.get("name"),
            }
        )

    for order in safe_fetch("orders", lambda: fetch_paginated_resource(src_client, "orders", "orders")):
        owners["order"].append(
            {
                "id": order.get("id"),
                "name": order.get("name"),
                "email": order.get("email"),
            }
        )

    for draft_order in safe_fetch(
        "draft orders", lambda: fetch_paginated_resource(src_client, "draft_orders", "draft_orders")
    ):
        owners["draft_order"].append(
            {
                "id": draft_order.get("id"),
                "name": draft_order.get("name"),
                "email": draft_order.get("email"),
            }
        )

    return owners


def export_store_metafields(src_client: ShopifyClient) -> Dict[str, Any]:
    exported: Dict[str, Any] = {"owners": [], "unsupported_owners": []}
    source_owners = fetch_all_source_owners(src_client)

    for owner_type, items in source_owners.items():
        for item in items:
            owner_id = item.get("id")
            if not owner_id:
                continue

            label = item.get("handle") or item.get("title") or item.get("email") or item.get("name") or str(owner_id)
            logger.info("Exporting metafields for %s (%s)", owner_type, label)

            try:
                metafields = fetch_owner_metafields(src_client, owner_type, owner_id)
            except Exception as e:
                logger.warning("Failed to fetch metafields for %s (%s): %s", owner_type, label, e)
                continue

            owner_entry = {
                "owner_resource": owner_type,
                "owner_id": owner_id,
                "identity": {k: v for k, v in item.items() if k != "id"},
                "metafields": metafields,
            }
            exported["owners"].append(owner_entry)
            logger.info("Exported %s metafield(s) for %s (%s)", len(metafields), owner_type, label)

    return exported


def build_destination_indexes(dest_client: ShopifyClient) -> Dict[str, Any]:
    indexes: Dict[str, Any] = {}

    shop_rows = safe_fetch("destination shop", lambda: [retry_with_backoff(lambda: dest_client.rest_get("shop")).get("shop") or {}])
    shop = shop_rows[0] if shop_rows else {}
    indexes["shop"] = {"id": shop.get("id"), "gid": shop_gid(shop.get("id")) if shop.get("id") else None}

    indexes["collections"] = build_collection_lookup(
        safe_fetch("destination custom collections", lambda: fetch_all_collections(dest_client, "custom_collections"))
        + safe_fetch("destination smart collections", lambda: fetch_all_collections(dest_client, "smart_collections"))
    )

    products = safe_fetch("destination products", lambda: fetch_all_products_with_title(dest_client))
    indexes["products_by_handle"] = {
        (product.get("handle") or "").strip().lower(): product
        for product in products
        if product.get("handle")
    }
    indexes["variants_by_product_handle"] = {}
    indexes["images_by_product_handle"] = {}
    for product in products:
        handle_key = (product.get("handle") or "").strip().lower()
        product_id = product.get("id")
        if not handle_key or not product_id:
            continue

        variants = safe_fetch(
            f"destination variants for product {handle_key}",
            lambda: fetch_paginated_resource(dest_client, f"products/{product_id}/variants", "variants"),
        )
        indexes["variants_by_product_handle"][handle_key] = {
            "by_sku": {
                (variant.get("sku") or "").strip().lower(): variant
                for variant in variants
                if variant.get("sku")
            },
            "by_title": {
                (variant.get("title") or "").strip().lower(): variant
                for variant in variants
                if variant.get("title")
            },
            "by_position": {
                str(variant.get("position")): variant
                for variant in variants
                if variant.get("position") is not None
            },
        }

        images = safe_fetch(
            f"destination images for product {handle_key}",
            lambda: fetch_paginated_resource(dest_client, f"products/{product_id}/images", "images"),
        )
        indexes["images_by_product_handle"][handle_key] = {
            "by_position": {
                str(image.get("position")): image
                for image in images
                if image.get("position") is not None
            },
            "by_alt": {
                (image.get("alt") or "").strip().lower(): image
                for image in images
                if image.get("alt")
            },
        }

    blogs = safe_fetch("destination blogs", lambda: fetch_paginated_resource(dest_client, "blogs", "blogs"))
    indexes["blogs_by_handle"] = {
        (blog.get("handle") or "").strip().lower(): blog for blog in blogs if blog.get("handle")
    }
    indexes["blogs_by_title"] = {
        (blog.get("title") or "").strip().lower(): blog for blog in blogs if blog.get("title")
    }

    pages = safe_fetch("destination pages", lambda: fetch_paginated_resource(dest_client, "pages", "pages"))
    indexes["pages_by_handle"] = {
        (page.get("handle") or "").strip().lower(): page for page in pages if page.get("handle")
    }
    indexes["pages_by_title"] = {
        (page.get("title") or "").strip().lower(): page for page in pages if page.get("title")
    }

    customers = safe_fetch("destination customers", lambda: fetch_paginated_resource(dest_client, "customers", "customers"))
    indexes["customers_by_email"] = {
        (customer.get("email") or "").strip().lower(): customer for customer in customers if customer.get("email")
    }

    locations = safe_fetch("destination locations", lambda: fetch_paginated_resource(dest_client, "locations", "locations"))
    indexes["locations_by_name"] = {
        (location.get("name") or "").strip().lower(): location for location in locations if location.get("name")
    }

    orders = safe_fetch("destination orders", lambda: fetch_paginated_resource(dest_client, "orders", "orders"))
    indexes["orders_by_name"] = {
        (order.get("name") or "").strip().lower(): order for order in orders if order.get("name")
    }

    draft_orders = safe_fetch(
        "destination draft orders", lambda: fetch_paginated_resource(dest_client, "draft_orders", "draft_orders")
    )
    indexes["draft_orders_by_name"] = {
        (draft_order.get("name") or "").strip().lower(): draft_order
        for draft_order in draft_orders
        if draft_order.get("name")
    }

    articles_by_blog = {}
    for blog in blogs:
        blog_key = (blog.get("handle") or blog.get("title") or "").strip().lower()
        blog_id = blog.get("id")
        article_items = safe_fetch(
            f"destination articles for blog {blog_key}",
            lambda: fetch_paginated_resource(dest_client, f"blogs/{blog_id}/articles", "articles"),
        )
        articles_by_blog[blog_key] = {
            "by_handle": {
                (article.get("handle") or "").strip().lower(): article
                for article in article_items
                if article.get("handle")
            },
            "by_title": {
                (article.get("title") or "").strip().lower(): article
                for article in article_items
                if article.get("title")
            },
        }
    indexes["articles_by_blog"] = articles_by_blog

    return indexes


def resolve_owner_gid(owner_type: str, identity: Dict[str, Any], indexes: Dict[str, Any]) -> Optional[str]:
    if owner_type == "shop":
        shop_id = indexes.get("shop", {}).get("id")
        return shop_gid(shop_id) if shop_id else None

    if owner_type == "collection":
        handle_key = (identity.get("handle") or "").strip().lower()
        title_key = (identity.get("title") or "").strip().lower()
        collections = indexes.get("collections", {})
        destination = collections["by_handle"].get(handle_key) or collections["by_title"].get(title_key)
        return collection_gid(destination.get("id")) if destination and destination.get("id") else None

    if owner_type == "product":
        handle_key = (identity.get("handle") or "").strip().lower()
        destination = indexes.get("products_by_handle", {}).get(handle_key)
        return product_gid(destination.get("id")) if destination and destination.get("id") else None

    if owner_type == "variant":
        product_handle = (identity.get("product_handle") or "").strip().lower()
        variant_sku = (identity.get("sku") or "").strip().lower()
        variant_title = (identity.get("title") or "").strip().lower()
        variant_position = identity.get("position")
        product_variants = indexes.get("variants_by_product_handle", {}).get(product_handle, {})
        destination = (
            product_variants.get("by_sku", {}).get(variant_sku)
            or product_variants.get("by_title", {}).get(variant_title)
            or product_variants.get("by_position", {}).get(str(variant_position))
        )
        return product_variant_gid(destination.get("id")) if destination and destination.get("id") else None

    if owner_type == "product_image":
        product_handle = (identity.get("product_handle") or "").strip().lower()
        image_position = identity.get("position")
        image_alt = (identity.get("alt") or "").strip().lower()
        product_images = indexes.get("images_by_product_handle", {}).get(product_handle, {})
        destination = (
            product_images.get("by_position", {}).get(str(image_position))
            or product_images.get("by_alt", {}).get(image_alt)
        )
        return product_image_gid(destination.get("id")) if destination and destination.get("id") else None

    if owner_type == "blog":
        handle_key = (identity.get("handle") or "").strip().lower()
        title_key = (identity.get("title") or "").strip().lower()
        destination = indexes.get("blogs_by_handle", {}).get(handle_key) or indexes.get("blogs_by_title", {}).get(title_key)
        return blog_gid(destination.get("id")) if destination and destination.get("id") else None

    if owner_type == "page":
        handle_key = (identity.get("handle") or "").strip().lower()
        title_key = (identity.get("title") or "").strip().lower()
        destination = indexes.get("pages_by_handle", {}).get(handle_key) or indexes.get("pages_by_title", {}).get(title_key)
        return page_gid(destination.get("id")) if destination and destination.get("id") else None

    if owner_type == "customer":
        email_key = (identity.get("email") or "").strip().lower()
        destination = indexes.get("customers_by_email", {}).get(email_key)
        return customer_gid(destination.get("id")) if destination and destination.get("id") else None

    if owner_type == "location":
        name_key = (identity.get("name") or "").strip().lower()
        destination = indexes.get("locations_by_name", {}).get(name_key)
        return location_gid(destination.get("id")) if destination and destination.get("id") else None

    if owner_type == "order":
        name_key = (identity.get("name") or "").strip().lower()
        destination = indexes.get("orders_by_name", {}).get(name_key)
        return order_gid(destination.get("id")) if destination and destination.get("id") else None

    if owner_type == "draft_order":
        name_key = (identity.get("name") or "").strip().lower()
        destination = indexes.get("draft_orders_by_name", {}).get(name_key)
        return draft_order_gid(destination.get("id")) if destination and destination.get("id") else None

    if owner_type == "article":
        blog_key = (identity.get("blog_handle") or identity.get("blog_title") or "").strip().lower()
        article_handle = (identity.get("handle") or "").strip().lower()
        article_title = (identity.get("title") or "").strip().lower()
        blog_articles = indexes.get("articles_by_blog", {}).get(blog_key, {})
        destination = blog_articles.get("by_handle", {}).get(article_handle) or blog_articles.get("by_title", {}).get(article_title)
        return f"gid://shopify/Article/{destination.get('id')}" if destination and destination.get("id") else None

    return None


def import_store_metafields(dest_client: ShopifyClient, exported: Dict[str, Any]) -> None:
    indexes = build_destination_indexes(dest_client)

    updated_owners = 0
    skipped_owners = 0
    skipped_metafields = 0

    for owner in exported.get("owners", []):
        owner_type = owner.get("owner_resource")
        identity = owner.get("identity", {})
        metafields = owner.get("metafields", [])
        label = identity.get("handle") or identity.get("title") or identity.get("email") or identity.get("name") or owner_type

        if owner_type not in SUPPORTED_OWNER_TYPES:
            logger.warning("Skipping unsupported owner type %s (%s)", owner_type, label)
            skipped_owners += 1
            continue

        owner_gid = resolve_owner_gid(owner_type, identity, indexes)
        if not owner_gid:
            logger.warning("Could not resolve destination owner for %s (%s)", owner_type, label)
            skipped_owners += 1
            skipped_metafields += len(metafields)
            continue

        if not metafields:
            logger.info("No metafields for %s (%s)", owner_type, label)
            continue

        logger.info("Importing %s metafield(s) for %s (%s)", len(metafields), owner_type, label)
        result = set_metafields(dest_client, owner_gid, metafields)
        updated_owners += 1
        skipped_metafields += result["skipped_count"]

    logger.info(
        "Store metafield import complete: %s owners updated, %s owners skipped, %s metafields skipped",
        updated_owners,
        skipped_owners,
        skipped_metafields,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer all store metafields from source to destination Shopify stores")
    parser.add_argument("--execute", action="store_true", help="Actually write metafields to the destination store")
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
    parser.add_argument(
        "--import-from",
        help=(
            "Skip the source export step and import this previously-saved canonical JSON file "
            "instead (see docs/CANONICAL_SCHEMA.md). Lets you import from a non-Shopify source "
            "connector or replay a prior dry-run export. No SRC_SHOPIFY_* credentials needed "
            "in this mode."
        ),
    )
    args = parser.parse_args()

    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")
    require_env(DEST_SHOPIFY_SHOP=dest_shop, DEST_SHOPIFY_ACCESS_TOKEN=dest_token)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    dest_host = shop_host(dest_shop)
    dest_client = ShopifyClient(access_token=dest_token)
    dest_client.set_shop(admin_base_for_host(dest_host), f"https://{dest_host}/admin/oauth/access_token")

    src_client: Optional[ShopifyClient] = None

    if args.import_from:
        logger.info("Loading export from %s (skipping source fetch)", args.import_from)
        if args.import_from.lower().endswith(".xlsx"):
            from utils.tabular_io import import_from_xlsx
            loaded = import_from_xlsx(args.import_from)
        else:
            with open(args.import_from, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        exported_definitions = loaded.get("definitions", [])
        exported = {
            "owners": loaded.get("owners", []),
            "unsupported_owners": loaded.get("unsupported_owners", []),
        }
        logger.info(
            "Loaded %s metafield definition(s) and %s owner(s) to import",
            len(exported_definitions),
            len(exported["owners"]),
        )
    else:
        src_shop = os.getenv("SRC_SHOPIFY_SHOP")
        src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
        require_env(SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token)

        src_host = shop_host(src_shop)
        src_client = ShopifyClient(access_token=src_token)
        src_client.set_shop(admin_base_for_host(src_host), f"https://{src_host}/admin/oauth/access_token")

        logger.info("Exporting metafield definitions from %s", src_host)
        exported_definitions = export_metafield_definitions(src_client)

        logger.info("Exporting store metafields from %s", src_host)
        exported = export_store_metafields(src_client)

        ts = int(time.time())
        definitions_file = out_dir / f"metafield_definitions_export_{ts}.json"
        with open(definitions_file, "w", encoding="utf-8") as f:
            json.dump(exported_definitions, f, indent=2, ensure_ascii=False)
        logger.info("Definitions export complete: %s (%s definitions)", definitions_file, len(exported_definitions))

        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported_definitions, out_dir / f"metafield_definitions_export_{ts}.xlsx")

        out_file = out_dir / f"store_metafields_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(exported, f, indent=2, ensure_ascii=False)

        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported, out_dir / f"store_metafields_export_{ts}.xlsx")

        logger.info("Export complete: %s", out_file)
        logger.info("Exported %s owners", len(exported.get("owners", [])))

    if args.execute:
        if src_client is not None:
            import transfer.transfer_metaobjects as transfer_metaobjects

            logger.info("Exporting/importing metaobjects into %s", dest_host)
            metaobject_gid_map = transfer_metaobjects.import_metaobjects(
                dest_client, transfer_metaobjects.export_metaobjects(src_client)
            )
        else:
            logger.warning(
                "Skipping metaobjects sync -- no live source connection (import-from mode). Any "
                "metaobject_reference-type metafield definition will be skipped unless its metaobject "
                "definition already exists on the destination (transfer it separately via "
                "transfer_metaobjects.py --import-from first if needed)."
            )
            metaobject_gid_map = {}

        logger.info("Importing metafield definitions into %s", dest_host)
        import_metafield_definitions(dest_client, exported_definitions, metaobject_gid_map)

        logger.info("Importing store metafields into %s", dest_host)
        import_store_metafields(dest_client, exported)
        logger.info("Transfer complete")
    else:
        logger.info("Dry-run finished. Re-run with --execute to write definitions and metafields to destination")


if __name__ == "__main__":
    main()
