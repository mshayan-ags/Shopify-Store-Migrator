import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from transfer.transfer_product import make_client
from transfer.transfer_store_metafields import retry_with_backoff, gql_quote
from utils.shopify_graphql_utils import mutation_errors
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_policies")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def fetch_shop_policies(client) -> List[Dict[str, Any]]:
    data = retry_with_backoff(lambda: client.query("{ shop { shopPolicies { type body url } } }"))
    return data["shop"]["shopPolicies"]


def export_policies(client) -> Dict[str, Any]:
    policies = fetch_shop_policies(client)
    exported = [{"type": p["type"], "body": p["body"]} for p in policies if p.get("body")]
    logger.info("Exported %s policy/policies with content", len(exported))
    return {"policies": exported}


def import_policies(dest_client, exported: Dict[str, Any]) -> None:
    updated = 0
    unchanged = 0
    failed = 0

    existing_by_type = {p["type"]: p.get("body") for p in fetch_shop_policies(dest_client)}

    for policy in exported.get("policies", []):
        if existing_by_type.get(policy["type"]) == policy["body"]:
            unchanged += 1
            continue

        mutation = f"""
        mutation {{
          shopPolicyUpdate(shopPolicy: {{ type: {policy["type"]}, body: {gql_quote(policy["body"])} }}) {{
            shopPolicy {{ type }}
            userErrors {{ field message }}
          }}
        }}
        """
        try:
            result = retry_with_backoff(lambda: dest_client.mutation(mutation))
        except Exception as e:
            logger.warning("Failed to update policy %s: %s", policy["type"], e)
            failed += 1
            continue

        errors = mutation_errors(result, "shopPolicyUpdate")
        if errors:
            logger.warning("Failed to update policy %s: %s", policy["type"], errors)
            failed += 1
            continue

        logger.info("Updated policy %s", policy["type"])
        updated += 1

    logger.info("Policies import complete: %s updated, %s unchanged, %s failed", updated, unchanged, failed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer shop policies from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Write policy text to the destination store")
    parser.add_argument(
        "--import-from",
        help=(
            "Skip the source export step and import this previously-saved canonical JSON file "
            "instead (see docs/CANONICAL_SCHEMA.md). Lets you import from a non-Shopify source "
            "connector or replay a prior dry-run export. No SRC_SHOPIFY_* credentials needed "
            "in this mode."
        ),
    )
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
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
            exported = import_from_xlsx(args.import_from)
        else:
            with open(args.import_from, "r", encoding="utf-8") as f:
                exported = json.load(f)
    else:
        src_shop = os.getenv("SRC_SHOPIFY_SHOP")
        src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
        require_env(SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token)

        src_client = make_client(src_shop, src_token)

        logger.info("Exporting shop policies from %s", src_shop)
        exported = export_policies(src_client)

        ts = int(time.time())
        out_file = out_dir / f"policies_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(exported, f, indent=2, ensure_ascii=False)
        logger.info("Export complete: %s", out_file)

        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported, out_dir / f"policies_export_{ts}.xlsx")

    if args.execute:
        logger.info("Importing policies into %s", dest_shop)
        import_policies(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to write policies to the destination store")


if __name__ == "__main__":
    main()
