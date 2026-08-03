import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from transfer.transfer_product import make_client
from transfer.transfer_store_metafields import retry_with_backoff, set_metafields, gql_quote
from utils.shopify_graphql_utils import paginate_connection, export_metafields, mutation_errors
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_pages")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


PAGE_NODE_FIELDS = """
    id
    handle
    title
    body
    isPublished
    publishedAt
    templateSuffix
    metafields(first: 100) {
      edges { node { namespace key value type } }
    }
"""


def fetch_all_pages(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          pages(first: 100{after_clause}) {{
            edges {{ node {{ {PAGE_NODE_FIELDS} }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("pages",))


def export_pages(src_client) -> List[Dict[str, Any]]:
    pages = fetch_all_pages(src_client)
    exported = [
        {
            "id": p["id"],
            "handle": p["handle"],
            "title": p["title"],
            "body": p["body"],
            "is_published": p["isPublished"],
            "published_at": p.get("publishedAt"),
            "template_suffix": p.get("templateSuffix"),
            "metafields": export_metafields(p.get("metafields")),
        }
        for p in pages
    ]
    logger.info("Exported %s page(s)", len(exported))
    return exported


def import_pages(dest_client, exported: List[Dict[str, Any]]) -> None:
    dest_pages = fetch_all_pages(dest_client)
    by_handle = {(p.get("handle") or "").strip().lower(): p for p in dest_pages}

    created = 0
    updated = 0
    unchanged = 0

    for page in exported:
        handle_key = (page.get("handle") or "").strip().lower()
        existing = by_handle.get(handle_key)

        if existing:
            needs_update = (
                existing.get("title") != page.get("title")
                or existing.get("body") != page.get("body")
                or existing.get("isPublished") != page.get("is_published")
                or existing.get("templateSuffix") != page.get("template_suffix")
            )
            if needs_update:
                extra_fields = ""
                if page.get("template_suffix"):
                    extra_fields += f"\n                    templateSuffix: {gql_quote(page['template_suffix'])}"
                if page.get("is_published") and page.get("published_at"):
                    extra_fields += f"\n                    publishDate: {gql_quote(page['published_at'])}"
                mutation = f"""
                mutation {{
                  pageUpdate(id: {gql_quote(existing["id"])}, page: {{
                    title: {gql_quote(page.get("title"))}
                    body: {gql_quote(page.get("body"))}
                    isPublished: {"true" if page.get("is_published") else "false"}{extra_fields}
                  }}) {{
                    page {{ id handle }}
                    userErrors {{ field message }}
                  }}
                }}
                """
                try:
                    result = retry_with_backoff(lambda: dest_client.mutation(mutation))
                except Exception as e:
                    logger.warning("Failed to update page '%s': %s", page.get("title"), e)
                    continue

                errors = mutation_errors(result, "pageUpdate")
                if errors:
                    logger.warning("Failed to update page '%s': %s", page.get("title"), errors)
                    continue
                logger.info("Updated page '%s'", page.get("title"))
                updated += 1
            else:
                unchanged += 1
            page_gid = existing["id"]
        else:
            extra_fields = ""
            if page.get("template_suffix"):
                extra_fields += f"\n                templateSuffix: {gql_quote(page['template_suffix'])}"
            if page.get("is_published") and page.get("published_at"):
                extra_fields += f"\n                publishDate: {gql_quote(page['published_at'])}"
            mutation = f"""
            mutation {{
              pageCreate(page: {{
                title: {gql_quote(page.get("title"))}
                body: {gql_quote(page.get("body"))}
                handle: {gql_quote(page.get("handle"))}
                isPublished: {"true" if page.get("is_published") else "false"}{extra_fields}
              }}) {{
                page {{ id handle }}
                userErrors {{ field message }}
              }}
            }}
            """
            try:
                result = retry_with_backoff(lambda: dest_client.mutation(mutation))
            except Exception as e:
                logger.warning("Failed to create page '%s': %s", page.get("title"), e)
                continue

            errors = mutation_errors(result, "pageCreate")
            if errors:
                logger.warning("Failed to create page '%s': %s", page.get("title"), errors)
                continue
            page_gid = result["pageCreate"]["page"]["id"]
            logger.info("Created page '%s'", page.get("title"))
            created += 1

        if page.get("metafields"):
            result = set_metafields(dest_client, page_gid, page["metafields"])
            logger.info(
                "Page '%s' metafields: %s updated, %s skipped",
                page.get("title"),
                result["updated_count"],
                result["skipped_count"],
            )

    logger.info("Pages import complete: %s created, %s updated, %s unchanged", created, updated, unchanged)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer Online Store pages from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create/update pages on the destination store")
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
    parser.add_argument(
        "--import-from",
        help=(
            "Skip the source export step and import this previously-saved canonical JSON file "
            "instead (see docs/CANONICAL_SCHEMA.md). Lets you import from a non-Shopify source "
            "connector or replay a prior dry-run export. No SRC_SHOPIFY_* credentials needed in "
            "this mode."
        ),
    )
    args = parser.parse_args()

    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")
    require_env(DEST_SHOPIFY_SHOP=dest_shop, DEST_SHOPIFY_ACCESS_TOKEN=dest_token)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    dest_client = make_client(dest_shop, dest_token)

    if args.import_from:
        logger.info("Loading export from %s (skipping source fetch)", args.import_from)
        if args.import_from.lower().endswith(".xlsx"):
            from utils.tabular_io import import_from_xlsx
            exported = import_from_xlsx(args.import_from)
        else:
            with open(args.import_from, "r", encoding="utf-8") as f:
                exported = json.load(f)
    else:
        src_shop = os.getenv("SRC_SHOPIFY_SHOP")
        src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
        require_env(SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token)

        src_client = make_client(src_shop, src_token)

        logger.info("Exporting pages from %s", src_shop)
        exported = export_pages(src_client)

        ts = int(time.time())
        out_file = out_dir / f"pages_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(exported, f, indent=2, ensure_ascii=False)
        logger.info("Export complete: %s", out_file)

        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported, out_dir / f"pages_export_{ts}.xlsx")

    if args.execute:
        logger.info("Importing pages into %s", dest_shop)
        import_pages(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to write pages to the destination store")


if __name__ == "__main__":
    main()
