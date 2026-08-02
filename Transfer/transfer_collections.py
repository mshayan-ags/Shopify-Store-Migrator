"""Transfer collections between Shopify stores.

This script currently supports a dry-run export that collects:
- custom and smart collection metadata
- images (downloaded)
- metafields
- list of product handles belonging to each collection

To perform a real transfer (create collections on destination and assign
products), run with `--execute` after reviewing the dry-run output.
"""
import argparse
import base64
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from utils.shopify_client import ShopifyClient
from utils.config import SHOPIFY_API_VERSION
from utils.concurrency_utils import run_concurrently, DEFAULT_WORKERS, retry_with_backoff

load_dotenv()

logger = logging.getLogger("transfer_collections")
logging.basicConfig(level=logging.INFO)


def host_from_url(url: str) -> str:
    parsed = urlparse(url if ":" in url else f"https://{url}")
    return parsed.netloc


def admin_base_for_host(host: str) -> str:
    return f"https://{host}/admin/api/{SHOPIFY_API_VERSION}"


def download_image(url: str, dest: Path, max_retries: int = 3) -> None:
    """Download url to dest, retrying transient network/TLS hiccups.

    Under concurrent runs (many workers streaming images at once), occasional
    connection-level failures (including TLS record errors) are common and
    resolve on retry -- without this, a single hiccup permanently drops that
    collection's cover image for the run.
    """
    delay = 1.0
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, stream=True, timeout=30)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(1024 * 8):
                    if chunk:
                        f.write(chunk)
            return
        except Exception as e:
            if attempt >= max_retries:
                logger.warning(f"Failed to download image {url}: {e}")
                return
            logger.warning(f"Transient error downloading image {url} (attempt {attempt + 1}/{max_retries + 1}): {e}. Retrying in {delay:.1f}s...")
            time.sleep(delay)
            delay *= 2


def _gql_str(value) -> str:
    # ensure_ascii=False: see the docstring on concurrency_utils.gql_quote --
    # the default would mangle emoji/astral characters into a GraphQL parse error.
    return json.dumps(value, ensure_ascii=False) if value else "null"


def fetch_collection_seo_fields(client: ShopifyClient, collection_id: int) -> Dict[str, Any]:
    """seo/sortOrder/templateSuffix are GraphQL-only -- invisible to the REST
    custom_collections/smart_collections endpoints used everywhere else here."""
    try:
        data = client.query(
            f'{{ collection(id: "gid://shopify/Collection/{collection_id}") {{ '
            f"seo {{ title description }} sortOrder templateSuffix }} }}"
        )
        c = data.get("collection") or {}
        seo = c.get("seo") or {}
        return {
            "seo_title": seo.get("title"),
            "seo_description": seo.get("description"),
            "sort_order": c.get("sortOrder"),
            "template_suffix": c.get("templateSuffix"),
        }
    except Exception:
        logger.exception("Failed to fetch seo/sortOrder for collection %s", collection_id)
        return {}


def sync_collection_seo_fields(dest_client: ShopifyClient, dest_collection_id, seo_fields: Dict[str, Any]) -> None:
    if not dest_collection_id or not seo_fields:
        return

    fields = []
    if seo_fields.get("seo_title") or seo_fields.get("seo_description"):
        fields.append(
            "seo: {"
            f" title: {_gql_str(seo_fields.get('seo_title'))},"
            f" description: {_gql_str(seo_fields.get('seo_description'))}"
            " }"
        )
    if seo_fields.get("sort_order"):
        fields.append(f"sortOrder: {seo_fields['sort_order']}")
    if seo_fields.get("template_suffix"):
        fields.append(f"templateSuffix: {_gql_str(seo_fields['template_suffix'])}")

    if not fields:
        return

    mutation = (
        "mutation { collectionUpdate(input: { id: "
        f'"gid://shopify/Collection/{dest_collection_id}", {", ".join(fields)}'
        " }) { collection { id } userErrors { field message } } }"
    )
    try:
        result = dest_client.mutation(mutation)
        errors = (result.get("collectionUpdate") or {}).get("userErrors")
        if errors:
            logger.warning("Failed to sync seo/sortOrder for collection %s: %s", dest_collection_id, errors)
    except Exception as e:
        logger.warning("Failed to sync seo/sortOrder for collection %s: %s", dest_collection_id, e)


