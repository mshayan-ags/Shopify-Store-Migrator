"""Transfer Online Store navigation menus from Src to dest.

Requires the `read_online_store_navigation` / `write_online_store_navigation`
Admin API scope on both stores' custom apps (grant under Shopify Admin > Apps >
[app name] > Configuration, then reinstall to refresh the .env token). Reading
pages/blogs/collections for resourceId remapping also needs `read_content` and
`read_products` (already granted).

CAVEAT: menu items that link to a product, collection, page, blog, or article
are remapped to the matching destination resource by handle (this only works
if that resource has already been transferred to dest first). Items of type
SHOP_POLICY, METAOBJECT, or CUSTOMER_ACCOUNT_PAGE cannot be reliably remapped
across stores and are copied with their original `url` only, flagged in the
log for manual review.

Usage:
    python transfer_navigation.py                # dry-run export
    python transfer_navigation.py --execute       # create/update menus on dest
"""
import argparse
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from Transfer.transfer_product import make_client, fetch_all_product_handles
from Transfer.transfer_collections import fetch_all_collections
from Transfer.transfer_pages import fetch_all_pages
from Transfer.transfer_blogs import fetch_all_blogs, fetch_blog_articles
from Transfer.transfer_store_metafields import retry_with_backoff, gql_quote
from utils.shopify_graphql_utils import paginate_connection, mutation_errors

load_dotenv()

logger = logging.getLogger("transfer_navigation")
logging.basicConfig(level=logging.INFO)

# Menu items can nest a few levels deep; MenuItem.items is self-referential so a
# named fragment can recurse without having to hardcode a fixed number of levels.
MENU_ITEM_FRAGMENT = """
fragment MenuItemFields on MenuItem {
  id
  title
  type
  url
  resourceId
  tags
  items {
    id
    title
    type
    url
    resourceId
    tags
    items {
      id
      title
      type
      url
      resourceId
      tags
      items {
        id
        title
        type
        url
        resourceId
        tags
      }
    }
  }
}
"""

REMAPPABLE_TYPES_NO_LOOKUP_NEEDED = {"FRONTPAGE", "CATALOG", "SEARCH", "HTTP", "COLLECTIONS"}
NOT_REMAPPABLE_TYPES = {"SHOP_POLICY", "METAOBJECT", "CUSTOMER_ACCOUNT_PAGE"}


