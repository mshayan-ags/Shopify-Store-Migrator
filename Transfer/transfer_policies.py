"""Transfer shop policies (refund, shipping, privacy, terms, etc.) from Src to dest.

Shop policies are a fixed set of slots (you can't create new ones, only fill in
the existing ones), so this is always an update, never a create. Requires
read_content/write_content scope on both stores (policies live under the
"Online Store" content permission group).

Usage:
    python transfer_policies.py                # dry-run export
    python transfer_policies.py --execute       # write policy text to dest
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
from Transfer.transfer_store_metafields import retry_with_backoff, gql_quote
from utils.shopify_graphql_utils import mutation_errors

load_dotenv()

logger = logging.getLogger("transfer_policies")
logging.basicConfig(level=logging.INFO)


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

    logger.info("Exporting shop policies from %s", src_shop)
    exported = export_policies(src_client)

    ts = int(time.time())
    out_file = out_dir / f"policies_export_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(exported, f, indent=2, ensure_ascii=False)
    logger.info("Export complete: %s", out_file)

    if args.execute:
        logger.info("Importing policies into %s", dest_shop)
        import_policies(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to write policies to the destination store")


if __name__ == "__main__":
    main()