def sync_collection_image(dest_client: ShopifyClient, dest_collection_id, collection_type: str, image_path: Optional[str]) -> None:
    """Upload/refresh a collection's cover image on the destination.

    Needed for BOTH the create path and the already-exists path: the REST
    create payload only sets the image at creation time, so any collection
    that already existed on the destination (the common case on repeat runs)
    never got its image set/updated without this explicit sync call.
    """
    if not dest_collection_id or not image_path or not os.path.exists(image_path):
        return

    resource = "custom_collections" if collection_type == "custom" else "smart_collections"
    key = "custom_collection" if collection_type == "custom" else "smart_collection"
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")
        retry_with_backoff(lambda: dest_client.rest_put(
            f"{resource}/{dest_collection_id}",
            {key: {"id": dest_collection_id, "image": {"attachment": img_b64}}},
        ))
    except Exception as e:
        logger.warning("Failed to sync image for collection %s: %s", dest_collection_id, e)


def get_collection_products(src_client: ShopifyClient, collection_id: int) -> list:
    """Fetch products that belong to a collection via products endpoint.

    Using products?collection_id works for both custom and smart collections
    and avoids API differences around collects.
    """
    products = []
    since_id = 0

    while True:
        params = {
            "collection_id": collection_id,
            "limit": 250,
            "fields": "id,handle",
        }
        if since_id:
            params["since_id"] = since_id

        page = retry_with_backoff(lambda: src_client.rest_get("products", params=params))
        page_products = page.get("products", [])
        if not page_products:
            break

        for prod in page_products:
            pid = prod.get("id")
            handle = prod.get("handle")
            if pid and handle:
                products.append({"id": pid, "handle": handle})

        since_id = page_products[-1].get("id") or 0
        if len(page_products) < 250 or not since_id:
            break

    return products


def fetch_all_products_by_handle(client: ShopifyClient) -> Dict[str, int]:
    """Return all destination products as a handle->id map using pagination."""
    handle_to_id: Dict[str, int] = {}
    since_id = 0

    while True:
        params = {
            "limit": 250,
            "fields": "id,handle",
        }
        if since_id:
            params["since_id"] = since_id

        page = retry_with_backoff(lambda: client.rest_get("products", params=params))
        products = page.get("products", [])
        if not products:
            break

        for prod in products:
            handle = prod.get("handle")
            pid = prod.get("id")
            if handle and pid:
                handle_to_id[handle] = pid

        since_id = products[-1].get("id") or 0
        if len(products) < 250 or not since_id:
            break

    return handle_to_id


def fetch_all_collections(client: ShopifyClient, resource: str) -> list:
    """Fetch all collections for a resource (custom_collections or smart_collections)."""
    all_collections = []
    since_id = 0

    while True:
        params = {"limit": 250}
        if since_id:
            params["since_id"] = since_id

        page = retry_with_backoff(lambda: client.rest_get(resource, params=params))
        page_collections = page.get(resource, [])
        if not page_collections:
            break

        all_collections.extend(page_collections)
        since_id = page_collections[-1].get("id") or 0
        if len(page_collections) < 250 or not since_id:
            break

    return all_collections


def get_selected_collections(
    client: ShopifyClient,
    resource: str,
    collection_filter: Optional[str] = None,
    initial_limit: Optional[int] = None,
) -> list:
    """Fetch and filter collections for a resource."""
    data = retry_with_backoff(lambda: client.rest_get(resource, params={"limit": 250}))
    collections = data.get(resource, [])

    if collection_filter:
        collections = [c for c in collections if matches_collection_filter(c, collection_filter)]
    elif initial_limit:
        collections = collections[:initial_limit]

    return collections


def build_collection_lookup(collections: list) -> Dict[str, Dict[str, Any]]:
    """Build lookup maps for existing collections by handle and title."""
    by_handle: Dict[str, Dict[str, Any]] = {}
    by_title: Dict[str, Dict[str, Any]] = {}

    for coll in collections:
        handle = (coll.get("handle") or "").strip().lower()
        title = (coll.get("title") or "").strip().lower()
        if handle:
            by_handle[handle] = coll
        if title:
            by_title[title] = coll

    return {
        "by_handle": by_handle,
        "by_title": by_title,
    }


