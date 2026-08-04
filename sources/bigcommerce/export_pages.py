import logging
import re
from typing import Any, Dict, List

from sources.bigcommerce.bigcommerce_client import BigCommerceClient

logger = logging.getLogger("bigcommerce_export_pages")

BODY_TYPES = {"page", "raw"}


def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "page"


def page_handle(page: Dict[str, Any]) -> str:
    url = page.get("url") or (page.get("custom_url") or {}).get("url")
    if url:
        return url.lstrip("/") or slugify(page.get("name"))
    return slugify(page.get("name"))


def map_page(page: Dict[str, Any]) -> Dict[str, Any]:
    page_type = page.get("type")
    return {
        "id": page.get("id"),
        "handle": page_handle(page),
        "title": page.get("name"),
        "body": page.get("body") if page_type in BODY_TYPES else None,
        "is_published": bool(page.get("is_visible", True)),
        "published_at": None,
        "template_suffix": None,
        "metafields": [],
    }


def fetch_pages(client: BigCommerceClient):
    return client.get_paginated_v3("content/pages")


def export_pages(client: BigCommerceClient) -> List[Dict[str, Any]]:
    exported = []
    for page in fetch_pages(client):
        try:
            exported.append(map_page(page))
        except Exception:
            logger.exception("Failed to export page %s", page.get("id"))
    logger.info("Exported %s page(s)", len(exported))
    return exported
