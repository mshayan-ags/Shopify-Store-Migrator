import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from transfer.transfer_product import make_client
from transfer.transfer_store_metafields import gql_quote, retry_with_backoff
from utils.shopify_graphql_utils import paginate_connection, mutation_errors
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_customer_segments")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def fetch_all_segments(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          segments(first: 50{after_clause}) {{
            edges {{ node {{ id name query creationDate lastEditDate }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("segments",))


def export_segments(src_client) -> List[Dict[str, Any]]:
    segments = fetch_all_segments(src_client)
    exported = [{"name": s["name"], "query": s["query"]} for s in segments]
    logger.info("Exported %s segment(s)", len(exported))
    return exported


def import_segments(dest_client, exported: List[Dict[str, Any]]) -> None:
    existing = fetch_all_segments(dest_client)
    existing_names = {(s.get("name") or "").strip().lower() for s in existing}

    created = 0
    skipped = 0
    failed = 0

    for segment in exported:
        name = segment.get("name") or ""
        try:
            if name.strip().lower() in existing_names:
                skipped += 1
                continue

            mutation = f"""
            mutation {{
              segmentCreate(name: {gql_quote(name)}, query: {gql_quote(segment.get("query"))}) {{
                segment {{ id name }}
                userErrors {{ field message }}
              }}
            }}
            """
            try:
                result = retry_with_backoff(lambda: dest_client.mutation(mutation))
            except Exception as e:
                logger.warning("Failed to create segment '%s': %s", name, e)
                failed += 1
                continue

            errors = mutation_errors(result, "segmentCreate")
            if errors:
                logger.warning("Failed to create segment '%s': %s", name, errors)
                failed += 1
                continue

            created += 1
        except Exception as e:
            logger.warning("Unexpected error processing segment '%s': %s", name, e)
            failed += 1

    logger.info(
        "Customer segments import complete: %s created, %s already existed, %s failed",
        created,
        skipped,
        failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer customer segments from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create missing segments on the destination store")
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    parser.add_argument(
        "--import-from",
        help=(
            "Skip the source export step and import this previously-saved canonical JSON file "
            "instead (see docs/CANONICAL_SCHEMA.md). Lets you import from a non-Shopify source "
            "connector or replay a prior dry-run export. No SRC_SHOPIFY_* credentials needed in "
            "this mode."
        ),
    )
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
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
            loaded = import_from_xlsx(args.import_from)
        else:
            with open(args.import_from, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        exported = loaded if isinstance(loaded, list) else [loaded]
    else:
        src_shop = os.getenv("SRC_SHOPIFY_SHOP")
        src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
        require_env(SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token)

        src_client = make_client(src_shop, src_token)

        logger.info("Exporting customer segments from %s", src_shop)
        exported = export_segments(src_client)

        ts = int(time.time())
        out_file = out_dir / f"customer_segments_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(exported, f, indent=2, ensure_ascii=False)
        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported, out_dir / f"customer_segments_export_{ts}.xlsx")
        logger.info("Export complete: %s", out_file)

    if args.execute:
        logger.info("Importing customer segments into %s", dest_shop)
        import_segments(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to write customer segments to the destination store")


if __name__ == "__main__":
    main()