def matches_collection_filter(collection: Dict[str, Any], collection_filter: Optional[str]) -> bool:
    """Return True if collection matches the optional id/handle filter."""
    if not collection_filter:
        return True

    coll_id = collection.get("id")
    coll_handle = (collection.get("handle") or "").strip().lower()
    filter_value = collection_filter.strip().lower()

    return str(coll_id) == filter_value or coll_handle == filter_value


def export_collections(
    src_client: ShopifyClient,
    out_dir: Path,
    collection_filter: Optional[str] = None,
    initial_limit: Optional[int] = None,
) -> Dict[str, Any]:
    out = {"custom_collections": [], "smart_collections": []}

    # Custom collections
    custom_collections = get_selected_collections(
        src_client,
        "custom_collections",
        collection_filter=collection_filter,
        initial_limit=initial_limit,
    )

    for c in custom_collections:
        cid = c.get("id")
        products = []
        try:
            products = get_collection_products(src_client, cid)
        except Exception:
            logger.exception("Failed to fetch products for collection %s", cid)

        # metafields
        metafields = []
        try:
            mf = retry_with_backoff(lambda: src_client.rest_get(
                "metafields",
                params={"metafield[owner_resource]": "collection", "metafield[owner_id]": cid},
            ))
            metafields = mf.get("metafields", [])
        except Exception:
            logger.exception("Failed to fetch metafields for collection %s", cid)

        image_path = None
        image = c.get("image") or {}
        img_src = image.get("src")
        if img_src:
            img_dir = out_dir / "collection_images"
            img_dir.mkdir(parents=True, exist_ok=True)
            filename = f"collection_{cid}.jpg"
            image_path = str(img_dir / filename)
            download_image(img_src, Path(image_path))

        out["custom_collections"].append(
            {
                "type": "custom",
                "id": cid,
                "title": c.get("title"),
                "handle": c.get("handle"),
                "body_html": c.get("body_html"),
                "image": img_src,
                "image_local": image_path,
                "products": products,
                "metafields": metafields,
            }
        )

    # Smart collections
    smart_collections = get_selected_collections(
        src_client,
        "smart_collections",
        collection_filter=collection_filter,
        initial_limit=initial_limit,
    )

    for c in smart_collections:
        cid = c.get("id")
        products = []
        try:
            products = get_collection_products(src_client, cid)
        except Exception:
            logger.exception("Failed to fetch products for smart collection %s", cid)

        metafields = []
        try:
            mf = retry_with_backoff(lambda: src_client.rest_get(
                "metafields",
                params={"metafield[owner_resource]": "collection", "metafield[owner_id]": cid},
            ))
            metafields = mf.get("metafields", [])
        except Exception:
            logger.exception("Failed to fetch metafields for smart collection %s", cid)

        out["smart_collections"].append(
            {
                "type": "smart",
                "id": cid,
                "title": c.get("title"),
                "handle": c.get("handle"),
                "body_html": c.get("body_html"),
                "rules": c.get("rules"),
                "published": c.get("published"),
                "products": products,
                "metafields": metafields,
            }
        )

    return out


