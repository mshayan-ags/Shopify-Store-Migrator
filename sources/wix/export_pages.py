import logging
from typing import Any, Dict, List

from sources.wix.wix_client import WixClient
from sources.wix.export_products import slugify

logger = logging.getLogger("wix_export_pages")

PAGE_LIMIT = 100


def fetch_posts(client: WixClient, page_limit: int = PAGE_LIMIT) -> List[Dict[str, Any]]:
    posts: List[Dict[str, Any]] = []
    offset = 0

    while True:
        body = {"paging": {"limit": page_limit, "offset": offset}}
        response = client.post("blog/v3/posts/query", json_body=body) or {}
        page = response.get("posts") or []
        posts.extend(page)

        if len(page) < page_limit:
            break
        offset += page_limit

    return posts


def map_post(post: Dict[str, Any]) -> Dict[str, Any]:
    status = str(post.get("status") or "").upper()
    return {
        "id": post.get("id"),
        "handle": post.get("slug") or slugify(post.get("title")),
        "title": post.get("title"),
        "body": post.get("plainContent") or post.get("excerpt") or None,
        "is_published": status == "PUBLISHED",
        "published_at": post.get("firstPublishedDate") or None,
        "template_suffix": None,
        "metafields": [],
    }


def export_pages(client: WixClient) -> List[Dict[str, Any]]:
    posts_raw = fetch_posts(client)
    exported = []

    for post in posts_raw:
        try:
            exported.append(map_post(post))
        except Exception:
            logger.exception("Failed to export post %s", post.get("id"))

    logger.info("Exported %s page(s)", len(exported))
    return exported
