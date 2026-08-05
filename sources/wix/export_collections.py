import logging
from typing import Any, Dict, List, Optional

from sources.wix.wix_client import WixClient
from sources.wix.export_products import slugify

logger = logging.getLogger("wix_export_collections")

PAGE_LIMIT = 100

COLLECTION_FILTER_TEMPLATE = {"collectionIds": {"$hasSome": None}}


def collection_handle(collection: Dict[str, Any]) -> str:
    return collection.get("slug") or slugify(collection.get("name"))


def fetch_collections(client: WixClient, page_limit: int = PAGE_LIMIT) -> List[Dict[str, Any]]:
    collections: List[Dict[str, Any]] = []
    offset = 0

    while True:
        body = {"query": {"paging": {"limit": page_limit, "offset": offset}}}
        response = client.post("stores/v1/collections/query", json_body=body) or {}
        page = response.get("collections") or []
        collections.extend(page)

        if len(page) < page_limit:
            break
        offset += page_limit

    return collections


def build_membership_from_products(products: List[Dict[str, Any]]) -> Dict[Any, List[Dict[str, Any]]]:
    membership: Dict[Any, List[Dict[str, Any]]] = {}
    for product in products:
        pid = product.get("id")
        handle = product.get("handle")
        for collection_id in product.get("_wix_collection_ids") or []:
            membership.setdefault(collection_id, []).append({"id": pid, "handle": handle})
    return membership


def fetch_collection_product_members(client: WixClient, collection_id: Any) -> List[Dict[str, Any]]:
    members: List[Dict[str, Any]] = []
    offset = 0
    filter_body = {"collectionIds": {"$hasSome": [collection_id]}}

    while True:
        body = {"query": {"filter": filter_body, "paging": {"limit": PAGE_LIMIT, "offset": offset}}}
        try:
            response = client.post("stores/v1/products/query", json_body=body) or {}
        except Exception:
            logger.exception(
                "Failed to query products for collection %s via collectionIds filter -- "
                "this endpoint/filter shape is unconfirmed, see module docstring",
                collection_id,
            )
            return members

        page = response.get("products") or []
        for product in page:
            members.append({"id": product.get("id"), "handle": product.get("slug") or slugify(product.get("name"))})

        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT

    return members


def map_collection(
    collection: Dict[str, Any],
    membership: Optional[Dict[Any, List[Dict[str, Any]]]],
    client: Optional[WixClient],
) -> Dict[str, Any]:
    cid = collection.get("id")

    if membership is not None:
        products = membership.get(cid, [])
    elif client is not None:
        products = fetch_collection_product_members(client, cid)
    else:
        products = []

    image_url = ((collection.get("media") or {}).get("mainMedia") or {}).get("image", {}).get("url")

    return {
        "type": "custom",
        "id": cid,
        "title": collection.get("name"),
        "handle": collection_handle(collection),
        "body_html": collection.get("description") or None,
        "image": image_url or None,
        "image_local": None,
        "products": products,
        "rules": None,
        "published": bool(collection.get("visible", True)),
        "metafields": [],
    }


def export_collections(
    client: WixClient, products: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    membership = build_membership_from_products(products) if products is not None else None

    collections_raw = fetch_collections(client)
    custom_collections = []
    for collection in collections_raw:
        try:
            custom_collections.append(map_collection(collection, membership, client))
        except Exception:
            logger.exception("Failed to export collection %s", collection.get("id"))

    logger.info("Exported %s collection(s)", len(custom_collections))
    return {"custom_collections": custom_collections, "smart_collections": []}