def transfer_collections_one_by_one(
    src_client: ShopifyClient,
    dest_client: ShopifyClient,
    out_dir: Path,
    collection_filter: Optional[str] = None,
    initial_limit: Optional[int] = None,
    max_workers: int = DEFAULT_WORKERS,
) -> Dict[str, Any]:
    """Transfer collections concurrently (up to max_workers at a time).

    existing_custom/existing_smart are loaded once up front. Concurrent workers
    only need to read them to detect "already exists" -- Shopify guarantees
    collection handles are unique within a store, so no two collections in the
    source list can ever collide with each other, only with this destination
    snapshot. CPython dict/list operations are atomic per-call under the GIL,
    so the post-create writes back into these dicts (kept for readability/
    parity with the sequential version) are safe without an explicit lock too.
    """
    exported = {"custom_collections": [], "smart_collections": []}

    # Destination lookups are loaded once and reused for all collections.
    handle_to_id = fetch_all_products_by_handle(dest_client)
    existing_custom = build_collection_lookup(fetch_all_collections(dest_client, "custom_collections"))
    existing_smart = build_collection_lookup(fetch_all_collections(dest_client, "smart_collections"))
    logger.info("Found %s products in destination store", len(handle_to_id))

    custom_collections = get_selected_collections(
        src_client,
        "custom_collections",
        collection_filter=collection_filter,
        initial_limit=initial_limit,
    )
    smart_collections = get_selected_collections(
        src_client,
        "smart_collections",
        collection_filter=collection_filter,
        initial_limit=initial_limit,
    )

    custom_progress_lock = threading.Lock()
    custom_completed = 0

    def process_custom(c: Dict[str, Any]) -> None:
        nonlocal custom_completed
        cid = c.get("id")
        title = c.get("title")
        with custom_progress_lock:
            custom_completed += 1
            position = custom_completed
        logger.info("Processing custom collection %s/%s: %s", position, len(custom_collections), title)

        products = []
        try:
            products = get_collection_products(src_client, cid)
        except Exception:
            logger.exception("Failed to fetch products for collection %s", cid)

        metafields = []
        try:
            mf = retry_with_backoff(lambda: src_client.rest_get(
                "metafields",
                params={"metafield[owner_resource]": "collection", "metafield[owner_id]": cid},
            ))
            metafields = mf.get("metafields", [])
        except Exception:
            logger.exception("Failed to fetch metafields for collection %s", cid)

        image_path = None
        image = c.get("image") or {}
        img_src = image.get("src")
        if img_src:
            img_dir = out_dir / "collection_images"
            img_dir.mkdir(parents=True, exist_ok=True)
            filename = f"collection_{cid}.jpg"
            image_path = str(img_dir / filename)
            download_image(img_src, Path(image_path))

        exported_coll = {
            "type": "custom",
            "id": cid,
            "title": title,
            "handle": c.get("handle"),
            "body_html": c.get("body_html"),
            "image": img_src,
            "image_local": image_path,
            "products": products,
            "metafields": metafields,
        }
        exported["custom_collections"].append(exported_coll)

        payload = {
            "custom_collection": {
                "title": exported_coll.get("title"),
                "handle": exported_coll.get("handle"),
                "body_html": exported_coll.get("body_html"),
            }
        }

        if image_path and os.path.exists(image_path):
            try:
                with open(image_path, "rb") as f:
                    img_data = f.read()
                    img_b64 = base64.b64encode(img_data).decode("utf-8")
                    payload["custom_collection"]["image"] = {"attachment": img_b64}
            except Exception as e:
                logger.warning("Failed to upload image for %s: %s", title, e)

        try:
            handle_key = (exported_coll.get("handle") or "").strip().lower()
            title_key = (exported_coll.get("title") or "").strip().lower()
            existing = existing_custom["by_handle"].get(handle_key) or existing_custom["by_title"].get(title_key)

            seo_fields = fetch_collection_seo_fields(src_client, cid)

            if existing:
                logger.info("Skipping creation of existing custom collection %s; syncing seo/sortOrder/image", title)
                sync_collection_seo_fields(dest_client, existing.get("id"), seo_fields)
                sync_collection_image(dest_client, existing.get("id"), "custom", image_path)
                return

            result = retry_with_backoff(lambda: dest_client.rest_post("custom_collections", payload))
            new_coll_id = result.get("custom_collection", {}).get("id")
            logger.info("Created custom collection %s with ID %s", title, new_coll_id)
            sync_collection_seo_fields(dest_client, new_coll_id, seo_fields)

            if handle_key:
                existing_custom["by_handle"][handle_key] = {"id": new_coll_id, "title": title, "handle": exported_coll.get("handle")}
            if title_key:
                existing_custom["by_title"][title_key] = {"id": new_coll_id, "title": title, "handle": exported_coll.get("handle")}

            for prod in exported_coll.get("products", []):
                handle = prod.get("handle")
                dest_id = handle_to_id.get(handle)
                if dest_id:
                    try:
                        retry_with_backoff(lambda: dest_client.rest_post("collects", {
                            "collect": {
                                "product_id": dest_id,
                                "collection_id": new_coll_id,
                            }
                        }))
                    except Exception as e:
                        if "already exists" in str(e).lower() or "unprocessable entity" in str(e).lower():
                            logger.info("Product %s already assigned to collection %s", handle, title)
                        else:
                            logger.warning("Failed to add product %s to collection: %s", handle, e)
                else:
                    logger.warning("Product %s not found in destination store", handle)

            for mf in exported_coll.get("metafields", []):
                try:
                    retry_with_backoff(lambda: dest_client.rest_post("metafields", {
                        "metafield": {
                            "owner_resource": "collection",
                            "owner_id": new_coll_id,
                            "namespace": mf.get("namespace"),
                            "key": mf.get("key"),
                            "value": mf.get("value"),
                            "type": mf.get("type"),
                        }
                    }))
                except Exception as e:
                    logger.warning("Failed to set metafield: %s", e)

        except Exception as e:
            logger.error("Failed to create/update collection %s: %s", title, e)

    run_concurrently(custom_collections, process_custom, max_workers=max_workers, label="custom collection")

    smart_progress_lock = threading.Lock()
    smart_completed = 0

    def process_smart(c: Dict[str, Any]) -> None:
        nonlocal smart_completed
        cid = c.get("id")
        title = c.get("title")
        with smart_progress_lock:
            smart_completed += 1
            position = smart_completed
        logger.info("Processing smart collection %s/%s: %s", position, len(smart_collections), title)

        products = []
        try:
            products = get_collection_products(src_client, cid)
        except Exception:
            logger.exception("Failed to fetch products for smart collection %s", cid)

        metafields = []
        try:
            mf = retry_with_backoff(lambda: src_client.rest_get(
                "metafields",
                params={"metafield[owner_resource]": "collection", "metafield[owner_id]": cid},
            ))
            metafields = mf.get("metafields", [])
        except Exception:
            logger.exception("Failed to fetch metafields for smart collection %s", cid)

        image_path = None
        image = c.get("image") or {}
        img_src = image.get("src")
        if img_src:
            img_dir = out_dir / "collection_images"
            img_dir.mkdir(parents=True, exist_ok=True)
            filename = f"collection_{cid}.jpg"
            image_path = str(img_dir / filename)
            download_image(img_src, Path(image_path))

        exported_coll = {
            "type": "smart",
            "id": cid,
            "title": title,
            "handle": c.get("handle"),
            "body_html": c.get("body_html"),
            "rules": c.get("rules"),
            "published": c.get("published"),
            "image": img_src,
            "image_local": image_path,
            "products": products,
            "metafields": metafields,
        }
        exported["smart_collections"].append(exported_coll)

        payload = {
            "smart_collection": {
                "title": exported_coll.get("title"),
                "handle": exported_coll.get("handle"),
                "body_html": exported_coll.get("body_html"),
                "rules": exported_coll.get("rules", []),
                "published": exported_coll.get("published", True),
            }
        }

        if image_path and os.path.exists(image_path):
            try:
                with open(image_path, "rb") as f:
                    img_data = f.read()
                    img_b64 = base64.b64encode(img_data).decode("utf-8")
                    payload["smart_collection"]["image"] = {"attachment": img_b64}
            except Exception as e:
                logger.warning("Failed to upload image for %s: %s", title, e)

        try:
            handle_key = (exported_coll.get("handle") or "").strip().lower()
            title_key = (exported_coll.get("title") or "").strip().lower()
            existing = existing_smart["by_handle"].get(handle_key) or existing_smart["by_title"].get(title_key)

            seo_fields = fetch_collection_seo_fields(src_client, cid)

            if existing:
                logger.info("Skipping creation of existing smart collection %s; syncing seo/sortOrder/image", title)
                sync_collection_seo_fields(dest_client, existing.get("id"), seo_fields)
                sync_collection_image(dest_client, existing.get("id"), "smart", image_path)
                return

            result = retry_with_backoff(lambda: dest_client.rest_post("smart_collections", payload))
            new_coll_id = result.get("smart_collection", {}).get("id")
            logger.info("Created smart collection %s with ID %s", title, new_coll_id)
            sync_collection_seo_fields(dest_client, new_coll_id, seo_fields)

            if handle_key:
                existing_smart["by_handle"][handle_key] = {"id": new_coll_id, "title": title, "handle": exported_coll.get("handle")}
            if title_key:
                existing_smart["by_title"][title_key] = {"id": new_coll_id, "title": title, "handle": exported_coll.get("handle")}

            for mf in exported_coll.get("metafields", []):
                try:
                    retry_with_backoff(lambda: dest_client.rest_post("metafields", {
                        "metafield": {
                            "owner_resource": "collection",
                            "owner_id": new_coll_id,
                            "namespace": mf.get("namespace"),
                            "key": mf.get("key"),
                            "value": mf.get("value"),
                            "type": mf.get("type"),
                        }
                    }))
                except Exception as e:
                    logger.warning("Failed to set metafield: %s", e)

        except Exception as e:
            logger.error("Failed to create/update smart collection %s: %s", title, e)

    run_concurrently(smart_collections, process_smart, max_workers=max_workers, label="smart collection")

    logger.info("One-by-one transfer complete")
    return exported


