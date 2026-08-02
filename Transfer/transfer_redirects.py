"""Transfer URL redirects from Src to dest.

Requires the `read_content` / `write_content` Admin API scope on both stores'
custom apps. Grant it under Shopify Admin > Apps > [app name] > Configuration,
then reinstall the app to refresh the access token stored in .env.

Redirects have no metafields and are matched/deduped by source path only.

Usage:
    python transfer_redirects.py                # dry-run export
    python transfer_redirects.py --execute       # create missing redirects on dest
"""
import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from Transfer.transfer_product import make_client
from Transfer.transfer_store_metafields import gql_quote, retry_with_backoff
from utils.shopify_graphql_utils import paginate_connection, mutation_errors

load_dotenv()

logger = logging.getLogger("transfer_redirects")
logging.basicConfig(level=logging.INFO)


def fetch_all_redirects(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          urlRedirects(first: 250{after_clause}) {{
            edges {{ node {{ id path target }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("urlRedirects",))


def export_redirects(src_client) -> List[Dict[str, Any]]:
    redirects = fetch_all_redirects(src_client)
    exported = [{"path": r["path"], "target": r["target"]} for r in redirects]
    logger.info("Exported %s redirect(s)", len(exported))
    return exported


def import_redirects(dest_client, exported: List[Dict[str, Any]]) -> None:
    existing = fetch_all_redirects(dest_client)
    existing_paths = {r["path"] for r in existing}

    created = 0
    skipped = 0

    for redirect in exported:
        if redirect["path"] in existing_paths:
            skipped += 1
            continue

        mutation = f"""
        mutation {{
          urlRedirectCreate(urlRedirect: {{
            path: {gql_quote(redirect["path"])}
            target: {gql_quote(redirect["target"])}
          }}) {{
            urlRedirect {{ id path }}
            userErrors {{ field message }}
          }}
        }}
        """
        try:
            result = retry_with_backoff(lambda: dest_client.mutation(mutation))
        except Exception as e:
            logger.warning("Failed to create redirect %s -> %s: %s", redirect["path"], redirect["target"], e)
            continue

        errors = mutation_errors(result, "urlRedirectCreate")
        if errors:
            logger.warning("Failed to create redirect %s -> %s: %s", redirect["path"], redirect["target"], errors)
            continue
        created += 1

    logger.info("Redirects import complete: %s created, %s already existed", created, skipped)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer URL redirects from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create missing redirects on the destination store")
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

    logger.info("Exporting redirects from %s", src_shop)
    exported = export_redirects(src_client)

    ts = int(time.time())
    out_file = out_dir / f"redirects_export_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(exported, f, indent=2, ensure_ascii=False)
    logger.info("Export complete: %s", out_file)

    if args.execute:
        logger.info("Importing redirects into %s", dest_shop)
        import_redirects(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to write redirects to the destination store")


if __name__ == "__main__":
    main()