def fetch_all_menus(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {MENU_ITEM_FRAGMENT}
        {{
          menus(first: 50{after_clause}) {{
            edges {{
              node {{
                id
                handle
                title
                isDefault
                items {{ ...MenuItemFields }}
              }}
            }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("menus",))


def export_menu_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": item["title"],
        "type": item["type"],
        "url": item.get("url"),
        "resource_id": item.get("resourceId"),
        "tags": item.get("tags") or [],
        "items": [export_menu_item(child) for child in item.get("items", [])],
    }


def export_menus(src_client) -> List[Dict[str, Any]]:
    menus = fetch_all_menus(src_client)
    exported = [
        {
            "handle": m["handle"],
            "title": m["title"],
            "is_default": m.get("isDefault", False),
            "items": [export_menu_item(item) for item in m.get("items", [])],
        }
        for m in menus
    ]
    logger.info("Exported %s menu(s)", len(exported))
    return exported


class DestinationResourceIndex:
    """Lazily-built handle -> destination GID lookups for menu item remapping."""

    def __init__(self, dest_client):
        self.dest_client = dest_client
        self._products: Optional[Dict[str, int]] = None
        self._collections: Optional[Dict[str, int]] = None
        self._pages: Optional[Dict[str, str]] = None
        self._blogs: Optional[Dict[str, Dict[str, Any]]] = None

    def products(self) -> Dict[str, int]:
        if self._products is None:
            self._products = {
                (p.get("handle") or "").strip().lower(): p["id"]
                for p in fetch_all_product_handles(self.dest_client)
            }
        return self._products

    def collections(self) -> Dict[str, int]:
        if self._collections is None:
            collections = fetch_all_collections(self.dest_client, "custom_collections") + fetch_all_collections(
                self.dest_client, "smart_collections"
            )
            self._collections = {(c.get("handle") or "").strip().lower(): c["id"] for c in collections}
        return self._collections

    def pages(self) -> Dict[str, str]:
        if self._pages is None:
            self._pages = {(p.get("handle") or "").strip().lower(): p["id"] for p in fetch_all_pages(self.dest_client)}
        return self._pages

    def blogs(self) -> Dict[str, Dict[str, Any]]:
        if self._blogs is None:
            blogs = fetch_all_blogs(self.dest_client)
            index = {}
            for blog in blogs:
                blog_key = (blog.get("handle") or "").strip().lower()
                articles = fetch_blog_articles(self.dest_client, blog["id"])
                index[blog_key] = {
                    "id": blog["id"],
                    "articles_by_handle": {(a.get("handle") or "").strip().lower(): a["id"] for a in articles},
                }
            self._blogs = index
        return self._blogs


def resolve_resource_id(item: Dict[str, Any], index: DestinationResourceIndex) -> Optional[str]:
    item_type = item.get("type")
    url = item.get("url") or ""

    if item_type in REMAPPABLE_TYPES_NO_LOOKUP_NEEDED:
        return None

    if item_type in NOT_REMAPPABLE_TYPES:
        logger.warning(
            "Menu item '%s' (type %s) can't be remapped across stores; keeping url only, review manually",
            item.get("title"),
            item_type,
        )
        return None

    if item_type == "PRODUCT":
        m = re.search(r"/products/([^/?#]+)", url)
        handle = (m.group(1) if m else "").strip().lower()
        dest_id = index.products().get(handle)
        if dest_id:
            return f"gid://shopify/Product/{dest_id}"

    elif item_type == "COLLECTION":
        m = re.search(r"/collections/([^/?#]+)", url)
        handle = (m.group(1) if m else "").strip().lower()
        dest_id = index.collections().get(handle)
        if dest_id:
            return f"gid://shopify/Collection/{dest_id}"

    elif item_type == "PAGE":
        m = re.search(r"/pages/([^/?#]+)", url)
        handle = (m.group(1) if m else "").strip().lower()
        dest_gid = index.pages().get(handle)
        if dest_gid:
            return dest_gid

    elif item_type == "BLOG":
        m = re.search(r"/blogs/([^/?#]+)", url)
        handle = (m.group(1) if m else "").strip().lower()
        blog = index.blogs().get(handle)
        if blog:
            return blog["id"]

    elif item_type == "ARTICLE":
        m = re.search(r"/blogs/([^/?#]+)/([^/?#]+)", url)
        if m:
            blog_handle, article_handle = m.group(1).strip().lower(), m.group(2).strip().lower()
            blog = index.blogs().get(blog_handle)
            if blog:
                article_gid = blog["articles_by_handle"].get(article_handle)
                if article_gid:
                    return article_gid

    logger.warning(
        "Could not resolve destination resource for menu item '%s' (type %s, url %s); keeping url only",
        item.get("title"),
        item_type,
        url,
    )
    return None


def build_menu_item_input(item: Dict[str, Any], index: DestinationResourceIndex) -> str:
    resource_id = resolve_resource_id(item, index)
    tags_literal = "[" + ", ".join(gql_quote(t) for t in item.get("tags", [])) + "]"
    child_items = [build_menu_item_input(child, index) for child in item.get("items", [])]

    fields = [
        f"title: {gql_quote(item.get('title'))}",
        f"type: {item.get('type')}",
        f"tags: {tags_literal}",
    ]
    if resource_id:
        fields.append(f"resourceId: {gql_quote(resource_id)}")
    elif item.get("url"):
        fields.append(f"url: {gql_quote(item['url'])}")
    if child_items:
        fields.append("items: [" + ", ".join(child_items) + "]")

    return "{" + ", ".join(fields) + "}"


def import_menus(dest_client, exported: List[Dict[str, Any]]) -> None:
    index = DestinationResourceIndex(dest_client)
    existing_menus = fetch_all_menus(dest_client)
    by_handle = {(m.get("handle") or "").strip().lower(): m for m in existing_menus}

    created = updated = skipped = 0

    for menu in exported:
        handle_key = (menu.get("handle") or "").strip().lower()
        items_literal = "[" + ", ".join(build_menu_item_input(item, index) for item in menu.get("items", [])) + "]"
        existing = by_handle.get(handle_key)

        if existing:
            if existing.get("isDefault"):
                logger.info("Skipping default menu '%s' (Shopify manages it automatically)", menu.get("title"))
                skipped += 1
                continue

            mutation = f"""
            mutation {{
              menuUpdate(id: {gql_quote(existing["id"])}, title: {gql_quote(menu.get("title"))}, items: {items_literal}) {{
                menu {{ id handle }}
                userErrors {{ field message }}
              }}
            }}
            """
            try:
                result = retry_with_backoff(lambda: dest_client.mutation(mutation))
            except Exception as e:
                logger.warning("Failed to update menu '%s': %s", menu.get("title"), e)
                continue

            errors = mutation_errors(result, "menuUpdate")
            if errors:
                logger.warning("Failed to update menu '%s': %s", menu.get("title"), errors)
                continue
            logger.info("Updated menu '%s'", menu.get("title"))
            updated += 1
        else:
            mutation = f"""
            mutation {{
              menuCreate(title: {gql_quote(menu.get("title"))}, handle: {gql_quote(menu.get("handle"))}, items: {items_literal}) {{
                menu {{ id handle }}
                userErrors {{ field message }}
              }}
            }}
            """
            try:
                result = retry_with_backoff(lambda: dest_client.mutation(mutation))
            except Exception as e:
                logger.warning("Failed to create menu '%s': %s", menu.get("title"), e)
                continue

            errors = mutation_errors(result, "menuCreate")
            if errors:
                logger.warning("Failed to create menu '%s': %s", menu.get("title"), errors)
                continue
            logger.info("Created menu '%s'", menu.get("title"))
            created += 1

    logger.info("Navigation import complete: %s created, %s updated, %s skipped (default menus)", created, updated, skipped)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer navigation menus from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create/update menus on the destination store")
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    args = parser.parse_args()

    src_shop = os.getenv("SRC_SHOPIFY_SHOP")
    src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")

    if not all([src_shop, src_token, dest_shop, dest_token]):
        raise RuntimeError(
            "Missing .env values: SRC_SHOPIFY_SHOP, SRC_SHOPIFY_ACCESS_TOKEN, DEST_SHOPIFY_SHOP, DEST_SHOPIFY_ACCESS_TOKEN"
        )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    src_client = make_client(src_shop, src_token)
    dest_client = make_client(dest_shop, dest_token)

    logger.info("Exporting menus from %s", src_shop)
    exported = export_menus(src_client)

    ts = int(time.time())
    out_file = out_dir / f"navigation_export_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(exported, f, indent=2, ensure_ascii=False)
    logger.info("Export complete: %s", out_file)

    if args.execute:
        logger.info(
            "Importing menus into %s (make sure products/collections/pages/blogs were transferred first "
            "so menu items can be remapped)",
            dest_shop,
        )
        import_menus(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to write menus to the destination store")


if __name__ == "__main__":
    main()
