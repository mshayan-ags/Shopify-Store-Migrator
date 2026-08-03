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

logger = logging.getLogger("transfer_locations")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def fetch_all_locations(client) -> List[Dict[str, Any]]:
    locations: List[Dict[str, Any]] = []
    since_id = 0

    while True:
        params = {"limit": 250}
        if since_id:
            params["since_id"] = since_id
        page = retry_with_backoff(lambda: client.rest_get("locations", params=params))
        page_locations = page.get("locations", [])
        if not page_locations:
            break
        locations.extend(page_locations)
        since_id = page_locations[-1].get("id") or 0
        if len(page_locations) < 250 or not since_id:
            break

    return locations


def export_locations(client) -> Dict[str, Any]:
    locations = fetch_all_locations(client)
    exported = [
        {
            "name": loc.get("name"),
            "address1": loc.get("address1"),
            "address2": loc.get("address2"),
            "city": loc.get("city"),
            "province_code": loc.get("province_code"),
            "country_code": loc.get("country_code"),
            "zip": loc.get("zip"),
            "phone": loc.get("phone"),
            "active": loc.get("active", True),
            "fulfills_online_orders": loc.get("legacy", False) or True,
        }
        for loc in locations
    ]
    logger.info("Exported %s location(s)", len(exported))
    return {"locations": exported}


def import_locations(dest_client, exported: Dict[str, Any]) -> None:
    existing_names = {(loc.get("name") or "").strip().lower() for loc in fetch_all_locations(dest_client)}

    created = 0
    skipped = 0
    failed = 0

    for loc in exported.get("locations", []):
        if (loc.get("name") or "").strip().lower() in existing_names:
            skipped += 1
            continue

        address_fields = []
        for src_key, input_key in [
            ("address1", "address1"),
            ("address2", "address2"),
            ("city", "city"),
            ("phone", "phone"),
            ("zip", "zip"),
            ("province_code", "provinceCode"),
        ]:
            if loc.get(src_key):
                address_fields.append(f"{input_key}: {gql_quote(loc[src_key])}")
        if loc.get("country_code"):
            address_fields.append(f"countryCode: {loc['country_code']}")

        mutation = f"""
        mutation {{
          locationAdd(input: {{
            name: {gql_quote(loc.get("name"))}
            address: {{ {", ".join(address_fields)} }}
            fulfillsOnlineOrders: {"true" if loc.get("fulfills_online_orders") else "false"}
          }}) {{
            location {{ id name }}
            userErrors {{ field message }}
          }}
        }}
        """
        try:
            result = retry_with_backoff(lambda: dest_client.mutation(mutation))
        except Exception as e:
            logger.warning("Failed to create location '%s': %s", loc.get("name"), e)
            failed += 1
            continue

        errors = mutation_errors(result, "locationAdd")
        if errors:
            logger.warning("Failed to create location '%s': %s", loc.get("name"), errors)
            failed += 1
            continue

        logger.info("Created location '%s'", loc.get("name"))
        created += 1

    logger.info("Locations import complete: %s created, %s already existed, %s failed", created, skipped, failed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer store locations from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create missing locations on the destination store")
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

        logger.info("Exporting locations from %s", src_shop)
        exported = export_locations(src_client)

        ts = int(time.time())
        out_file = out_dir / f"locations_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(exported, f, indent=2, ensure_ascii=False)
        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported, out_dir / f"locations_export_{ts}.xlsx")
        logger.info("Export complete: %s", out_file)

    if args.execute:
        logger.info("Importing locations into %s", dest_shop)
        import_locations(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to create locations on the destination store")


if __name__ == "__main__":
    main()