def main():
    parser = argparse.ArgumentParser(description="Transfer collections from Src to Supra")
    parser.add_argument("--execute", action="store_true", help="Perform the transfer (dangerous, imports to destination)")
    parser.add_argument("--out", default="Results", help="Output directory for dry-run export")
    parser.add_argument(
        "--collection",
        help="Export/import only one collection by ID or handle (checks both custom and smart)",
    )
    parser.add_argument(
        "--initial-limit",
        type=int,
        default=None,
        help="Export/import only the first N custom and first N smart collections",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="Number of collections to transfer concurrently (default: %(default)s)",
    )
    args = parser.parse_args()

    if args.initial_limit is not None and args.initial_limit <= 0:
        raise RuntimeError("--initial-limit must be greater than 0")

    if args.collection and args.initial_limit:
        logger.info("--collection provided; ignoring --initial-limit")

    # Load from .env
    src_shop = os.getenv("SRC_SHOPIFY_SHOP")
    src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")

    if not all([src_shop, src_token, dest_shop, dest_token]):
        raise RuntimeError("Missing .env values: SRC_SHOPIFY_SHOP, SRC_SHOPIFY_ACCESS_TOKEN, DEST_SHOPIFY_SHOP, DEST_SHOPIFY_ACCESS_TOKEN")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build admin API URLs from shop names
    src_host = f"{src_shop}.myshopify.com"
    dest_host = f"{dest_shop}.myshopify.com"

    src_admin = admin_base_for_host(src_host)
    dest_admin = admin_base_for_host(dest_host)

    src_oauth = f"https://{src_host}/admin/oauth/access_token"
    dest_oauth = f"https://{dest_host}/admin/oauth/access_token"

    src_client = ShopifyClient(access_token=src_token)
    src_client.set_shop(src_admin, src_oauth)

    dest_client = ShopifyClient(access_token=dest_token)
    dest_client.set_shop(dest_admin, dest_oauth)

    if args.execute:
        logger.info("Starting one-by-one transfer from %s to %s (%s worker(s))", src_host, dest_host, args.workers)
        exported = transfer_collections_one_by_one(
            src_client,
            dest_client,
            out_dir,
            collection_filter=args.collection,
            initial_limit=args.initial_limit if not args.collection else None,
            max_workers=args.workers,
        )
    else:
        logger.info("Starting dry-run export from %s (Src)", src_host)
        exported = export_collections(
            src_client,
            out_dir,
            collection_filter=args.collection,
            initial_limit=args.initial_limit if not args.collection else None,
        )

    custom_count = len(exported.get("custom_collections", []))
    smart_count = len(exported.get("smart_collections", []))
    logger.info("Dry-run selected %s custom and %s smart collections", custom_count, smart_count)

    ts = int(time.time())
    out_file = out_dir / f"collections_export_src_to_dest_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(exported, f, indent=2, ensure_ascii=False)

    logger.info("Export complete. File: %s", out_file)
    
    if args.execute:
        logger.info("Transfer complete!")
    else:
        logger.info("Dry-run finished. Review the export and run with --execute to perform the real transfer")


if __name__ == "__main__":
    main()
